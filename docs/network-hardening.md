# NetworkPolicy — Before/After Pen Test

A record of adding a default-deny `NetworkPolicy` to `chainlit-rag` on EKS: what the absence of one let you do, what changed, and how each change was verified live. Same methodology as [container-hardening.md](container-hardening.md) — confirm the weakness, apply the change, confirm it's closed and the app still works. Kept here so the exercise doesn't have to be redone from scratch to answer "why do we have this `NetworkPolicy`, and why did it look broken the first time?"

## Goal

With no `NetworkPolicy` in the `rag` namespace, demonstrate concretely what that exposes, then apply a default-deny ingress policy scoped to only the ALB, and verify both that the exposure is closed and the app still works through its real public path.

## Why this matters here

`chainlit-rag` is a single-tenant deployment today, so this isn't isolating tenant A from tenant B. The exposure is more basic: **any pod, anywhere in the cluster, in any namespace, has a direct network path to the app that only the ALB is supposed to have** — bypassing TLS termination, the 600s idle timeout, sticky sessions, and any assumption that "traffic reaches this pod only via the load balancer." A compromised or careless workload dropped into any other namespace on `myeks` could talk to `chainlit-rag` directly. Default-deny closes that regardless of whether the cluster ever becomes multi-tenant.

## Before: what the absence of a NetworkPolicy allowed

No `NetworkPolicy` existed in the `rag` namespace (`kubectl get networkpolicy -A` → `No resources found`). From a throwaway pod in an unrelated namespace (`default`), simulating any other workload on the cluster:

```bash
kubectl run netpol-test -n default --image=curlimages/curl:latest --restart=Never --command -- sleep 3600
kubectl wait -n default pod/netpol-test --for=condition=Ready --timeout=60s

# via the in-cluster Service DNS
kubectl exec -n default netpol-test -- curl -sS -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 \
  http://chainlit-rag.rag.svc.cluster.local/healthz
# HTTP 200

# via the raw pod IP, bypassing the Service too
POD_IP=$(kubectl get pod -n rag -l app=chainlit-rag -o jsonpath='{.items[0].status.podIP}')
kubectl exec -n default netpol-test -- curl -sS -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 \
  http://$POD_IP:8080/healthz
# HTTP 200

kubectl exec -n default netpol-test -- curl -sS -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 \
  http://chainlit-rag.rag.svc.cluster.local/mcp
# HTTP 401 — only because MCP_API_KEY happens to be set; nothing at the network layer required it
```

**Summary of exposure:** every endpoint on the pod (health, chat UI, `/mcp`) was reachable from any namespace with zero network-layer defense-in-depth. The `/mcp` 401 was the app's own auth check, not the cluster's — if that key were ever unset (a scenario [eks-runbook.md](eks-runbook.md) explicitly calls out as "publicly UNAUTHENTICATED"), there would have been nothing else standing between an arbitrary in-cluster pod and that endpoint.

## Changes made

| File | Change |
|---|---|
| [k8s/networkpolicy.yaml](../k8s/networkpolicy.yaml) | Default-deny ingress (`podSelector: {}`, `policyTypes: ["Ingress"]`) with two explicit allows: the ALB's public subnets on `8080`, and same-namespace traffic (`podSelector: {}` under `from`) for any future in-namespace service. |

### The `target-type: ip` gotcha

[k8s/ingress.yaml](../k8s/ingress.yaml) sets `alb.ingress.kubernetes.io/target-type: ip`, so the ALB registers pod IPs directly as targets and sends traffic straight to the pod — it never goes through the `ClusterIP` Service or `kube-proxy`. That means the ALB has **no Kubernetes pod identity**, so a `podSelector`-based allow rule can't express "let the load balancer in." The fix is an `ipBlock` allow scoped to the ALB's actual subnets rather than the pod/node subnets:

```bash
aws eks describe-cluster --name myeks --region us-east-2 \
  --query 'cluster.resourcesVpcConfig.subnetIds'
# node/pod subnets: 10.0.1.0/24, 10.0.2.0/24, 10.0.3.0/24

aws elbv2 describe-load-balancers --region us-east-2 \
  --query "LoadBalancers[?contains(DNSName,'k8s-rag-chainlit')].AvailabilityZones"
# ALB subnets (tagged kubernetes.io/role/elb): 10.0.4.0/24, 10.0.5.0/24, 10.0.6.0/24
```

