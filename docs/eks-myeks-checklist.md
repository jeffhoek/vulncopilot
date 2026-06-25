# EKS Deploy Checklist — `myeks` (Supabase Postgres)

A focused, checkable companion to [eks-runbook.md](eks-runbook.md) for bringing the
chatbot up on the **`myeks`** cluster. The app reads from the **same read-only
Supabase Postgres** the Azure deployment uses — there is **no S3 / Pod Identity**
to configure anymore.

```bash
# Set once at the top of your shell session
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-2
CLUSTER=myeks
echo "Account: $ACCOUNT_ID  Region: $REGION  Cluster: $CLUSTER"
```

---

## A. Verify-first (account/repo-scoped — likely already done)

- [ ] **ECR repo exists** (runbook Step 1)
  ```bash
  aws ecr describe-repositories --repository-names chainlit-pydanticai-rag --region $REGION \
    --query 'repositories[0].repositoryUri' --output text
  ```
  Missing → `aws ecr create-repository --repository-name chainlit-pydanticai-rag --region $REGION`

- [ ] **GitHub OIDC provider exists** (Step 2)
  ```bash
  aws iam list-open-id-connect-providers | grep token.actions.githubusercontent.com
  ```

- [ ] **Deploy role trust policy points at the *real* repo** (Step 3) — ⚠️ the gotcha
  ```bash
  aws iam get-role --role-name github-actions-chainlit-rag \
    --query 'Role.AssumeRolePolicyDocument' --output json
  ```
  The `sub` must read **`repo:jeffhoek/chainlit-pydanticai-postgres:*`** (not `...-rag`).
  Fix → edit `/tmp/trust-policy.json`, then
  `aws iam update-assume-role-policy --role-name github-actions-chainlit-rag --policy-document file:///tmp/trust-policy.json`

- [ ] **Deploy role has ECR + EKS-describe permissions** (Step 4)
  ```bash
  aws iam list-attached-role-policies --role-name github-actions-chainlit-rag
  aws iam list-role-policies --role-name github-actions-chainlit-rag
  ```

- [ ] **`AWS_DEPLOY_ROLE_ARN` GitHub secret is set** (Step 2) — was missing last check
  ```bash
  gh secret list --repo jeffhoek/chainlit-pydanticai-postgres | grep AWS_DEPLOY_ROLE_ARN
  ```
  Missing →
  ```bash
  gh secret set AWS_DEPLOY_ROLE_ARN --repo jeffhoek/chainlit-pydanticai-postgres \
    --body "arn:aws:iam::${ACCOUNT_ID}:role/github-actions-chainlit-rag"
  ```

> **No S3 IAM role.** The old `chainlit-rag-s3` role + Pod Identity association +
> `eks-pod-identity-agent` addon are no longer used. If they linger from a prior
> deploy you can delete them, but they're harmless.

---

## B. Cluster-scoped (must redo for the new `myeks` cluster)

- [ ] **Point kubeconfig at `myeks`**
  ```bash
  aws eks update-kubeconfig --name $CLUSTER --region $REGION
  kubectl config current-context
  ```

- [ ] **Step 5 — Map the deploy role into `myeks` aws-auth** (so GH Actions `kubectl` works)
  ```bash
  eksctl create iamidentitymapping --cluster $CLUSTER --region $REGION \
    --arn arn:aws:iam::${ACCOUNT_ID}:role/github-actions-chainlit-rag \
    --username github-actions --group system:masters
  eksctl get iamidentitymapping --cluster $CLUSTER --region $REGION   # verify
  ```

- [ ] **Step 6 — Namespace + ServiceAccount**
  ```bash
  kubectl apply -f k8s/namespace.yaml
  kubectl apply -f k8s/serviceaccount.yaml   # bare pod identity; no AWS access
  ```

