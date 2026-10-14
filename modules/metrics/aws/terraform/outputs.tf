output "role_arn" {
  description = "IAM role ARN the metrics collector assumes via web identity."
  value       = aws_iam_role.metrics_reader.arn
}
