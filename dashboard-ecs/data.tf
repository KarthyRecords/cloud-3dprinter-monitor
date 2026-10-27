locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  # same naming as the iot-ingest stack: printer-monitor-data-<account>-<region>
  data_bucket = "printer-monitor-data-${local.account_id}-${local.region}"
}

data "aws_vpc" "main" {
  filter {
    name   = "tag:Name"
    values = ["k-vpc"]
  }
}

# tasks run in the private subnets (internet through the NAT gateway)
data "aws_subnet" "private" {
  for_each = toset(["k-subnet1", "k-subnet2"])

  filter {
    name   = "tag:Name"
    values = [each.key]
  }
}

# the ALB needs two public subnets in different AZs
data "aws_subnet" "public" {
  for_each = toset(["k-public-subnet", "k-public-subnet-2"])

  filter {
    name   = "tag:Name"
    values = [each.key]
  }
}

data "aws_dynamodb_table" "telemetry" {
  name = var.telemetry_table
}

data "aws_dynamodb_table" "state" {
  name = var.state_table
}

data "aws_s3_bucket" "data" {
  bucket = local.data_bucket
}
