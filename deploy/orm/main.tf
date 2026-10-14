data "oci_objectstorage_namespace" "this" {
  compartment_id = var.compartment_ocid
}

data "oci_identity_availability_domains" "this" {
  compartment_id = var.tenancy_ocid
}

locals {
  logs_clouds = var.enable_logs ? compact([
    var.enable_aws ? "aws" : "",
    var.enable_azure ? "azure" : "",
    var.enable_gcp ? "gcp" : "",
  ]) : []

  any_cloud       = var.enable_aws || var.enable_azure || var.enable_gcp
  enabled_clouds  = compact([var.enable_aws ? "aws" : "", var.enable_azure ? "azure" : "", var.enable_gcp ? "gcp" : ""])
  run_bridge      = var.enable_metrics && local.any_cloud
  run_puller      = var.enable_metrics && local.any_cloud
  run_gateway     = var.enable_apm
  run_anything    = length(local.logs_clouds) > 0 || local.run_bridge || local.run_gateway
  need_vault      = var.enable_apm || local.any_cloud
  create_vcn      = local.run_anything && var.existing_subnet_ocid == ""
  create_loggroup = var.enable_logs && var.existing_log_group_ocid == ""
  create_apm      = var.enable_apm && var.existing_apm_domain_ocid == ""
  la_namespace    = data.oci_objectstorage_namespace.this.namespace
  ad_name         = data.oci_identity_availability_domains.this.availability_domains[var.availability_domain_index].name
  name            = var.name_prefix
}
