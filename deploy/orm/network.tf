data "oci_core_services" "all" {
  count = local.create_vcn ? 1 : 0
  filter {
    name   = "name"
    values = ["All .* Services In Oracle Services Network"]
    regex  = true
  }
}

resource "oci_core_vcn" "this" {
  count          = local.create_vcn ? 1 : 0
  compartment_id = var.compartment_ocid
  cidr_blocks    = [var.vcn_cidr]
  display_name   = "${local.name}-vcn"
}

resource "oci_core_nat_gateway" "this" {
  count          = local.create_vcn ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this[0].id
  display_name   = "${local.name}-nat"
}

resource "oci_core_service_gateway" "this" {
  count          = local.create_vcn ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this[0].id
  display_name   = "${local.name}-sgw"

  services {
    service_id = data.oci_core_services.all[0].services[0].id
  }
}

resource "oci_core_route_table" "this" {
  count          = local.create_vcn ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this[0].id
  display_name   = "${local.name}-private-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.this[0].id
  }

  route_rules {
    destination       = data.oci_core_services.all[0].services[0].cidr_block
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.this[0].id
  }
}

# Egress only: nothing in the stack accepts inbound connections.
resource "oci_core_security_list" "this" {
  count          = local.create_vcn ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this[0].id
  display_name   = "${local.name}-private-sl"

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }
}

resource "oci_core_subnet" "this" {
  count                      = local.create_vcn ? 1 : 0
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.this[0].id
  cidr_block                 = var.vcn_cidr
  display_name               = "${local.name}-private-subnet"
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.this[0].id
  security_list_ids          = [oci_core_security_list.this[0].id]
}

locals {
  subnet_id = local.create_vcn ? oci_core_subnet.this[0].id : var.existing_subnet_ocid
}
