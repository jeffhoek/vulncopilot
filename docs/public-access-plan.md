# Public Access Plan: OAuth, Rate Limiting & Admin Dashboard

## Overview

Replace single shared username/password auth with per-user GitHub OAuth, add per-user daily query caps tracked in Postgres, and expose a `/admin` dashboard showing usage and estimated LLM costs. No payment flows needed.

---

## Step 1 — Database Schema

**File:** `rag/database.py`

Add `user_usage` table to `SCHEMA_SQL`:

```sql
CREATE TABLE IF NOT EXISTS user_usage (
    id              SERIAL PRIMARY KEY,
    user_identifier TEXT     NOT NULL,
    query_date      DATE     NOT NULL DEFAULT CURRENT_DATE,
    query_count     INTEGER  NOT NULL DEFAULT 0,
    input_tokens    INTEGER  NOT NULL DEFAULT 0,
    output_tokens   INTEGER  NOT NULL DEFAULT 0,
    UNIQUE (user_identifier, query_date)
);
CREATE INDEX IF NOT EXISTS user_usage_date_idx ON user_usage (query_date DESC);
CREATE INDEX IF NOT EXISTS user_usage_user_idx ON user_usage (user_identifier);
```

The `UNIQUE` constraint enables atomic upserts via `INSERT ... ON CONFLICT DO UPDATE`.

Applied automatically on next app startup. Can also be applied manually:
```bash
psql -h localhost -U postgresuser -d mydb -c "CREATE TABLE IF NOT EXISTS user_usage ..."
```

---

## Step 2 — `rag/usage.py` (new file)

Four async functions, all accept `pool` (existing asyncpg pool from `rag.database.get_pool()`):

| Function | Purpose |
|---|---|
| `get_daily_count(pool, user_id)` | SELECT today's query_count for a user |
| `check_rate_limit(pool, user_id, limit)` | Returns `True` if under limit |
| `increment_usage(pool, user_id, input_tokens, output_tokens)` | Upsert: increment count + tokens |
| `get_usage_stats(pool)` | Aggregate per-user: today / 7-day / 30-day + token totals + estimated cost |

Token cost constants (module-level, driven by config):
- Input: `$0.80/M` tokens (Claude Haiku 4.5)
- Output: `$4.00/M` tokens (Claude Haiku 4.5)

`increment_usage` SQL pattern:
```sql
INSERT INTO user_usage (user_identifier, query_date, query_count, input_tokens, output_tokens)
VALUES ($1, CURRENT_DATE, 1, $2, $3)
ON CONFLICT (user_identifier, query_date) DO UPDATE SET
    query_count   = user_usage.query_count + 1,
    input_tokens  = user_usage.input_tokens + EXCLUDED.input_tokens,
    output_tokens = user_usage.output_tokens + EXCLUDED.output_tokens
```

---

## Step 3 — `config.py` additions

Add to the `Settings` class:

```python
# OAuth
oauth_github_client_id: str | None = None
oauth_github_client_secret: str | None = None
# oauth_google_client_id / oauth_google_client_secret (optional alternative)

# Authorization
allowed_email_domains: list[str] = []   # e.g. ["mycompany.com"]
allowed_emails: list[str] = []          # explicit emails or GitHub usernames
open_registration: bool = False         # True = any OAuth user allowed

# Rate Limiting
daily_query_limit: int = 20

# Admin Dashboard
admin_emails: list[str] = []
chainlit_auth_secret: str = ""          # same value as CHAINLIT_AUTH_SECRET

# Token Cost Estimation (USD per million tokens)
llm_input_cost_per_million: float = 0.80
llm_output_cost_per_million: float = 4.00
```

---

## Step 4 — OAuth login

**File:** `app.py`

**Remove** the entire `@cl.password_auth_callback` block.

**Add** `@cl.oauth_callback`:

```python
@cl.oauth_callback
def oauth_callback(provider_id, token, raw_user_data, default_user):
    email = raw_user_data.get("email", "")
    login = raw_user_data.get("login", "")  # GitHub username (fallback if email is private)

    if settings.open_registration:
        return default_user
    if email and email in settings.allowed_emails:
        return default_user
    if email and any(email.endswith(f"@{d}") for d in settings.allowed_email_domains):
        return default_user
    if login and login in settings.allowed_emails:
        return default_user
    return None  # deny
```

Chainlit auto-discovers the provider from `OAUTH_GITHUB_*` env vars. Both GitHub and Google can coexist — Chainlit shows a button for each present provider.

**Testing tip:** Set `OPEN_REGISTRATION=true` first to confirm the OAuth redirect flow works before locking down the allow-list.

---

## Step 5 — Rate limiting

**File:** `app.py`

In `on_message` and the `@cl.action_callback("quick_query")` handler:

