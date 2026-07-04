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

### 9. Azure DevOps automation

Separate from the in-repo `azure-pipelines.yml` updates in step 6, the Azure DevOps side likely needs attention too:

- [ ] **Azure DevOps repo mirror / connection** — if ADO pulls from GitHub via a service connection, the connection may reference the old repo URL. Update the service connection or re-authorize against the new URL.
- [ ] **Pipeline definitions** — the pipeline itself may be named after the old repo. Rename in ADO UI (Pipelines → ⋯ → Rename).
- [ ] **Variable groups / library** — check for any variable group names or values that hardcode the old repo name.
- [ ] **Environment names** — deployment environments (e.g. `chainlit-pydanticai-postgres-prod`) may need renaming.
- [ ] **Artifact / container registry names** — if ADO publishes images or artifacts named after the repo (e.g. ACR image `chainlit-pydanticai-postgres:latest`), decide whether to rename or leave as-is. Renaming is cleaner but breaks pull references in deployed environments — coordinate with any running deployments.
- [ ] **Webhook / trigger config** — GitHub redirects webhook payloads, but ADO may have cached the old URL in the trigger config. Verify a build fires after a push to the renamed repo.
- [ ] **Related feature branches** — `ado-pipeline-fix`, `deploy-config-pipeline-vars`, `fix-pipeline-ui-var-override`, `harden-pipeline-deploy-vars` all touch this area; if any get merged later, they may need the same rename treatment.

Test the full pipeline end-to-end after the rename by pushing a small no-op commit.

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
