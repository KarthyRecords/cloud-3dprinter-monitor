# ---------------------------------------------------------------- device identity

resource "aws_iot_thing" "board" {
  name = var.thing_name
}

# Least privilege: connect as one client ID, publish under the topic prefix, retain only
# status and setup. No Subscribe or Receive, so nothing can reach the board through MQTT.
resource "aws_iot_policy" "board" {
  name = "${var.thing_name}-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "iot:Connect"
        Resource = "${local.iot_arn}:client/${var.thing_name}"
        Condition = {
          Bool = { "iot:Connection.Thing.IsAttached" = "true" }
        }
      },
      {
        Effect   = "Allow"
        Action   = "iot:Publish"
        Resource = "${local.iot_arn}:topic/${var.topic_prefix}/*"
      },
      {
        Effect = "Allow"
        Action = "iot:RetainPublish"
        Resource = [
          "${local.iot_arn}:topic/${var.topic_prefix}/status",
          "${local.iot_arn}:topic/${var.topic_prefix}/setup",
        ]
      },
    ]
  })
}

# Created only once the certificate exists (see the cli_steps output)
resource "aws_iot_policy_attachment" "board" {
  count = var.certificate_arn == null ? 0 : 1

  policy = aws_iot_policy.board.name
  target = var.certificate_arn
}

resource "aws_iot_thing_principal_attachment" "board" {
  count = var.certificate_arn == null ? 0 : 1

  thing     = aws_iot_thing.board.name
  principal = var.certificate_arn
}

# ---------------------------------------------------------------- what the rules may do

data "aws_iam_policy_document" "iot_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["iot.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "rules" {
  name               = "${var.name}-iot-rules"
  assume_role_policy = data.aws_iam_policy_document.iot_assume.json
}

data "aws_iam_policy_document" "rules" {
  statement {
    sid       = "WriteTables"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.telemetry.arn, aws_dynamodb_table.state.arn]
  }

  statement {
    sid       = "WriteSnapshots"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.data.arn}/kobra/snapshots/*"]
  }

  statement {
    sid       = "SendAlerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }

  statement {
    sid       = "LogRuleErrors"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.rule_errors.arn}:*"]
  }

  dynamic "statement" {
    for_each = var.enable_raw_accel ? [1] : []

    content {
      sid       = "SendRawToFirehose"
      actions   = ["firehose:PutRecord", "firehose:PutRecordBatch"]
      resources = [aws_kinesis_firehose_delivery_stream.raw[0].arn]
    }
  }
}

resource "aws_iam_role_policy" "rules" {
  name   = "rules"
  role   = aws_iam_role.rules.id
  policy = data.aws_iam_policy_document.rules.json
}

# ---------------------------------------------------------------- topic rules

# Telemetry, vibration and events -> DynamoDB. ttl is the item's expiry in epoch seconds.
resource "aws_iot_topic_rule" "telemetry" {
  name        = "${local.rule_name}_telemetry"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT *, concat(dev, '#telemetry') AS pk, floor(timestamp() / 1000) + ${local.ttl_seconds} AS ttl FROM '${var.topic_prefix}/telemetry'"

  dynamodbv2 {
    role_arn = aws_iam_role.rules.arn

    put_item {
      table_name = aws_dynamodb_table.telemetry.name
    }
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.rule_errors.name
      role_arn       = aws_iam_role.rules.arn
    }
  }
}

resource "aws_iot_topic_rule" "vibration" {
  name        = "${local.rule_name}_vibration"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT *, concat(dev, '#vibration') AS pk, floor(timestamp() / 1000) + ${local.ttl_seconds} AS ttl FROM '${var.topic_prefix}/vibration'"

  dynamodbv2 {
    role_arn = aws_iam_role.rules.arn

    put_item {
      table_name = aws_dynamodb_table.telemetry.name
    }
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.rule_errors.name
      role_arn       = aws_iam_role.rules.arn
    }
  }
}

resource "aws_iot_topic_rule" "event" {
  name        = "${local.rule_name}_event"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT *, concat(dev, '#event') AS pk, floor(timestamp() / 1000) + ${local.ttl_seconds} AS ttl FROM '${var.topic_prefix}/event'"

  dynamodbv2 {
    role_arn = aws_iam_role.rules.arn

    put_item {
      table_name = aws_dynamodb_table.telemetry.name
    }
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.rule_errors.name
      role_arn       = aws_iam_role.rules.arn
    }
  }
}

# Retained topics -> latest-state table (each write replaces the previous one)
resource "aws_iot_topic_rule" "status" {
  name        = "${local.rule_name}_status"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT *, dev AS pk, 'status' AS sk FROM '${var.topic_prefix}/status'"

  dynamodbv2 {
    role_arn = aws_iam_role.rules.arn

    put_item {
      table_name = aws_dynamodb_table.state.name
    }
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.rule_errors.name
      role_arn       = aws_iam_role.rules.arn
    }
  }
}

resource "aws_iot_topic_rule" "setup" {
  name        = "${local.rule_name}_setup"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT *, dev AS pk, 'setup' AS sk FROM '${var.topic_prefix}/setup'"

  dynamodbv2 {
    role_arn = aws_iam_role.rules.arn

    put_item {
      table_name = aws_dynamodb_table.state.name
    }
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.rule_errors.name
      role_arn       = aws_iam_role.rules.arn
    }
  }
}

# Snapshots -> S3. The payload is a bare JPEG; SELECT * passes the bytes through unchanged.
# Topic levels: printer(1) / kobra(2) / snapshot(3) / <unix_ms>(4)
resource "aws_iot_topic_rule" "snapshot" {
  name        = "${local.rule_name}_snapshot"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT * FROM '${var.topic_prefix}/snapshot/+'"

  s3 {
    bucket_name = aws_s3_bucket.data.id
    key         = "kobra/snapshots/$${topic(4)}.jpg"
    role_arn    = aws_iam_role.rules.arn
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.rule_errors.name
      role_arn       = aws_iam_role.rules.arn
    }
  }
}

# Alerts -> SNS: printer problems, and the board going offline (the Last Will message)
resource "aws_iot_topic_rule" "printer_alert" {
  name        = "${local.rule_name}_printer_alert"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT * FROM '${var.topic_prefix}/event' WHERE type = 'printer_alert'"

  sns {
    target_arn     = aws_sns_topic.alerts.arn
    role_arn       = aws_iam_role.rules.arn
    message_format = "RAW"
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.rule_errors.name
      role_arn       = aws_iam_role.rules.arn
    }
  }
}

resource "aws_iot_topic_rule" "offline_alert" {
  name        = "${local.rule_name}_offline_alert"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT * FROM '${var.topic_prefix}/status' WHERE state = 'offline'"

  sns {
    target_arn     = aws_sns_topic.alerts.arn
    role_arn       = aws_iam_role.rules.arn
    message_format = "RAW"
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.rule_errors.name
      role_arn       = aws_iam_role.rules.arn
    }
  }
}
