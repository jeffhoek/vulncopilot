# Rename: chainlit-pydanticai-postgres → vulncopilot

Rename the GitHub repo and align in-repo metadata and external references.

## Decisions

- **New repo name**: `vulncopilot`
- **New GitHub URL**: `github.com/jeffhoek/vulncopilot`
- **Domain**: `vulncopilot.org` (already owned)
- **Old local directory**: keep as-is at `~/Development/ai-ml/chainlit-pydanticai-postgres/` to preserve Claude Code session history and per-project memory (`~/.claude/projects/-Users-jeff-Development-ai-ml-chainlit-pydanticai-postgres/`)
- **New local directory**: fresh clone at `~/Development/ai-ml/vulncopilot/`
- **Open PRs at rename time**: #28 (public-access-plan), #45 (ref-url-scraping), #65 (evals) — will need rebase after in-repo rename PR merges

## Steps

### 1. Pre-flight (in old local dir)

```bash
cd ~/Development/ai-ml/chainlit-pydanticai-postgres

# Confirm clean state, nothing unpushed, no stashes you care about
git status
git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads
git stash list

# Note the .env and any other gitignored files you'll want to copy over
ls -la .env* 2>/dev/null
```

### 2. Rename on GitHub

- Go to `github.com/jeffhoek/chainlit-pydanticai-postgres` → Settings
- Scroll to top → Rename → `vulncopilot` → confirm
- GitHub sets up a permanent redirect from the old URL. Open PRs #28, #45, #65 keep their numbers, branches, and diffs intact.

### 3. Clone fresh into new directory

```bash
cd ~/Development/ai-ml
git clone git@github.com:jeffhoek/vulncopilot.git
cd vulncopilot
```

Leave `~/Development/ai-ml/chainlit-pydanticai-postgres/` alone. Its `origin` remote will keep working via GitHub redirect.

### 4. Restore local-only files and set up env

```bash
# Copy .env from old dir
cp ~/Development/ai-ml/chainlit-pydanticai-postgres/.env .

# Any other local-only files (settings.local.json, scratch notes, etc.)
# Inspect old dir and copy what you need

# Sync dependencies
uv sync
```

### 5. Create rename branch and find all references

```bash
git checkout -b rename-to-vulncopilot

# Full sweep for the old name
grep -rn "chainlit-pydanticai-postgres" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules

# Also sweep for identifier-style uses of the project shorthand
grep -rn "chainlit-rag\|chainlit_rag\|chainlitrag" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules

# Broad sweep for other "chainlit"-prefixed identifiers used as project name
# (NOT the Chainlit framework itself — see caveat below)
grep -rn "chainlit" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules
```

