# Users sign up themselves (email + verification code) but only get in once they are
# added to the "approved" group: aws cognito-idp admin-add-user-to-group ...
resource "aws_cognito_user_pool" "dashboard" {
  name                     = var.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"

  lambda_config {
    post_confirmation = aws_lambda_function.notify.arn # emails the admin an approval link
  }

  admin_create_user_config {
    allow_admin_create_user_only = false # self sign-up on
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  software_token_mfa_configuration {
    enabled = true # authenticator app, no SMS cost
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

# Membership of this group is the approval. It shows up in the ID token as cognito:groups.
resource "aws_cognito_user_group" "approved" {
  name         = local.approved_group
  user_pool_id = aws_cognito_user_pool.dashboard.id
  description  = "Users allowed to see the dashboard"
}

# The hosted login / sign-up pages. The prefix must be unique across all of AWS.
resource "aws_cognito_user_pool_domain" "dashboard" {
  domain       = "${var.name}-${local.account_id}"
  user_pool_id = aws_cognito_user_pool.dashboard.id
}

resource "aws_cognito_user_pool_client" "dashboard" {
  name         = var.name
  user_pool_id = aws_cognito_user_pool.dashboard.id

  generate_secret = true # the app swaps the login code for tokens server side

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]

  # Cognito only redirects to exactly these addresses
  callback_urls = ["https://${aws_cloudfront_distribution.dashboard.domain_name}/callback"]
  logout_urls   = ["https://${aws_cloudfront_distribution.dashboard.domain_name}/"]

  explicit_auth_flows           = ["ALLOW_REFRESH_TOKEN_AUTH"] # hosted UI handles the password step
  prevent_user_existence_errors = "ENABLED"                    # login errors do not reveal which emails exist

  id_token_validity      = 60 # minutes
  access_token_validity  = 60
  refresh_token_validity = 1 # days
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}

# Secrets reach the container as env vars; ECS reads them with the execution role at start-up
resource "random_password" "session_key" {
  length  = 64
  special = false
}

resource "aws_ssm_parameter" "client_secret" {
  name  = "/${var.name}/cognito-client-secret"
  type  = "SecureString"
  value = aws_cognito_user_pool_client.dashboard.client_secret
}

resource "aws_ssm_parameter" "session_key" {
  name  = "/${var.name}/session-key"
  type  = "SecureString"
  value = random_password.session_key.result
}
