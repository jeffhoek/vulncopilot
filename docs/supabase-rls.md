# Row-Level Security on Supabase

Closing the `rls_disabled_in_public` finding Supabase raises against this project — what it
actually means here, the fix, and how to apply it to production without locking the app out of
its own database.

Related: [supabase-readonly-role.md](supabase-readonly-role.md) (the `app_readonly` / `app_etl`
roles this interacts with), [risk-scoring.md](risk-scoring.md) (the `v_cve_risk` materialized view).

---

## The finding

> **Table publicly accessible.** Anyone with your project URL can read, edit, and delete all data
> in this table because Row-Level Security is not enabled.

Supabase serves every table in the `public` schema over its PostgREST **Data API** at
`https://<project-ref>.supabase.co/rest/v1/<table>`. Requests on that path authenticate as the
built-in `anon` or `authenticated` roles, and Supabase's own default privileges grant those roles
`ALL` on tables created in `public`. Row-Level Security is the only remaining gate. With it off,
the grant is the whole story.

Seven tables were affected: the six this repo creates — `kev_vulnerabilities`,
`nvd_vulnerabilities`, `epss_scores`, `cwe_definitions`, `etl_runs`, `user_usage` — plus
`cve_references`, which exists in production but in no migration here (it appears only as a
proposal in [future-enhancements.md](../plans/future-enhancements.md)). Someone created it by
hand. That table is the reason `RLS_SQL` discovers its targets from `pg_catalog` instead of
working from a list: the linter reports what is *in* the schema, not what this repo put there.

Two further findings share the same root cause. `v_cve_risk` was selectable by `anon`
(`materialized_view_in_api`), and `refresh_v_cve_risk()` was **executable** by `anon` and
`authenticated` via `/rest/v1/rpc/refresh_v_cve_risk` — a `SECURITY DEFINER` function, so an
unauthenticated caller could drive an admin-privileged rebuild of the whole view at will.

### How exposed was it, really

Worth being precise, because the alert's wording overstates one part and understates another.

**Overstated:** the project URL alone is not enough. A caller also needs the anon/publishable key.
This app never uses PostgREST — it connects with `asyncpg` over the pooler using the dedicated
roles in [supabase-readonly-role.md](supabase-readonly-role.md) — so that key is not embedded in a
shipped client bundle the way it would be in a `supabase-js` app, and it appears in no committed
file. That is a meaningful difference from the typical instance of this finding.

**Understated:** the exposure is read *and write*. The datasets themselves are public information
(CISA KEV, NVD, EPSS, MITRE CWE are all freely published), so disclosure is close to a non-event —
but an attacker who can `DELETE` or `UPDATE` can silently poison the corpus this tool answers
from. A vulnerability assistant that under-reports a KEV entry because someone edited the row is a
worse outcome than the data leaking. And `user_usage` is not public data: it holds OAuth
identifiers (`github:<user>`) alongside per-day query and token counts.

The anon key is designed to be publishable and is one leaked config file away from being public.
Treating it as a secret is not a security control.

---

## The fix

Three layers, applied together by `RLS_SQL` in [rag/database.py](../rag/database.py) and the tail
of `view_ddl()` in [rag/risk.py](../rag/risk.py).

### 1. RLS, with policies for the app roles

`ALTER TABLE ... ENABLE ROW LEVEL SECURITY` on every table in `public` that the applying role
owns, discovered from `pg_class` (extension-owned tables are skipped — they are not ours to
alter, and `ALTER TABLE` requires ownership). **This is the step that can take
the app down**, and it is worth understanding before running it: RLS applies to every role except
the table owner. `app_readonly` and `app_etl` are not owners — `postgres` is — so the instant RLS
is on and no policy matches, the live app's queries start returning **zero rows** and the ETL's
upserts start failing. Nothing errors at connection time; reads just quietly go empty.

`BYPASSRLS` would sidestep it but is not available — Supabase does not grant it on the `postgres`
role. Policies are the only route:

```sql
CREATE POLICY app_roles_rw ON <table> FOR ALL TO app_readonly, app_etl
  USING (true) WITH CHECK (true);
```

`USING (true)` looks like it gives everything away; it does not. **A policy filters rows, it never
confers a privilege.** What `app_readonly` may do is still bounded entirely by its `GRANT`s —
`SELECT` on the vulnerability tables, `SELECT/INSERT/UPDATE` on `user_usage`, no `DELETE`
anywhere. The policy restores exactly the access that already existed and nothing more, while
`anon` — which now holds no grant and matches no policy — is stopped twice over.

### 2. Revoke the Data API roles

Defence in depth behind the policies. With no grant at all, a Data API request never reaches the
RLS check:

```sql
REVOKE ALL ON <table> FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;
```

