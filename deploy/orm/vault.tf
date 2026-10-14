# Reuse an existing vault + key when given (tenancies often sit at the vault limit);
# otherwise create a DEFAULT (virtual) vault.
locals {
  reuse_vault  = var.existing_vault_ocid != ""
  create_vault = local.need_vault && !local.reuse_vault
  vault_id     = local.reuse_vault ? var.existing_vault_ocid : try(oci_kms_vault.this[0].id, null)
  kms_key_id   = local.reuse_vault ? var.existing_kms_key_ocid : try(oci_kms_key.this[0].id, null)
}

resource "oci_kms_vault" "this" {
  count          = local.create_vault ? 1 : 0
  compartment_id = var.compartment_ocid
  display_name   = "${local.name}-vault"
  vault_type     = "DEFAULT"
}

resource "oci_kms_key" "this" {
  count               = local.create_vault ? 1 : 0
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
  vault_id       = local.vault_id
  key_id         = local.kms_key_id
  secret_name    = "${local.name}-apm-private-datakey"

  lifecycle {
    precondition {
      condition     = local.kms_key_id != null && local.kms_key_id != ""
      error_message = "existing_vault_ocid needs existing_kms_key_ocid (a key in that vault)."
    }
  }
  description = "APM private data key"

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
  vault_id       = local.vault_id
  key_id         = local.kms_key_id
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
