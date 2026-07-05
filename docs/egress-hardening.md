# Egress NetworkPolicy — Before/After Pen Test

A record of adding a default-deny **egress** `NetworkPolicy` to `chainlit-rag` on EKS, complementing the ingress-side policy in [network-hardening.md](network-hardening.md). Same methodology: confirm the weakness, apply the change, confirm it's closed and the app still works. Kept here so the exercise doesn't have to be redone from scratch to answer "why do we have this egress `NetworkPolicy`, and why is it a list of raw IPs?"

## Goal

With the pod free to reach any address on the internet, demonstrate that concretely, then apply a default-deny egress policy scoped to only the destinations the app actually needs — Supabase, Anthropic, OpenAI, GitHub OAuth, Logfire, and cluster DNS — and verify both that everything else is closed and the app still works end-to-end through its real public path.

## Why this matters here

[network-hardening.md](network-hardening.md) closed off *inbound* paths to the pod. Egress is the other half: today, if the app (or a dependency, or an attacker who lands a shell in the container) wanted to exfiltrate data or reach a C2 server, nothing at the network layer would stop it — only the pod's own code path decides where it sends traffic. A default-deny egress policy makes "the pod can only talk to the handful of services it's declared to need" a property enforced by the platform, not an assumption about the code.

## Before: what the pod's egress allowed

No egress `NetworkPolicy` existed in the `rag` namespace — only the pre-existing ingress one from [network-hardening.md](network-hardening.md) (`policyTypes: ["Ingress"]`, no `Egress` entry). From inside the live `chainlit-rag` pod (no `curl`/`wget` in the hardened Debian-slim image, so `python3` stands in):

```bash
POD=$(kubectl get pod -n rag -l app=chainlit-rag -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n rag "$POD" -- python3 -c "
import urllib.request
r = urllib.request.urlopen('https://example.com', timeout=5)
print('HTTP', r.status)
"
# HTTP 200 — an address with zero relationship to this app, reachable anyway

kubectl exec -n rag "$POD" -- python3 -c "
import socket
s = socket.create_connection(('1.1.1.1', 443), timeout=5)
print('CONNECTED', s.getpeername())
"
# CONNECTED ('1.1.1.1', 443) — raw TCP to an arbitrary IP, no DNS/allow-list involved at all
```

**Summary of exposure:** the pod could reach any host, any port, anywhere on the internet. Nothing at the network layer distinguished "Anthropic API call" from "exfiltrate to an attacker-controlled IP."

## What the app actually needs to reach

Reverse-engineered from the live pod rather than assumed, since the ConfigMap in this repo had drifted from what's actually deployed (`LOGFIRE_ENABLED=true` is live in the cluster's `rag-config` but not present in the copy of [k8s/configmap.yaml](../k8s/configmap.yaml) checked into this branch — worth reconciling separately):

| Destination | Why | Host | Resolved |
|---|---|---|---|
| Supabase Postgres pooler | `PG_DATABASE_URL` (read-only role, [supabase-readonly-role.md](supabase-readonly-role.md)) | `aws-1-us-west-2.pooler.supabase.com:5432` | `44.225.139.66`, `44.252.246.120` |
| Anthropic API | `LLM_MODEL=anthropic:*`, `logfire.instrument_pydantic_ai()` | `api.anthropic.com:443` | `160.79.104.10` |
| OpenAI API | embeddings, `logfire.instrument_openai()` | `api.openai.com:443` | `162.159.140.245`, `172.66.0.243` (Cloudflare-fronted) |
| GitHub OAuth | `app.py` `@cl.oauth_callback` — server-side token exchange + user lookup, not just the browser redirect | `github.com:443`, `api.github.com:443` | published ranges, see below |
| Logfire ingest | `LOGFIRE_ENABLED=true` (live), [observability.md](observability.md) | `logfire-us.pydantic.dev:443` | `104.26.8.129`, `104.26.9.129`, `172.67.69.88` (Cloudflare-fronted) |
| Cluster DNS | required to resolve every host above | CoreDNS `kube-dns.kube-system.svc:53` | — |

## Changes made

| File | Change |
|---|---|
| [k8s/networkpolicy-egress.yaml](../k8s/networkpolicy-egress.yaml) | Default-deny egress (`podSelector: {}`, `policyTypes: ["Egress"]`) with explicit allows: CoreDNS, Supabase, Anthropic, OpenAI, GitHub, Logfire. |

