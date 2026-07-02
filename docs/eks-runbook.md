# EKS Deployment Runbook — chainlit-pydanticai-rag

This runbook covers deploying the Chainlit + Pydantic AI RAG chatbot to an AWS EKS cluster using GitHub Actions CI/CD.

---

## Architecture Overview

```
GitHub (push to main)
    ↓
[Build Job]
  • OIDC → AWS credentials (no long-lived keys)
  • docker build → push to ECR (tag: commit SHA)
    ↓
[Deploy Job]
  • Check EKS cluster health (skips gracefully if down)
  • kubectl apply k8s/ manifests
  • Rolling update with new image
  • Wait for rollout (120s timeout)
    ↓
AWS EKS (myeks, us-east-2)
  └─ Namespace: rag
      ├─ Deployment: chainlit-rag (1 replica)
      ├─ Service: ClusterIP (port 80 → 8080)
      └─ Ingress: ALB (internet-facing, HTTPS, sticky sessions)
           ↓  (TLS via ACM cert; rag.manheok.com → ALB via Cloudflare DNS)
      Chainlit app (port 8080)
        • GitHub OAuth login (no password auth)
        • Connects to managed Postgres (Supabase, pgvector) on startup
        • Read-only DB role; embeddings/queries served from pgvector
        • Pydantic AI agent → Claude (Anthropic)
```

The knowledge base lives in a managed **Supabase Postgres** (the same database the Azure deployment uses), with embeddings stored in `pgvector`. The app connects with a **read-only** role via `PG_DATABASE_URL` and does not own the schema — the admin/ETL connection creates and loads it. See [supabase-readonly-role.md](supabase-readonly-role.md).

Authentication is **GitHub OAuth** only (`@cl.oauth_callback`; there is no password login). That requires the app to be served over **HTTPS** at a real domain — Chainlit builds the OAuth `redirect_uri` from `CHAINLIT_URL`, and GitHub rejects an `http`/host mismatch. Here that's `https://rag.manheok.com`, with TLS terminated at the ALB using an ACM certificate and the hostname pointed at the ALB via Cloudflare DNS. OAuth provider setup and the authorization allow-list are covered in [public-access-setup.md](public-access-setup.md).

### Why 1 Replica?

State lives in Postgres, not in the pod, so the app scales horizontally cleanly. Starting with 1 replica simply keeps the initial deployment simple — scale up once you've validated it (`kubectl scale deployment chainlit-rag -n rag --replicas=2`).

### WebSocket Considerations

Chainlit uses WebSockets. The ALB is configured with:
- **600-second idle timeout** — exceeds Chainlit's session keep-alive to prevent mid-chat drops
- **24-hour sticky sessions** — keeps each browser on the same pod for the duration of its WebSocket session

---

## Prerequisites

### Tools

| Tool | Purpose | Install |
|------|---------|---------|
| `aws` CLI v2 | ECR login, EKS kubeconfig, IAM | https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html |
| `kubectl` | Apply manifests, check pod status | `brew install kubectl` |
| `eksctl` | IAM identity mapping for RBAC | `brew install eksctl` |
| `docker` or `podman` | Local image build/test | `brew install podman` |
| `uv` | Generate Chainlit auth secret | already in project |

### AWS Requirements

- EKS cluster `myeks` is **ACTIVE** in `us-east-2`
- ECR repository `chainlit-pydanticai-rag` exists (created in setup below)
- AWS CLI configured with sufficient permissions for setup steps

### GitHub Repository Requirements

- Repository is in GitHub (not just local)
- You have access to **Settings → Secrets and variables → Actions**

---

## One-Time Setup

### Step 1 — Create ECR Repository

```bash
aws ecr create-repository \
  --repository-name chainlit-pydanticai-rag \
  --region us-east-2
```

Note the registry URI from the output (format: `<account-id>.dkr.ecr.us-east-2.amazonaws.com`).

### Step 2 — Configure GitHub OIDC Provider in AWS

This allows GitHub Actions to assume an IAM role without storing long-lived AWS credentials.

```bash
# Check if OIDC provider already exists (may already be set up from another deployed application)
aws iam list-open-id-connect-providers | grep token.actions.githubusercontent.com
```

If not listed, create it:

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

