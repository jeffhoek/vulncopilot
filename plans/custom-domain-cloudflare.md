# Custom domain: vulncopilot.org → Azure App Service

Runbook for putting the Azure App Service dev deployment (`app-chainlit-rag-dev.azurewebsites.net`) behind the custom domain **vulncopilot.org**, registered at Cloudflare.

## Context

The app is a Chainlit chatbot: it needs **WebSockets** and relies on **ARR sticky sessions** (`clientAffinityEnabled: true`). App Service terminates TLS at the front end and forwards plain HTTP to the container, so Chainlit builds its OAuth `redirect_uri` from the `CHAINLIT_URL` env var rather than the incoming request. Two app-specific values are therefore coupled to the hostname and must change alongside DNS:

- **`CHAINLIT_URL`** — hardcoded to `https://${appServiceName}.azurewebsites.net` in [infra/modules/app-service.bicep:210](../infra/modules/app-service.bicep). If it doesn't match the browser's host, GitHub OAuth login fails with a `redirect_uri` mismatch.
- **GitHub OAuth App callback URL** — currently points at the `azurewebsites.net` host (see [docs/public-access-setup.md](../docs/public-access-setup.md)).

| Setting | Current | Target |
|---|---|---|
| Public host | `app-chainlit-rag-dev.azurewebsites.net` | `vulncopilot.org` (apex), `www` redirect |
| TLS | Azure default cert | Azure Managed Certificate (free) |
| `CHAINLIT_URL` | `https://app-chainlit-rag-dev.azurewebsites.net` | `https://vulncopilot.org` |
| OAuth callback | `…azurewebsites.net/auth/oauth/github/callback` | `https://vulncopilot.org/auth/oauth/github/callback` |
| DNS / registrar | — | Cloudflare |

## Architecture decision

Two viable paths for how Cloudflare sits in front of Azure:

| | **A. Cloudflare DNS-only (recommended)** | **B. Cloudflare proxied (orange cloud)** |
|---|---|---|
| TLS termination | Azure (managed cert) | Cloudflare edge → Azure origin |
| Cloudflare role | Registrar + DNS only | Registrar + DNS + WAF/CDN/DDoS |
| WebSockets | Native | Supported (enable in Network settings) |
| Managed-cert validation | Works (grey cloud) | Must temporarily grey-cloud to validate |
| Complexity | Low | Higher — needs Full (strict) SSL mode + valid origin cert |

**Plan: ship Path A first.** It is the minimal, reliable route and works natively with Chainlit's WebSockets + sticky sessions. Path B (turning on the proxy for WAF/CDN) is captured as an optional follow-up at the end.

## Prerequisites

- Azure CLI (`az`) authenticated to the subscription
- Contributor on `rg-chainlit-rag-dev`
- Cloudflare account with `vulncopilot.org` active (nameservers delegated to Cloudflare)
- Access to the GitHub OAuth App (github.com/settings/developers)

---

## Step 1 — Read verification ID and inbound IP from Azure

```bash
RG=rg-chainlit-rag-dev
APP=app-chainlit-rag-dev

az webapp show -g $RG -n $APP \
  --query "{verifyId:customDomainVerificationId, inboundIp:inboundIpAddress, defaultHost:defaultHostName}" -o table
```

Record `verifyId` (used in the `asuid` TXT records) and `defaultHost` (the CNAME target).

## Step 2 — Add DNS records in Cloudflare

Cloudflare CNAME flattening allows a CNAME at the apex, avoiding A-record management. In **Cloudflare → DNS**:

| Type | Name | Value | Proxy |
|---|---|---|---|
| CNAME | `@` (`vulncopilot.org`) | `app-chainlit-rag-dev.azurewebsites.net` | **DNS only** (grey) |
| TXT | `asuid` | *(verifyId from step 1)* | — |
| CNAME | `www` | `app-chainlit-rag-dev.azurewebsites.net` | **DNS only** (grey) |
| TXT | `asuid.www` | *(same verifyId)* | — |

Grey-cloud (DNS-only) is **required** during setup — Azure's domain verification and managed-cert issuance both fail if Cloudflare's proxy is in front.

## Step 3 — Bind hostnames in Azure

```bash
az webapp config hostname add -g $RG --webapp-name $APP --hostname vulncopilot.org
az webapp config hostname add -g $RG --webapp-name $APP --hostname www.vulncopilot.org
```

## Step 4 — Create + bind free Managed Certificates

> **Policy gotcha — untagged certs are denied.** The resource group enforces
> `require-tag-environment` and `require-tag-application` ([policy.bicep](../infra/modules/policy.bicep)).
> `az webapp config ssl create` provides no way to set tags, so it fails with
> `RequestDisallowedByPolicy`. Create the `Microsoft.Web/certificates` resource
> with `az resource create` (which accepts tags) instead. Required values:
> `environment=dev`, `application=chainlit-rag`.

```bash
ASP=asp-chainlit-rag-dev
LOC=$(az webapp show -g $RG -n $APP --query location -o tsv)
ASP_ID=$(az appservice plan show -g $RG -n $ASP --query id -o tsv)

for host in vulncopilot.org www.vulncopilot.org; do
  case "$host" in www.*) name=cert-www-vulncopilot-org ;; *) name=cert-vulncopilot-org ;; esac
  az resource create -g $RG --resource-type "Microsoft.Web/certificates" \
    --name "$name" --is-full-object \
    --properties "{
      \"location\": \"$LOC\",
      \"tags\": {\"environment\":\"dev\",\"application\":\"chainlit-rag\"},
      \"properties\": {
        \"serverFarmId\": \"$ASP_ID\",
        \"canonicalName\": \"$host\",
        \"domainValidationMethod\": \"cname-delegation\"
      }
    }"
done
```

