variable "name" {
  description = "Prefix for the ECR repository, ECS cluster and service, ALB and roles"
  type        = string
  default     = "printer-dashboard"
}

variable "container_port" {
  type    = number
  default = 8080
}

variable "cpu" {
  description = "Fargate task CPU units (256 = 0.25 vCPU)"
  type        = number
  default     = 256
}

variable "memory" {
  description = "Fargate task memory in MiB"
  type        = number
  default     = 512
}

variable "desired_count" {
  description = "Number of dashboard tasks. 0 switches the app off but keeps the ALB."
  type        = number
  default     = 1
}

variable "device_id" {
  description = "The dev field the board puts in every message"
  type        = string
  default     = "kobra2neo"
}

variable "telemetry_table" {
  description = "DynamoDB telemetry table made by the iot-ingest stack"
  type        = string
  default     = "printer-monitor-telemetry"
}

variable "state_table" {
  description = "DynamoDB state table made by the iot-ingest stack"
  type        = string
  default     = "printer-monitor-state"
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "admin_email" {
  description = "Gets an email with an approval link whenever someone registers for the dashboard"
  type        = string
  default     = "karthikasankaran19@gmail.com"
}
