data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# Execution role: used by ECS itself to pull the image from ECR and write logs
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role: what the dashboard code itself may do. Read-only on the printer data.
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "ReadPrinterTables"
    actions   = ["dynamodb:Query", "dynamodb:GetItem"]
    resources = [data.aws_dynamodb_table.telemetry.arn, data.aws_dynamodb_table.state.arn]
  }

  statement {
    sid       = "ListSnapshots"
    actions   = ["s3:ListBucket"]
    resources = [data.aws_s3_bucket.data.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["kobra/snapshots/*"]
    }
  }

  statement {
    sid       = "ReadSnapshots"
    actions   = ["s3:GetObject"]
    resources = ["${data.aws_s3_bucket.data.arn}/kobra/snapshots/*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "read-printer-data"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
