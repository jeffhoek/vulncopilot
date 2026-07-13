"""Regenerate k8s/networkpolicy-egress.yaml with current IPs for the SaaS
egress allow-list (see the "ipBlock-vs-FQDN gotcha" in docs/egress-hardening.md).

Kubernetes NetworkPolicy only matches ipBlock/port, never a hostname, so the
allow-list for Supabase/Anthropic/OpenAI/Logfire is a snapshot of whatever
those hostnames resolved to when it was written. This script re-resolves them
(and re-pulls GitHub's published CIDR ranges) and rewrites the policy file so
that snapshot can be refreshed on demand instead of hand-edited.

It only ever rewrites the file on disk — it never touches the cluster. Review
the diff, then apply and commit like any other manifest change:

    uv run python scripts/refresh_egress_ips.py
    git diff k8s/networkpolicy-egress.yaml
    kubectl apply -f k8s/networkpolicy-egress.yaml

Exits 1 if the file changed (so this can also run as a scheduled CI check
that fails loudly when a provider's IPs have drifted), 0 if already current.
"""

import ipaddress
import socket
import sys
from pathlib import Path

import httpx

POLICY_PATH = Path(__file__).resolve().parent.parent / "k8s" / "networkpolicy-egress.yaml"

# (hostname, port) for the entries that have no published IP range and are
# just resolved live. Anycast/CDN-fronted ones (OpenAI, Logfire) are the most
# likely to drift — see docs/egress-hardening.md.
DNS_RESOLVED = {
    "supabase": ("aws-1-us-west-2.pooler.supabase.com", 5432),
    "anthropic": ("api.anthropic.com", 443),
    "openai": ("api.openai.com", 443),
    "logfire": ("logfire-us.pydantic.dev", 443),
}

GITHUB_META_URL = "https://api.github.com/meta"


def resolve_ipv4(host: str, port: int) -> list[str]:
    """Current IPv4 addresses for host:port as /32 CIDRs, sorted for a stable diff."""
    addrs = {info[4][0] for info in socket.getaddrinfo(host, port, family=socket.AF_INET)}
    return [f"{addr}/32" for addr in sorted(addrs, key=ipaddress.IPv4Address)]


def github_cidrs() -> list[str]:
    """GitHub's published web+api IPv4 ranges (api.github.com/meta), excluding
    the /32 entries for individual Actions-runner hosts that meta also lists —
    those aren't relevant to the OAuth token-exchange/user-lookup calls this
    policy exists for."""
    meta = httpx.get(GITHUB_META_URL, timeout=10).raise_for_status().json()
    cidrs: set[str] = set()
    for key in ("web", "api"):
        for cidr in meta.get(key, []):
            net = ipaddress.ip_network(cidr, strict=False)
            if net.version == 4 and net.prefixlen < 32:
                cidrs.add(str(net))
    return sorted(cidrs, key=lambda c: ipaddress.IPv4Network(c))


def render(ips: dict[str, list[str]], github: list[str]) -> str:
    def blocks(cidrs: list[str]) -> str:
        return "\n".join(f"        - ipBlock:\n            cidr: {cidr}" for cidr in cidrs)

    return f"""apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: vulncopilot-restrict-egress
  namespace: rag
spec:
  podSelector: {{}}
  policyTypes: ["Egress"]
  egress:
    # Cluster DNS — required for every other rule below to resolve anything.
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    # Supabase Postgres pooler (PG_DATABASE_URL). AWS-owned IPs behind the
    # pooler, resolved at write time — see docs/egress-hardening.md for the
    # re-resolution caveat if Supabase ever rotates them.
    - to:
{blocks(ips["supabase"])}
      ports:
        - protocol: TCP
          port: 5432
    # Anthropic API (LLM_MODEL=anthropic:*, logfire.instrument_pydantic_ai).
    - to:
{blocks(ips["anthropic"])}
      ports:
        - protocol: TCP
          port: 443
    # OpenAI API (embeddings; logfire.instrument_openai). Fronted by
    # Cloudflare — anycast, most likely to rotate of anything in this file.
    - to:
{blocks(ips["openai"])}
      ports:
        - protocol: TCP
          port: 443
    # GitHub OAuth token exchange + user lookup (app.py oauth_callback).
    # Official published ranges (https://api.github.com/meta "web"/"api"),
    # not a resolved IP — far more stable than the SaaS entries above.
    - to:
{blocks(github)}
      ports:
        - protocol: TCP
          port: 443
    # Logfire ingest (LOGFIRE_ENABLED=true in the live ConfigMap). Also
    # Cloudflare-fronted — same rotation caveat as OpenAI.
    - to:
{blocks(ips["logfire"])}
      ports:
        - protocol: TCP
          port: 443
"""


def main() -> int:
    ips = {name: resolve_ipv4(host, port) for name, (host, port) in DNS_RESOLVED.items()}
    github = github_cidrs()
    new_content = render(ips, github)

    old_content = POLICY_PATH.read_text() if POLICY_PATH.exists() else ""
    if new_content == old_content:
        print(f"{POLICY_PATH} is up to date — no IP changes detected.")
        return 0

    POLICY_PATH.write_text(new_content)
    print(f"{POLICY_PATH} updated with current IPs. Review the diff, then:")
    print("  kubectl apply -f k8s/networkpolicy-egress.yaml")
    return 1


if __name__ == "__main__":
    sys.exit(main())
