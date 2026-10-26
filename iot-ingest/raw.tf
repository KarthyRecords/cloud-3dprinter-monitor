# Optional: the 400 Hz raw accelerometer stream, batched into S3 for offline analysis.
# Off by default: it is about two thirds of all messages and about 600 MB of payload a day.

resource "aws_iam_role" "firehose" {
  count = var.enable_raw_accel ? 1 : 0

  name = "${var.name}-firehose-raw"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "firehose.amazonaws.com" }
      Condition = { StringEquals = { "sts:ExternalId" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "firehose" {
  count = var.enable_raw_accel ? 1 : 0

  name = "write-raw"
  role = aws_iam_role.firehose[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:AbortMultipartUpload",
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:PutObject",
      ]
      Resource = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/raw/*"]
    }]
  })
}

resource "aws_kinesis_firehose_delivery_stream" "raw" {
  count = var.enable_raw_accel ? 1 : 0

  name        = "${var.name}-raw-accel"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose[0].arn
    bucket_arn          = aws_s3_bucket.data.arn
    prefix              = "raw/accel/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "raw/errors/!{firehose:error-output-type}/"
    buffering_size      = 5
    buffering_interval  = 300
    compression_format  = "GZIP"
  }
}

resource "aws_iot_topic_rule" "accel_raw" {
  count = var.enable_raw_accel ? 1 : 0

  name        = "${local.rule_name}_accel_raw"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT * FROM '${var.topic_prefix}/accel_raw'"

  firehose {
    delivery_stream_name = aws_kinesis_firehose_delivery_stream.raw[0].name
    role_arn             = aws_iam_role.rules.arn
    separator            = "\n"
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.rule_errors.name
      role_arn       = aws_iam_role.rules.arn
    }
  }
}
