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
variable "callback_urls" {
  type        = list(string)
  default     = ["http://localhost:3000/auth/callback"]
  description = "Cognito code-flow callbacks. Replace with the HTTPS web origin in 8b."
  validation {
    condition     = alltrue([for u in var.callback_urls : startswith(u, "https://") || startswith(u, "http://localhost:")])
    error_message = "Callbacks must use HTTPS, except localhost during development."
  }
}
variable "logout_urls" {
  type    = list(string)
  default = ["http://localhost:3000"]
  validation {
    condition     = alltrue([for u in var.logout_urls : startswith(u, "https://") || startswith(u, "http://localhost:")])
    error_message = "Logout URLs must use HTTPS, except localhost during development."
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
