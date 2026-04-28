"""ETL script: Download MITRE CWE definitions and load into PostgreSQL.

Usage: uv run python scripts/load_cwe.py

Downloads the CWE Research Concepts view (view 1000) from cwe.mitre.org,
parses CWE IDs, names, abstraction levels, and descriptions, then upserts
into the cwe_definitions table. No API key required. Safe to re-run.
"""

import asyncio
import csv
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
import httpx

from config import settings

CWE_CSV_URL = "https://cwe.mitre.org/data/csv/1000.csv.zip"
CWE_URL_TEMPLATE = "https://cwe.mitre.org/data/definitions/{id}.html"

UPSERT_SQL = """
    INSERT INTO cwe_definitions (cwe_id, name, abstraction, description, url)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (cwe_id) DO UPDATE SET
        name        = EXCLUDED.name,
        abstraction = EXCLUDED.abstraction,
        description = EXCLUDED.description,
        url         = EXCLUDED.url
"""


async def download_cwe_csv() -> list[dict]:
    """Download the MITRE CWE CSV zip and return parsed rows."""
    print(f"Downloading CWE definitions from {CWE_CSV_URL}...")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(CWE_CSV_URL)
        resp.raise_for_status()
        zip_bytes = resp.content

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_name = next((name for name in zf.namelist() if name.endswith(".csv")), None)
        if csv_name is None:
            raise ValueError(f"No CSV file found in zip. Contents: {zf.namelist()}")
        with zf.open(csv_name) as f:
            content = f.read().decode("utf-8", errors="replace")

    rows = []
    reader = csv.DictReader(io.StringIO(content))
    expected = {"CWE-ID", "Name", "Weakness Abstraction", "Description"}
    missing = expected - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"CWE CSV missing expected columns: {missing}")
    for row in reader:
        raw_id = row.get("CWE-ID", "").strip()
        if not raw_id:
            continue
        cwe_id = f"CWE-{raw_id}"
        name = row.get("Name", "").strip()
        abstraction = row.get("Weakness Abstraction", "").strip() or None
        description = row.get("Description", "").strip() or None
        url = CWE_URL_TEMPLATE.format(id=raw_id)
        rows.append(
            {
                "cwe_id": cwe_id,
                "name": name,
                "abstraction": abstraction,
                "description": description,
                "url": url,
            }
        )

    return rows


async def upsert_definitions(conn: asyncpg.Connection, rows: list[dict]) -> None:
    """Upsert CWE definitions into PostgreSQL."""
    for row in rows:
        await conn.execute(
            UPSERT_SQL,
            row["cwe_id"],
            row["name"],
            row["abstraction"],
            row["description"],
            row["url"],
        )


async def main() -> None:
    print("Starting MITRE CWE ETL...")

    rows = await download_cwe_csv()
    if not rows:
        print("No CWE definitions parsed. Exiting.")
        return

    print(f"Parsed {len(rows)} CWE definitions.")

    print("Connecting to PostgreSQL...")
    conn = await asyncpg.connect(dsn=settings.get_database_dsn())
    try:
        print("Upserting records...")
        await upsert_definitions(conn, rows)
    finally:
        await conn.close()

    print(f"Done! Loaded {len(rows)} CWE definitions.")


if __name__ == "__main__":
    asyncio.run(main())
