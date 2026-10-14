output "log_group_ocid" {
  description = "Log Analytics log group receiving logs."
  value       = var.enable_logs ? local.log_group_ocid : null
}

output "apm_domain_ocid" {
  description = "APM domain in use."
  value       = var.enable_apm ? local.apm_domain_id : null
}

output "apm_data_upload_endpoint" {
  description = "APM data upload endpoint."
  value       = var.enable_apm ? local.apm_endpoint : null
}

output "container_instance_ocid" {
  description = "Container instance running the collectors."
  value       = length(oci_container_instances_container_instance.runtime) > 0 ? oci_container_instances_container_instance.runtime[0].id : null
}

output "vault_ocid" {
  description = "Vault holding the APM key and federation placeholders."
  value       = local.need_vault ? oci_kms_vault.this[0].id : null
}

output "next_steps" {
  description = "What to do after the apply finishes."
  value       = <<-EOT
    1. Run mcctl enable <cloud> for each enabled cloud to create the cloud-side pipeline (queue/event hub/subscription and the OIDC federation).
    2. Put the resulting federation token material into the vault secret named ${local.name}-<cloud>-federation (never into stack variables).
    3. Check the container instance logs in the console, then open Log Analytics and search the log group.
    4. Remove everything with a Resource Manager Destroy job; run mcctl disable <cloud> first so the cloud side is cleaned in the right order.
  EOT
}
