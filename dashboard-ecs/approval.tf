# Sign-up approval: Cognito confirms a new user -> notify Lambda emails the admin a signed link
# -> the link opens the approve Lambda, whose button adds the user to the "approved" group.

resource "random_password" "approval_key" {
  length  = 48
  special = false
}

resource "aws_sns_topic" "approvals" {
  name = "${var.name}-approvals"
}

# SNS emails a confirmation link once; the subscription stays pending until it is clicked
resource "aws_sns_topic_subscription" "admin" {
  topic_arn = aws_sns_topic.approvals.arn
  protocol  = "email"
  endpoint  = var.admin_email
}

data "archive_file" "notify" {
  type        = "zip"
  source_file = "${path.module}/lambda/notify.py"
  output_path = "${path.module}/.build/notify.zip"
}

data "archive_file" "approve" {
  type        = "zip"
  source_file = "${path.module}/lambda/approve.py"
  output_path = "${path.module}/.build/approve.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "notify" {
  name               = "${var.name}-notify"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role" "approve" {
  name               = "${var.name}-approve"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "notify_logs" {
  role       = aws_iam_role.notify.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "approve_logs" {
  role       = aws_iam_role.approve.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "notify" {
  name = "publish-approval-request"
  role = aws_iam_role.notify.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.approvals.arn }]
  })
}

# A separate policy resource (not inline in the role) keeps the dependency chain
# user pool -> notify Lambda -> approve URL -> approve Lambda from becoming a cycle.
resource "aws_iam_role_policy" "approve" {
  name = "add-user-to-approved-group"
  role = aws_iam_role.approve.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "cognito-idp:AdminAddUserToGroup"
      Resource = aws_cognito_user_pool.dashboard.arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "notify" {
  name              = "/aws/lambda/${var.name}-notify"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "approve" {
  name              = "/aws/lambda/${var.name}-approve"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "approve" {
  function_name    = "${var.name}-approve"
  role             = aws_iam_role.approve.arn
  runtime          = "python3.12"
  handler          = "approve.handler"
  filename         = data.archive_file.approve.output_path
  source_code_hash = data.archive_file.approve.output_base64sha256
  timeout          = 10

  environment {
    variables = {
      APPROVAL_KEY   = random_password.approval_key.result
      APPROVED_GROUP = local.approved_group
    }
  }

  depends_on = [aws_cloudwatch_log_group.approve]
}

# Public URL, but every request needs a valid signed token (see approve.py)
resource "aws_lambda_function_url" "approve" {
  function_name      = aws_lambda_function.approve.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "approve_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.approve.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# Since late 2025 a public function URL also needs plain InvokeFunction, limited to calls via the URL
resource "aws_lambda_permission" "approve_url_invoke" {
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.approve.function_name
  principal                = "*"
  invoked_via_function_url = true
}

resource "aws_lambda_function" "notify" {
  function_name    = "${var.name}-notify"
  role             = aws_iam_role.notify.arn
  runtime          = "python3.12"
  handler          = "notify.handler"
  filename         = data.archive_file.notify.output_path
  source_code_hash = data.archive_file.notify.output_base64sha256
  timeout          = 10

  environment {
    variables = {
      APPROVAL_KEY = random_password.approval_key.result
      APPROVE_URL  = aws_lambda_function_url.approve.function_url
      TOPIC_ARN    = aws_sns_topic.approvals.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.notify]
}

resource "aws_lambda_permission" "cognito_notify" {
  statement_id  = "AllowCognitoPostConfirmation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notify.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.dashboard.arn
}
