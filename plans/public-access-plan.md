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
-- user_identifier-only index omitted: the UNIQUE (user_identifier, query_date) constraint
-- already creates a B-tree on both columns with user_identifier as the leading key.
-- PostgreSQL can use that composite index for single-column lookups on user_identifier,
-- so a second index on user_identifier alone adds write overhead without improving reads.
```

The `UNIQUE` constraint enables atomic upserts via `INSERT ... ON CONFLICT DO UPDATE`.

**`user_identifier` format:** always store the stable numeric GitHub ID (e.g. `"github:12345678"`), not the login. GitHub usernames are mutable — a rename orphans all prior usage rows and resets the counter. Use `f"github:{raw_user_data['id']}"` in `oauth_callback` (Step 4). Chainlit sets `cl.User.identifier` to the value returned from `oauth_callback`; use `cl.user_session.get("user").identifier` consistently everywhere (rate-limit lookup, usage insert). Document this in Step 8.

Applied automatically on next app startup. Can also be applied manually:
```bash
psql -h localhost -U postgresuser -d mydb -c "CREATE TABLE IF NOT EXISTS user_usage ..."
```

---

## Step 2 — `rag/usage.py` (new file)

Three async functions, all accept `pool` (existing asyncpg pool from `rag.database.get_pool()`):

| Function | Purpose |
|---|---|
| `check_and_increment(pool, user_id, limit, input_tokens, output_tokens)` | Atomic check + increment; returns `(allowed: bool, new_count: int)` |
| `get_usage_stats(pool, input_cost_per_million, output_cost_per_million)` | Aggregate per-user: today / 7-day / 30-day + token totals + estimated cost |

**No module-level cost constants.** Token prices are read from `Settings` by the caller and passed as arguments so there is one source of truth.

`check_and_increment` collapses the former `check_rate_limit` + `increment_usage` into a single atomic SQL statement to eliminate the check-then-act race condition where two concurrent requests could both pass the limit check before either increments the counter:

```sql
WITH upserted AS (
    INSERT INTO user_usage (user_identifier, query_date, query_count, input_tokens, output_tokens)
    VALUES ($1, CURRENT_DATE, 1, $2, $3)
    ON CONFLICT (user_identifier, query_date) DO UPDATE SET
        query_count   = user_usage.query_count + 1,          -- always increment for audit trail
        input_tokens  = CASE WHEN user_usage.query_count < $4
                             THEN user_usage.input_tokens + EXCLUDED.input_tokens
                             ELSE user_usage.input_tokens END,
        output_tokens = CASE WHEN user_usage.query_count < $4
                             THEN user_usage.output_tokens + EXCLUDED.output_tokens
                             ELSE user_usage.output_tokens END
    RETURNING query_count, $4 AS lim
)
SELECT query_count <= lim AS allowed, query_count FROM upserted
```

`$4` is the limit. `query_count` is always incremented so over-limit calls are visible in the DB for abuse auditing; cap the display value in the dashboard template. The token CASE guards prevent accumulating tokens for blocked requests. On the 21st call `query_count` returns 21 and `21 <= 20 = false` correctly blocks it.

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
allowed_emails: list[str] = []          # explicit email addresses only
allowed_logins: list[str] = []          # GitHub usernames (login field)
open_registration: bool = False         # True = any OAuth user allowed

# Rate Limiting
daily_query_limit: int = 20

# Admin Dashboard
admin_secret: str = ""                  # HTTP Basic Auth password for /admin; set a strong random value
# admin_logins removed — dashboard is protected by admin_secret alone (bearer token),
# not per-user login checks. If per-user admin ACLs are needed in future, add them then.

# Token Cost Estimation (USD per million tokens)
llm_input_cost_per_million: float = 0.80
llm_output_cost_per_million: float = 4.00
```

Add a module-level startup guard — an empty `admin_secret` would let `Authorization: Basic <base64 of ":">` pass, silently leaving the dashboard open. A module-level check fails the process immediately during `chainlit run app.py`, before any user can reach the app (unlike `@cl.on_chat_start`, which fires per session and would leave the dashboard exposed until the first user connects):

