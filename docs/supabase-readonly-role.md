# Supabase Read-Only Role Setup

Create a dedicated `app_readonly` PostgreSQL role with SELECT-only access for the live web application, keeping ETL operations under the admin role.

| Role | Purpose | Privileges |
|---|---|---|
| `postgres` (admin) | ETL scripts (`load_kev.py`, `load_nvd.py`) | Full access |
| `app_readonly` (new) | Live app at runtime | SELECT on `kev_vulnerabilities`, `nvd_vulnerabilities` only |

## Step 1 — Open the Supabase SQL Editor

In the Supabase dashboard: **SQL Editor → New query**

## Step 2 — Create the role

```sql
CREATE ROLE app_readonly WITH LOGIN PASSWORD 'replace-with-strong-password';
```

> Use a strong, unique password. Store it in a secrets manager (e.g., Azure Key Vault, AWS Secrets Manager, GCP Secret Manager) — not in a `.env` file committed to git.

## Step 3 — Grant database connect

```sql
GRANT CONNECT ON DATABASE postgres TO app_readonly;
```

## Step 4 — Grant schema usage

```sql
GRANT USAGE ON SCHEMA public TO app_readonly;
```

## Step 5 — Grant SELECT on the two tables

```sql
GRANT SELECT ON kev_vulnerabilities TO app_readonly;
GRANT SELECT ON nvd_vulnerabilities TO app_readonly;
```

Only these two tables are granted — no wildcard `ALL TABLES`. Any new table added later requires an explicit grant before `app_readonly` can read it.

## Step 6 — Row Level Security note

If RLS is ever enabled on either table, a SELECT grant alone is not enough — Supabase will return zero rows without a matching policy. Add a policy for `app_readonly`:

```sql
-- Only needed if RLS is enabled on the table
CREATE POLICY "app_readonly_select" ON kev_vulnerabilities
  FOR SELECT TO app_readonly USING (true);

CREATE POLICY "app_readonly_select" ON nvd_vulnerabilities
  FOR SELECT TO app_readonly USING (true);
```

## Step 7 — Build the connection string

Use the Supabase **transaction pooler** (port 6543) for app runtime.

> **Important:** When connecting through Supabase's pooler (Supavisor), the username must include the project reference as a suffix — `username.project-ref` — otherwise you will get a `Tenant or user not found` or `XX000` error. This applies to all non-default roles, not just `app_readonly`.

```
postgresql://app_readonly.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
```

Find the full connection string (with the correct project ref already embedded) under **Project Settings → Database → Connection string**, then substitute `postgres` with `app_readonly` in the username portion.

## Step 8 — Update `.env`

```dotenv
# App runtime — read-only role (use transaction pooler, port 6543)
# Note: username must include the project ref suffix for the pooler
PG_DATABASE_URL=postgresql://app_readonly.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
```

Keep the admin connection string in a separate file (e.g., `.env.etl`) or a CI/CD secret — never use `app_readonly` for ETL, as INSERT/UPDATE will fail with a permission error.

## Step 9 — Verify

Run these checks in the Supabase SQL Editor:

```sql
-- Confirm grants are applied
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_name IN ('kev_vulnerabilities', 'nvd_vulnerabilities')
  AND grantee = 'app_readonly'
ORDER BY table_name;

-- Confirm the role cannot write (expect: ERROR: permission denied)
SET ROLE app_readonly;
INSERT INTO kev_vulnerabilities (cve_id) VALUES ('TEST-WRITE-CHECK');
RESET ROLE;
```

Then smoke-test the app with the updated `.env`:

```bash
uv run chainlit run app.py
```

Run a query like "What vulnerabilities affect Apache Log4j?" to confirm reads still work end-to-end.

## Revoking access

To revoke the role entirely if credentials are compromised:

```sql
REVOKE ALL ON kev_vulnerabilities FROM app_readonly;
REVOKE ALL ON nvd_vulnerabilities FROM app_readonly;
REVOKE CONNECT ON DATABASE postgres FROM app_readonly;
DROP ROLE app_readonly;
```

Then rotate credentials and recreate the role from Step 2.
