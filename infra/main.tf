provider "vercel" {
  api_token = var.vercel_api_token
  team      = var.vercel_team_id
}

resource "vercel_project" "registry" {
  name             = var.project_name
  framework        = null
  output_directory = "public"

  git_repository = {
    type              = "github"
    repo              = var.github_repo
    production_branch = "main"
  }
}

resource "vercel_project_domain" "production" {
  project_id = vercel_project.registry.id
  domain     = var.production_domain
  team_id    = var.vercel_team_id
}

# The apex domain itself is not declared here. The vercel/vercel provider has
# no resource for registering or holding a domain; `vercel_domain` does not
# exist. Registration of var.apex_domain is a manual bootstrap step recorded in
# docs/infrastructure.md, and the zone must already exist in Vercel DNS for the
# record below to apply.
resource "vercel_dns_record" "registry_cname" {
  domain  = var.apex_domain
  team_id = var.vercel_team_id
  name    = "registry"
  type    = "CNAME"
  value   = "cname.vercel-dns.com."
  ttl     = 60
}