```python
# In app.py, at module level (outside any handler):
if not settings.admin_secret:
    raise ValueError("ADMIN_SECRET must be set to a non-empty value before starting the app")
```

---

## Step 4 — OAuth login

**File:** `app.py`

**Remove** the entire `@cl.password_auth_callback` block.

**Add** `@cl.oauth_callback`:

```python
logger = logging.getLogger(__name__)

@cl.oauth_callback
def oauth_callback(provider_id, token, raw_user_data, default_user):
    email = raw_user_data.get("email", "")
    login = raw_user_data.get("login", "")  # GitHub username (mutable — for allow-list matching only)
    user_id = f"github:{raw_user_data['id']}"  # stable numeric ID — never changes on rename

    if settings.open_registration:
        default_user.identifier = user_id
        return default_user
    if email and email in settings.allowed_emails:
        default_user.identifier = user_id
        return default_user
    if email and any(email.endswith(f"@{d}") for d in settings.allowed_email_domains):
        default_user.identifier = user_id
        return default_user
    if login and login in settings.allowed_logins:
        default_user.identifier = user_id
        return default_user

    logger.warning("OAuth denied: provider=%s login=%s email=%s", provider_id, login, email)
    return None  # deny
```

`allowed_emails` is now strictly for email addresses; `allowed_logins` is for GitHub usernames. Using separate fields avoids ambiguity when the same string (e.g. `"alice"`) could be either.

Chainlit auto-discovers the provider from `OAUTH_GITHUB_*` env vars. Both GitHub and Google can coexist — Chainlit shows a button for each present provider.

**Testing tip:** Set `OPEN_REGISTRATION=true` first to confirm the OAuth redirect flow works before locking down the allow-list.

---

## Step 5 — Rate limiting

**File:** `app.py`

In `on_message` and the `@cl.action_callback("quick_query")` handler, use a two-phase pattern:

```python
user_id = cl.user_session.get("user").identifier
pool = get_pool()

# Phase 1 — cheap read-only pre-check: avoid spending an LLM call on an already-blocked user.
row = await pool.fetchrow(
    "SELECT query_count FROM user_usage WHERE user_identifier = $1 AND query_date = CURRENT_DATE",
    user_id,
)
if row and row["query_count"] >= settings.daily_query_limit:
    await cl.Message(
        content=f"You've reached your daily limit of {settings.daily_query_limit} queries. Try again tomorrow."
    ).send()
    return

# Phase 2 — run agent, then atomically record usage.
result = await rag_agent.run(...)

usage = result.usage()
allowed, new_count = await check_and_increment(
    pool, user_id, settings.daily_query_limit,
    usage.request_tokens or 0, usage.response_tokens or 0,
)

if not allowed:
    logger.warning("Rate limit hit: user=%s count=%d limit=%d", user_id, new_count, settings.daily_query_limit)
    await cl.Message(
        content=f"You've reached your daily limit of {settings.daily_query_limit} queries. Try again tomorrow."
    ).send()
    return
```

`result.usage()` is Pydantic-AI's `RunResult.usage()` — returns `Usage(request_tokens, response_tokens)`. Guard with `or 0` in case the model call doesn't return token counts.

**TOCTOU trade-off:** there is a window between the pre-check (Phase 1) and the atomic upsert (Phase 2) where concurrent requests from the same user could both pass the pre-check and both run the LLM. The worst case is one extra LLM call **per concurrent inflight request** at the limit boundary — if N requests arrive when a user is at `count = limit - 1`, all N pass Phase 1 before any has incremented the counter. For a typical single-browser-tab UI this is rarely more than one or two. This is far better than the previous design where *every* blocked user spent a full LLM call before being denied. The `check_and_increment` atomic upsert in Phase 2 remains the authoritative gate; the pre-check is a best-effort optimisation only.

---

## Step 6 — Admin dashboard

**New files:** `admin/__init__.py` (empty), `admin/dashboard.py`, `admin/templates/dashboard.html`

`admin/dashboard.py` uses HTTP Basic Auth instead of a bearer token. Bearer tokens require the caller to set the `Authorization` header manually — browsers don't do this on normal page navigation, so opening `/admin` in a browser would always 403. HTTP Basic Auth causes the browser to show a native credential prompt, making the dashboard accessible without any frontend JavaScript.

