variable "name" {
  description = "Prefix for resource names"
  type        = string
  default     = "printer-monitor"
}

variable "thing_name" {
  description = "IoT thing name. The board's MQTT client ID must be the same."
  type        = string
  default     = "kobra2neo-unoq"
}

variable "topic_prefix" {
  description = "First two levels of every topic the board publishes to"
  type        = string
  default     = "printer/kobra"
}

variable "certificate_arn" {
  description = "ARN of the board's device certificate. Create it with the AWS CLI (see the cli_steps output) so the private key never enters Terraform state. Leave null until it exists."
  type        = string
  default     = null
}

variable "alert_email" {
  description = "Email address for printer alerts and the offline notice. null creates the topic without a subscription."
  type        = string
  default     = null
}

variable "enable_raw_accel" {
  description = "Also ingest the 400 Hz raw accelerometer stream (about two thirds of all messages) through Firehose into S3. Keep the board's raw_accel setting in step with this."
  type        = bool
  default     = false
}

variable "telemetry_retention_days" {
  description = "How long telemetry, vibration and event items stay in DynamoDB (TTL)"
  type        = number
  default     = 7
}

variable "snapshot_retention_days" {
  description = "How long camera snapshots stay in S3"
  type        = number
  default     = 7
}

variable "raw_retention_days" {
  description = "How long raw accelerometer files stay in S3"
  type        = number
  default     = 30
}
