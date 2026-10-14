# Dynamic group and policies exist only in the home region.
resource "oci_identity_dynamic_group" "runtime" {
  provider       = oci.home
  count          = local.run_anything ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-runtime-${substr(sha1(var.compartment_ocid), 0, 8)}"
  description    = "Container instances of the multicloud stack"
  matching_rule  = "ALL {resource.type='computecontainerinstance', resource.compartment.id='${var.compartment_ocid}'}"
}

resource "oci_identity_policy" "runtime" {
  provider       = oci.home
  count          = local.run_anything ? 1 : 0
  compartment_id = var.compartment_ocid
  name           = "${local.name}-runtime-policy"
  description    = "Least-privilege access for the multicloud runtime, scoped to one compartment"

  statements = concat(
    var.enable_logs ? [
      "allow dynamic-group ${oci_identity_dynamic_group.runtime[0].name} to use log-analytics-log-group in compartment id ${var.compartment_ocid}",
      "allow dynamic-group ${oci_identity_dynamic_group.runtime[0].name} to {LOG_ANALYTICS_LOG_GROUP_UPLOAD_LOGS} in compartment id ${var.compartment_ocid}",
    ] : [],
    local.run_bridge ? [
      "allow dynamic-group ${oci_identity_dynamic_group.runtime[0].name} to use metrics in compartment id ${var.compartment_ocid} where target.metrics.namespace = /custom_*/",
    ] : [],
    local.need_vault ? [
      "allow dynamic-group ${oci_identity_dynamic_group.runtime[0].name} to read secret-bundles in compartment id ${var.compartment_ocid}",
    ] : [],
  )
}