- [ ] **ESO prereqs present** — secrets are delivered via External Secrets Operator
  ```bash
  kubectl get clustersecretstore aws-ssm-parameter-store   # STATUS Valid/Ready
  kubectl get crd externalsecrets.external-secrets.io      # ESO installed
  ```
  Also confirm the ESO controller's IAM identity can read `/rag/*`:
  `ssm:GetParameter`, `ssm:GetParametersByPath`, and `kms:Decrypt` on the
  SecureString KMS key.

- [ ] **Step 6 — Write secrets to SSM Parameter Store** (`/rag/*`, SecureString)
  ```bash
  # CHAINLIT_AUTH_SECRET: uv run chainlit create-secret
  # ADMIN_SECRET:         openssl rand -hex 32
  # PG_DATABASE_URL:      read-only Supabase DSN (same as Azure 'database-url-readonly')
  for var in ANTHROPIC_API_KEY OPENAI_API_KEY APP_PASSWORD CHAINLIT_AUTH_SECRET ADMIN_SECRET PG_DATABASE_URL; do
    echo "$var" && read -rs val
    aws ssm put-parameter --name "/rag/$var" --value "$val" \
      --type SecureString --overwrite --region $REGION
  done
  ```
  > Do **not** `kubectl create secret rag-secrets` — ESO owns it (`creationPolicy: Owner`)
  > and would overwrite a manual secret.

- [ ] **Step 6 — Apply the ExternalSecret and confirm it syncs**
  ```bash
  kubectl apply -f k8s/external-secret.yaml
  kubectl get externalsecret rag-secrets -n rag   # STATUS → SecretSynced
  kubectl get secret rag-secrets -n rag           # now exists, ESO-managed
  ```
  (The deploy workflow also applies this, but syncing now verifies SSM + ESO before deploying.)

- [ ] **Supabase allows the cluster's egress IP** — pods leave via the EKS NAT
      gateway, a different IP than Azure App Service. If Supabase restricts inbound
      by IP, add the NAT gateway IP or the pod gets `connection refused`.

> **No Step 7.** Pod Identity / S3 IAM role is gone — the app uses Postgres, not AWS data services.

---

## C. Deploy + verify

- [ ] **AWS Load Balancer Controller present on `myeks`** (the ingress needs it)
  ```bash
  kubectl get deploy -n kube-system aws-load-balancer-controller
  ```
  Missing → install it, or `k8s/ingress.yaml` never gets an ALB hostname.

- [ ] **`deploy.yml` (manual-only, cluster `myeks`) is on `main`, and the workflow is re-enabled**
  - Push/merge the manual-only `deploy.yml` to `main` **first**, then GitHub →
    Actions → "Deploy to EKS" → **Enable workflow** (safe — no `push` trigger, so
    enabling doesn't auto-deploy).

- [ ] **Dispatch the deploy** — GitHub → Actions → **Deploy to EKS** → Run workflow

- [ ] **Watch rollout** (startup is fast now — just a DB connection)
  ```bash
  kubectl get pods -n rag -w
  kubectl logs -n rag deploy/chainlit-rag --follow
  kubectl get ingress -n rag                 # grab ALB hostname
  curl http://<alb-hostname>/healthz         # expect {"status":"ok"}
  ```

---

## Most likely snags

1. **ExternalSecret not `SecretSynced`** — `rag-secrets` never gets created, so pods hang in `ContainerCreating`. Usually a missing SSM param, the ESO role lacking `ssm:Get*`/`kms:Decrypt`, or a `ClusterSecretStore` that isn't `Valid`. Check `kubectl describe externalsecret rag-secrets -n rag`.
2. **`PG_DATABASE_URL` unreachable** — wrong DSN, or Supabase IP allow-list missing the cluster NAT IP.
3. **`ADMIN_SECRET` unset** — app fails fast at startup (`config.py` enforces it).
4. **AWS Load Balancer Controller missing** — deploy looks "successful" but the app is unreachable (no ALB hostname).
