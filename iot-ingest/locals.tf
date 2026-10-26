locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  rule_name  = replace(var.name, "-", "_") # IoT rule names allow letters, digits and _ only
  iot_arn    = "arn:aws:iot:${local.region}:${local.account_id}"

  # the board sends dev = "kobra2neo" in every JSON message
  ttl_seconds = var.telemetry_retention_days * 86400
}