That last statement is the one that keeps this fixed. Supabase installs
`ALTER DEFAULT PRIVILEGES ... GRANT ALL ON TABLES TO anon, authenticated`, so without
countermanding it, the next table added to `SCHEMA_SQL` arrives publicly readable the moment it is
created — and the alert comes back. Default privileges are per-creating-role, so this must run as
the admin role that owns the schema.

### 3. `v_cve_risk`, which RLS cannot protect

PostgreSQL does not support RLS on materialized views. The protection every base table gets is
simply unavailable for `v_cve_risk` — which, being a denormalized join across all four datasets,
is the single most useful object on the API surface. Revoking the grant is the only lever, and
Supabase lints this separately as `materialized_view_in_api`.

The revoke lives inside `view_ddl()` rather than beside the other RLS statements for a specific
reason: `view_ddl()` drops and recreates the view, which discards every privilege on it. A revoke
applied anywhere else would be silently undone the next time the score arithmetic changed.

### 4. Turn the Data API off — do this first

Nothing in this project uses PostgREST. Both clients reach Postgres directly as `app_readonly`:
the Python app over `asyncpg`, and [`vulncopilot-js`](https://github.com/jeffhoek/vulncopilot-js)
over node-postgres. Neither depends on `@supabase/supabase-js`. So the whole API surface can
simply be switched off: **Integrations → Data API → Overview → Enable Data API**, off, Save.

Done on 2026-09-13, and on its own it cleared every one of the findings above —
`rls_disabled_in_public` ×7, `materialized_view_in_api`, and both
`*_security_definer_function_executable`. Only `extension_in_public` (an unrelated WARN about
`vector`, `pg_buffercache` and `postgres_fdw` living in `public`) survives.

It removes the attack surface rather than filtering it, takes one click, and is instantly
reversible. It does not touch direct Postgres connections, the SQL Editor, or the Table Editor —
neither app noticed.

The three layers above stay worth applying anyway, and the reason is precisely that this one is a
toggle: it can be flipped back on by anyone with dashboard access, by a future feature that wants
PostgREST, or by a Supabase default changing. RLS and the revokes are what hold when it is.

---

## Applying it

### Local / dev / CI

Nothing to do. `RLS_SQL` is part of `SCHEMA_SQL`, so `init_db()` applies it under the existing
`db_init_schema` gate.

None of `anon`, `authenticated`, `app_readonly`, or `app_etl` exist outside Supabase, and neither
`REVOKE` nor `CREATE POLICY` supports `IF EXISTS` — so every role reference in `RLS_SQL` is
guarded on a `pg_roles` lookup. Off Supabase the statements are skipped rather than aborting the
file. RLS still gets switched on locally, which is harmless: the dev connection owns the tables
and owners are exempt.

### Production

Production runs `DB_INIT_SCHEMA=false` (read-only app role, no DDL), so **none of this arrives
just because it is in the code path.** Apply it with the admin role, the same way `view_ddl()` is
applied:

```bash
uv run python -c "from rag.database import RLS_SQL; print(RLS_SQL)" > /tmp/rls.sql
```

```bash
psql "<admin-pooled-supabase-dsn>" -f /tmp/rls.sql
```

Every statement is idempotent and safe to replay: `ENABLE ROW LEVEL SECURITY` is a no-op when
already on, and each policy is dropped before being recreated (`CREATE POLICY` has neither
`OR REPLACE` nor `IF NOT EXISTS`).

**Order matters if you are also reapplying `view_ddl()`.** The view's `DROP`/`CREATE` discards its
grants, so re-issue them *after*, not before — otherwise the app loses `v_cve_risk` while
appearing to have been granted it:

```sql
GRANT SELECT ON v_cve_risk TO app_readonly, app_etl;
GRANT EXECUTE ON FUNCTION refresh_v_cve_risk() TO app_etl;
```

---

## Verify

> **The advisors cannot verify this work.** Those lints only evaluate schemas exposed to
> PostgREST, so once the Data API is off (step 4) they report clean whether or not a single
> statement below has ever run. During the actual rollout the advisors went green from the toggle
> alone, while the database still had zero policies and `anon` could read every table. Only the
> catalog queries here tell you what is true.

```sql
-- 1. RLS on every table in the schema. Expect every row t — including any table
--    created by hand that no migration here knows about.
SELECT relname, relrowsecurity
FROM pg_class
WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p')
ORDER BY relname;
```

```sql
-- 2. One policy per table, scoped to both app roles.
SELECT tablename, policyname, roles, cmd
FROM pg_policies WHERE schemaname = 'public'
ORDER BY tablename;
```

```sql
-- 3. The Data API roles can read nothing in `public`. Expect zero rows.
--
-- Deliberately NOT information_schema.role_table_grants: that view omits
-- materialized views entirely, so it reports v_cve_risk as clean while `anon` can
-- still select it — a false pass on the one object RLS cannot protect. Asking
-- pg_class via has_table_privilege covers tables, views and matviews alike, and
-- resolves privileges inherited through role membership rather than only direct
-- grants.
SELECT c.relname AS object,
       CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'table'
                      WHEN 'v' THEN 'view'  WHEN 'm' THEN 'matview' END AS kind,
       api_role AS readable_by
FROM pg_class c
CROSS JOIN unnest(ARRAY['anon', 'authenticated']) AS api_role
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND has_table_privilege(api_role, c.oid, 'SELECT')
ORDER BY 1, 3;
```

```sql
-- 4. No function of ours in `public` is callable by the Data API roles. Expect zero
--    rows. refresh_v_cve_risk() is SECURITY DEFINER, so an EXECUTE grant there is an
--    unauthenticated caller running admin-privileged work.
--
-- The extension filter is not optional. PostgreSQL grants EXECUTE to PUBLIC on every
-- function by default, so without it pgvector alone returns ~200 rows (l2_distance,
-- vector_add, …) and buries the one result that matters. Those are harmless and are
-- not what the advisor flags; `deptype = 'e'` excludes anything an extension owns,
-- exactly as the table loop in RLS_SQL does.
SELECT p.proname AS function, p.prosecdef AS security_definer, api_role AS callable_by
FROM pg_proc p
CROSS JOIN unnest(ARRAY['anon', 'authenticated']) AS api_role
WHERE p.pronamespace = 'public'::regnamespace
  AND has_function_privilege(api_role, p.oid, 'EXECUTE')
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend d
      WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
  )
ORDER BY 1, 3;
```

Then re-run the linter in the dashboard: **Advisors → Security Advisor**.
`rls_disabled_in_public`, `materialized_view_in_api` and both
`*_security_definer_function_executable` findings should all be gone.

The end-to-end check that matters more than any of the above is that the app still works, because
the failure mode of this change is silent. Run a query and confirm it returns results **and**
records a `user_usage` row:

```bash
uv run chainlit run app.py
```

```sql
SELECT * FROM user_usage WHERE query_date = CURRENT_DATE ORDER BY id DESC LIMIT 5;
```

If reads come back empty, the policies did not apply — check that `app_readonly` existed at the
time `RLS_SQL` ran, since the guard skips policy creation entirely when the role is absent. That
is the one way this can leave the app locked out: applying the file to a database where the app
roles have not been created yet.

Then run the ETL and confirm it still writes ([data-loading.md](data-loading.md)) — `app_etl` goes
through the same policies.

---

## Coverage

`RLS_SQL` loops over what it finds in `pg_catalog`, so coverage does not depend on a list being
kept up to date — which is the failure `cve_references` demonstrated. `RLS_TABLES` in
[rag/database.py](../rag/database.py) survives as the set of tables this repo owns:
[tests/unit/test_database_rls.py](../tests/unit/test_database_rls.py) asserts it matches the
`CREATE TABLE` statements in the same file, so the docs and the integration test cannot quietly
go stale, and asserts no table name is baked into the SQL.

[tests/integration/test_rls_db.py](../tests/integration/test_rls_db.py) covers the rest against a
real database, including a check that *no* table in `public` has RLS off — which is now the
assertion that actually matters, since it catches tables created outside `SCHEMA_SQL`.

---

## Who granted it, and to whom

Two revoke mistakes cost time during the rollout, in opposite directions. Both are read straight
off the ACL, so check it before writing a `REVOKE`:

```sql
SELECT relname, relacl FROM pg_class WHERE relnamespace = 'public'::regnamespace;
SELECT proname, proacl FROM pg_proc  WHERE pronamespace = 'public'::regnamespace;
```

**An entry with an empty grantee is `PUBLIC`.** `{=X/postgres, postgres=X/postgres}` means every
role can execute it, `anon` included, without `anon` appearing anywhere. Revoking from `anon` and
`authenticated` succeeds, changes nothing, and leaves the function callable — which is what
happened with `create_playground`. PostgreSQL grants `EXECUTE` to `PUBLIC` on every function by
default, so a function needs `REVOKE ... FROM PUBLIC`.

**The name after the slash is the grantor, and only the grantor can revoke.**
`anon=arwdDxtm/supabase_admin` on `pg_buffercache` means `supabase_admin` issued that grant.
Running `REVOKE` as `postgres` is a silent no-op — no error, no effect — and Supabase does not
give you `supabase_admin`. Extension objects in `public` land this way; the only levers are
dropping or relocating the extension.

The inverse of the first case is why `view_ddl()` revokes from both: `REVOKE ALL ON FUNCTION
refresh_v_cve_risk() FROM PUBLIC` does not remove the *explicit* `anon` and `authenticated` grants
that Supabase's `ALTER DEFAULT PRIVILEGES` attaches at creation. Revoking `PUBLIC` alone leaves
them; revoking the roles alone leaves `PUBLIC`. The function needs both, and after this work its
ACL should read `{postgres=X/postgres, service_role=X/postgres, app_etl=X/postgres}`.