The deploy role ARN follows a fixed format, so you can add the GitHub Actions secret now — before the role exists. All you need is your account ID:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "arn:aws:iam::${ACCOUNT_ID}:role/github-actions-chainlit-rag" | pbcopy
```

In your GitHub repository:
1. Go to **Settings → Secrets and variables → Actions**
2. Click **New repository secret**
3. Name: `AWS_DEPLOY_ROLE_ARN`
4. Value: paste the ARN copied above

### Step 3 — Create the IAM Deploy Role

An IAM role has two independent parts: a **trust policy** that controls *who* can assume the role, and **permission policies** that control *what* the role can do once assumed. This step handles the first part — creating the role and scoping assumption to GitHub Actions OIDC tokens from this repository. Permissions are attached in Step 4.

Replace `<your-github-org>` with your GitHub org or username.

```bash
GITHUB_ORG=<your-github-org>
REPO_NAME=chainlit-pydanticai-rag
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

- Write a trust policy document scoping role assumption to GitHub Actions OIDC tokens from this repo
```
cat > /tmp/trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:${GITHUB_ORG}/${REPO_NAME}:*"
        }
      }
    }
  ]
}
EOF
```

- Create the IAM role using the trust policy
```
aws iam create-role \
  --role-name github-actions-chainlit-rag \
  --assume-role-policy-document file:///tmp/trust-policy.json
```

### Step 4 — Attach Permissions to the Deploy Role

With the role created, attach the policies that define what it's authorized to do. These are evaluated independently of the trust policy — a caller must satisfy *both* to successfully use the role.

- Attach the ECR PowerUser managed policy to allow the role to push and pull images
```
aws iam attach-role-policy \
  --role-name github-actions-chainlit-rag \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser
```

- Write an inline policy granting the role permission to describe EKS clusters (needed to fetch kubeconfig)
```
cat > /tmp/eks-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "eks:DescribeCluster",
        "eks:ListClusters"
      ],
      "Resource": "*"
    }
  ]
}
EOF
```

- Attach the EKS inline policy to the role
```
aws iam put-role-policy \
  --role-name github-actions-chainlit-rag \
  --policy-name eks-describe \
  --policy-document file:///tmp/eks-policy.json
```

### Step 5 — Grant the Deploy Role kubectl Access

Add the IAM role to the EKS cluster's `aws-auth` ConfigMap, mapping it to a Kubernetes user with `system:masters` (cluster-admin) access so the GitHub Actions workflow can run `kubectl` commands.

```bash
eksctl create iamidentitymapping \
  --cluster myeks \
  --region us-east-2 \
  --arn arn:aws:iam::${ACCOUNT_ID}:role/github-actions-chainlit-rag \
  --username github-actions \
  --group system:masters
```

> **Note:** `system:masters` is the simplest way to grant full cluster access for CI/CD. For tighter security, create a custom ClusterRole limited to the `rag` namespace.

### Step 6 — Create the Kubernetes Namespace and Secrets

```bash
# Update your kubeconfig to point at myeks
aws eks update-kubeconfig --name myeks --region us-east-2
```

- Create the `rag` namespace in the cluster
```
kubectl apply -f k8s/namespace.yaml
```

- Create the `chainlit-rag` ServiceAccount the deployment runs as
```
kubectl apply -f k8s/serviceaccount.yaml
```

> The app reads its knowledge base from managed Postgres, not AWS, so the
> pod needs **no AWS data-plane access** — there is no Pod Identity / S3 IAM
> role to configure. The ServiceAccount is kept simply as the pod's identity.

Secrets are delivered by the **External Secrets Operator (ESO)**: you write the
values into **AWS SSM Parameter Store** under `/rag/*`, and the `ExternalSecret`
in [k8s/external-secret.yaml](../k8s/external-secret.yaml) syncs them into the
`rag-secrets` Kubernetes Secret. The deploy workflow applies that manifest, so
you do **not** create `rag-secrets` by hand.

**Prerequisites (provisioned with the cluster):**
- ESO is installed in `myeks`.
- A `ClusterSecretStore` named `aws-ssm-parameter-store` exists.
- The ESO controller's IAM identity (IRSA / Pod Identity) can read the params:
  `ssm:GetParameter`, `ssm:GetParametersByPath` on `/rag/*`, plus `kms:Decrypt`
  on the KMS key backing the `SecureString` values.

- Gather the values: generate the Chainlit signing secret and an admin secret,
  and get the **read-only** Supabase DSN (the same DB the Azure deployment uses —
  reuse Azure Key Vault's `database-url-readonly`; see
  [supabase-readonly-role.md](supabase-readonly-role.md)).
