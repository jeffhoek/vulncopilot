# Public Access Setup

How to run this app with per-user GitHub OAuth login instead of a single shared
password, with a per-user daily query cap. This covers **PR 1** (OAuth +
allow-list) and **PR 2** (rate limiting) of the
[public access plan](../plans/public-access-plan.md). The `/admin` dashboard
(PR 3) is documented here as it lands.

## GitHub OAuth App Setup

1. Go to **github.com/settings/developers → OAuth Apps → New OAuth App**.
2. **Application name:** anything (e.g. "Vuln RAG Chatbot").
3. **Homepage URL:** `https://your-domain`
4. **Authorization callback URL:** `https://your-domain/auth/oauth/github/callback`
   — this path is fixed by Chainlit; it is not configurable. (Older Chainlit docs
   reference `/login/callback`; the installed version uses
   `/auth/oauth/{provider}/callback`. Verify against your version with the route
   list if in doubt.)
5. Click **Register application**, then **Generate a new client secret**.
6. Copy the **Client ID** and **Client Secret** into your environment:
   - `OAUTH_GITHUB_CLIENT_ID`
   - `OAUTH_GITHUB_CLIENT_SECRET`

Chainlit auto-discovers the provider from the `OAUTH_GITHUB_*` env vars and renders
a "Continue with GitHub" button. No provider config in code is required.

For local development, set the Homepage/callback URLs to `http://localhost:8000`
and `http://localhost:8000/auth/oauth/github/callback`. GitHub allows
`http://localhost` for OAuth apps; production must use HTTPS.

## Environment Variable Reference

| Variable | Default | Description |
|---|---|---|
| `OAUTH_GITHUB_CLIENT_ID` | — | GitHub OAuth App client ID. Required to enable login. |
| `OAUTH_GITHUB_CLIENT_SECRET` | — | GitHub OAuth App client secret. |
| `CHAINLIT_AUTH_SECRET` | — | Signs OAuth session cookies. **Required.** Generate with `uv run chainlit create-secret`. |
| `OPEN_REGISTRATION` | `false` | `true` lets any GitHub account in. Use only for smoke-testing. |
| `ALLOWED_EMAILS` | `[]` | JSON array of exact email addresses allowed. |
| `ALLOWED_EMAIL_DOMAINS` | `[]` | JSON array of email domains (no `@`), e.g. `["mycompany.com"]`. |
| `ALLOWED_LOGINS` | `[]` | JSON array of GitHub usernames, e.g. `["jeffhoek"]`. |
| `DAILY_QUERY_LIMIT` | `20` | Max queries per user per UTC day. |
| `ADMIN_DAILY_QUERY_LIMIT` | `100000` | Elevated cap for identifiers in `ADMIN_USER_IDENTIFIERS`. |
| `ADMIN_USER_IDENTIFIERS` | `[]` | JSON array of GitHub identifiers, e.g. `["github:12345678"]`, that get the elevated cap. |

> **List values are JSON arrays, not comma-separated.** A bare
> `ALLOWED_LOGINS=jeff,alice` raises a `SettingsError` on startup. Use
> `ALLOWED_LOGINS=["jeff","alice"]`, matching the existing `ACTION_BUTTONS` field.

`APP_USERNAME` and `APP_PASSWORD` are no longer used — remove them from your
environment.

## Authorization Strategies

The `oauth_callback` in `app.py` admits a user if **any** of these match (checked
in order):

1. `OPEN_REGISTRATION=true` — everyone is allowed. Lowest friction, no
   allow-list. Intended only for smoke-testing the OAuth redirect flow.
2. `ALLOWED_EMAILS` — the GitHub account's primary email is in the list.
3. `ALLOWED_EMAIL_DOMAINS` — the email ends with `@<domain>` for a listed domain.
   Good for "anyone at my company".
4. `ALLOWED_LOGINS` — the GitHub username is in the list. Useful when you don't
   know a collaborator's email but know their handle.

You can combine these — e.g. allow a whole domain plus a couple of external
collaborators by login. If none match, the login is denied and the attempt is
logged at WARNING level (`OAuth denied: ...`).

> GitHub only exposes an email here if the user has a **public** primary email.
> If you rely on `ALLOWED_EMAILS` / `ALLOWED_EMAIL_DOMAINS`, confirm your users'
> emails are public, or fall back to `ALLOWED_LOGINS`.

### Why the identifier is the numeric GitHub ID

Every allowed branch sets `default_user.identifier = f"github:{raw_user_data['id']}"`
— the stable numeric GitHub ID, **not** the login. GitHub usernames are mutable;
a rename would otherwise orphan a user's history and reset their usage counter.
The login is used only for allow-list matching. Use
`cl.user_session.get("user").identifier` consistently everywhere a user is keyed
— including the rate-limit lookup and usage insert.

