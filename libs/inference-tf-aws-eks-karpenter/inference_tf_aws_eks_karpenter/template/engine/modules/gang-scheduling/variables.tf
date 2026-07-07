variable "gang_scheduling_provider" {
  description = "Gang scheduling provider: 'coscheduling', 'volcano', or 'none'."
  type        = string
  validation {
    condition     = contains(["coscheduling", "volcano", "none"], var.gang_scheduling_provider)
    error_message = "Must be 'coscheduling', 'volcano', or 'none'."
  }
}

variable "gang_scheduling_namespace" {
  description = "Namespace to install the gang scheduling components."
  type        = string
}

variable "coscheduling_version" {
  description = "Version of the scheduler-plugins Helm chart (when provider is coscheduling)."
  type        = string
}

variable "volcano_version" {
  description = "Version of the Volcano Helm chart (when provider is volcano)."
  type        = string
}
