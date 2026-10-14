resource "oci_kms_vault" "this" {
  count          = local.need_vault ? 1 : 0
  compartment_id = var.compartment_ocid
  display_name   = "${local.name}-vault"
  vault_type     = "DEFAULT"
}

resource "oci_kms_key" "this" {
  count               = local.need_vault ? 1 : 0
  compartment_id      = var.compartment_ocid
  display_name        = "${local.name}-key"
  management_endpoint = oci_kms_vault.this[0].management_endpoint

  key_shape {
    algorithm = "AES"
    length    = 32
  }
}

# APM private data key. It also lands in Terraform state because the stack
# reads it from the APM data source; the state is protected by Resource Manager IAM.
resource "oci_vault_secret" "apm_key" {
  count          = var.enable_apm ? 1 : 0
  compartment_id = var.compartment_ocid
  vault_id       = oci_kms_vault.this[0].id
  key_id         = oci_kms_key.this[0].id
  secret_name    = "${local.name}-apm-private-datakey"
  description    = "APM private data key"

  secret_content {
    content_type = "BASE64"
    content      = base64encode(local.apm_private_key)
  }
}

# Empty placeholders for per-cloud federation tokens. Operators fill them out
# of band (never through stack variables); the stack ignores later changes.
resource "oci_vault_secret" "federation" {
  for_each       = toset(local.enabled_clouds)
  compartment_id = var.compartment_ocid
  vault_id       = oci_kms_vault.this[0].id
  key_id         = oci_kms_key.this[0].id
  secret_name    = "${local.name}-${each.key}-federation"
  description    = "Placeholder for the ${each.key} OIDC federation token"

  secret_content {
    content_type = "BASE64"
    content      = base64encode("PLACEHOLDER")
  }

  lifecycle {
    ignore_changes = [secret_content]
  }
}
