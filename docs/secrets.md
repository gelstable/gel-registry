# Secrets and key management

Inventory of every credential used by this project: storage location, access, and rotation procedure. Neither secret is stored in this repository.

## Inventory

| Secret | Location | Readable by | Rotation |
| --- | --- | --- | --- |
| Vercel provisioning token | HCP workspace variable `vercel_api_token` | HCP runs only | Manual (see below). **Expires 2026-11-20** |
| HCP team token | GitHub Actions secret `TF_API_TOKEN` | CI only | HCP dashboard. **Expires 2026-11-21** |
| HCP user tokens | Maintainer workstation (`terraform login`) | Maintainer | Owner's responsibility |

Maintainers do not hold Vercel tokens. `terraform plan` runs remotely in HCP using the workspace variable, requiring only HCP access.

The Vercel deployment path uses the Vercel Git integration and stores no Vercel credential in GitHub Actions; see `docs/infrastructure.md`.

## Where the Vercel token lives, and why

The Vercel provisioning token is stored as a sensitive workspace variable in HCP Terraform. Execution happens in HCP, keeping the token off workstations and GitHub Actions runners.

However, CI holds `TF_API_TOKEN`. While HCP prevents reading sensitive variables back via the API, any holder of `TF_API_TOKEN` can queue runs with arbitrary configurations that execute using the Vercel token.

This is not only a concern for a leaked token. `infra-plan.yml` runs automatically on pull requests and passes `TF_API_TOKEN` to a remote plan of the *pull request's own* `infra/` configuration. A contributor with push access can therefore reach the Vercel token at plan time. Fork pull requests cannot: the workflow uses `pull_request`, not `pull_request_target`, so no secret is available to them.

Treat a leaked `TF_API_TOKEN` as equivalent in blast radius to a leaked Vercel provisioning token, and treat repository push access as carrying the same weight.

Scope `TF_API_TOKEN` to an HCP team token limited strictly to the `gel-registry` workspace. Never use a maintainer user token in CI.

## Scope, naming, and expiry

The provisioning token must be scoped to the entire `gelstable` Vercel team across all projects. Do not narrow it to the `gel-registry` project.

A project-scoped token breaks two operations:
1. `vercel_dns_record` writes to the `gelstable.com` apex zone (team-level resource).
2. Attribute changes forcing `vercel_project` replacement will destroy the project but fail recreation, leaving DNS dangling and state corrupted.

Name tokens with their location and creation month: `hcp-gel-registry-provisioning-2026-08`.

Both credentials expire in late November 2026:
- Vercel provisioning token: **2026-11-20**
- HCP team token: **2026-11-21**

Expiry triggers no advance warnings; applies fail with generic provider authorization errors. Set calendar reminders with two weeks lead time.

Rotate sequentially in one session: rotate the **HCP team token first**. `scripts/rotate-vercel-token.sh` requires a valid HCP credential to update variables and run the verification plan.

## Rotating the Vercel provisioning token

Vercel does not support token lifecycle APIs at this scope; token creation and revocation are manual. `scripts/rotate-vercel-token.sh` handles validation and HCP variable updates.

Keep the old token valid until a real plan succeeds with the replacement.

### Step 1: Create replacement (Vercel Dashboard)

1. Create a new token scoped to the `gelstable` team.
2. Record the identifier of the old token before proceeding (needed for Step 3).

### Step 2: Update workspace variable (Scripted)

Pass the new token via stdin (avoid argv exposure in `ps` and shell history):

```text
scripts/rotate-vercel-token.sh --old-token-id <id> < new-token.txt
```

The script verifies team visibility via the Vercel API, updates `vercel_api_token` in HCP, and triggers a verification plan. If the plan fails, revert the workspace variable and investigate.

### Step 3: Revoke old token (Vercel Dashboard)

After a plan or apply succeeds with the new token, revoke the old token using the identifier saved in Step 1. Confirm deletion in the dashboard.

Periodically audit active Vercel tokens against this document; revoke unclaimed tokens.

## Rotating the HCP team token

1. Generate a new team token in HCP scoped to the `gel-registry` workspace.
2. Update the `TF_API_TOKEN` secret in GitHub Actions.
3. Verify by running an infra plan in CI.
4. Revoke the old token in HCP.

## Compromise

**Leaked `TF_API_TOKEN`**: Enables execution with the Vercel provisioning token and can retarget production domains.
1. Revoke the team token in HCP immediately.
2. Audit HCP workspace run history for unauthorized runs.
3. Audit Vercel project settings and DNS records.
4. Rotate the Vercel provisioning token (an attacker run could have exfiltrated it).
5. Follow outage triage in `docs/operations.md` and verify the last known good commit and snapshot ID.

**Leaked maintainer HCP token**: Revoke the token in HCP and review workspace run history.

## Scaling secret management

GitHub Actions secrets and HCP workspace variables are sufficient for two credentials.

If secret count grows to 3+, adopt [SOPS](https://github.com/getsops/sops) with [age](https://github.com/FiloSottile/age) keys committed to the repository. Do not adopt SOPS prematurely: committed ciphertext is permanent and subject to retroactive decryption if an age key ever leaks.
