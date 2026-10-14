resource "oci_apm_apm_domain" "this" {
  count          = local.create_apm ? 1 : 0
  compartment_id = var.compartment_ocid
  display_name   = "${local.name}-apm"
  description    = "Multicloud APM domain (managed by Resource Manager stack)"
  is_free_tier   = var.apm_free_tier
}

locals {
  apm_domain_id = var.enable_apm ? (local.create_apm ? oci_apm_apm_domain.this[0].id : var.existing_apm_domain_ocid) : ""
}

data "oci_apm_apm_domain" "this" {
  count         = var.enable_apm ? 1 : 0
  apm_domain_id = local.apm_domain_id
}

data "oci_apm_data_keys" "this" {
  count         = var.enable_apm ? 1 : 0
  apm_domain_id = local.apm_domain_id
}

locals {
  apm_endpoint = var.enable_apm ? data.oci_apm_apm_domain.this[0].data_upload_endpoint : ""
  apm_private_keys = var.enable_apm ? [
    for k in data.oci_apm_data_keys.this[0].data_keys : k.value if k.type == "PRIVATE"
  ] : []
  apm_private_key = length(local.apm_private_keys) > 0 ? local.apm_private_keys[0] : ""
}