```python
user_id = cl.user_session.get("user").identifier
pool = get_pool()

if not await check_rate_limit(pool, user_id, settings.daily_query_limit):
    await cl.Message(
        content=f"You've reached your daily limit of {settings.daily_query_limit} queries. Try again tomorrow."
    ).send()
    return

result = await rag_agent.run(...)

usage = result.usage()
await increment_usage(pool, user_id, usage.request_tokens or 0, usage.response_tokens or 0)
```

`result.usage()` is Pydantic-AI's `RunResult.usage()` — returns `Usage(request_tokens, response_tokens)`. Guard with `or 0` in case the model call doesn't return token counts.

---

## Step 6 — Admin dashboard

**New files:** `admin/__init__.py` (empty), `admin/dashboard.py`

`admin/dashboard.py` is a single FastAPI route handler:

1. Read the `access_token` cookie that Chainlit sets after login (signed HS256 JWT)
2. Decode with `python-jose`: `jwt.decode(token, settings.chainlit_auth_secret, algorithms=["HS256"])` — extract `sub` (user identifier)
3. If `sub not in settings.admin_emails` → return 403
4. Call `get_usage_stats(pool)` and render inline HTML table

**Wire into `app.py`** at module level (after middleware setup):
```python
from chainlit.server import app as fastapi_app
from admin.dashboard import admin_dashboard
fastapi_app.add_api_route("/admin", admin_dashboard, methods=["GET"])
```

`/admin` doesn't conflict with Chainlit's internal routes. `McpRouterMiddleware` only intercepts `/mcp*` paths.

**Dependency check:** `python-jose[cryptography]` is likely already a transitive dep of Chainlit. Verify with `uv pip show python-jose`; if missing, add it explicitly.

Dashboard HTML columns: User | Queries Today | Last 7 Days | Last 30 Days | Total Input Tokens | Total Output Tokens | Est. Cost (USD)

---

## Step 7 — `.env.example` update

**Remove:**
```
APP_USERNAME
APP_PASSWORD
```

**Add:**
```bash
# OAuth — GitHub
OAUTH_GITHUB_CLIENT_ID=
OAUTH_GITHUB_CLIENT_SECRET=
# OAuth — Google (optional, can coexist with GitHub)
# OAUTH_GOOGLE_CLIENT_ID=
# OAUTH_GOOGLE_CLIENT_SECRET=

# Authorization
OPEN_REGISTRATION=false
# ALLOWED_EMAILS=alice@example.com,bob@example.com
# ALLOWED_EMAIL_DOMAINS=mycompany.com

# Rate Limiting
DAILY_QUERY_LIMIT=20

# Admin Dashboard
ADMIN_EMAILS=your@email.com
```

---

## Step 8 — Documentation, setup guide & rollout

**New file:** `docs/public-access-setup.md`

Cover:

### GitHub OAuth App Setup
- Go to github.com/settings/developers → OAuth Apps → New OAuth App
- Homepage URL: `https://your-domain`
- Authorization callback URL: `https://your-domain/login/callback` (Chainlit's fixed path)
- Copy Client ID and Client Secret into env

### Environment Variable Reference
Full table of all new env vars, defaults, and valid values.

### Authorization Strategies
Explain the three modes: `OPEN_REGISTRATION`, `ALLOWED_EMAILS`, `ALLOWED_EMAIL_DOMAINS`, and how they combine.

### Rate Limit Guidance
Recommended starting values based on expected LLM cost per query. E.g. at ~$0.01/query, 20 queries/day × 100 users = ~$20/day.

### Rollout Sequence
1. Apply DB schema change (zero-downtime — additive only)
2. Deploy with `OPEN_REGISTRATION=true` and `DAILY_QUERY_LIMIT=9999` to smoke-test OAuth
3. Confirm `/admin` dashboard is accessible
4. Lock down `ALLOWED_EMAILS` or `ALLOWED_EMAIL_DOMAINS`
5. Set real `DAILY_QUERY_LIMIT`
6. Remove `APP_USERNAME` / `APP_PASSWORD` from env

### Testing Checklist
- [ ] Unauthenticated visit → redirected to OAuth login page
- [ ] Login with allowed account → lands in chat
- [ ] Login with disallowed account → denied (if allow-list configured)
- [ ] Send queries up to the limit → 21st query returns limit message
- [ ] `/admin` as admin email → usage table renders
- [ ] `/admin` as non-admin → 403
- [ ] `SELECT * FROM user_usage;` in psql shows rows after queries
- [ ] Token counts in DB are non-zero

---

## Implementation Sequence

| # | Step | Files changed |
|---|---|---|
| 1 | DB schema | `rag/database.py` |
| 2 | Usage helpers | `rag/usage.py` (new) |
| 3 | Config fields | `config.py` |
| 4 | OAuth swap | `app.py` |
| 5 | Rate limiting | `app.py` |
| 6 | Admin dashboard | `admin/__init__.py`, `admin/dashboard.py` (new), `app.py` |
| 7 | Env example | `.env.example` |
| 8 | Documentation | `docs/public-access-setup.md` (new) |
