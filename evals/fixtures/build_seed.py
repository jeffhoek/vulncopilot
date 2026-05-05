"""Generate evals/fixtures/eval_db_seed.sql from a populated dev/prod DB.

Run once (and re-run when adding golden questions whose answers need new data):

    uv run python -m evals.fixtures.build_seed

Reads connection from PG_DATABASE_URL (or the pg_* settings) — same as the app.
The committed seed file is the source of truth for eval runs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

import asyncpg
from pgvector.asyncpg import register_vector

from config import settings


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


OUT = Path(__file__).parent / "eval_db_seed.sql"

# Explicit CVEs the golden dataset references.
SEED_CVE_IDS: list[str] = [
    "CVE-2026-25253",
    "CVE-2021-44228",  # Log4Shell
    "CVE-2021-45046",  # Log4j follow-on
    "CVE-2021-45105",  # Log4j follow-on
    "CVE-2017-5645",  # Log4j 1.x
]

# CWEs the golden dataset references explicitly.
SEED_CWES: list[str] = ["CWE-78"]

# Bulk slice sizes for listing/analytics questions.
LATEST_KEV_LIMIT = 50
TOP_VENDORS_KEV_LIMIT = 50  # covered by the same latest slice; keep one slice


def _sql_str(value: str | None) -> str:
    if value is None:
        return "NULL"
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _sql_date(value: dt.date | None) -> str:
    if value is None:
        return "NULL"
    return f"'{value.isoformat()}'"


def _sql_numeric(value) -> str:
    if value is None:
        return "NULL"
    return str(value)


def _sql_text_array(value: list[str] | None) -> str:
    if value is None:
        return "NULL"
    inner = ",".join(_sql_str(v) for v in value)
    return f"ARRAY[{inner}]::TEXT[]"


def _sql_vector(value) -> str:
    """Serialize a pgvector value to its text representation."""
    if value is None:
        return "NULL"
    # pgvector.asyncpg returns numpy arrays; convert to list of floats.
    floats = list(value)
    inner = ",".join(repr(float(f)) for f in floats)
    return f"'[{inner}]'::vector"


def _sql_jsonb(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        # asyncpg returns JSONB as already-decoded value, but be defensive.
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return f"{_sql_str(value)}::jsonb"
    return f"{_sql_str(json.dumps(value))}::jsonb"


def _emit_kev(rows) -> str:
    if not rows:
        return "-- (no KEV rows)\n"
    lines = [
        "INSERT INTO kev_vulnerabilities ("
        "cve_id, vendor_project, product, vulnerability_name, short_description, "
        "required_action, notes, date_added, due_date, known_ransomware_campaign_use, "
        "cwes, content, embedding) VALUES",
    ]
    values = []
    for r in rows:
        values.append(
            "  ("
            f"{_sql_str(r['cve_id'])}, "
            f"{_sql_str(r['vendor_project'])}, "
            f"{_sql_str(r['product'])}, "
            f"{_sql_str(r['vulnerability_name'])}, "
            f"{_sql_str(r['short_description'])}, "
            f"{_sql_str(r['required_action'])}, "
            f"{_sql_str(r['notes'])}, "
            f"{_sql_date(r['date_added'])}, "
            f"{_sql_date(r['due_date'])}, "
            f"{_sql_str(r['known_ransomware_campaign_use'])}, "
            f"{_sql_text_array(r['cwes'])}, "
            f"{_sql_str(r['content'])}, "
            f"{_sql_vector(r['embedding'])}"
            ")"
        )
    return "\n".join(lines) + "\n" + ",\n".join(values) + "\nON CONFLICT (cve_id) DO NOTHING;\n"


def _emit_nvd(rows) -> str:
    if not rows:
        return "-- (no NVD rows)\n"
    lines = [
        "INSERT INTO nvd_vulnerabilities ("
        "cve_id, description, cvss_v31_score, cvss_v31_severity, cvss_v31_vector, "
        "cvss_v2_score, cvss_v2_severity, cwes, affected_products, reference_urls, "
        "published, last_modified, raw_json, content, embedding) VALUES",
    ]
    values = []
    for r in rows:
        values.append(
            "  ("
            f"{_sql_str(r['cve_id'])}, "
            f"{_sql_str(r['description'])}, "
            f"{_sql_numeric(r['cvss_v31_score'])}, "
            f"{_sql_str(r['cvss_v31_severity'])}, "
            f"{_sql_str(r['cvss_v31_vector'])}, "
            f"{_sql_numeric(r['cvss_v2_score'])}, "
            f"{_sql_str(r['cvss_v2_severity'])}, "
            f"{_sql_text_array(r['cwes'])}, "
            f"{_sql_text_array(r['affected_products'])}, "
            f"{_sql_text_array(r['reference_urls'])}, "
            f"{_sql_date(r['published'])}, "
            f"{_sql_date(r['last_modified'])}, "
            f"{_sql_jsonb(r['raw_json'])}, "
            f"{_sql_str(r['content'])}, "
            f"{_sql_vector(r['embedding'])}"
            ")"
        )
    return "\n".join(lines) + "\n" + ",\n".join(values) + "\nON CONFLICT (cve_id) DO NOTHING;\n"


def _emit_cwe(rows) -> str:
    if not rows:
        return "-- (no CWE rows)\n"
    lines = [
        "INSERT INTO cwe_definitions (cwe_id, name, abstraction, description, url) VALUES",
    ]
    values = []
    for r in rows:
        values.append(
            "  ("
            f"{_sql_str(r['cwe_id'])}, "
            f"{_sql_str(r['name'])}, "
            f"{_sql_str(r['abstraction'])}, "
            f"{_sql_str(r['description'])}, "
            f"{_sql_str(r['url'])}"
            ")"
        )
    return "\n".join(lines) + "\n" + ",\n".join(values) + "\nON CONFLICT (cwe_id) DO NOTHING;\n"


async def _fetch_all(conn: asyncpg.Connection):
    # 1) Specific KEV rows by CVE-ID.
    kev_specific = await conn.fetch(
        "SELECT * FROM kev_vulnerabilities WHERE cve_id = ANY($1::text[])",
        SEED_CVE_IDS,
    )

    # 2) Latest KEV rows for listing/analytics questions.
    kev_latest = await conn.fetch(
        "SELECT * FROM kev_vulnerabilities ORDER BY date_added DESC NULLS LAST LIMIT $1",
        LATEST_KEV_LIMIT,
    )

    # 3) Log4j-related KEV rows by name match.
    kev_log4j = await conn.fetch(
        "SELECT * FROM kev_vulnerabilities "
        "WHERE vulnerability_name ILIKE '%log4j%' OR product ILIKE '%log4j%' OR notes ILIKE '%log4j%'"
    )

    # Dedup on cve_id, preserving column order.
    seen: set[str] = set()
    kev_rows = []
    for r in list(kev_specific) + list(kev_latest) + list(kev_log4j):
        if r["cve_id"] in seen:
            continue
        seen.add(r["cve_id"])
        kev_rows.append(r)
    kev_rows.sort(key=lambda r: r["cve_id"])

    # 4) NVD rows for the same CVE-IDs (specific + log4j + the latest KEV slice).
    target_cves = sorted({r["cve_id"] for r in kev_rows} | set(SEED_CVE_IDS))
    nvd_rows = await conn.fetch(
        "SELECT * FROM nvd_vulnerabilities WHERE cve_id = ANY($1::text[]) ORDER BY cve_id",
        target_cves,
    )

    # 5) CWE rows referenced by any included KEV/NVD row, plus explicit CWE-78.
    cwe_set: set[str] = set(SEED_CWES)
    for r in kev_rows:
        if r["cwes"]:
            cwe_set.update(r["cwes"])
    for r in nvd_rows:
        if r["cwes"]:
            cwe_set.update(r["cwes"])
    cwe_rows = await conn.fetch(
        "SELECT * FROM cwe_definitions WHERE cwe_id = ANY($1::text[]) ORDER BY cwe_id",
        sorted(cwe_set),
    )

    return kev_rows, nvd_rows, cwe_rows


async def main() -> int:
    dsn = os.environ.get("PG_DATABASE_URL") or settings.get_database_dsn()
    if not dsn:
        print("PG_DATABASE_URL is not set; aborting.", file=sys.stderr)
        return 1

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2, init=_init_connection)
    try:
        async with pool.acquire() as conn:
            kev_rows, nvd_rows, cwe_rows = await _fetch_all(conn)
    finally:
        await pool.close()

    header = (
        f"-- generated by evals/fixtures/build_seed.py on "
        f"{dt.datetime.now(dt.UTC).date().isoformat()}; do not edit by hand\n"
        f"-- counts: kev={len(kev_rows)} nvd={len(nvd_rows)} cwe={len(cwe_rows)}\n\n"
    )
    body = "\n".join(
        [
            "-- KEV rows",
            _emit_kev(kev_rows),
            "-- NVD rows",
            _emit_nvd(nvd_rows),
            "-- CWE rows",
            _emit_cwe(cwe_rows),
        ]
    )
    OUT.write_text(header + body)
    print(f"wrote {OUT} (kev={len(kev_rows)} nvd={len(nvd_rows)} cwe={len(cwe_rows)})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
