# Set as a sensitive workspace variable in HCP Terraform. No default: a missing
# credential must fail immediately rather than surface as a confusing provider
# authentication error partway through a plan.
variable "vercel_api_token" {
  description = "Vercel API token used for provisioning resources"
  type        = string
  sensitive   = true
}

variable "vercel_team_id" {
  description = "Vercel Team ID scope for the project (optional if using personal account)"
  type        = string
  default     = null
}

variable "github_repo" {
  description = "GitHub repository identifier in owner/repo format"
  type        = string
  default     = "gelstable/gel-registry"
}

variable "project_name" {
  description = "Name of the Vercel project"
  type        = string
  default     = "gel-registry"
}

variable "production_domain" {
  description = "Custom production domain name attached to the project"
  type        = string
  default     = "registry.gelstable.com"
}

variable "apex_domain" {
  description = "The registered apex domain in Vercel (e.g. gelstable.com)"
  type        = string
  default     = "gelstable.com"
}
