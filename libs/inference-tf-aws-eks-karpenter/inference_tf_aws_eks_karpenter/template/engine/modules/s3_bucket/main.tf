resource "aws_s3_bucket" "this" {
  bucket_prefix = var.bucket_name_prefix
  force_destroy = true

  tags = merge(var.combined_tags, {
    Name = var.bucket_name_prefix
  })
}

# @secure_recommendation: Disable object ACLs and keep bucket ownership with this account.
resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = "Enabled"
  }
}

locals {
  lifecycle_rule = var.lifecycle_rule == null ? {
    id                                     = "disabled"
    expiration_days                        = 1
    noncurrent_version_expiration_days     = 1
    abort_incomplete_multipart_upload_days = 1
  } : var.lifecycle_rule
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  count  = var.lifecycle_rule == null ? 0 : 1
  bucket = aws_s3_bucket.this.id

  rule {
    id     = local.lifecycle_rule.id
    status = "Enabled"

    filter {}

    expiration {
      days = local.lifecycle_rule.expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = local.lifecycle_rule.noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = local.lifecycle_rule.abort_incomplete_multipart_upload_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

# SSE-KMS with the AWS-managed aws/s3 key (no kms_master_key_id): its key policy lets
# same-account principals decrypt transparently through S3, so the node role (S3-direct
# streaming) and the Mountpoint-for-S3 CSI driver keep a pure s3:* read grant — no
# kms:Decrypt needed. Matches the jupyter-deploy base templates. bucket_key_enabled
# collapses per-object KMS calls to one data key per bucket/period.
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# @secure_recommendation: Reject all bucket and object requests that do not use TLS.
data "aws_iam_policy_document" "secure_transport" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "secure_transport" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.secure_transport.json
}
