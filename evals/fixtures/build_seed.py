"""Generate evals/fixtures/eval_db_seed.jsonl from a populated dev/prod DB.

Run once (and re-run when adding golden questions whose answers need new data):

    uv run python -m evals.fixtures.build_seed

Reads connection from PG_DATABASE_URL (or the pg_* settings) — same as the app.
Output is one JSON object per line, tagged with a "table" field. The committed
file is the source of truth for eval runs; conftest.py loads it via asyncpg.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import asyncpg
from pgvector.asyncpg import register_vector

from config import settings

OUT = Path(__file__).parent / "eval_db_seed.jsonl"

# Explicit CVEs the golden dataset references.
SEED_CVE_IDS: list[str] = [
    "CVE-2026-25253",  # OpenClaw — PR 1
    "CVE-2021-44228",  # Log4Shell — PR 1
    "CVE-2021-45046",  # Log4j follow-on — PR 1
    "CVE-2021-45105",  # Log4j follow-on — PR 1
    "CVE-2017-5645",  # Log4j 1.x — PR 1
    "CVE-2017-11882",  # PR 2: Microsoft Office RCE; reference URLs question
    # PR 2: Anthropic Claude Code / MCP vulns
    "CVE-2026-39861",  # Claude Code sandbox symlink escape (10.0 CRITICAL)
    "CVE-2026-25723",  # Claude Code piped-sed file-write bypass
    "CVE-2025-34072",  # Anthropic Slack MCP data exfiltration
    # PR 2: AI / LLM ecosystem
    "CVE-2026-25592",  # Microsoft Semantic Kernel arbitrary file write
    "CVE-2026-34070",  # LangChain prompt-loading path traversal
    "CVE-2026-33873",  # Langflow agentic assistant RCE
    # PR 2: VPN / remote-access vulns
    "CVE-2018-13379",  # Fortinet FortiOS SSL VPN path traversal
    "CVE-2025-0282",  # Ivanti Connect Secure stack buffer overflow
    "CVE-2024-53704",  # SonicWall SSLVPN auth bypass
]
SEED_CWES: list[str] = ["CWE-78"]
LATEST_KEV_LIMIT = 50


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


def _to_json(value):
    """Coerce asyncpg row values into JSON-serializable Python types."""
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        # NUMERIC columns (e.g. CVSS scores) — float is precise enough at 3 sig figs.
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    if hasattr(value, "tolist"):  # numpy array (pgvector)
        return [float(f) for f in value.tolist()]
    return value


def _row_to_dict(row) -> dict:
    return {k: _to_json(v) for k, v in row.items()}


async def _fetch_all(conn: asyncpg.Connection):
    kev_specific = await conn.fetch(
        "SELECT * FROM kev_vulnerabilities WHERE cve_id = ANY($1::text[])",
        SEED_CVE_IDS,
    )
    kev_latest = await conn.fetch(
        "SELECT * FROM kev_vulnerabilities ORDER BY date_added DESC NULLS LAST LIMIT $1",
        LATEST_KEV_LIMIT,
    )
    kev_log4j = await conn.fetch(
        "SELECT * FROM kev_vulnerabilities WHERE vulnerability_name ILIKE '%log4j%' OR product ILIKE '%log4j%'"
    )

    seen: set[str] = set()
    kev_rows = []
    for r in list(kev_specific) + list(kev_latest) + list(kev_log4j):
        if r["cve_id"] in seen:
            continue
        seen.add(r["cve_id"])
        kev_rows.append(r)
    kev_rows.sort(key=lambda r: r["cve_id"])

    target_cves = sorted({r["cve_id"] for r in kev_rows} | set(SEED_CVE_IDS))
    nvd_rows = await conn.fetch(
        "SELECT * FROM nvd_vulnerabilities WHERE cve_id = ANY($1::text[]) ORDER BY cve_id",
        target_cves,
    )

    cwe_set: set[str] = set(SEED_CWES)
    for r in list(kev_rows) + list(nvd_rows):
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

    with OUT.open("w") as f:
        # First line is a header for human readers and a schema-version pin.
        f.write(
            json.dumps(
                {
                    "_meta": True,
                    "generated": dt.datetime.now(dt.UTC).date().isoformat(),
                    "counts": {"kev": len(kev_rows), "nvd": len(nvd_rows), "cwe": len(cwe_rows)},
                }
            )
            + "\n"
        )
        for r in kev_rows:
            f.write(json.dumps({"table": "kev_vulnerabilities", **_row_to_dict(r)}) + "\n")
        for r in nvd_rows:
            f.write(json.dumps({"table": "nvd_vulnerabilities", **_row_to_dict(r)}) + "\n")
        for r in cwe_rows:
            f.write(json.dumps({"table": "cwe_definitions", **_row_to_dict(r)}) + "\n")

    print(f"wrote {OUT} (kev={len(kev_rows)} nvd={len(nvd_rows)} cwe={len(cwe_rows)})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