### The ipBlock-vs-FQDN gotcha

Kubernetes `NetworkPolicy` (and the `vpc-cni`/`aws-eks-nodeagent` enforcement backing it — see the addon note in [network-hardening.md](network-hardening.md)) only matches on `ipBlock` CIDRs, ports, and pod/namespace selectors. It has no concept of a hostname. That's a real gap for the SaaS entries above:

- **GitHub** publishes stable CIDR ranges at `https://api.github.com/meta` (`web`/`api` keys) — used here (`140.82.112.0/20`, `192.30.252.0/22`, `143.55.64.0/20`), so this entry is solid.
- **Anthropic** and **Supabase** currently resolve to fixed-looking, provider-owned IPs — used directly, but neither publishes a documented CIDR list, so there's no contractual guarantee they won't change.
- **OpenAI** and **Logfire** are both fronted by Cloudflare's anycast network — the resolved IPs used here are a snapshot, and are the most likely of anything in this file to rotate without notice, silently breaking outbound calls until the `ipBlock`s are re-resolved and reapplied.

This is the practical version of "wouldn't there need to be exceptions for Supabase/model providers/Logfire" — yes, and every one of those exceptions is a hand-maintained IP list with a shelf life. The durable fix is a DNS-aware egress mechanism (e.g. Cilium `toFQDNs`, or an explicit egress proxy/gateway) that allows by hostname and re-resolves automatically; that's a bigger lift than this cluster's current `vpc-cni` network-policy mode supports, so it's called out here as a known limitation rather than solved.

## After: verification

`urlopen()`'s and `create_connection()`'s default timeout is `None` (blocking) unless passed explicitly — without `timeout=5`, a dropped connection reads as a multi-minute hang (the kernel's TCP SYN retry loop, ~130s on Linux) rather than a fast, obvious failure. Every call below sets it explicitly for that reason.

```bash
# Blocked: the same arbitrary host and IP from the "before" section
kubectl exec -n rag "$POD" -- python3 -c "
import urllib.request
urllib.request.urlopen('https://example.com', timeout=5)
"
# urllib.error.URLError: <urlopen error [Errno 101] Network is unreachable>

kubectl exec -n rag "$POD" -- python3 -c "
import socket
socket.create_connection(('1.1.1.1', 443), timeout=5)
"
# TimeoutError: timed out

# Allowed: every declared destination, confirmed by direct TCP connect
kubectl exec -n rag "$POD" -- python3 -c "
import socket
socket.create_connection(('160.79.104.10', 443), timeout=5)   # Anthropic
socket.create_connection(('162.159.140.245', 443), timeout=5) # OpenAI
socket.create_connection((socket.gethostbyname('github.com'), 443), timeout=5)
socket.create_connection((socket.gethostbyname('api.github.com'), 443), timeout=5)
socket.create_connection((socket.gethostbyname('logfire-us.pydantic.dev'), 443), timeout=5)
socket.create_connection((socket.gethostbyname('aws-1-us-west-2.pooler.supabase.com'), 5432), timeout=5)
print('all connected')
"
# all connected
```

Enforcement confirmed via the same `PolicyEndpoint` mechanism as the ingress policy (no separate feature to enable — one `vpc-cni` addon setting covers both directions):

```bash
kubectl get policyendpoints -n rag
# chainlit-rag-default-deny-ingress-bxtf7   30h
# chainlit-rag-restrict-egress-n7mlb        <1m
```

Functional check on the real path: `curl https://rag.manheok.com/healthz` → `200`, pod restart count unchanged (`0`) since applying, and no connection-error log lines in the minutes following the change.

## Rollback

- **Policy only**: `kubectl delete -f k8s/networkpolicy-egress.yaml` — immediately reopens all egress; the ingress policy is untouched since it's a separate object.
- If a SaaS provider's IPs rotate and break the app: check `kubectl logs -n rag deploy/chainlit-rag` for connection errors, re-resolve the affected host, update the corresponding `ipBlock`(s) in [k8s/networkpolicy-egress.yaml](../k8s/networkpolicy-egress.yaml), and `kubectl apply`. There's no automation for this yet — see the ipBlock-vs-FQDN gotcha above.