**IMPORTANT — `chainlit` disambiguation**: Chainlit is also the framework this app is built on, so the string `chainlit` appears legitimately in many places. Only rename occurrences where it's used as a *project identifier or resource name*. Do NOT rename:
- `chainlit` package imports, dependency entries in `pyproject.toml`/`uv.lock`
- `chainlit run app.py` commands
- `chainlit.md` filename (Chainlit's welcome-screen config)
- `.chainlit/` config directory references
- Doc prose describing the Chainlit framework

DO rename (these use `chainlit-*` as our project shorthand):
- `k8s/*.yaml` — `metadata.name`, `metadata.labels`, `selector.matchLabels`, service names, network policies (`chainlit-rag`, etc.)
- `infra/*.bicep` / `infra/*.bicepparam` — resource names, tags, App Service names
- `Dockerfile` — image labels, `LABEL org.opencontainers.image.title`
- `azure-pipelines.yml` — variable values, artifact/image names
- Deployment scripts referencing container/service names

Likely hit locations:
- `pyproject.toml` — `name = "..."`, any URL fields, `[project.urls]`
- `README.md` — title, description, clone URL, badges, screenshots links
- `CLAUDE.md` — first-line description
- `docker-compose.yml` / Dockerfile — image names, container names, service labels
- `docs/*.md` — clone instructions, deployment references
- `plans/*.md` — any URL references
- `.github/workflows/*.yml` — workflow names, image tags if pushed to GHCR
- `azure-pipelines.yml` — pipeline name, resource references, image/artifact names, any hardcoded repo URL, service connection names that embed the repo name
- `k8s/*.yaml` — `metadata.name`, labels, selectors, network policies (currently use `chainlit-rag`); note: netpol YAMLs on another feature branch also use `chainlit-rag` and will conflict on rebase
- `infra/*.bicep`, `infra/*.bicepparam` — Azure resource names, tags, App Service names
- `chainlit.md` (Chainlit welcome screen) if it mentions the project name
- `uv.lock` — the project name field (will regenerate on next `uv sync`)

### 6. Update files

Do a global find-and-replace where safe. For `pyproject.toml`, at minimum update:
- `name = "vulncopilot"`
- Any `[project.urls]` entries pointing to the GitHub repo

For README, also update:
- Title / H1
- Any shields.io badge URLs
- Clone commands in setup instructions
- Add `vulncopilot.org` link once the domain points somewhere

Then regenerate the lock file:
```bash
uv sync
```

### 7. Verify and commit

```bash
# Re-run the grep to confirm no stragglers
grep -rn "chainlit-pydanticai-postgres" . --exclude-dir=.git --exclude-dir=.venv

# Smoke test the app still starts
uv run chainlit run app.py
# Ctrl+C after confirming it loads

git add -A
git commit -m "rename project to vulncopilot"
git push -u origin rename-to-vulncopilot
gh pr create --title "Rename project to vulncopilot" --body "Repo renamed on GitHub; this PR updates in-repo metadata and docs."
```

Merge when green.

### 8. Rebase the three open PRs

For each of #28, #45, #65:

```bash
gh pr checkout <number>
git rebase main
# Resolve conflicts in pyproject.toml / README / CLAUDE.md — accept the rename
git push --force-with-lease
```

Use `--force-with-lease` (not `--force`) to avoid clobbering any updates.

### 9. Azure App Service redeploy (blue/green)

**Scope**: Azure App Service **dev** only. EKS and GCP Cloud Run are untouched by this step — no namespace/SSM changes needed here (handle the EKS `/rag/*` rename separately if/when desired).

Rather than rename live Azure resources (RGs, ACR, and the App Service hostname **cannot** be renamed in place), stand up a fresh `vulncopilot` stack from the renamed IaC, validate it, then delete the old `chainlit-rag` stack. Every globally-unique name differs (`*-chainlit-rag-*` → `*-vulncopilot-*`), so the two stacks coexist and the old one stays as a rollback until cutover.

**Safe because**: the database is **external (Supabase)** — the bicep only references `database-url` / `database-url-readonly` from Key Vault, so nothing here touches the KEV/NVD/pgvector data. No ETL reload required.

Old → new resource names (from the renamed IaC):

| | Old | New |
|---|---|---|
| Resource group | `rg-chainlit-rag-dev` | `rg-vulncopilot-dev` |
| Container registry | `acrchainlitragdev` | `acrvulncopilotdev` |
| ACR image | `chainlit-pydanticai-rag:latest` | `vulncopilot:latest` |
| App Service | `app-chainlit-rag-dev` | `app-vulncopilot-dev` |
| Key Vault | `kv-chainlit-rag-dev` | `kv-vulncopilot-dev` |
| ADO service connections | `azure-chainlit-rag`, `github-chainlit-rag` | `azure-vulncopilot`, `github-vulncopilot` |
| ADO environment | `chainlit-rag-dev` | `vulncopilot-dev` |

> **Ordering matters.** The ETL Container Apps Job in `main.bicep` validates **both its image and its Key Vault secrets at create time**, so the registry and vault must be seeded **before** the stack deploy (9.2 before 9.3). The App Service tolerates a missing image/secret at deploy time; the Container Apps Job does not. (Learned the hard way — deploying first fails the ETL job on missing secret, then on missing image.)

#### 9.1 Prerequisites — resource group, service connection, SP elevation

1. Create the empty resource group first — the ARM service connection is RG-scoped, so the RG must exist:
   ```bash
   az group create -n rg-vulncopilot-dev -l eastus \
     --tags environment=dev application=vulncopilot
   ```
2. In ADO, **reuse the existing project** (do not create a new one — the project name is internal-only and matches nothing in Azure; rename it to `vulncopilot` later if you want the cosmetics). Create the ARM service connection **`azure-vulncopilot`** (automatic SP, scope = resource group `rg-vulncopilot-dev`). This auto-grants the SP **Contributor**.
3. **Elevate the SP to Owner** on the RG — the bicep `rbac`/`policy` modules create role and policy assignments (`Microsoft.Authorization/*/write`), which Contributor cannot do. The old pipeline SPs had Owner; match that:
   ```bash
   SP_OID=$(az role assignment list -g rg-vulncopilot-dev \
     --query "[?principalType=='ServicePrincipal'].principalId | [0]" -o tsv)
   az role assignment create --role Owner --assignee-object-id "$SP_OID" \
     --assignee-principal-type ServicePrincipal \
     --scope $(az group show -n rg-vulncopilot-dev --query id -o tsv)
   ```
   Keep `$SP_OID` — it's the `pipelineServicePrincipalObjectId` / `PIPELINE_SP_OBJECT_ID` used below.

#### 9.2 Seed the registry AND Key Vault (before deploying)

Image — copy the existing artifact so the stack has a known-good image to run:
```bash
az acr import --name acrvulncopilotdev \
  --source acrchainlitragdev.azurecr.io/chainlit-pydanticai-rag:latest \
  --image vulncopilot:latest
```

Secrets — both vaults are **RBAC-mode**, and being subscription Owner does **not** grant data-plane access. Grant yourself Secrets Officer on the new vault, then copy **every** secret (values never printed):
```bash
az role assignment create --role "Key Vault Secrets Officer" \
  --assignee-object-id $(az ad signed-in-user show --query id -o tsv) \
  --assignee-principal-type User \
  --scope $(az keyvault show -n kv-vulncopilot-dev --query id -o tsv)

OLD_KV=kv-chainlit-rag-dev; NEW_KV=kv-vulncopilot-dev
for s in $(az keyvault secret list --vault-name "$OLD_KV" --query "[].name" -o tsv); do
  v=$(az keyvault secret show --vault-name "$OLD_KV" --name "$s" --query value -o tsv)
  az keyvault secret set --vault-name "$NEW_KV" --name "$s" --value "$v" >/dev/null
  echo "copied: $s"
done
```
Copy the **whole** vault, not a hand-picked subset — it includes `logfire-token`, `mcp-api-key`, `app-password`, etc. beyond the obvious app/DB keys. `database-url*` point at Supabase and are reused verbatim.

#### 9.3 Provision the stack (green-field overrides)

Deploy `main.bicep` with two overrides that keep the new app **off the shared domain** during blue/green:
- `deployCustomDomainCerts=false` — the `vulncopilot.org` managed cert can't issue until the hostname is bound to the new app and DNS resolves to it (still the old app). Leaving it on **fails** the deploy.
- `publicUrl=''` — falls back to `https://app-vulncopilot-dev.azurewebsites.net`, keeping the OAuth `redirect_uri` on the new app.

```bash
az deployment group create -g rg-vulncopilot-dev -n vulncopilot-provision \
  --template-file infra/main.bicep \
  --parameters infra/parameters.dev.bicepparam \
  --parameters pipelineServicePrincipalObjectId="$SP_OID" \
               deployCustomDomainCerts=false publicUrl=''
```

The **pipeline** (9.5) redeploys the same bicep from the committed `parameters.dev.bicepparam` *without* CLI overrides, so also commit these two values to the **branch** param file (certs off, `publicUrl=''`). **⚠️ This toggle must be reverted at cutover (9.6)**, or `main` ships with the domain disabled.

Validate: all resources present incl. `job-vulncopilot-etl-dev`; `curl .../healthz` → 200; `CHAINLIT_URL` = the azurewebsites host.

#### 9.4 Repoint the GitHub OAuth callback

The new app reuses the **same** OAuth App (client id/secret copied in 9.2). A classic OAuth App has a **single** callback URL, so old and new hosts can't both work at once. Repoint it to the new host for validation:
```
https://app-vulncopilot-dev.azurewebsites.net/auth/oauth/github/callback
```
Old-app login breaks until cutover (fine for a single dev user); at cutover it becomes `https://vulncopilot.org/auth/oauth/github/callback`. (Alternative: a second OAuth App for the new stack — avoids old-app disruption but adds an app + new vault client id/secret.)

#### 9.5 ADO pipeline wiring + validation run

- [ ] Push the branch (incl. the 9.3 param toggle) to GitHub — the pipeline checks out from origin.
- [ ] New pipeline → GitHub → repo `jeffhoek/vulncopilot` → existing YAML, branch `rename-to-vulncopilot`, path **`azure-pipelines.yml`** (`.yml`, not `.yaml`).
- [ ] Set pipeline variable **`PIPELINE_SP_OBJECT_ID = $SP_OID`** — **required**; if unset it passes literally to the bicep and DeployInfra fails. (`ETL_EMAIL_TO` / `ADMIN_USER_IDENTIFIERS` optional; they default safely.)
- [ ] Run manually against `rename-to-vulncopilot` — the trigger is `main`-only + `pr: none`, so it won't auto-fire (manual run is expected and matches merge-last). The `vulncopilot-dev` environment auto-creates on first run.

The `github-vulncopilot` variable in the YAML is **unused** (`checkout: self` uses the pipeline's own GitHub connection) — no need to create that connection. Green = Build pushes `vulncopilot:<sha>`+`latest`; DeployInfra bicep succeeds (SP has Owner); DeployApp sets the `:<sha>` image and healthz passes.

#### 9.6 Cutover + tear down the old stack

Final validation on the new app:
- [ ] OAuth login round-trip; a sample KEV/NVD query returns data; `/mcp` responds
- [ ] optionally trigger the ETL job once: `az containerapp job start -n job-vulncopilot-etl-dev -g rg-vulncopilot-dev`

Domain cutover:
- [ ] Repoint `vulncopilot.org` DNS from the old app to the new app; add the hostname binding on `app-vulncopilot-dev`.
- [ ] **Flip the 9.3 toggle back**: `deployCustomDomainCerts=true`, `publicUrl='https://vulncopilot.org'` in the branch param file; redeploy so the managed cert issues.
- [ ] Update the OAuth callback to `https://vulncopilot.org/auth/oauth/github/callback`.

Switch-flip and teardown:
- [ ] Merge `rename-to-vulncopilot` → `main`; repoint the pipeline default branch to `main`.
- [ ] Only after the new stack is fully green: delete the old stack and old wiring:
  ```bash
  az group delete --name rg-chainlit-rag-dev --yes --no-wait
  ```
  plus the old ADO service connection/environment/pipeline and the stale OAuth callback.

**Notes**
- Feature branches `ado-pipeline-fix`, `deploy-config-pipeline-vars`, `fix-pipeline-ui-var-override`, `harden-pipeline-deploy-vars` touch this area — if merged later, they inherit the new names from `main`.
- Skipping step 8 only defers it: merging this rename to `main` conflicts PRs #28/#45/#65, which still need rebasing before they can merge.

### 10. External references

Update these on your own time (GitHub's redirect covers you indefinitely, but they're nicer as direct links):

- [ ] LinkedIn — project entry, any posts
- [ ] Personal website — project page / portfolio
- [ ] Resume / CV
- [ ] Twitter/X / Bluesky / Mastodon bio or pinned posts
- [ ] Dev.to / Medium / Hashnode articles (if any)
- [ ] Deployed app footers / "source code" links (Azure, GCP, EKS per docs)
- [ ] Any other repos that badge-link back to this one
- [ ] MCP server config if it references the old name/path (`kev-nvd-rag`)

### 11. Domain setup (whenever ready)

- Point `vulncopilot.org` at whatever hosts the docs/demo (GitHub Pages, deployed Chainlit, etc.)
- Add DNS records via the domain registrar
- Once live, add the domain to `pyproject.toml` `[project.urls]` as Homepage

## Rollback

If something goes badly wrong before merging the rename PR:
- Rename back on GitHub Settings (redirect flips direction, all URLs keep working)
- Delete the fresh clone dir
- Continue working from the old local dir as if nothing happened

## Follow-ups (not blocking)

- Reserve `chainlit-pydanticai-postgres` under your account by *not* creating a new repo with that name, so the redirect keeps working forever
- Consider archiving old blog posts / social references that use the old name for a cleaner search footprint
- If the project graduates further, revisit `.ai` domain and `.com` (currently premium-priced at $100+)
