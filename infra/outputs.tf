output "project_id" {
  description = "The ID of the created Vercel project"
  value       = vercel_project.registry.id
}

output "production_domain" {
  description = "The production domain attached to the project"
  value       = vercel_project_domain.production.domain
}

output "registry_cname" {
  description = "The DNS record pointing the registry subdomain at Vercel"
  value       = "${vercel_dns_record.registry_cname.name}.${vercel_dns_record.registry_cname.domain}"
}
