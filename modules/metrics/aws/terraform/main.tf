locals {
  oidc_key     = replace(var.oidc_issuer, "https://", "")
  provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.this[0].arn : var.existing_oidc_provider_arn

  read_actions = concat(
    ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "cloudwatch:GetMetricStatistics"],
    var.allow_tag_lookup ? ["tag:GetResources"] : [],
  )
}

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "this" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = var.oidc_issuer
  client_id_list = [var.oidc_audience]
}

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "AssumeViaWebIdentity"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_key}:aud"
      values   = [var.oidc_audience]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_key}:sub"
      values   = [var.oidc_subject]
    }
  }
}

resource "aws_iam_role" "metrics_reader" {
  name               = "${var.name_prefix}-metrics-reader"
  assume_role_policy = data.aws_iam_policy_document.trust.json

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.account_id
      error_message = "The active AWS provider must belong to account_id."
    }
    precondition {
      condition     = var.create_oidc_provider != (var.existing_oidc_provider_arn != null)
      error_message = "Set exactly one of create_oidc_provider=true or existing_oidc_provider_arn."
    }
  }
}

# CloudWatch read APIs do not support resource-level permissions, so Resource "*" is required.
data "aws_iam_policy_document" "read" {
  statement {
    sid       = "ReadMetricsOnly"
    effect    = "Allow"
    actions   = local.read_actions
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "read" {
  name   = "${var.name_prefix}-metrics-read"
  role   = aws_iam_role.metrics_reader.id
  policy = data.aws_iam_policy_document.read.json
}
