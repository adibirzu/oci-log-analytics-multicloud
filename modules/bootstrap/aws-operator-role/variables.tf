variable "account_id" {
  description = "12-digit AWS account ID the active provider must belong to."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "partition" {
  description = "AWS partition (aws, aws-cn, aws-us-gov)."
  type        = string
  default     = "aws"
}

variable "role_name" {
  description = "Name of the operator role."
  type        = string
  default     = "mc-operator"
}

variable "name_prefix" {
  description = "Prefix (including any trailing hyphen) that every operator-managed resource name must start with."
  type        = string
  default     = "mc-"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter and contain only lowercase letters, digits and hyphens."
  }
}

variable "trusted_principal_arn" {
  description = "Exact ARN of the existing IAM principal (user or role) allowed to assume the operator role."
  type        = string
}

variable "require_mfa" {
  description = "Require aws:MultiFactorAuthPresent when assuming the role."
  type        = bool
  default     = false
}

variable "max_session_duration" {
  description = "Maximum session duration in seconds (3600-43200)."
  type        = number
  default     = 3600

  validation {
    condition     = var.max_session_duration >= 3600 && var.max_session_duration <= 43200
    error_message = "max_session_duration must be between 3600 and 43200 seconds."
  }
}

variable "oidc_provider_hosts" {
  description = "OIDC issuer hosts (no scheme) the operator may create providers for. Provider ARNs are named by issuer, not prefix, so narrow this to your issuer; \"*\" allows any."
  type        = list(string)
  default     = ["*"]
}
