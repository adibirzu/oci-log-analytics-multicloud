# Requires Log Analytics to be onboarded in the tenancy/region already.
resource "oci_log_analytics_log_analytics_log_group" "this" {
  count          = local.create_loggroup ? 1 : 0
  compartment_id = var.compartment_ocid
  namespace      = local.la_namespace
  display_name   = var.log_group_name
  description    = "Multicloud log ingestion (managed by Resource Manager stack)"
}

locals {
  log_group_ocid = var.enable_logs ? (local.create_loggroup ? oci_log_analytics_log_analytics_log_group.this[0].id : var.existing_log_group_ocid) : ""
}
