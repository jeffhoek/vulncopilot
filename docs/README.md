# Documentation

## Guides

| Document | Description |
|---|---|
| [data-loading.md](data-loading.md) | ETL guide for populating the database with CISA KEV and NVD data (KEV-scoped and full NVD) |
| [nvd-integration.md](nvd-integration.md) | NVD & CISA KEV dataset integration, schema, and cross-reference query examples |
| [cwe-integration.md](cwe-integration.md) | MITRE CWE weakness taxonomy integration — `cwe_definitions` lookup table, ETL script, and example JOIN queries |
| [mcp-server.md](mcp-server.md) | MCP server setup — exposes `retrieve` and `query` tools at `/mcp` for external agents |
| [action-buttons.md](action-buttons.md) | Configure Chainlit action buttons on the welcome message for quick-access suggested questions |
| [observability.md](observability.md) | Observability integrations — self-hosted Langfuse (Compose) and cloud-hosted Logfire |
| [supabase-readonly-role.md](supabase-readonly-role.md) | Role-based access control in Supabase — `app_readonly` (SELECT only) for the live app, `app_etl` for ETL scripts |
| [deploy-azure-app-service.md](deploy-azure-app-service.md) | Deploy to Azure App Service as a Linux container, using ACR, Key Vault, and Azure Pipelines with Workload Identity Federation — includes MCP server setup |
| [deploy-gcp-cloud-run.md](deploy-gcp-cloud-run.md) | Deploy to Google Cloud Run |
| [eks-runbook.md](eks-runbook.md) | Deploy to AWS EKS using GitHub Actions CI/CD |

---

## Cloud Service Mapping

Equivalent services across AWS, GCP, and Azure for each layer of the stack:

| Service Type | AWS | GCP | Azure | Notes |
|---|---|---|---|---|
| Container Registry | ECR | Artifact Registry | Container Registry (ACR) | ACR admin disabled; image pull via Managed Identity |
| App Runtime | EKS | Cloud Run | App Service (Linux container) | All three require sticky sessions for WebSocket support |
| Secrets Management | Secrets Manager / SSM Parameter Store | Secret Manager | Key Vault | Azure uses RBAC model with Key Vault references in app settings |
| Object Storage | S3 | Cloud Storage | Blob Storage | App loads knowledge base from storage on startup |
| Workload Identity | EKS Pod Identity | Workload Identity (Service Account) | User-Assigned Managed Identity | Binds a cloud IAM role to the app runtime; no static credentials |
| CI/CD | GitHub Actions + OIDC | Cloud Build + Workload Identity | Azure Pipelines + Workload Identity Federation | OIDC/WIF eliminates stored access keys in all three |
| IaC | CloudFormation/Terraform | Cloud Deployment Manager | Bicep / ARM | Azure uses `.bicepparam` parameter files |
| Policy Enforcement | AWS Organizations / Config | Organization Policy | Azure Policy | Enforces guardrails (HTTPS-only, required tags) |
| Access Control | AWS IAM | Cloud IAM | Azure RBAC | Minimal-privilege role assignments throughout |
