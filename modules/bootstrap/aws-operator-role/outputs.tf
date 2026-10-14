output "role_arn" {
  description = "ARN of the operator role; use as role_arn in an AWS CLI profile."
  value       = aws_iam_role.operator.arn
}

output "policy_json" {
  description = "Rendered least-privilege permissions policy attached to the operator role."
  value       = local.policy_json
}
