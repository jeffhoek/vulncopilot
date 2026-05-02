# Configurable Chainlit Action Buttons

## Overview

The RAG chatbot supports configurable action buttons on the welcome message. These provide users with quick-access "suggested question" shortcuts — useful for domain-specific deployments where common queries are known in advance.

**Behavior:** Buttons appear on the "Ready!" welcome message. Clicking a button sends that button's label text as a RAG query to the agent (same as typing and submitting the message).

## Configuration

Set `ACTION_BUTTONS` to a JSON array of label strings. Any number of buttons is supported. Unset or empty means no buttons render.

### `.env` / environment variables

```env
ACTION_BUTTONS=["Latest KEV additions","Anthropic Claude","Top AI vulns in 2026","CVE-2026-25253 include URLs","OpenClaw include URLs","Which weakness types appear most in KEV?"]
```

### Kubernetes ConfigMap (`k8s/configmap.yaml`)

```yaml
data:
  ACTION_BUTTONS: '["Latest KEV additions","Anthropic Claude","Top AI vulns in 2026","CVE-2026-25253 include URLs","OpenClaw include URLs","Which weakness types appear most in KEV?"]'
```

Note: quotes around the value are required in YAML because JSON brackets would otherwise be misinterpreted.

### Designing good buttons

Good buttons exercise the full range of what the agent can do. Aim for a mix that covers:

| Pattern | Example button | Why |
|---|---|---|
| SQL → KEV | `"Latest KEV additions"` | Date-sorted query against `kev_vulnerabilities` |
| SQL → NVD | `"Top AI vulns in 2026"` | CVSS + date filter against `nvd_vulnerabilities` |
| SQL → CWE join | `"Which weakness types appear most in KEV?"` | Joins `kev_vulnerabilities` → `cwe_definitions` |
| SQL → specific CVE with URLs | `"CVE-2026-25253 include URLs"` | Fetches `reference_urls` from `nvd_vulnerabilities` |
| Semantic search | `"Anthropic Claude"` | Triggers the `retrieve` tool via embedding similarity |
| Semantic + SQL | `"OpenClaw include URLs"` | Semantic match resolves the CVE, then SQL fetches details |

Buttons that include "include URLs" explicitly signal to the agent to select `reference_urls` from NVD, which it may otherwise omit.

## Implementation Details

- pydantic-settings v2 parses `list[str]` from a JSON array string automatically — no custom validator needed
- All buttons share the action name `"quick_query"` — one `@cl.action_callback` handles all of them
- The button label text IS the query (`payload={"query": label}`), keeping label and behavior in sync
- If `ACTION_BUTTONS` is unset, `actions=[]` is passed and no buttons render (backward compatible)

## Verification

1. Set `ACTION_BUTTONS='["What is this about?","Give me a summary"]'` in `.env`, run `uv run chainlit run app.py`
2. Confirm both buttons appear on the "Ready!" message and after each response
3. Remove `ACTION_BUTTONS` — confirm no buttons render and no errors
4. Test with a single-item list and a 6+ item list to confirm no artificial limit
