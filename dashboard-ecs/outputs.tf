output "dashboard_url" {
  description = "Public HTTPS address of the dashboard (sign-up and login via Cognito)"
  value       = "https://${aws_cloudfront_distribution.dashboard.domain_name}"
}

output "user_pool_id" {
  value = aws_cognito_user_pool.dashboard.id
}

output "approve_user_command" {
  description = "Run this to let a registered user into the dashboard"
  value       = "aws cognito-idp admin-add-user-to-group --region ${local.region} --user-pool-id ${aws_cognito_user_pool.dashboard.id} --group-name approved --username <their-email>"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.dashboard.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.dashboard.name
}

output "ecs_service" {
  value = aws_ecs_service.dashboard.name
}

output "approval_email_to" {
  description = "Approval requests go here; confirm the SNS subscription email once after apply"
  value       = var.admin_email
}
