locals {
  oidc_provider_resources = [
    for host in var.oidc_provider_hosts : "arn:${var.partition}:iam::${var.account_id}:oidc-provider/${host}"
  ]

  policy_json = templatefile("${path.module}/policy.json.tpl", {
    partition               = var.partition
    account_id              = var.account_id
    name_prefix             = var.name_prefix
    operator_role_name      = var.role_name
    oidc_provider_resources = jsonencode(local.oidc_provider_resources)
  })

  mfa_condition = var.require_mfa ? {
    Condition = { Bool = { "aws:MultiFactorAuthPresent" = "true" } }
  } : {}

  trust_policy = {
    Version = "2012-10-17"
    Statement = [merge({
      Effect    = "Allow"
      Principal = { AWS = var.trusted_principal_arn }
      Action    = "sts:AssumeRole"
    }, local.mfa_condition)]
  }
}

data "aws_caller_identity" "current" {}

resource "aws_iam_role" "operator" {
  name                 = var.role_name
  assume_role_policy   = jsonencode(local.trust_policy)
  max_session_duration = var.max_session_duration

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.account_id
      error_message = "The active AWS provider must belong to account_id."
    }
    precondition {
      condition     = startswith(var.role_name, var.name_prefix)
      error_message = "role_name must start with name_prefix so the self-modification deny and prefix scoping apply."
    }
  }
}

resource "aws_iam_role_policy" "operator" {
  name   = "${var.role_name}-least-privilege"
  role   = aws_iam_role.operator.id
  policy = local.policy_json
}
