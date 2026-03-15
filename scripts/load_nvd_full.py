"""ETL script: Fetch the full NVD database, generate embeddings, and load into PostgreSQL.

Uses paginated bulk fetching from NVD API 2.0 (2000 CVEs per page).
Supports incremental sync, checkpoint/resume, and separate embedding backfill.

Usage:
    uv run python scripts/load_nvd_full.py                    # Full load
    uv run python scripts/load_nvd_full.py --incremental      # Sync since last run
    uv run python scripts/load_nvd_full.py --skip-embeddings  # Data only, no embeddings
    uv run python scripts/load_nvd_full.py --backfill-embeddings  # Fill missing embeddings
    uv run python scripts/load_nvd_full.py --limit 3          # Test with first 3 pages

Set NVD_API_KEY env var to increase rate limit from 5 to 50 requests per 30 seconds.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
import numpy as np
from openai import AsyncOpenAI
from pgvector.asyncpg import register_vector

from config import settings
from scripts.nvd_utils import (
    build_content,
    extract_affected_products,
    extract_cvss_v2,
    extract_cvss_v31,
    extract_cwes,
    extract_description,
    extract_reference_urls,
    parse_date,
)

NVD_API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
RESULTS_PER_PAGE = 2000
EMBEDDING_BATCH_SIZE = 500
CHECKPOINT_FILE = Path(__file__).resolve().parent.parent / "data" / "nvd_checkpoint.json"

# Rate limiting: 5 req/30s without key, 50 req/30s with key
NVD_API_KEY = os.getenv("NVD_API_KEY")
REQUEST_DELAY = 0.7 if NVD_API_KEY else 6.0


# -- Checkpoint --

def load_checkpoint() -> dict | None:
    if CHECKPOINT_FILE.exists():
        return json.loads(CHECKPOINT_FILE.read_text())
    return None


def save_checkpoint(data: dict) -> None:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(data, indent=2))


def clear_checkpoint() -> None:
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()


# -- NVD API --

async def fetch_nvd_page(
    session,
    start_index: int,
    last_mod_start: str | None = None,
    last_mod_end: str | None = None,
) -> dict:
    """Fetch a page of CVEs from the NVD API 2.0."""
    import httpx

    params = {
        "startIndex": start_index,
        "resultsPerPage": RESULTS_PER_PAGE,
    }
    if last_mod_start:
        params["lastModStartDate"] = last_mod_start
    if last_mod_end:
        params["lastModEndDate"] = last_mod_end

    headers = {}
    if NVD_API_KEY:
        headers["apiKey"] = NVD_API_KEY

    for attempt in range(3):
        try:
            resp = await session.get(NVD_API_URL, params=params, headers=headers)
            if resp.status_code == 403:
                print(f"  Rate limited, waiting 30s (attempt {attempt + 1}/3)...")
                await asyncio.sleep(30)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if attempt < 2:
                print(f"  HTTP {e.response.status_code}, retrying in 10s...")
                await asyncio.sleep(10)
            else:
                raise
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            if attempt < 2:
                print(f"  Timeout, retrying in 10s...")
                await asyncio.sleep(10)
            else:
                raise

    raise RuntimeError("Failed to fetch NVD page after 3 attempts")


def parse_cve_records(vulnerabilities: list[dict]) -> list[dict]:
    """Extract cve objects from NVD API response vulnerabilities array."""
    return [v["cve"] for v in vulnerabilities if "cve" in v]


# -- Database --

def _prepare_row(cve_data: dict, embedding: list[float] | None) -> tuple:
    """Prepare a single CVE record as a tuple for bulk insert."""
    metrics = cve_data.get("metrics", {})
    cvss_v31_score, cvss_v31_severity, cvss_v31_vector = extract_cvss_v31(metrics)
    cvss_v2_score, cvss_v2_severity = extract_cvss_v2(metrics)
    emb = np.array(embedding, dtype=np.float32) if embedding else None

    return (
        cve_data.get("id"),
        extract_description(cve_data.get("descriptions", [])),
        cvss_v31_score,
        cvss_v31_severity,
        cvss_v31_vector,
        cvss_v2_score,
        cvss_v2_severity,
        extract_cwes(cve_data.get("weaknesses", [])),
        extract_affected_products(cve_data.get("configurations", [])),
        extract_reference_urls(cve_data.get("references", [])),
        parse_date(cve_data.get("published")),
        parse_date(cve_data.get("lastModified")),
        json.dumps(cve_data),
        build_content(cve_data),
        emb,
    )


async def upsert_batch(conn: asyncpg.Connection, cve_records: list[dict], embeddings: list[list[float]] | None) -> None:
    """Upsert a batch of CVE records using a temp table + INSERT ON CONFLICT."""
    rows = [
        _prepare_row(cve, embeddings[i] if embeddings else None)
        for i, cve in enumerate(cve_records)
    ]

    # Use a temp table for fast COPY, then merge into the main table
    async with conn.transaction():
        await conn.execute("DROP TABLE IF EXISTS _nvd_staging")
        await conn.execute("""
            CREATE TEMP TABLE _nvd_staging (
                cve_id VARCHAR(20),
                description TEXT,
                cvss_v31_score NUMERIC(3,1),
                cvss_v31_severity VARCHAR(10),
                cvss_v31_vector TEXT,
                cvss_v2_score NUMERIC(3,1),
                cvss_v2_severity VARCHAR(10),
                cwes TEXT[],
                affected_products TEXT[],
                reference_urls TEXT[],
                published DATE,
                last_modified DATE,
                raw_json JSONB,
                content TEXT,
                embedding vector(1536)
            ) ON COMMIT DROP
        """)

        # Bulk copy into staging
        await conn.copy_records_to_table(
            "_nvd_staging",
            records=rows,
            columns=[
                "cve_id", "description", "cvss_v31_score", "cvss_v31_severity",
                "cvss_v31_vector", "cvss_v2_score", "cvss_v2_severity",
                "cwes", "affected_products", "reference_urls",
                "published", "last_modified", "raw_json", "content", "embedding",
            ],
        )

        # Merge from staging into main table
        await conn.execute("""
            INSERT INTO nvd_vulnerabilities (
                cve_id, description, cvss_v31_score, cvss_v31_severity,
                cvss_v31_vector, cvss_v2_score, cvss_v2_severity,
                cwes, affected_products, reference_urls,
                published, last_modified, raw_json, content, embedding
            )
            SELECT
                cve_id, description, cvss_v31_score, cvss_v31_severity,
                cvss_v31_vector, cvss_v2_score, cvss_v2_severity,
                cwes, affected_products, reference_urls,
                published, last_modified, raw_json, content, embedding
            FROM _nvd_staging
            ON CONFLICT (cve_id) DO UPDATE SET
                description = EXCLUDED.description,
                cvss_v31_score = EXCLUDED.cvss_v31_score,
                cvss_v31_severity = EXCLUDED.cvss_v31_severity,
                cvss_v31_vector = EXCLUDED.cvss_v31_vector,
                cvss_v2_score = EXCLUDED.cvss_v2_score,
                cvss_v2_severity = EXCLUDED.cvss_v2_severity,
                cwes = EXCLUDED.cwes,
                affected_products = EXCLUDED.affected_products,
                reference_urls = EXCLUDED.reference_urls,
                published = EXCLUDED.published,
                last_modified = EXCLUDED.last_modified,
                raw_json = EXCLUDED.raw_json,
                content = EXCLUDED.content,
                embedding = COALESCE(EXCLUDED.embedding, nvd_vulnerabilities.embedding)
        """)


# -- Embeddings --

async def generate_embeddings(openai_client: AsyncOpenAI, texts: list[str]) -> list[list[float]]:
    """Generate embeddings in batches."""
    all_embeddings = []
    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i : i + EMBEDDING_BATCH_SIZE]
        resp = await openai_client.embeddings.create(model=settings.embedding_model, input=batch)
        all_embeddings.extend([item.embedding for item in resp.data])
        print(f"  Embedded {min(i + EMBEDDING_BATCH_SIZE, len(texts))}/{len(texts)}")
    return all_embeddings


# -- Main modes --

async def full_load(args) -> None:
    """Fetch all CVEs from NVD API using pagination."""
    import httpx

    dsn = settings.get_database_dsn()

    # Check for existing checkpoint
    checkpoint = load_checkpoint()
    start_index = 0
    if checkpoint and checkpoint.get("mode") == "full":
        start_index = checkpoint["start_index"]
        print(f"Resuming from checkpoint at index {start_index}")

    # First request to get total count
    print("Fetching first page to determine total CVE count...")
    async with httpx.AsyncClient(timeout=60) as session:
        first_page = await fetch_nvd_page(session, start_index)

    total_results = first_page.get("totalResults", 0)
    total_pages = (total_results + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
    print(f"Total CVEs in NVD: {total_results} ({total_pages} pages)")

    openai_client = None
    if not args.skip_embeddings:
        openai_client = AsyncOpenAI(api_key=settings.openai_api_key)

    started_at = time.time()
    total_loaded = 0

    async with httpx.AsyncClient(timeout=60) as session:
        current_index = start_index
        page_num = current_index // RESULTS_PER_PAGE + 1

        while current_index < total_results:
            if args.limit and page_num > args.limit + (start_index // RESULTS_PER_PAGE):
                print(f"Reached page limit ({args.limit}), stopping.")
                break

            elapsed = time.time() - started_at
            elapsed_str = f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s"

            print(f"Page {page_num}/{total_pages} | index {current_index}/{total_results} | elapsed: {elapsed_str}")

            # Fetch (reuse first page if resuming from 0)
            if current_index == start_index and 'first_page' in dir():
                data = first_page
            else:
                data = await fetch_nvd_page(session, current_index)
                await asyncio.sleep(REQUEST_DELAY)

            vulnerabilities = data.get("vulnerabilities", [])
            if not vulnerabilities:
                print("  No vulnerabilities in response, advancing...")
                current_index += RESULTS_PER_PAGE
                page_num += 1
                continue

            cve_records = parse_cve_records(vulnerabilities)

            # Generate embeddings
            embeddings = None
            if openai_client and cve_records:
                contents = [build_content(cve) for cve in cve_records]
                print(f"  Generating embeddings for {len(cve_records)} records...")
                embeddings = await generate_embeddings(openai_client, contents)

            # Upsert to database (with retry on connection errors)
            print(f"  Upserting {len(cve_records)} records...")
            for db_attempt in range(3):
                try:
                    conn = await asyncpg.connect(dsn=dsn)
                    await register_vector(conn)
                    await upsert_batch(conn, cve_records, embeddings)
                    await conn.close()
                    break
                except (ConnectionResetError, OSError, asyncpg.ConnectionDoesNotExistError) as e:
                    if db_attempt < 2:
                        print(f"  DB connection error: {e}, retrying in 5s...")
                        await asyncio.sleep(5)
                    else:
                        raise

            total_loaded += len(cve_records)
            current_index += RESULTS_PER_PAGE
            page_num += 1

            # Save checkpoint
            save_checkpoint({
                "mode": "full",
                "start_index": current_index,
                "total_results": total_results,
                "started_at": datetime.now(timezone.utc).isoformat(),
            })

    clear_checkpoint()
    elapsed = time.time() - started_at
    print(f"Done! Loaded {total_loaded} CVEs in {int(elapsed // 60)}m{int(elapsed % 60):02d}s")


async def incremental_sync(args) -> None:
    """Fetch CVEs modified since the last sync."""
    import httpx

    dsn = settings.get_database_dsn()
    conn = await asyncpg.connect(dsn=dsn)

    # Get high-water mark
    row = await conn.fetchrow("SELECT MAX(last_modified) as max_date FROM nvd_vulnerabilities")
    await conn.close()

    if not row or not row["max_date"]:
        print("No existing records found. Run a full load first.")
        return

    high_water = datetime.combine(row["max_date"], datetime.min.time(), tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    print(f"Syncing changes from {high_water.date()} to {now.date()}")

    openai_client = None
    if not args.skip_embeddings:
        openai_client = AsyncOpenAI(api_key=settings.openai_api_key)

    total_loaded = 0
    started_at = time.time()

    # NVD API requires date ranges <= 120 days
    window_start = high_water
    async with httpx.AsyncClient(timeout=60) as session:
        while window_start < now:
            window_end = min(window_start + timedelta(days=120), now)
            start_str = window_start.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
            end_str = window_end.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")

            print(f"Window: {window_start.date()} to {window_end.date()}")

            # Paginate within this window
            current_index = 0
            while True:
                data = await fetch_nvd_page(
                    session, current_index,
                    last_mod_start=start_str,
                    last_mod_end=end_str,
                )
                await asyncio.sleep(REQUEST_DELAY)

                total_in_window = data.get("totalResults", 0)
                if current_index == 0:
                    print(f"  {total_in_window} modified CVEs in this window")

                vulnerabilities = data.get("vulnerabilities", [])
                if not vulnerabilities:
                    break

                cve_records = parse_cve_records(vulnerabilities)

                embeddings = None
                if openai_client and cve_records:
                    contents = [build_content(cve) for cve in cve_records]
                    embeddings = await generate_embeddings(openai_client, contents)

                for db_attempt in range(3):
                    try:
                        conn = await asyncpg.connect(dsn=dsn)
                        await register_vector(conn)
                        await upsert_batch(conn, cve_records, embeddings)
                        await conn.close()
                        break
                    except (ConnectionResetError, OSError, asyncpg.ConnectionDoesNotExistError) as e:
                        if db_attempt < 2:
                            print(f"  DB connection error: {e}, retrying in 5s...")
                            await asyncio.sleep(5)
                        else:
                            raise

                total_loaded += len(cve_records)
                current_index += RESULTS_PER_PAGE

                if current_index >= total_in_window:
                    break

            window_start = window_end

    elapsed = time.time() - started_at
    print(f"Done! Synced {total_loaded} CVEs in {int(elapsed // 60)}m{int(elapsed % 60):02d}s")


async def backfill_embeddings(args) -> None:
    """Generate embeddings for records that don't have them."""
    dsn = settings.get_database_dsn()
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key)

    conn = await asyncpg.connect(dsn=dsn)
    await register_vector(conn)

    total = await conn.fetchval(
        "SELECT COUNT(*) FROM nvd_vulnerabilities WHERE embedding IS NULL"
    )
    print(f"Found {total} records without embeddings")

    if total == 0:
        await conn.close()
        return

    processed = 0
    while processed < total:
        rows = await conn.fetch(
            """
            SELECT cve_id, content FROM nvd_vulnerabilities
            WHERE embedding IS NULL
            ORDER BY cve_id
            LIMIT $1
            """,
            EMBEDDING_BATCH_SIZE,
        )

        if not rows:
            break

        texts = [row["content"] for row in rows]
        cve_ids = [row["cve_id"] for row in rows]

        resp = await openai_client.embeddings.create(
            model=settings.embedding_model, input=texts
        )
        embeddings = [item.embedding for item in resp.data]

        for cve_id, emb in zip(cve_ids, embeddings):
            await conn.execute(
                "UPDATE nvd_vulnerabilities SET embedding = $1 WHERE cve_id = $2",
                np.array(emb, dtype=np.float32),
                cve_id,
            )

        processed += len(rows)
        print(f"  Backfilled {processed}/{total}")

    await conn.close()
    print(f"Done! Backfilled embeddings for {processed} records")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Full NVD ETL")
    parser.add_argument("--incremental", action="store_true", help="Sync changes since last run")
    parser.add_argument("--skip-embeddings", action="store_true", help="Load data without generating embeddings")
    parser.add_argument("--backfill-embeddings", action="store_true", help="Generate embeddings for rows missing them")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N pages (for testing)")
    args = parser.parse_args()

    rate_info = "with API key (50 req/30s)" if NVD_API_KEY else "without API key (5 req/30s)"
    print(f"NVD Full ETL | Rate limiting: {rate_info}")

    # Ensure schema exists
    dsn = settings.get_database_dsn()
    conn = await asyncpg.connect(dsn=dsn)
    from rag.database import SCHEMA_SQL
    await conn.execute(SCHEMA_SQL)
    await conn.close()

    if args.backfill_embeddings:
        await backfill_embeddings(args)
    elif args.incremental:
        await incremental_sync(args)
    else:
        await full_load(args)


if __name__ == "__main__":
    asyncio.run(main())
