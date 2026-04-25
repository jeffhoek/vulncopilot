# Supabase Role-Based Access Control

Create dedicated PostgreSQL roles for the live app and ETL processes, keeping the admin role reserved for schema changes only.

| Role | Purpose | Privileges |
|---|---|---|
| `postgres` (admin) | Schema setup and migrations | Full DDL + DML |
| `app_etl` (new) | ETL scripts (`load_kev.py`, `load_nvd.py`) | SELECT, INSERT, UPDATE — no DELETE, no DDL |
| `app_readonly` (new) | Live app at runtime | SELECT only |

No role can do what the other cannot — a compromised ETL credential cannot wipe data, and a compromised app credential cannot modify data at all.

---

## Part 1 — app_readonly role

### Step 1 — Open the Supabase SQL Editor

In the Supabase dashboard: **SQL Editor → New query**

### Step 2 — Create the role

```sql
CREATE ROLE app_readonly WITH LOGIN PASSWORD 'replace-with-strong-password';
```

> Use a strong, unique password. Store it in a secrets manager (e.g., Azure Key Vault, AWS Secrets Manager, GCP Secret Manager) — not in a `.env` file committed to git.

> **Supabase/Supavisor gotcha:** `CREATE ROLE ... PASSWORD` writes to PostgreSQL but Supavisor (the pooler) may not sync the credential immediately. If you get `password authentication failed` on the session pooler despite the password being correct, run `ALTER ROLE app_readonly PASSWORD 'same-password';` in the SQL Editor — this triggers a fresh write that the pooler picks up.

### Step 3 — Grant database connect

```sql
GRANT CONNECT ON DATABASE postgres TO app_readonly;
```

### Step 4 — Grant schema usage

```sql
GRANT USAGE ON SCHEMA public TO app_readonly;
```

### Step 5 — Grant SELECT on the two tables

```sql
GRANT SELECT ON kev_vulnerabilities TO app_readonly;
GRANT SELECT ON nvd_vulnerabilities TO app_readonly;
```

Only these two tables are granted — no wildcard `ALL TABLES`. Any new table added later requires an explicit grant before `app_readonly` can read it. ALTER DEFAULT PRIVILEGES is an optional way to automate this for future tables:

```sql
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO app_readonly;
```

### Step 6 — Row Level Security note

If RLS is ever enabled on either table, a SELECT grant alone is not enough — Supabase will return zero rows without a matching policy. Add a policy for `app_readonly`:

```sql
-- Only needed if RLS is enabled on the table
CREATE POLICY "app_readonly_select" ON kev_vulnerabilities
  FOR SELECT TO app_readonly USING (true);

CREATE POLICY "app_readonly_select" ON nvd_vulnerabilities
  FOR SELECT TO app_readonly USING (true);

CREATE POLICY "app_etl_write" ON kev_vulnerabilities
  FOR ALL TO app_etl USING (true) WITH CHECK (true);

CREATE POLICY "app_etl_write" ON nvd_vulnerabilities
  FOR ALL TO app_etl USING (true) WITH CHECK (true);
```

---

## Part 2 — app_etl role

### Step 7 — Create the ETL role

```sql
CREATE ROLE app_etl WITH LOGIN PASSWORD 'replace-with-strong-password';
```

> **Supabase/Supavisor gotcha:** Same as `app_readonly` — immediately follow with `ALTER ROLE app_etl PASSWORD 'same-password';` to ensure the pooler syncs the credential before you test the connection.

### Step 8 — Grant database connect and schema usage

```sql
GRANT CONNECT ON DATABASE postgres TO app_etl;
GRANT USAGE ON SCHEMA public TO app_etl;
```

### Step 9 — Grant SELECT, INSERT, UPDATE on the two tables

```sql
GRANT SELECT, INSERT, UPDATE ON kev_vulnerabilities TO app_etl;
GRANT SELECT, INSERT, UPDATE ON nvd_vulnerabilities TO app_etl;
```

`DELETE` is intentionally excluded. The ETL scripts use upserts (`INSERT ... ON CONFLICT DO UPDATE`), so DELETE is never needed. A compromised ETL credential cannot wipe vulnerability data.

The `vector` type used for embeddings is provided by the pgvector extension (installed in the `extensions` schema by Supabase), but the type is accessible from `public` without any additional grants to `app_etl`.

