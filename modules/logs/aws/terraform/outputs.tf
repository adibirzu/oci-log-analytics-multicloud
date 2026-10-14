output "bucket_name" {
  description = "CloudTrail S3 bucket name."
  value       = aws_s3_bucket.cloudtrail.bucket
}

output "trail_prefix" {
  description = "Prefix used by CloudTrail and the collector."
  value       = var.trail_prefix
}

output "queue_url" {
  description = "SQS URL receiving CloudTrail gzip object notifications."
  value       = aws_sqs_queue.events.url
}

output "dlq_url" {
  description = "SQS dead-letter queue URL."
  value       = aws_sqs_queue.dlq.url
}

output "collector_role_arn" {
  description = "IAM role ARN for the CloudTrail collector."
  value       = aws_iam_role.collector.arn
}

output "role_arn" {
  description = "IAM role ARN for the CloudTrail collector."
  value       = aws_iam_role.collector.arn
}

output "collector_max_receive_count" {
  description = "Set INGEST_MAX_RECEIVE_COUNT to this value."
  value       = var.queue_max_receive_count
}
