// Scheduled ETL job: refreshes KEV + NVD data weekly via a Container Apps Job.
// Reuses the web app's image, managed identity, ACR, and Key Vault — no new
// secrets store and no new role assignments (the identity already holds
// AcrPull + Key Vault Secrets User at resource-group scope, see rbac.bicep).

param location string
param logAnalyticsName string
param managedEnvironmentName string
param jobName string
param identityId string
param acrLoginServer string
param keyVaultName string

@description('Cron schedule in UTC. Default: Mondays 06:00 UTC.')
param cronExpression string = '0 6 * * 1'

@description('Max seconds a run may take before it is terminated. Default 2h.')
param replicaTimeout int = 7200

param tags object = {}

var imageRef = '${acrLoginServer}/chainlit-pydanticai-rag:latest'
var kvBase = 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets'

// Order matters: run the full NVD incremental FIRST so the KEV-scoped loaders
// (which write recent last_modified/published dates into nvd_vulnerabilities)
// don't poison the high-water mark the incremental derives its start from.
var etlCommand = join([
  '/app/.venv/bin/python scripts/load_nvd_full.py --incremental'
  '/app/.venv/bin/python scripts/load_kev.py'
  '/app/.venv/bin/python scripts/load_nvd.py'
], ' && ')

// Log Analytics workspace backs the Container Apps Environment's log stream.
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// Container Apps Environment (Consumption) — required host for the job.
resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: managedEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// The scheduled job itself. Scales to zero between runs (pay per run).
resource etlJob 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: replicaTimeout
      replicaRetryLimit: 1
      // Pull the image with the user-assigned identity (AcrPull granted in rbac.bicep).
      registries: [
        {
          server: acrLoginServer
          identity: identityId
        }
      ]
      // Secrets sourced from Key Vault via the same identity (Key Vault Secrets User).
      secrets: [
        {
          name: 'openai-api-key'
          keyVaultUrl: '${kvBase}/openai-api-key'
          identity: identityId
        }
        {
          name: 'database-url'
          keyVaultUrl: '${kvBase}/database-url'
          identity: identityId
        }
        {
          name: 'nvd-api-key'
          keyVaultUrl: '${kvBase}/nvd-api-key'
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'etl'
          image: imageRef
          command: [
            '/bin/sh'
            '-c'
          ]
          args: [
            etlCommand
          ]
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: [
            {
              name: 'OPENAI_API_KEY'
              secretRef: 'openai-api-key'
            }
            {
              name: 'PG_DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'NVD_API_KEY'
              secretRef: 'nvd-api-key'
            }
          ]
        }
      ]
    }
  }
}

output jobName string = etlJob.name
output managedEnvironmentId string = managedEnvironment.id