Issuance typically takes a few minutes (occasionally up to ~15–20). Then SNI-bind
each — `ssl bind` is an update, not a new resource, so the tag policy doesn't apply:

```bash
THUMB_APEX=$(az resource show -g $RG --resource-type "Microsoft.Web/certificates" \
  --name cert-vulncopilot-org --query properties.thumbprint -o tsv)
THUMB_WWW=$(az resource show -g $RG --resource-type "Microsoft.Web/certificates" \
  --name cert-www-vulncopilot-org --query properties.thumbprint -o tsv)

az webapp config ssl bind -g $RG --name $APP --certificate-thumbprint $THUMB_APEX --ssl-type SNI
az webapp config ssl bind -g $RG --name $APP --certificate-thumbprint $THUMB_WWW  --ssl-type SNI
```

`cname-delegation` is the right validation method since the Cloudflare records are
CNAMEs (flattened at the apex); keep them grey-cloud until binding succeeds.
`httpsOnly: true` is already set ([app-service.bicep:116](../infra/modules/app-service.bicep)), so HTTP→HTTPS redirect is automatic.

> These certs are also declared in bicep (gated behind `deployCustomDomainCerts`,
> with `tags` applied) so future deploys/environments don't re-hit the policy — see
> [IaC changes](#iac-changes-so-this-survives-redeploys).

## Step 5 — Update `CHAINLIT_URL` (the critical app fix)

Choose the canonical host (recommend apex `vulncopilot.org`, redirect `www` → apex). Update the live setting **and** the IaC so a redeploy doesn't revert it:

```bash
az webapp config appsettings set -g $RG -n $APP --settings CHAINLIT_URL=https://vulncopilot.org
```

Then make it a bicep parameter (see [IaC changes](#iac-changes-so-this-survives-redeploys) below). Without the bicep change, the next `az deployment` resets `CHAINLIT_URL` to `azurewebsites.net` and OAuth silently breaks — exactly the failure the comment at [app-service.bicep:206](../infra/modules/app-service.bicep) warns about.

## Step 6 — Update the GitHub OAuth App

In **github.com/settings/developers → the app's OAuth App**:

- Homepage URL: `https://vulncopilot.org`
- Authorization callback URL: `https://vulncopilot.org/auth/oauth/github/callback`

## Step 7 — Verify

```bash
curl -sI https://vulncopilot.org/healthz          # 200, valid cert
curl -sI http://vulncopilot.org/                   # 301 → https
curl -sI https://www.vulncopilot.org/              # reaches app (or redirects to apex)
```

Then in a browser:

- [ ] `https://vulncopilot.org` loads with a valid padlock
- [ ] Chainlit UI renders (WebSocket connects — no console errors)
- [ ] GitHub login redirects out and lands back on `vulncopilot.org` authenticated
- [ ] `/admin` prompts for Basic Auth over HTTPS

---

## IaC changes (so this survives redeploys)

These are **already implemented** in bicep so Step 5 isn't reverted and future
environments don't re-hit the tag policy:

1. **Public URL parametrized.** `publicUrl` param on [main.bicep](../infra/main.bicep) → [app-service.bicep](../infra/modules/app-service.bicep), defaulting empty (falls back to the `azurewebsites.net` host). `CHAINLIT_URL` is set from it. Dev value `https://vulncopilot.org` in [parameters.dev.bicepparam](../infra/parameters.dev.bicepparam).
2. **Managed certs declared** as `Microsoft.Web/certificates` with `tags` applied, gated behind `deployCustomDomainCerts` + `customDomain`. Same resource names as the CLI created (`cert-vulncopilot-org`, `cert-www-vulncopilot-org`), so a deploy reconciles them in place rather than duplicating.

> **Deploy ordering.** Don't run the bicep deployment until the manual Steps 3–4
> have completed and been verified — the cert resources require the hostname
> binding + live DNS, and the SNI SSL binding stays a CLI step (the
> hostname-binding/cert ordering doesn't express cleanly in a single ARM pass).
> Once the domain is live, `az deployment group create ... -p parameters.dev.bicepparam`
> is idempotent against the manually-created resources.

## Rollback

- DNS is authoritative at Cloudflare — deleting the CNAME/TXT records reverts traffic; the `azurewebsites.net` host keeps working throughout.
- Revert `CHAINLIT_URL` and the OAuth callback URL to the `azurewebsites.net` values.
- Custom hostname bindings and certs can be removed with `az webapp config hostname delete` / `ssl unbind` without affecting the default host.

## Optional follow-up — enable Cloudflare proxy (Path B)

Once Path A is verified and stable, to gain Cloudflare WAF/CDN/DDoS:

1. Flip the apex/`www` records to **Proxied** (orange cloud).
2. Set Cloudflare **SSL/TLS → Overview → Full (strict)** (Azure presents a valid managed cert, so strict is safe).
3. Confirm **Network → WebSockets** is enabled (default on most plans).
4. Re-run the Step 7 checks — the ARR sticky-session cookie and Chainlit WebSocket both pass through the proxy. `CHAINLIT_URL` and the OAuth callback stay on the apex.

## References

- [docs/deploy-azure-app-service.md](../docs/deploy-azure-app-service.md) — deployment, `CHAINLIT_URL` proxy gotcha
- [docs/public-access-setup.md](../docs/public-access-setup.md) — GitHub OAuth App, callback path
- [infra/modules/app-service.bicep](../infra/modules/app-service.bicep) — `httpsOnly`, `clientAffinityEnabled`, `CHAINLIT_URL`
