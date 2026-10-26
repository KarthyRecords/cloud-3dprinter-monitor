data "aws_iot_endpoint" "data" {
  endpoint_type = "iot:Data-ATS"
}

output "iot_endpoint" {
  description = "Goes in the board's aws_config.json as endpoint"
  value       = data.aws_iot_endpoint.data.endpoint_address
}

output "telemetry_table" {
  value = aws_dynamodb_table.telemetry.name
}

output "state_table" {
  value = aws_dynamodb_table.state.name
}

output "data_bucket" {
  value = aws_s3_bucket.data.id
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "cli_steps" {
  description = "Create the device certificate outside Terraform so the private key never enters state"
  value       = <<-EOT
    aws iot create-keys-and-certificate --region ${local.region} --set-as-active \
      --certificate-pem-outfile device.pem.crt \
      --private-key-outfile private.pem.key \
      --public-key-outfile public.pem.key
    # note the certificateArn it prints, then:
    terraform apply -var certificate_arn=<certificateArn>
    curl -o AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
    # give AmazonRootCA1.pem, device.pem.crt, private.pem.key and the endpoint to the board; never commit them
  EOT
}
