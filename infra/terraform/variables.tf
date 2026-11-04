variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "name" {
  type    = string
  default = "ledgerguard"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.name))
    error_message = "Use a short lowercase application name."
  }
}
variable "budget_alert_email" {
  type        = string
  description = "Owner's email for AWS Budgets alerts; supply privately when planning."
  validation {
    condition     = can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.budget_alert_email))
    error_message = "Supply an email for budget alerts."
  }
}
variable "api_zip" {
  type        = string
  default     = "../../dist/api.zip"
  description = "Linux x86_64 Python 3.12 deployment ZIP produced by make api-zip."
}

variable "protect_user_pool" {
  type        = bool
  default     = true
  description = "Set false only for an explicitly approved teardown."
}

variable "web_zip" {
  type        = string
  default     = "../../dist/web.zip"
  description = "Next.js standalone Lambda ZIP produced by make web-zip."
}

variable "pipeline_batch_max_cost" {
  type        = string
  default     = "0.50"
  description = "Server-controlled USD budget for one pipeline batch; cannot raise the existing $0.50 cap."
  validation {
    condition     = can(tonumber(var.pipeline_batch_max_cost)) && try(tonumber(var.pipeline_batch_max_cost) >= 0 && tonumber(var.pipeline_batch_max_cost) <= 0.50, false)
    error_message = "The batch budget must be between 0 and 0.50 USD."
  }
}
