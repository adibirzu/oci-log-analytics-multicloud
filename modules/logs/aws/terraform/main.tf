locals {
  bucket_name = coalesce(var.bucket_name, "${var.name_prefix}-${var.account_id}-cloudtrail")
  trail_name  = coalesce(var.trail_name, "${var.name_prefix}-cloudtrail")
  queue_name  = "${var.name_prefix}-cloudtrail-events"
  dlq_name    = "${var.name_prefix}-cloudtrail-events-dlq"
  oidc_key    = var.collector_oidc_issuer == null ? "invalid-oidc-issuer" : replace(var.collector_oidc_issuer, "https://", "")

  oidc_trust = {
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = var.collector_oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_key}:aud" = var.collector_oidc_audience
          "${local.oidc_key}:sub" = var.collector_oidc_subject
        }
      }
    }]
  }

  principal_trust = {
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        AWS = var.collector_principal_arn
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "sts:ExternalId" = var.collector_external_id
        }
      }
    }]
  }
}

resource "aws_s3_bucket" "cloudtrail" {
  bucket        = local.bucket_name
  force_destroy = false
  depends_on    = [terraform_data.configuration_guards]
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

resource "aws_s3_bucket_ownership_controls" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  bucket                  = aws_s3_bucket.cloudtrail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  rule {
    id     = "expire-cloudtrail-objects"
    status = "Enabled"
    filter {
      prefix = "${var.trail_prefix}/"
    }
    expiration {
      days = var.lifecycle_expiration_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = var.multipart_abort_days
    }
  }
}

