param location string
param appServicePlanName string
param appServiceName string
param appServicePlanSku string
param identityId string
param identityClientId string
param acrLoginServer string
param keyVaultName string
param logfireEnabled bool = false
param tags object = {}

var imageRef = '${acrLoginServer}/chainlit-pydanticai-rag:latest'

var systemPrompt = concat(
  'You are a security analyst assistant with access to the ',
  'CISA Known Exploited Vulnerabilities (KEV) database ',
  'and NIST National Vulnerability Database (NVD).\n\n',
  '## Database Schema\n\n',
  'TABLE: kev_vulnerabilities (\n',
  '  cve_id VARCHAR(20),\n',
  '  vendor_project TEXT,\n',
  '  product TEXT,\n',
  '  vulnerability_name TEXT,\n',
  '  short_description TEXT,\n',
  '  required_action TEXT,\n',
  '  notes TEXT,\n',
  '  date_added DATE,\n',
  '  due_date DATE,\n',
  '  known_ransomware_campaign_use VARCHAR(20),\n',
  '  cwes TEXT[]\n',
  ')\n\n',
  'TABLE: nvd_vulnerabilities (\n',
  '  cve_id VARCHAR(20),\n',
  '  description TEXT,\n',
  '  cvss_v31_score NUMERIC(3,1),\n',
  '  cvss_v31_severity VARCHAR(10),\n',
  '  cvss_v31_vector TEXT,\n',
  '  cvss_v2_score NUMERIC(3,1),\n',
  '  cvss_v2_severity VARCHAR(10),\n',
  '  cwes TEXT[],\n',
  '  affected_products TEXT[],\n',
  '  reference_urls TEXT[],\n',
  '  published DATE,\n',
  '  last_modified DATE,\n',
  '  raw_json JSONB -- full NVD API response, query with -> and ->> operators\n',
  ')\n\n',
  'TABLE: cwe_definitions (\n',
  '  cwe_id VARCHAR(20),       -- e.g., \'CWE-79\'\n',
  '  name TEXT,                -- human-readable weakness name\n',
  '  abstraction VARCHAR(20),  -- Pillar, Class, Base, Variant, Compound\n',
  '  description TEXT,\n',
  '  url TEXT\n',
  ')\n\n',
  'JOIN tables on cve_id to cross-reference KEV and NVD data.\n',
  'JOIN cwe_definitions using: cwe_id = ANY(nvd_vulnerabilities.cwes) ',
  'or cwe_id = ANY(kev_vulnerabilities.cwes) to resolve CWE IDs to names.\n\n',
  '## Tools\n\n',
  '- **retrieve**: semantic search across both datasets. ',
  'Use for conceptual questions (e.g. \'tell me about Log4j\').\n',
  '- **query**: execute SQL. Use for counts, top-N, date filters, grouping, listing, and JOINs across tables.\n\n',
  'Answer concisely. If the answer is not in the data, say so. ',
  'When the user asks a follow-up question, use the conversation history to resolve references ',
  '(e.g., \'it\', \'that CVE\', \'the one you just described\') before querying the database.'
)

var actionButtons = join([
  'Latest KEV additions'
  'Ransomware-linked vulns'
  'Critical vulns with active exploits'
  'Anthropic Claude include URLs'
  'CVE-2026-25253 include URLs'
  'OpenClaw include URLs'
  'Top AI vulns in 2026'
  'VPN and remote access vulns'
  'Network device vulns'
  'Microsoft product vulns'
  'Top vendors in KEV'
  'Top CWE categories by avg CVSS score'
], '","')

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: appServicePlanSku
  }
  properties: {
    reserved: true
  }
}

resource appService 'Microsoft.Web/sites@2023-12-01' = {
  name: appServiceName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    serverFarmId: appServicePlan.id
    keyVaultReferenceIdentity: identityId
    httpsOnly: true
    clientAffinityEnabled: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${imageRef}'
      webSocketsEnabled: true
      alwaysOn: true
      healthCheckPath: '/healthz'
      acrUseManagedIdentityCreds: true
      acrUserManagedIdentityID: identityClientId
      appSettings: [
        {
          name: 'AZURE_CLIENT_ID'
          value: identityClientId
        }
        {
          name: 'APP_USERNAME'
          value: 'admin'
        }
        {
          name: 'LLM_MODEL'
          value: 'anthropic:claude-haiku-4-5-20251001'
        }
        {
          name: 'TOP_K'
          value: '5'
        }
        {
          name: 'SYSTEM_PROMPT'
          value: systemPrompt
        }
        {
          name: 'ACTION_BUTTONS'
          value: '["${actionButtons}"]'
        }
        {
          name: 'PG_DATABASE_URL'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=database-url)'
        }
        {
          name: 'WEBSITES_PORT'
          value: '8080'
        }
        {
          name: 'WEBSITE_CONTAINER_START_TIME_LIMIT'
          value: '230'
        }
        {
          name: 'ANTHROPIC_API_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=anthropic-api-key)'
        }
        {
          name: 'OPENAI_API_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=openai-api-key)'
        }
        {
          name: 'APP_PASSWORD'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=app-password)'
        }
        {
          name: 'CHAINLIT_AUTH_SECRET'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=chainlit-auth-secret)'
        }
        {
          name: 'MCP_API_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=mcp-api-key)'
        }
        {
          name: 'LOGFIRE_ENABLED'
          value: string(logfireEnabled)
        }
        {
          name: 'LOGFIRE_TOKEN'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=logfire-token)'
        }
      ]
    }
  }
}

output appServiceId string = appService.id
output defaultHostName string = appService.properties.defaultHostName