```python
from fastapi import Request, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
import secrets

security = HTTPBasic()
templates = Jinja2Templates(directory="admin/templates")  # auto-escapes by default

async def admin_dashboard(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    # Use secrets.compare_digest to prevent timing attacks.
    # Username can be anything; only the password (admin_secret) is checked.
    ok = secrets.compare_digest(
        credentials.password.encode(), settings.admin_secret.encode()
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Forbidden",
            headers={"WWW-Authenticate": "Basic realm=\"Admin\""},
        )

    pool = get_pool()
    rows = await get_usage_stats(
        pool,
        settings.llm_input_cost_per_million,
        settings.llm_output_cost_per_million,
    )
    return templates.TemplateResponse("dashboard.html", {"request": request, "rows": rows})
```

`admin/templates/dashboard.html` is a Jinja2 template — use `{{ value | e }}` escaping for all user-derived data. This eliminates the XSS risk of building HTML via f-strings and makes the template maintainable.

**Auth pattern:** navigate to `/admin` in any browser — the browser prompts for a username and password. Leave the username blank (or anything); set the password to `ADMIN_SECRET`. `curl` usage: `curl -u :$ADMIN_SECRET https://your-domain/admin`. Set `ADMIN_SECRET` to a strong random value (e.g. `openssl rand -hex 32`). This avoids `python-jose` and any coupling to Chainlit internals.

**HTTPS is required.** HTTP Basic Auth encodes credentials as base64 plaintext — without TLS the `ADMIN_SECRET` is trivially readable on the wire. On Azure App Service, enforce HTTPS-only in the TLS/SSL settings. On GCP Cloud Run, HTTPS is the default; block HTTP via ingress settings. Never expose `/admin` over plain HTTP.

**Wire into `app.py`** at module level (after middleware setup):
```python
from chainlit.server import app as fastapi_app
from admin.dashboard import admin_dashboard
fastapi_app.add_api_route("/admin", admin_dashboard, methods=["GET"])
```

`/admin` doesn't conflict with Chainlit's internal routes. `McpRouterMiddleware` only intercepts `/mcp*` paths.

**Dependency check:** `jinja2` is already a transitive dep of FastAPI/Chainlit — no new packages needed.

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
CHAINLIT_AUTH_SECRET=           # required for OAuth sessions — generate with: openssl rand -hex 32
# OAuth — Google (optional, can coexist with GitHub)
# OAUTH_GOOGLE_CLIENT_ID=
# OAUTH_GOOGLE_CLIENT_SECRET=

# Authorization
OPEN_REGISTRATION=false
# ALLOWED_EMAILS=alice@example.com,bob@example.com   (email addresses only)
# ALLOWED_LOGINS=jeffhoek,alice                      (GitHub usernames only)
# ALLOWED_EMAIL_DOMAINS=mycompany.com

# Rate Limiting
DAILY_QUERY_LIMIT=20

# Admin Dashboard — HTTP Basic Auth password for /admin
# ADMIN_SECRET=                  (strong random value, e.g. openssl rand -hex 32)
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
- [ ] Navigate to `/admin` in browser → Basic Auth credential prompt appears
- [ ] Enter correct `ADMIN_SECRET` as password → usage table renders
- [ ] Enter wrong password → 401
- [ ] `SELECT * FROM user_usage;` in psql shows rows after queries
- [ ] Token counts in DB are non-zero

---

## Implementation Sequence

| # | Step | Files changed |
|---|---|---|
| 1 | DB schema | `rag/database.py` |
| 2 | Usage helpers | `rag/usage.py` (new) — `check_and_increment`, `get_usage_stats` |
| 3 | Config fields | `config.py` |
| 4 | OAuth swap | `app.py` |
| 5 | Rate limiting | `app.py` |
| 6 | Admin dashboard | `admin/__init__.py`, `admin/dashboard.py`, `admin/templates/dashboard.html` (all new), `app.py` |
| 7 | Env example | `.env.example` |
| 8 | Documentation | `docs/public-access-setup.md` (new) |
