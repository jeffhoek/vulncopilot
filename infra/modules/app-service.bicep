param location string
param appServicePlanName string
param appServiceName string
param appServicePlanSku string
param identityId string
param identityClientId string
param acrLoginServer string
param keyVaultName string
param logfireEnabled bool = false
// Authorization for GitHub OAuth login (see docs/public-access-setup.md).
// Keep locked to your own login until rate limiting (PR 2) lands.
param openRegistration bool = false
param allowedLogins string = '[]' // JSON array, e.g. '["jeffhoek"]'
// Elevated rate-limit cap for admin/trusted users, keyed by stable GitHub
// identifier. Personal/env-specific — injected at deploy time from a pipeline
// variable, not committed to the param file. JSON array, e.g. '["github:123"]'.
param adminUserIdentifiers string = '[]'
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
  'Anthropic Claude'
  'CVE-2026-25253'
  'OpenClaw'
  'Reference URLs for CVE-2017-11882'
  'Top AI CVEs in 2026'
  'VPN and remote access vulns'
  'Network device vulns'
  'Microsoft product vulns'
  'Top vendors in KEV'
  'Top CWE categories by avg CVSS score'
  'CWE-78'
  'Log4j'
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
          name: 'OPEN_REGISTRATION'
          value: string(openRegistration)
        }
        {
          name: 'ALLOWED_LOGINS'
          value: allowedLogins
        }
        {
          name: 'ADMIN_USER_IDENTIFIERS'
          value: adminUserIdentifiers
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
          // Read-only role for the live app (see docs/supabase-readonly-role.md).
          // The ETL job uses the separate write/admin 'database-url' secret.
          name: 'PG_DATABASE_URL'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=database-url-readonly)'
        }
        {
          // Read-only role can't run schema DDL; admin/ETL connection owns the schema.
          name: 'DB_INIT_SCHEMA'
          value: 'false'
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
          // GitHub OAuth App client ID. Not strictly secret (it appears in the
          // redirect URL), but kept in Key Vault to keep the deploy flow uniform.
          name: 'OAUTH_GITHUB_CLIENT_ID'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=oauth-github-client-id)'
        }
        {
          name: 'OAUTH_GITHUB_CLIENT_SECRET'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=oauth-github-client-secret)'
        }
        {
          name: 'CHAINLIT_AUTH_SECRET'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=chainlit-auth-secret)'
        }
        {
          // HTTP Basic password for the /admin dashboard. app.py fails fast at
          // startup if this is empty, so the admin-secret Key Vault secret must
          // exist before this reference deploys or the app crash-loops (503).
          name: 'ADMIN_SECRET'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=admin-secret)'
        }
        {
          // Canonical public URL. App Service terminates TLS at the front end and
          // forwards plain HTTP to the container, so without this Chainlit builds
          // the OAuth redirect_uri as http://… and GitHub rejects the mismatch.
          name: 'CHAINLIT_URL'
          value: 'https://${appServiceName}.azurewebsites.net'
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
