# Resource Manager supplies authentication (no API keys) and the built-ins
# region / tenancy_ocid / compartment_ocid. Nothing tenant-specific is stored here.
provider "oci" {
  region = var.region
}

# IAM objects (dynamic group, policies) only exist in the tenancy home region.
data "oci_identity_tenancy" "this" {
  tenancy_id = var.tenancy_ocid
}

data "oci_identity_region_subscriptions" "this" {
  tenancy_id = var.tenancy_ocid
}

locals {
  home_region = [
    for s in data.oci_identity_region_subscriptions.this.region_subscriptions : s.region_name
    if s.is_home_region
  ][0]
}

provider "oci" {
  alias  = "home"
  region = local.home_region
}
