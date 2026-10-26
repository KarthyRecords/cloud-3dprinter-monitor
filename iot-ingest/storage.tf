# Telemetry, vibration summaries and events: one item per message.
#   pk  = "<dev>#telemetry" | "<dev>#vibration" | "<dev>#event"
#   seq = the board's message counter (shared by the three queued topics, only ever increases)
# A duplicate (QoS 1 sends at least once) has the same pk and seq, so it overwrites itself.
resource "aws_dynamodb_table" "telemetry" {
  name         = "${var.name}-telemetry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "seq"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "seq"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# Latest value of the two retained topics: pk = dev, sk = "status" | "setup"
resource "aws_dynamodb_table" "state" {
  name         = "${var.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }
}

# Camera snapshots (kobra/snapshots/) and raw accelerometer batches (raw/)
resource "aws_s3_bucket" "data" {
  bucket = "${var.name}-data-${local.account_id}-${local.region}"
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-snapshots"
    status = "Enabled"

    filter {
      prefix = "kobra/snapshots/"
    }

    expiration {
      days = var.snapshot_retention_days
    }
  }

  rule {
    id     = "expire-raw"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    expiration {
      days = var.raw_retention_days
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}