> **GitHub only for PR 1.** The callback reads `raw_user_data['id']` and
> `raw_user_data['login']`, which are GitHub-shaped. Google's payload differs and
> has no `login`, so wiring up Google would require branching the identifier and
> allow-list logic on `provider_id`. Keep PR 1 GitHub-only.

## Rate Limiting

Each query is capped per user per UTC day via `DAILY_QUERY_LIMIT` (default 20).
Usage is tracked in the `user_usage` table (one row per user per day), keyed by the
stable numeric GitHub identifier.

**Elevated cap for admins.** Identifiers listed in `ADMIN_USER_IDENTIFIERS` get
`ADMIN_DAILY_QUERY_LIMIT` (default 100000) instead of `DAILY_QUERY_LIMIT`, so the
standard cap applies to everyone else while trusted users run effectively
unthrottled. Look up your identifier in the `user_usage` table after a query
(`SELECT user_identifier FROM user_usage;`) and set, e.g.,
`ADMIN_USER_IDENTIFIERS=["github:12345678"]`. The effective limit is resolved per
request by `_limit_for()` in `app.py` and used by both the pre-check and the atomic
record, so admins are never blocked at either phase.

Both the `on_message` and quick-query handlers use a two-phase pattern (factored
into `enforce_daily_limit()` / `record_usage()` in `app.py`):

1. **Phase 1 — pre-check.** A cheap read-only `SELECT` of today's count. If the
   user is already at the limit, the request is rejected *before* any LLM call.
2. **Phase 2 — atomic record.** After the agent runs, a single
   `INSERT ... ON CONFLICT DO UPDATE` increments the count and adds the run's
   input/output token totals (`RunResult.usage()`), returning the new count. This
   upsert is the authoritative gate; the pre-check is a best-effort optimisation.

**TOCTOU trade-off:** concurrent requests from the same user can both pass Phase 1
before either increments, so a user at the boundary may get one extra LLM call per
inflight request. For a single-tab UI this is rarely more than one. The atomic
upsert still records every call accurately, so cost accounting stays correct even
when a request slips past the boundary.

### Rate Limit Guidance

Pick `DAILY_QUERY_LIMIT` from your tolerable LLM spend. At roughly $0.01 per query,
20 queries/day × 100 users ≈ $20/day worst case. Start conservative and raise it
once real usage and `user_usage` token totals show headroom. Token prices for cost
estimation are configured separately (PR 3, `LLM_INPUT_COST_PER_MILLION` /
`LLM_OUTPUT_COST_PER_MILLION`).

## Rollout Sequence

0. **Generate and set `CHAINLIT_AUTH_SECRET`** (`uv run chainlit create-secret`)
   **before** deploying OAuth. Without it, Chainlit OAuth sessions silently fail
   to persist — the most common "works locally, breaks on deploy" surprise.
1. Deploy with `OPEN_REGISTRATION=true` to smoke-test the OAuth redirect flow end
   to end (button → GitHub → back into chat).
2. Verify login works with your own GitHub account.
3. Lock down: set `ALLOWED_LOGINS` (and/or `ALLOWED_EMAILS` /
   `ALLOWED_EMAIL_DOMAINS`) and set `OPEN_REGISTRATION=false`.
4. Confirm a non-allowed account is denied, then remove `APP_USERNAME` /
   `APP_PASSWORD` from the environment.
5. Apply the `user_usage` schema (additive, zero-downtime). It is created
   automatically on next startup when `DB_INIT_SCHEMA=true`; on a read-only app
   role apply it once with the admin/ETL connection (see below).
6. Set a real `DAILY_QUERY_LIMIT`.

> The `user_usage` table is additive — applying it never blocks existing traffic.
> Now that rate limiting has landed you can safely widen the allow-list beyond a
> single login; until you do, keeping `ALLOWED_LOGINS=["jeffhoek"]` is the
> zero-risk default.

## Testing Checklist

- [ ] Unauthenticated visit → redirected to the OAuth login page.
- [ ] Login with an allowed account → lands in chat.
- [ ] Login with a disallowed account → denied (when an allow-list is configured).
- [ ] `OPEN_REGISTRATION=true` → any GitHub account lands in chat.
- [ ] Send queries up to the limit → the next query returns the limit message.
- [ ] `SELECT * FROM user_usage;` in psql shows a row after queries.
- [ ] `query_count` and the token columns are non-zero.

Automated coverage: the allow-list logic in `tests/unit/test_oauth_callback.py`
and the atomic counter in `tests/integration/test_usage_db.py` (the latter needs
`TEST_DATABASE_URL` pointed at a pgvector database).