### Step 10 — Grant sequence usage

The `id` serial columns require sequence access for INSERTs:

```sql
GRANT USAGE ON SEQUENCE kev_vulnerabilities_id_seq TO app_etl;
GRANT USAGE ON SEQUENCE nvd_vulnerabilities_id_seq TO app_etl;
```

### Step 11 — Schema setup remains admin-only

The ETL role has no DDL privileges. Initial table and index creation (`init_db()` in `rag/database.py`) must still use the admin connection string. Run schema setup once with the admin role before switching ETL scripts to `app_etl`.

---

## Part 3 — Connection strings

> **Important:** When connecting through Supabase's pooler (Supavisor), the username must include the project reference as a suffix — `username.project-ref` — otherwise you will get a `Tenant or user not found` or `XX000` error. This applies to all non-default roles.

Find the base connection string under **Project Settings → Database → Connection string**, then substitute the username.

| Use case | Port | Username format |
|---|---|---|
| Live app (`app_readonly`) | 6543 (pooler) | `app_readonly.<project-ref>` |
| ETL scripts (`app_etl`) | 6543 (pooler) or 5432 (session mode) | `app_etl.<project-ref>` |
| Schema setup / migrations | 5432 (direct) | `postgres.<project-ref>` |

## Part 4 — Update `.env`

```dotenv
# Live app — read-only role (transaction pooler, port 6543)
PG_DATABASE_URL=postgresql://app_readonly.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
```

Keep the ETL and admin connection strings in a separate file (e.g., `.env.etl`) or CI/CD secrets:

```dotenv
# Session mode (port 5432) — avoids prepared-statement limits, recommended for ETL
PG_DATABASE_URL=postgresql://app_etl.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require

# OR transaction pooler (port 6543) — use only if session mode is unavailable
# PG_DATABASE_URL=postgresql://app_etl.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require

# Schema setup only — admin role (direct connection, port 5432)
# PG_DATABASE_URL=postgresql://postgres.<project-ref>:<password>@db.<project-ref>.supabase.co:5432/postgres?sslmode=require
```

---

## Part 5 — Verify

Run these checks in the Supabase SQL Editor:

```sql
-- Confirm grants for both roles
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_name IN ('kev_vulnerabilities', 'nvd_vulnerabilities')
  AND grantee IN ('app_readonly', 'app_etl')
ORDER BY grantee, table_name, privilege_type;

-- Confirm app_readonly cannot write (expect: ERROR: permission denied)
SET ROLE app_readonly;
INSERT INTO kev_vulnerabilities (cve_id) VALUES ('TEST-WRITE-CHECK');
RESET ROLE;

-- Confirm app_etl cannot delete (expect: ERROR: permission denied)
SET ROLE app_etl;
DELETE FROM kev_vulnerabilities WHERE cve_id = 'CVE-0000-00000';
RESET ROLE;

-- Confirm app_etl cannot alter schema (expect: ERROR: permission denied)
SET ROLE app_etl;
ALTER TABLE kev_vulnerabilities ADD COLUMN test_col TEXT;
RESET ROLE;
```

Then smoke-test the app with the updated `.env`:

```bash
uv run chainlit run app.py
```

Run a query like "What vulnerabilities affect Apache Log4j?" to confirm reads work end-to-end.

---

## Revoking access

To revoke a role entirely if credentials are compromised:

```sql
-- Revoke app_readonly
REVOKE ALL ON kev_vulnerabilities FROM app_readonly;
REVOKE ALL ON nvd_vulnerabilities FROM app_readonly;
REVOKE USAGE ON SCHEMA public FROM app_readonly;
REVOKE CONNECT ON DATABASE postgres FROM app_readonly;
DROP ROLE app_readonly;
-- Revoke app_etl
REVOKE ALL ON kev_vulnerabilities FROM app_etl;
REVOKE ALL ON nvd_vulnerabilities FROM app_etl;
REVOKE ALL ON SEQUENCE kev_vulnerabilities_id_seq FROM app_etl;
REVOKE ALL ON SEQUENCE nvd_vulnerabilities_id_seq FROM app_etl;
REVOKE USAGE ON SCHEMA public FROM app_etl;
REVOKE CONNECT ON DATABASE postgres FROM app_etl;
DROP ROLE app_etl;
```

Then rotate credentials and recreate the role from the relevant steps above.
