variable "bucket_name_prefix" {
  type = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*$", var.bucket_name_prefix))
    error_message = "bucket_name_prefix must be lowercase alphanumeric with hyphens, cannot start with a hyphen."
  }

  validation {
    condition     = length(var.bucket_name_prefix) >= 3 && length(var.bucket_name_prefix) <= 36
    error_message = "bucket_name_prefix must be between 3 and 36 characters."
  }
}

variable "combined_tags" {
  type = map(string)
}

variable "lifecycle_rule" {
  type = object({
    id                                     = string
    expiration_days                        = number
    noncurrent_version_expiration_days     = number
    abort_incomplete_multipart_upload_days = number
  })

  validation {
    condition = var.lifecycle_rule == null ? true : alltrue([
      var.lifecycle_rule.expiration_days > 0,
      var.lifecycle_rule.noncurrent_version_expiration_days > 0,
      var.lifecycle_rule.abort_incomplete_multipart_upload_days > 0,
    ])
    error_message = "Lifecycle retention values must be positive day counts."
  }
}
