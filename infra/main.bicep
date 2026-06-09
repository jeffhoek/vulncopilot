targetScope = 'resourceGroup'

@description('Deployment environment (dev, prod)')
param environment string

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Azure Container Registry name (globally unique, no hyphens)')
param acrName string

@description('Key Vault name (globally unique, 3-24 chars)')
param keyVaultName string

@description('App Service Plan SKU (B2 for dev, P1v3 for prod)')
param appServicePlanSku string

@description('Object ID of the pipeline service principal for RBAC assignments')
param pipelineServicePrincipalObjectId string

@description('Enable Logfire observability (requires logfire-token in Key Vault)')
param logfireEnabled bool = false

@description('Cron schedule (UTC) for the weekly ETL refresh job. Default: Mondays 06:00 UTC.')
param etlCronExpression string = '0 6 * * 1'

var appName = 'chainlit-rag'
var tags = {
  environment: environment
  application: appName
}

var identityName = 'id-${appName}-${environment}'
var appServicePlanName = 'asp-${appName}-${environment}'
var appServiceName = 'app-${appName}-${environment}'
var logAnalyticsName = 'log-${appName}-${environment}'
var managedEnvironmentName = 'cae-${appName}-${environment}'
var etlJobName = 'job-${appName}-etl-${environment}'

// Step 1: User-Assigned Managed Identity (must run first)
module identity 'modules/identity.bicep' = {
  name: 'identity'
  params: {
    location: location
    name: identityName
    tags: tags
  }
}

// Step 2: Container Registry
module acr 'modules/acr.bicep' = {
  name: 'acr'
  params: {
    location: location
    name: acrName
    tags: tags
  }
}

// Step 3: Key Vault
module keyVault 'modules/key-vault.bicep' = {
  name: 'keyVault'
  params: {
    location: location
    name: keyVaultName
    tags: tags
  }
}

// Step 4: App Service Plan + Web App
module appService 'modules/app-service.bicep' = {
  name: 'appService'
  params: {
    location: location
    appServicePlanName: appServicePlanName
    appServiceName: appServiceName
    appServicePlanSku: appServicePlanSku
    identityId: identity.outputs.identityId
    identityClientId: identity.outputs.clientId
    acrLoginServer: acr.outputs.loginServer
    keyVaultName: keyVaultName
    logfireEnabled: logfireEnabled
    tags: tags
  }
}

// Step 5: RBAC — all role assignments (depends on all resources above)
module rbac 'modules/rbac.bicep' = {
  name: 'rbac'
  dependsOn: [
    appService
  ]
  params: {
    managedIdentityPrincipalId: identity.outputs.principalId
    pipelinePrincipalId: pipelineServicePrincipalObjectId
    keyVaultId: keyVault.outputs.keyVaultId
    acrId: acr.outputs.acrId
  }
}

// Step 6: Azure Policy assignments
module policy 'modules/policy.bicep' = {
  name: 'policy'
}

// Step 7: Scheduled ETL job (Container Apps Job) — weekly KEV + NVD refresh.
// Depends on rbac so the identity can pull from ACR and read Key Vault secrets.
module etlJob 'modules/etl-job.bicep' = {
  name: 'etlJob'
  dependsOn: [
    rbac
  ]
  params: {
    location: location
    logAnalyticsName: logAnalyticsName
    managedEnvironmentName: managedEnvironmentName
    jobName: etlJobName
    identityId: identity.outputs.identityId
    acrLoginServer: acr.outputs.loginServer
    keyVaultName: keyVaultName
    cronExpression: etlCronExpression
    tags: tags
  }
}

output appServiceUrl string = 'https://${appService.outputs.defaultHostName}'
output acrLoginServer string = acr.outputs.loginServer
output keyVaultUri string = keyVault.outputs.keyVaultUri
output etlJobName string = etlJob.outputs.jobName
