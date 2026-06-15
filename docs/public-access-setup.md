# Public Access Setup

How to run this app with per-user GitHub OAuth login instead of a single shared
password. This is **PR 1** of the [public access plan](../plans/public-access-plan.md):
OAuth + allow-list only. Rate limiting (PR 2) and the `/admin` dashboard (PR 3)
are documented here as they land.

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
a rename would otherwise orphan a user's history and (in PR 2) reset their usage
counter. The login is used only for allow-list matching. Use
`cl.user_session.get("user").identifier` consistently everywhere a user is keyed.

> **GitHub only for PR 1.** The callback reads `raw_user_data['id']` and
> `raw_user_data['login']`, which are GitHub-shaped. Google's payload differs and
> has no `login`, so wiring up Google would require branching the identifier and
> allow-list logic on `provider_id`. Keep PR 1 GitHub-only.

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

> Until PR 2 lands (rate limiting), keep the allow-list locked to your own login
> (`ALLOWED_LOGINS=["jeffhoek"]`) so there's no public-abuse window.

## Testing Checklist

- [ ] Unauthenticated visit → redirected to the OAuth login page.
- [ ] Login with an allowed account → lands in chat.
- [ ] Login with a disallowed account → denied (when an allow-list is configured).
- [ ] `OPEN_REGISTRATION=true` → any GitHub account lands in chat.

Automated coverage for the allow-list logic lives in
`tests/unit/test_oauth_callback.py` (`uv run pytest tests/unit/test_oauth_callback.py`).