```bash
uv run chainlit create-secret   # → CHAINLIT_AUTH_SECRET
openssl rand -hex 32            # → ADMIN_SECRET
# PG_DATABASE_URL format: postgresql://USER:PASSWORD@HOST:5432/postgres
```

- Create a **GitHub OAuth App** for this deployment (auth is OAuth-only). A classic
  OAuth App has a single callback URL tied to one host, so the EKS app needs its
  own — separate from the Azure one. In GitHub → Settings → Developer settings →
  OAuth Apps → New OAuth App:
  - Homepage URL: `https://rag.manheok.com`
  - Authorization callback URL: `https://rag.manheok.com/auth/oauth/github/callback`

  Copy the Client ID and generate a client secret. See [public-access-setup.md](public-access-setup.md).

- Write each value into SSM Parameter Store as a `SecureString`
```bash
for var in ANTHROPIC_API_KEY OPENAI_API_KEY CHAINLIT_AUTH_SECRET ADMIN_SECRET MCP_API_KEY \
           OAUTH_GITHUB_CLIENT_ID OAUTH_GITHUB_CLIENT_SECRET PG_DATABASE_URL; do
  echo "$var" && read -rs val
  aws ssm put-parameter --name "/rag/$var" --value "$val" --type SecureString --overwrite --region us-east-2
done
```

- Apply the `ExternalSecret` and confirm ESO syncs it (the deploy does this too,
  but applying now lets you verify before deploying)
```bash
kubectl apply -f k8s/external-secret.yaml
kubectl get externalsecret rag-secrets -n rag   # STATUS should reach SecretSynced
kubectl get secret rag-secrets -n rag           # ESO-managed; should now exist
```

> `ADMIN_SECRET` guards the `/admin` dashboard — the app **fails fast at
> startup if it is unset**. `MCP_API_KEY` authenticates the `/mcp` endpoint;
> if unset, `/mcp` is **publicly UNAUTHENTICATED** (it's reachable through the
> ALB), so set it before exposing the app. Generate either with `openssl rand -hex 32`.
>
> `DB_INIT_SCHEMA=false` is set in the ConfigMap (Step earlier): the live app
> uses a read-only role and must not run schema DDL — the admin/ETL connection
> owns the schema.
>
> The plain `kubectl create secret` flow (and [k8s/secret.yaml.example](../k8s/secret.yaml.example))
> remains documented as a fallback, but ESO/SSM is the path used here — do not
> create `rag-secrets` manually, or ESO (`creationPolicy: Owner`) will fight it.

### Step 7 — Public domain + TLS (required for OAuth)

GitHub OAuth needs the app on **HTTPS at a real hostname**. TLS is terminated at
the ALB with an ACM certificate; the hostname (`rag.manheok.com`) resolves to the
ALB via Cloudflare DNS. The `k8s/ingress.yaml` already carries the
`certificate-arn`, an `HTTPS:443` listener, `ssl-redirect`, and the `host` rule;
`CHAINLIT_URL` and `ALLOWED_LOGINS` are set in `k8s/configmap.yaml`.

