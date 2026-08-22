# Resources that existed before this configuration. Import blocks are
# committed rather than run as one-off `terraform import` commands, so that the
# import is reviewable and reproducible from a clean state.
#
# Remove an import block once the resource is in state and an apply has
# reported no changes for it. Leaving one in place is harmless but misleading.
#
# The apex domain has no import block because the provider has no resource for
# it; see the comment in main.tf.
#
# Fill in the IDs from the Vercel dashboard before the first apply. An import
# block naming a resource that does not exist fails the plan, so each stays
# commented until its ID is confirmed.

# import {
#   to = vercel_project.registry
#   id = "prj_..."
# }

# import {
#   to = vercel_project_domain.production
#   id = "prj_.../registry.gelstable.com"
# }

# import {
#   to = vercel_dns_record.registry_cname
#   id = "rec_..."
# }
