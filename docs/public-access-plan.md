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

**`user_identifier` format:** always store the GitHub login (e.g. `"jeffhoek"`), not the email. Chainlit sets `cl.User.identifier` to the value returned from `oauth_callback`; our callback sets it to `raw_user_data["login"]` (Step 4). `admin_emails` in config must therefore contain GitHub logins, not email addresses — document this clearly in Step 8.

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
-- Returns the new query_count after the upsert, or NULL if already at/over limit
WITH upserted AS (
    INSERT INTO user_usage (user_identifier, query_date, query_count, input_tokens, output_tokens)
    VALUES ($1, CURRENT_DATE, 1, $2, $3)
    ON CONFLICT (user_identifier, query_date) DO UPDATE SET
        query_count   = CASE WHEN user_usage.query_count < $4
                             THEN user_usage.query_count + 1
                             ELSE user_usage.query_count END,
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

`$4` is the limit. If `allowed` is `False`, the row was not incremented (the CASE guards ensure idempotency at the boundary).

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
# Must contain GitHub login values (matching user_identifier stored in DB)
admin_logins: list[str] = []
admin_secret: str = ""                  # bearer token for /admin; set a strong random value

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
logger = logging.getLogger(__name__)

@cl.oauth_callback
def oauth_callback(provider_id, token, raw_user_data, default_user):
    email = raw_user_data.get("email", "")
    login = raw_user_data.get("login", "")  # GitHub username

    if settings.open_registration:
        return default_user
    if email and email in settings.allowed_emails:
        return default_user
    if email and any(email.endswith(f"@{d}") for d in settings.allowed_email_domains):
        return default_user
    if login and login in settings.allowed_logins:
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

In `on_message` and the `@cl.action_callback("quick_query")` handler:

```python
user_id = cl.user_session.get("user").identifier
pool = get_pool()

# Run the agent first so we know the real token counts before persisting.
# check_and_increment is atomic — no TOCTOU race between check and write.
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

**Note:** the agent runs before the limit check so that token counts are available for the atomic upsert. The practical effect is that a user can run exactly `daily_query_limit` queries per day — the (limit+1)th attempt succeeds at the model level but the response is suppressed and the row is not incremented.

---

## Step 6 — Admin dashboard

**New files:** `admin/__init__.py` (empty), `admin/dashboard.py`, `admin/templates/dashboard.html`

`admin/dashboard.py` uses a simple bearer-token approach instead of decoding Chainlit's JWT (which ties the implementation to Chainlit's internal token format and requires keeping `CHAINLIT_AUTH_SECRET` in sync):

```python
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="admin/templates")  # auto-escapes by default

async def admin_dashboard(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != settings.admin_secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    pool = get_pool()
    rows = await get_usage_stats(
        pool,
        settings.llm_input_cost_per_million,
        settings.llm_output_cost_per_million,
    )
    return templates.TemplateResponse("dashboard.html", {"request": request, "rows": rows})
```

`admin/templates/dashboard.html` is a Jinja2 template — use `{{ value | e }}` escaping for all user-derived data. This eliminates the XSS risk of building HTML via f-strings and makes the template maintainable.

**Auth pattern:** the caller (browser, curl, monitoring script) passes `Authorization: Bearer <admin_secret>`. Set `ADMIN_SECRET` to a strong random value (e.g. `openssl rand -hex 32`). This avoids `python-jose` and any coupling to Chainlit internals.

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

# Admin Dashboard
# ADMIN_LOGINS=jeffhoek          (GitHub logins; must match user_identifier in DB)
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
- [ ] `/admin` as admin email → usage table renders
- [ ] `/admin` as non-admin → 403
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
