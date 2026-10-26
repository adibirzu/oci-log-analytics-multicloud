# Dynamic group and policies exist only in the home region.
resource "oci_identity_dynamic_group" "runtime" {
  provider       = oci.home
  count          = local.run_bridge ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-runtime-${substr(sha1(var.compartment_ocid), 0, 8)}"
  description    = "Non-log container instances of the multicloud stack"
  matching_rule  = "ALL {resource.type='computecontainerinstance', resource.id='${oci_container_instances_container_instance.runtime[0].id}'}"
}

resource "oci_identity_policy" "runtime" {
  provider       = oci.home
  count          = local.run_bridge ? 1 : 0
  compartment_id = var.compartment_ocid
  name           = "${local.name}-runtime-policy"
  description    = "Least-privilege access for the multicloud runtime, scoped to one compartment"

  statements = concat(
    local.run_bridge ? [
      "allow dynamic-group ${oci_identity_dynamic_group.runtime[0].name} to use metrics in compartment id ${var.compartment_ocid} where target.metrics.namespace = /custom_*/",
    ] : [],
  )
}

resource "oci_identity_dynamic_group" "log_runtime" {
  provider       = oci.home
  for_each       = local.logs_containers
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-${each.key}-logs-${substr(sha1(var.compartment_ocid), 0, 8)}"
  description    = "${each.key} log collector Container Instance only"
  matching_rule  = "ALL {resource.type='computecontainerinstance', resource.id='${oci_container_instances_container_instance.log_runtime[each.key].id}'}"
}

resource "oci_identity_policy" "log_runtime" {
  provider       = oci.home
  for_each       = local.logs_containers
  compartment_id = var.compartment_ocid
  name           = "${local.name}-${each.key}-logs-policy"
  description    = "${each.key} log collector access, restricted to its own federation secret"

  statements = [
    "allow dynamic-group ${oci_identity_dynamic_group.log_runtime[each.key].name} to use loganalytics-log-group in compartment id ${var.compartment_ocid}",
    "allow dynamic-group ${oci_identity_dynamic_group.log_runtime[each.key].name} to {LOG_ANALYTICS_LOG_GROUP_UPLOAD_LOGS} in compartment id ${var.compartment_ocid}",
    "allow dynamic-group ${oci_identity_dynamic_group.log_runtime[each.key].name} to read secret-bundles in compartment id ${var.compartment_ocid} where target.secret.id = '${oci_vault_secret.federation[each.key].id}'",
  ]
}


# Metrics pullers consume only the assertion for their own cloud.  The bridge
# has a different resource principal and only receives OCI Monitoring access.
resource "oci_identity_dynamic_group" "puller_runtime" {
  provider       = oci.home
  for_each       = local.metrics_pullers
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-${each.key}-metrics-${substr(sha1(var.compartment_ocid), 0, 8)}"
  description    = "${each.key} metrics puller Container Instance only"
  matching_rule  = "ALL {resource.type='computecontainerinstance', resource.id='${oci_container_instances_container_instance.puller_runtime[each.key].id}'}"
}

resource "oci_identity_policy" "puller_runtime" {
  provider       = oci.home
  for_each       = local.metrics_pullers
  compartment_id = var.compartment_ocid
  name           = "${local.name}-${each.key}-metrics-policy"
  description    = "${each.key} metrics puller access, restricted to its own federation secret"

  statements = [
    "allow dynamic-group ${oci_identity_dynamic_group.puller_runtime[each.key].name} to read secret-bundles in compartment id ${var.compartment_ocid} where target.secret.id = '${oci_vault_secret.federation[each.key].id}'",
  ]
}
