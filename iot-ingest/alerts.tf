resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

# The subscriber must click the confirmation link in the email AWS sends.
resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email == null ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Where IoT writes a line when a rule action fails (for example a rejected DynamoDB write)
resource "aws_cloudwatch_log_group" "rule_errors" {
  name              = "/iot/${var.name}/rule-errors"
  retention_in_days = 14
}