data "aws_iam_policy_document" "cloudtrail_bucket" {
  statement {
    sid     = "AllowCloudTrailGetBucketAcl"
    effect  = "Allow"
    actions = ["s3:GetBucketAcl"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    resources = [aws_s3_bucket.cloudtrail.arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = ["arn:${var.partition}:cloudtrail:${var.region}:${var.account_id}:trail/${local.trail_name}"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }

  statement {
    sid     = "AllowCloudTrailWrite"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    resources = ["${aws_s3_bucket.cloudtrail.arn}/${var.trail_prefix}/AWSLogs/${var.account_id}/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = ["arn:${var.partition}:cloudtrail:${var.region}:${var.account_id}:trail/${local.trail_name}"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }

  statement {
    sid     = "DenyNonCloudTrailWrites"
    effect  = "Deny"
    actions = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = ["${aws_s3_bucket.cloudtrail.arn}/${var.trail_prefix}/AWSLogs/${var.account_id}/*"]
    condition {
      test     = "StringNotEqualsIfExists"
      variable = "aws:PrincipalServiceName"
      values   = ["cloudtrail.amazonaws.com"]
    }
  }

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [aws_s3_bucket.cloudtrail.arn, "${aws_s3_bucket.cloudtrail.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  policy = data.aws_iam_policy_document.cloudtrail_bucket.json
}

resource "aws_cloudtrail" "this" {
  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.cloudtrail.id
  s3_key_prefix                 = var.trail_prefix
  include_global_service_events = true
  is_multi_region_trail         = true
  enable_log_file_validation    = true

  event_selector {
    read_write_type           = "All"
    include_management_events = true
    dynamic "data_resource" {
      for_each = var.data_resources
      content {
        type   = data_resource.value.type
        values = data_resource.value.values
      }
    }
  }

  depends_on = [aws_s3_bucket_policy.cloudtrail]
}

resource "aws_sqs_queue" "dlq" {
  name                      = local.dlq_name
  message_retention_seconds = var.dlq_retention_seconds
  sqs_managed_sse_enabled   = true
  depends_on                = [terraform_data.configuration_guards]
}

resource "aws_sqs_queue" "events" {
  name                       = local.queue_name
  message_retention_seconds  = var.queue_retention_seconds
  visibility_timeout_seconds = var.queue_visibility_timeout_seconds
  sqs_managed_sse_enabled    = true
  depends_on                 = [terraform_data.configuration_guards]
  # The collector transfers sanitized poison envelopes. Native redrive would
  # bypass that boundary by copying arbitrary raw notification bodies.
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url            = aws_sqs_queue.dlq.url
  redrive_allow_policy = jsonencode({ redrivePermission = "denyAll" })
}

data "aws_iam_policy_document" "queue" {
  statement {
    sid     = "AllowCloudTrailNotifications"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    resources = [aws_sqs_queue.events.arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.cloudtrail.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "events" {
  queue_url = aws_sqs_queue.events.url
  policy    = data.aws_iam_policy_document.queue.json
}

resource "aws_s3_bucket_notification" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  queue {
    queue_arn     = aws_sqs_queue.events.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "${var.trail_prefix}/AWSLogs/${var.account_id}/"
    filter_suffix = ".gz"
  }
  depends_on = [aws_sqs_queue_policy.events]
}

resource "aws_iam_role" "collector" {
  name               = "${var.name_prefix}-cloudtrail-collector"
  assume_role_policy = var.collector_oidc_provider_arn != null ? jsonencode(local.oidc_trust) : jsonencode(local.principal_trust)
  depends_on         = [terraform_data.configuration_guards]
}

data "aws_iam_policy_document" "collector" {
  statement {
    sid       = "ReadTrailObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.cloudtrail.arn}/${var.trail_prefix}/AWSLogs/${var.account_id}/*"]
  }

  statement {
    sid       = "ConsumeEvents"
    effect    = "Allow"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.events.arn]
  }

  statement {
    sid       = "SendPoisonMessagesToDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
  }
}

resource "aws_iam_role_policy" "collector" {
  name   = "${var.name_prefix}-cloudtrail-collector"
  role   = aws_iam_role.collector.id
  policy = data.aws_iam_policy_document.collector.json
}

resource "terraform_data" "configuration_guards" {
  input = {
    oidc      = var.collector_oidc_provider_arn != null
    principal = var.collector_principal_arn != null
  }

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.account_id
      error_message = "The active AWS provider must belong to account_id."
    }
    precondition {
      condition     = data.aws_region.current.region == var.region && data.aws_partition.current.partition == var.partition
      error_message = "The active AWS provider region and partition must match the module inputs."
    }
    precondition {
      condition     = (var.collector_oidc_provider_arn != null) != (var.collector_principal_arn != null)
      error_message = "Configure exactly one collector trust mode: OIDC provider details or principal ARN with external ID."
    }
    precondition {
      condition     = var.collector_oidc_provider_arn == null ? true : (var.collector_oidc_issuer != null && var.collector_oidc_subject != null && var.collector_external_id == null && var.collector_oidc_provider_arn == "arn:${var.partition}:iam::${var.account_id}:oidc-provider/${local.oidc_key}")
      error_message = "OIDC trust requires exact issuer and subject and must not set collector_external_id."
    }
    precondition {
      condition     = var.collector_principal_arn == null ? true : (var.collector_external_id != null && var.collector_oidc_issuer == null && var.collector_oidc_subject == null)
      error_message = "Principal trust requires collector_external_id and must not set OIDC issuer or subject."
    }
    precondition {
      condition     = var.collector_principal_arn == null ? true : can(regex("^arn:[^:]+:iam::[0-9]{12}:role/.+$", var.collector_principal_arn))
      error_message = "collector_principal_arn must be an exact IAM role ARN, including any role path."
    }
    precondition {
      condition     = var.collector_external_id == null ? true : (length(var.collector_external_id) >= 2 && length(var.collector_external_id) <= 1224 && can(regex("^[A-Za-z0-9_+=,.@:/-]+$", var.collector_external_id)))
      error_message = "collector_external_id must be at least two characters when supplied."
    }
    precondition {
      condition     = var.bucket_name != null || length(local.bucket_name) <= 63
      error_message = "The computed bucket name exceeds S3's 63-character limit; shorten name_prefix or set bucket_name."
    }
    precondition {
      condition     = var.dlq_retention_seconds >= var.queue_retention_seconds
      error_message = "dlq_retention_seconds must be at least queue_retention_seconds."
    }
  }
}