- Request a public ACM certificate **in `us-east-2`** (the ALB's region):
```bash
aws acm request-certificate --domain-name rag.manheok.com \
  --validation-method DNS --region us-east-2
```

- Add the DNS validation record in **Cloudflare** as a `CNAME`, **Proxy status =
  DNS only (grey cloud)** — a proxied record breaks ACM validation. Get it with:
```bash
aws acm describe-certificate --certificate-arn <arn> --region us-east-2 \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord' --output table
```
  Wait until the cert is `ISSUED` (`...--query 'Certificate.Status' --output text`).
  Put its ARN in `k8s/ingress.yaml` (`certificate-arn`).

- The app `CNAME` (`rag` → the ALB hostname) is added **after** the deploy creates
  the ALB — see Post-Deploy Verification. Keep it **DNS only (grey cloud)** too, so
  clients reach the ALB directly and get the ACM cert (a proxied record would put
  Cloudflare's TLS in front and need SSL mode "Full").

> **Cert must be `ISSUED` before the deploy.** The AWS Load Balancer Controller
> can't attach a still-`PENDING_VALIDATION` cert to the HTTPS listener.

---

## Deploying

Once the one-time setup is complete, deployments happen automatically:

- **Auto-deploy**: push any commit to `main`
- **Manual trigger**: GitHub → Actions → "Deploy to EKS" → "Run workflow"

The workflow:
1. Builds a Docker image tagged with the commit SHA
2. Pushes to ECR
3. Checks the EKS cluster is active (skips deploy gracefully if not)
4. Applies all `k8s/` manifests
5. Does a rolling update (`kubectl set image`) to the new image
6. Waits up to 120s for rollout to complete

---

## Post-Deploy Verification

- Watch pod status until `Ready` (startup is fast — just a Postgres connection, no data loading)
```bash
kubectl get pods -n rag -w
```

- Stream logs to confirm successful startup
```bash
kubectl logs -n rag deploy/chainlit-rag --follow
```

- Get the ALB hostname from the `ADDRESS` column
```bash
kubectl get ingress -n rag
```

- Point DNS at it: add a `CNAME` in **Cloudflare**, `rag` → the ALB hostname,
  **Proxy status = DNS only (grey cloud)**. Wait for it to resolve:
```bash
dig +short rag.manheok.com
```

- Confirm the app is healthy over HTTPS (once DNS resolves and the cert is attached)
```bash
curl https://rag.manheok.com/healthz
# Expected: {"status":"ok"}
```

- Open in browser and log in with **GitHub** (the account must be on `ALLOWED_LOGINS`)
```bash
open https://rag.manheok.com
```

---

## Rollback

- Roll back to the previous deployment
```bash
kubectl rollout undo deployment/chainlit-rag -n rag
```

- To roll back to a specific revision, first list available revisions
```bash
kubectl rollout history deployment/chainlit-rag -n rag
```

- Then roll back to the chosen revision
```bash
kubectl rollout undo deployment/chainlit-rag -n rag --to-revision=<N>
```

---

## Pause / Resume (destroy the cluster to save cost)

EKS has no "pause." To stop paying for the cluster overnight (or over a break)
and pick up where you left off, **destroy the cluster and recreate it later**.
This is cheap and safe because almost everything that holds secrets or config is
**account-scoped, not cluster-scoped**, so it survives the teardown untouched —
only the in-cluster resources are lost, and those are exactly what a redeploy
recreates.

### Survives teardown — leave it all alone

This is your reusable config; do **not** delete any of it during a pause:

| Resource | Why it's safe to keep |
|----------|-----------------------|
| SSM parameters `/rag/*` | Account-scoped. ESO re-syncs `rag-secrets` from these on redeploy. |
| IAM role `github-actions-chainlit-rag` (+ policies) | Account-scoped. |
| ECR repo `chainlit-pydanticai-rag` (+ images) | Built image tags persist. |
| ACM certificate | Stays `ISSUED` as long as its **Cloudflare DNS validation CNAME** stays. Ingress references it by ARN, which doesn't change. |
| GitHub OAuth App + `AWS_DEPLOY_ROLE_ARN` repo secret | External to AWS. |
| GitHub OIDC provider | Shared account resource. |

Cost while paused is essentially zero (ECR storage is pennies; SSM standard
SecureStrings, ACM, and IAM are free).

### Recreated on resume

Everything in the `rag` namespace (deployment, service, ingress, configmap,
serviceaccount, ExternalSecret, and the ESO-synced `rag-secrets`), plus the
cluster-level infra: **ESO**, the `aws-ssm-parameter-store` **ClusterSecretStore**
(+ its IRSA/Pod Identity), the **AWS Load Balancer Controller**, and the
`aws-auth` mapping for the deploy role.

### Pause (tear down)

- **Delete the Ingress first and wait for the ALB to actually disappear.** The
  ALB is created out-of-band by the LB controller, so `eksctl`/Terraform doesn't
  track it. If you destroy the cluster with the ALB still up, its ENIs and
  security groups **block VPC deletion** and the destroy hangs.
```bash
kubectl delete -f k8s/ingress.yaml
aws elbv2 describe-load-balancers --region us-east-2 --query 'LoadBalancers[].DNSName'
# wait until the rag ALB is gone from that list
```

- **Destroy the cluster.** This project's `myeks` cluster is managed in Terraform
  Cloud (`app.terraform.io` → org `jhorg`, workspace `myeks`): Runs → **Queue
  destroy plan**. (For an `eksctl`-managed cluster it's
  `eksctl delete cluster --name myeks --region us-east-2`.) No need to delete the
  namespace separately — it goes with the cluster.

- **Cloudflare:** keep the **ACM validation CNAME** (so the cert stays `ISSUED`).
  The `rag` → ALB CNAME now points at a dead ALB; leave it or delete it — you'll
  repoint it on resume anyway, since the new ALB gets a new hostname.

> **⚠️ ESO destroy-ordering gotcha.** The Terraform helm provider needs a live
> Kubernetes API to *gracefully* uninstall `helm_release.eso`. If the destroy
> tears down the node group / `aws-auth` before ESO, or an `ExternalSecret`
> finalizer hangs the uninstall, the run fails with **`helm_release.eso Delete
> Failed`** and (in Terraform Cloud) halts there — leaving the cluster up. Add an
> explicit `depends_on` so `helm_release.eso` is destroyed **before** the cluster
> / node group to avoid the race. If you hit it anyway, see recovery below.

#### Recovering a wedged `helm_release.eso` destroy

1. Check whether the release actually uninstalled (it usually did, despite the error):
```bash
aws eks update-kubeconfig --name myeks --region us-east-2
helm list -A            # is `external-secrets` still listed?
```
2. **If `external-secrets` is gone from `helm list`** — the release is already
   uninstalled; the state just still references it. Re-run the destroy (Queue
   destroy plan); the provider finds no release and proceeds. If it errors
   `release: not found`, remove it from state with the CLI pointed at the
   workspace (Terraform Cloud has no state-rm button), then queue destroy again:
```bash
# ~/.terraformrc holds your app.terraform.io token; module has a cloud{} block
terraform state rm helm_release.eso
```
3. **If it's still listed / stuck**, clear the finalizers blocking uninstall,
   then re-queue:
```bash
kubectl get externalsecrets -A
kubectl patch externalsecret rag-secrets -n rag --type merge -p '{"metadata":{"finalizers":[]}}'
kubectl patch ns external-secrets --type merge -p '{"metadata":{"finalizers":[]}}'   # if Terminating
helm uninstall external-secrets -n external-secrets --no-hooks                        # last resort
```
   Leftover ESO CRDs/namespace don't need chasing — they're destroyed with the cluster.

### Resume (spin back up)

1. **Recreate the cluster** — Terraform Cloud: Runs → **Start new run** (apply)
   for the `myeks` workspace (or `eksctl create cluster ...`).
2. **Post-install cluster infra** (not created by this runbook's steps): reinstall
   **ESO**, recreate the **`aws-ssm-parameter-store` ClusterSecretStore** + its
   IRSA/Pod Identity (SSM read on `/rag/*` + `kms:Decrypt`), and reinstall the
   **AWS Load Balancer Controller**. (If these are in the Terraform config, the
   apply handles them.)
3. **Re-map the deploy role** into the new cluster (same as one-time Step 5):
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
eksctl create iamidentitymapping --cluster myeks --region us-east-2 \
  --arn arn:aws:iam::${ACCOUNT_ID}:role/github-actions-chainlit-rag \
  --username github-actions --group system:masters
```
4. **Update kubeconfig and redeploy** — push to `main` (or run the deploy
   workflow manually). It applies `k8s/`, the ExternalSecret re-syncs
   `rag-secrets` from your intact SSM params, and a new ALB is provisioned.
```bash
aws eks update-kubeconfig --name myeks --region us-east-2
```
5. **Repoint DNS and verify** — the new ALB has a new hostname:
```bash
kubectl get ingress -n rag                      # copy the ADDRESS
# update the Cloudflare `rag` CNAME → new ALB hostname (DNS only / grey cloud)
curl https://rag.manheok.com/healthz            # expect {"status":"ok"}
```

---

## Troubleshooting

### Pod stuck in `Pending`

```bash
kubectl describe pod -n rag -l app=chainlit-rag
```
Common causes: insufficient node resources, image pull error (check ECR permissions), or no nodes available.

### Pod `CrashLoopBackOff` / not becoming `Ready`

Startup only opens a Postgres connection, so failures here are almost always DB- or config-related:

```bash
kubectl logs -n rag deploy/chainlit-rag
# Look for: connection refused / auth failures, or "ADMIN_SECRET must be set"
```

Common causes:
- **`PG_DATABASE_URL` wrong or unreachable** — verify the read-only Supabase DSN, and that Supabase network restrictions allow connections from the cluster's egress.
- **`ADMIN_SECRET` unset** — the app fails fast at startup; confirm it's in `rag-secrets`.
- **`DB_INIT_SCHEMA` not `false`** — a read-only role can't run schema DDL, so the app errors on startup if it tries. Confirm the ConfigMap sets `DB_INIT_SCHEMA: "false"`.
- **`You must set the environment variable for at least one oauth provider`** — `OAUTH_GITHUB_CLIENT_ID`/`_SECRET` didn't reach the pod. Check the `ExternalSecret` synced them (`kubectl get secret rag-secrets -n rag -o jsonpath='{.data.OAUTH_GITHUB_CLIENT_ID}'`) and that the SSM params exist.

### OAuth login fails (redirect/denied)

- **GitHub error "redirect_uri is not associated with this application"** — the OAuth App's callback URL must be exactly `https://rag.manheok.com/auth/oauth/github/callback`, and `CHAINLIT_URL` must be `https://rag.manheok.com`. A trailing-slash or `http` mismatch fails here.
- **Login succeeds at GitHub but the app rejects you** — your GitHub username isn't on `ALLOWED_LOGINS` (or set `OPEN_REGISTRATION=true`). Check the pod log for `OAuth denied: ...`.
- **Browser cert warning / TLS errors** — the ACM cert isn't `ISSUED` or attached. Confirm `aws acm describe-certificate ... Status` is `ISSUED` and the ALB has an HTTPS:443 listener (`kubectl describe ingress -n rag`).

### Image pull errors

```bash
kubectl describe pod -n rag -l app=chainlit-rag | grep -A5 Events
```
Ensure the ECR repository exists and the deploy role has `AmazonEC2ContainerRegistryPowerUser`.

### WebSocket disconnects mid-chat

Check the ALB idle timeout. If users are seeing disconnects, confirm the ingress annotation is set to `idle_timeout.timeout_seconds=600`.

### Updating secrets

Secrets are sourced from SSM via ESO, so update the **parameter**, not the
Kubernetes Secret directly (ESO owns `rag-secrets` and would overwrite a manual
edit on its next sync):

```bash
# 1. Update the SSM parameter
aws ssm put-parameter --name /rag/PG_DATABASE_URL --value <new-dsn> \
  --type SecureString --overwrite --region us-east-2

# 2. Force ESO to re-sync now (or wait for refreshInterval: 1h)
kubectl annotate externalsecret rag-secrets -n rag force-sync="$(date +%s)" --overwrite

# 3. Running pods don't reload a changed Secret — restart to pick it up
kubectl rollout restart deployment/chainlit-rag -n rag
```

### Scaling up replicas

All state lives in Postgres, so replicas are stateless and safe to scale. ALB sticky sessions keep each browser's WebSocket pinned to one pod:

```bash
kubectl scale deployment chainlit-rag -n rag --replicas=2
```

---

## Environment Variables Reference

### Stored in SSM Parameter Store (`/rag/*`), synced to the `rag-secrets` Secret by ESO

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic Claude API key |
| `OPENAI_API_KEY` | OpenAI API key for embeddings |
| `CHAINLIT_AUTH_SECRET` | Chainlit session signing secret (`chainlit create-secret`) |
| `ADMIN_SECRET` | HTTP Basic password for `/admin`; app fails fast if unset (`openssl rand -hex 32`) |
| `MCP_API_KEY` | Authenticates the `/mcp` endpoint; if unset, `/mcp` is publicly unauthenticated (`openssl rand -hex 32`) |
| `OAUTH_GITHUB_CLIENT_ID` | GitHub OAuth App client ID |
| `OAUTH_GITHUB_CLIENT_SECRET` | GitHub OAuth App client secret |
| `PG_DATABASE_URL` | Read-only Supabase DSN (same DB as the Azure deployment) |

### Stored as Kubernetes ConfigMap (`rag-config`)

| Variable | Default | Description |
|----------|---------|-------------|
| `CHAINLIT_URL` | `https://rag.manheok.com` | Public HTTPS origin; Chainlit builds the OAuth `redirect_uri` from it |
| `ALLOWED_LOGINS` | `["jeffhoek"]` | GitHub usernames admitted by the OAuth callback (JSON array) |
| `DB_INIT_SCHEMA` | `false` | Read-only role — skip schema DDL (owned by admin/ETL connection) |
| `LLM_MODEL` | `anthropic:claude-haiku-4-5-20251001` | Pydantic AI model string |
| `TOP_K` | `5` | Number of chunks returned by RAG retrieval |
| `SYSTEM_PROMPT` | _(see configmap)_ | System prompt injected into the Pydantic AI agent |
