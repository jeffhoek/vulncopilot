using './main.bicep'

param environment = 'dev'
param acrName = 'acrvulncopilotdev'
param keyVaultName = 'kv-vulncopilot-dev'
param appServicePlanSku = 'B2'

param logfireEnabled = true

// GitHub OAuth authorization. Locked to a single login until rate limiting (PR 2)
// lands — flip openRegistration to true only for a short OAuth smoke test.
// Set oauth-github-client-id / oauth-github-client-secret in Key Vault (see
// docs/deploy-azure-app-service.md and docs/public-access-setup.md).
param openRegistration = false
param allowedLogins = '["jeffhoek"]'

// Custom domain (see plans/custom-domain-cloudflare.md).
// BLUE/GREEN (rename migration): the new app is NOT yet the target of
// vulncopilot.org — DNS + hostname binding still point at the old app — so certs
// are OFF and publicUrl falls back to app-vulncopilot-dev.azurewebsites.net,
// keeping the OAuth redirect_uri on the new app. FLIP BACK AT CUTOVER, once DNS
// points at the new app and the hostname binding exists:
//   param publicUrl = 'https://vulncopilot.org'
//   param deployCustomDomainCerts = true
param publicUrl = ''
param customDomain = 'vulncopilot.org'
param deployCustomDomainCerts = false

// ETL refresh schedule (UTC cron). Start frequent while validating, then dial back:
//   '0 6,18 * * *' — twice daily, 06:00 + 18:00 UTC (bootstrap / watching it work)
//   '0 6 * * *'    — daily, 06:00 UTC
//   '0 6 * * 1'    — weekly, Mondays 06:00 UTC (steady state)
param etlCronExpression = '0 6,18 * * *'

// Personal / environment-specific values are NOT committed here — they are injected
// at deploy time from Azure DevOps pipeline variables (Pipelines → Edit → Variables),
// so nothing identifying lives in git. See azure-pipelines.yml:
//   --parameters etlEmailTo=$(ETL_EMAIL_TO)
//   --parameters adminUserIdentifiers=$(ADMIN_USER_IDENTIFIERS)
//   --parameters pipelineServicePrincipalObjectId=$(PIPELINE_SP_OBJECT_ID)
// Each falls back to a safe default (empty recipient = no email; empty admin list =
// everyone gets the standard rate limit) if the variable is unset.
param pipelineServicePrincipalObjectId = ''
