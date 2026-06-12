// Azure Communication Services Email — used by the scheduled ETL job to send a
// results summary after each run. Uses an Azure-managed sender domain (no DNS
// setup required) and Entra ID auth via the job's managed identity (no secrets).

param communicationServiceName string
param emailServiceName string
param managedIdentityPrincipalId string

@description('Data residency for ACS + Email (e.g. UnitedStates, Europe).')
param dataLocation string = 'UnitedStates'

param tags object = {}

// Contributor — ACS has no fine-grained data-plane role for email send; scope it
// tightly to just this Communication Services resource (not the resource group).
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

// Email Communication Service + an Azure-managed domain (<guid>.azurecomm.net).
// ACS resources are always location 'global'; residency is set via dataLocation.
resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: emailServiceName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
  }
}

resource managedDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource communicationService 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: communicationServiceName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
    linkedDomains: [
      managedDomain.id
    ]
  }
}

// Let the job's managed identity authenticate to ACS for sending email.
resource acsRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(communicationService.id, managedIdentityPrincipalId, 'acs-contributor')
  scope: communicationService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
    description: 'Allow the ETL job identity to send email via ACS'
  }
}

// donotreply@<managed-domain> is provisioned automatically for AzureManaged domains.
output senderAddress string = 'donotreply@${managedDomain.properties.fromSenderDomain}'
output acsEndpoint string = 'https://${communicationService.properties.hostName}'