The ALB and node/pod subnets are cleanly disjoint in this VPC, so the `ipBlock` can be scoped tightly to `10.0.4.0/24`–`10.0.6.0/24` without accidentally also allowing every other pod in the cluster the way a broader VPC-CIDR `ipBlock` would.

## The addon gotcha: applying the policy did nothing at first

Applying `k8s/networkpolicy.yaml` produced a real `NetworkPolicy` object, but the first round of testing showed it having **no effect at all** — the exact same `curl`s from `default` still returned `200`/`401` instead of timing out.

Root cause: a `NetworkPolicy` object alone doesn't enforce anything on EKS's VPC CNI. The node agent (`aws-eks-nodeagent`, running inside the `aws-node` DaemonSet) only enforces what's described in `PolicyEndpoint` custom resources — and those are only created by a controller that watches `NetworkPolicy` objects, which only runs when the `vpc-cni` addon has `enableNetworkPolicy` turned on:

```bash
kubectl get policyendpoints -A
# No resources found  — confirms nothing was translating the NetworkPolicy into enforcement

aws eks describe-addon --cluster-name myeks --addon-name vpc-cni --region us-east-2
# no configurationValues set at all → enableNetworkPolicy defaults to false
```

The `NETWORK_POLICY_ENFORCING_MODE: standard` env var visible on `aws-node` is a red herring here — it ships as a default in the manifest regardless of whether the feature is actually enabled, so its presence doesn't prove enforcement is active.

Fix — enable it on the addon (this rolls `aws-node` cluster-wide, broader blast radius than the namespace-scoped `NetworkPolicy` itself, so this was confirmed before running):

```bash
aws eks update-addon --cluster-name myeks --addon-name vpc-cni \
  --resolve-conflicts PRESERVE \
  --configuration-values '{"enableNetworkPolicy":"true"}' \
  --region us-east-2
```

In this `vpc-cni` version (`v1.22.2-eksbuild.1`) the `NetworkPolicy`→`PolicyEndpoint` reconciliation runs inside the existing `aws-node`/`aws-eks-nodeagent` containers rather than as a separate controller pod, so there's no new pod to `grep` for by name — the real confirmation is the `PolicyEndpoint` object itself:

```bash
kubectl get policyendpoints -n rag
# chainlit-rag-default-deny-ingress-bxtf7   63s

kubectl get policyendpoints -n rag chainlit-rag-default-deny-ingress-bxtf7 -o yaml
# ownerReferences → the NetworkPolicy
# spec.ingress → the three ALB-subnet CIDRs on port 8080
# spec.podSelectorEndpoints → the live chainlit-rag pod IP
```

## After: verification

```bash
kubectl exec -n default netpol-test -- curl -sS -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 \
  http://$POD_IP:8080/healthz
# times out (HTTP 000) — no longer 200

kubectl exec -n default netpol-test -- curl -sS -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 \
  http://chainlit-rag.rag.svc.cluster.local/mcp
# times out (HTTP 000) — no longer 401
```

Functional check on the real path — the ALB target group actually wired to the live listener (`k8s-rag-chainlit-e166a5b916`; a second, orphaned target group from an earlier rollout, `k8s-rag-chainlit-ad180b5fc4`, shows `Target.NotInUse` and isn't attached to any listener — unrelated to this change):

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 10 https://rag.manheok.com/healthz
# HTTP 200

aws elbv2 describe-target-health --region us-east-2 \
  --target-group-arn arn:aws:elasticloadbalancing:us-east-2:958170895933:targetgroup/k8s-rag-chainlit-e166a5b916/a0be34e87b12b298
# TargetHealth.State: healthy
```

GitHub OAuth login through `https://rag.manheok.com` confirmed working manually in-browser.

## Rollback

- **Policy only**: `kubectl delete -f k8s/networkpolicy.yaml` — immediately reopens ingress to the pod; no pod restart needed since enforcement is via `PolicyEndpoint` reconciliation, not the pod's own config.
- **Addon-level (last resort)**: `aws eks update-addon --cluster-name myeks --addon-name vpc-cni --resolve-conflicts PRESERVE --configuration-values '{"enableNetworkPolicy":"false"}' --region us-east-2` — only needed if the network-policy feature itself is suspected to be misbehaving, since this rolls `aws-node` cluster-wide rather than touching just this one policy.
