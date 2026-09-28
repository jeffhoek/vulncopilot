"""Unit tests for the Row-Level Security block in rag/database.py.

No database. The behaviour of the generated SQL — policies created, `anon` revoked,
`app_readonly`/`app_etl` still able to work — is a property of PostgreSQL and is
exercised by tests/integration/test_rls_db.py. What is checked here is the shape of
the SQL, plus that RLS_TABLES stays honest about what _TABLES_SQL creates.

RLS_SQL protects whatever tables it finds in `public`, rather than a list baked in
here. That is deliberate: the linter fires on what is actually in the schema, not on
what this file created, and production had acquired a `cve_references` table present
in no migration — which a hardcoded list silently left exposed (docs/supabase-rls.md).
"""

import re

from rag.database import _TABLES_SQL, RLS_SQL, RLS_TABLES, SCHEMA_SQL


def created_tables() -> set[str]:
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", _TABLES_SQL))


def test_rls_tables_matches_what_the_schema_creates():
    # RLS_SQL no longer reads RLS_TABLES, so this is no longer load-bearing for
    # protection — but a stale list would make the docs and the integration test lie
    # about which tables this project owns.
    assert created_tables() == set(RLS_TABLES)


def test_rls_sql_discovers_tables_from_the_catalog_not_a_literal():
    # The regression that motivated the change: no table name may be baked into the
    # SQL, or a hand-created table goes unprotected again.
    assert "FROM pg_class c" in RLS_SQL
    assert "c.relnamespace = 'public'::regnamespace" in RLS_SQL
    for name in RLS_TABLES:
        assert name not in RLS_SQL


def test_rls_sql_only_touches_tables_it_can_alter():
    # ALTER TABLE requires ownership, so a table owned by another role would abort the
    # block with "must be owner"; extension-owned tables are not ours to modify.
    assert "pg_get_userbyid(c.relowner) = current_user" in RLS_SQL
    assert "d.deptype = 'e'" in RLS_SQL


def test_rls_sql_tolerates_a_schema_with_no_tables():
    # array_agg returns NULL rather than an empty array, and FOREACH over NULL raises.
    assert "COALESCE(protected, '{}'::text[])" in RLS_SQL


def test_rls_sql_enables_row_security_and_creates_a_policy():
    assert "ENABLE ROW LEVEL SECURITY" in RLS_SQL
    assert "CREATE POLICY app_roles_rw" in RLS_SQL


def test_rls_sql_drops_the_policy_before_creating_it():
    # CREATE POLICY has no OR REPLACE, so without the DROP the second application of
    # SCHEMA_SQL — which happens on every startup with DB_INIT_SCHEMA=true — aborts.
    assert RLS_SQL.index("DROP POLICY IF EXISTS app_roles_rw") < RLS_SQL.index("CREATE POLICY app_roles_rw")


def test_rls_sql_guards_every_role_reference_on_role_existence():
    # None of these roles exist on a local dev or CI database; an unguarded statement
    # would abort the whole file with "role does not exist".
    assert RLS_SQL.count("FROM pg_roles WHERE rolname IN") == 2
    assert "IF app_roles IS NOT NULL THEN" in RLS_SQL
    assert "IF api_roles IS NOT NULL THEN" in RLS_SQL


def test_rls_sql_revokes_the_data_api_roles():
    assert "REVOKE ALL ON %I FROM %s" in RLS_SQL
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public " in RLS_SQL


def test_schema_sql_applies_rls_after_the_tables_exist():
    # ALTER TABLE ... ENABLE ROW LEVEL SECURITY cannot run before CREATE TABLE.
    assert SCHEMA_SQL == _TABLES_SQL + RLS_SQL


def test_rls_sql_revokes_default_privileges_on_functions():
    # Supabase's ALTER DEFAULT PRIVILEGES covers FUNCTIONS as well as TABLES and
    # SEQUENCES. Missing it means the next function created in `public` arrives with
    # EXECUTE already granted to anon — and refresh_v_cve_risk() is SECURITY DEFINER,
    # so that is an unauthenticated caller running admin-privileged work.
    for obj in ("TABLES", "SEQUENCES", "FUNCTIONS"):
        assert f"REVOKE ALL ON {obj} FROM %s" in RLS_SQL
