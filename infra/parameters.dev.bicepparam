using './main.bicep'

param environment = 'dev'
param acrName = 'acrchainlitragdev'
param keyVaultName = 'kv-chainlit-rag-dev'
param appServicePlanSku = 'B2'

param logfireEnabled = true

// GitHub OAuth authorization. Locked to a single login until rate limiting (PR 2)
// lands — flip openRegistration to true only for a short OAuth smoke test.
// Set oauth-github-client-id / oauth-github-client-secret in Key Vault (see
// docs/deploy-azure-app-service.md and docs/public-access-setup.md).
param openRegistration = false
param allowedLogins = '["jeffhoek"]'

// ETL refresh schedule (UTC cron). Start frequent while validating, then dial back:
//   '0 6,18 * * *' — twice daily, 06:00 + 18:00 UTC (bootstrap / watching it work)
//   '0 6 * * *'    — daily, 06:00 UTC
//   '0 6 * * 1'    — weekly, Mondays 06:00 UTC (steady state)
param etlCronExpression = '0 6,18 * * *'

// Recipient(s) for the ETL results email (comma-separated for multiple).
param etlEmailTo = 'jeffreyscotthoekman@gmail.com'

// pipelineServicePrincipalObjectId is passed as a pipeline variable at deploy time:
//   --parameters pipelineServicePrincipalObjectId=$(PIPELINE_SP_OBJECT_ID)
param pipelineServicePrincipalObjectId = ''
