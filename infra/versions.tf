terraform {
  required_version = ">= 1.5.0"

  # Remote state and remote execution in HCP Terraform. Runs execute in the
  # workspace, so the Vercel provisioning token is a workspace variable and
  # never reaches GitHub Actions. CI and maintainers authenticate to HCP only.
  cloud {
    organization = "gelstable"

    workspaces {
      name = "gel-registry"
    }
  }

  required_providers {
    vercel = {
      source  = "vercel/vercel"
      version = "~> 2.0"
    }
  }
}
