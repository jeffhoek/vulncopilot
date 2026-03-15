"""ETL script: Fetch NVD data for KEV CVEs, generate embeddings, and load into PostgreSQL.

Queries the NVD API 2.0 for each CVE ID found in the kev_vulnerabilities table,
enriching the dataset with CVSS scores, affected products, and detailed descriptions.

Usage: uv run python scripts/load_nvd.py

Set NVD_API_KEY env var to increase rate limit from 5 to 50 requests per 30 seconds.
"""

import asyncio
import datetime
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
import httpx
import numpy as np
from openai import AsyncOpenAI
from pgvector.asyncpg import register_vector

from config import settings

NVD_API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
BATCH_SIZE = 500

# Rate limiting: 5 req/30s without key, 50 req/30s with key
NVD_API_KEY = os.getenv("NVD_API_KEY")
REQUEST_DELAY = 0.7 if NVD_API_KEY else 6.0


# ---------------------------------------------------------------------------
# NVD response parsing helpers
# ---------------------------------------------------------------------------


def extract_cvss_v31(metrics: dict) -> tuple:
    """Extract CVSS v3.1 score, severity, and vector string."""
    for entry in metrics.get("cvssMetricV31", []):
        data = entry.get("cvssData", {})
        return (
            data.get("baseScore"),
            data.get("baseSeverity"),
            data.get("vectorString"),
        )
    return None, None, None


def extract_cvss_v2(metrics: dict) -> tuple:
    """Extract CVSS v2 score and severity."""
    for entry in metrics.get("cvssMetricV2", []):
        data = entry.get("cvssData", {})
        return data.get("baseScore"), entry.get("baseSeverity")
    return None, None


def extract_cwes(weaknesses: list) -> list[str]:
    """Extract CWE IDs from weaknesses."""
    cwes = []
    for weakness in weaknesses:
        for desc in weakness.get("description", []):
            if desc.get("lang") == "en":
                cwes.append(desc["value"])
    return cwes


def extract_affected_products(configurations: list) -> list[str]:
    """Extract CPE strings from configurations."""
    products = []
    for config in configurations:
        for node in config.get("nodes", []):
            for match in node.get("cpeMatch", []):
                if match.get("vulnerable"):
                    products.append(match.get("criteria", ""))
    return products


def extract_description(descriptions: list) -> str:
    """Extract English description."""
    for desc in descriptions:
        if desc.get("lang") == "en":
            return desc.get("value", "")
    return ""


def extract_reference_urls(references: list) -> list[str]:
    """Extract reference URLs."""
    return [ref.get("url", "") for ref in references[:10]]


def parse_date(date_str: str | None) -> datetime.date | None:
    """Parse ISO-8601 date string to date object."""
    if not date_str:
        return None
    return datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()


def build_content(cve_data: dict) -> str:
    """Build content string for embedding from NVD CVE data."""
    description = extract_description(cve_data.get("descriptions", []))
    metrics = cve_data.get("metrics", {})
    cvss_score, cvss_severity, cvss_vector = extract_cvss_v31(metrics)
    cwes = extract_cwes(cve_data.get("weaknesses", []))
    products = extract_affected_products(cve_data.get("configurations", []))

    parts = [
        f"CVE ID: {cve_data.get('id', '')}",
        f"Description: {description}",
    ]
    if cvss_score is not None:
        parts.append(f"CVSS v3.1 Score: {cvss_score} ({cvss_severity})")
    if cvss_vector:
        parts.append(f"CVSS Vector: {cvss_vector}")
    if cwes:
        parts.append(f"CWEs: {', '.join(cwes)}")
    if products:
        parts.append(f"Affected Products: {', '.join(products[:5])}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


async def init_database(dsn: str) -> asyncpg.Connection:
    """Connect to PostgreSQL and ensure the schema exists.

    Returns an open connection with pgvector registered.
    """
    conn = await asyncpg.connect(dsn=dsn)
    from rag.database import SCHEMA_SQL

    await conn.execute(SCHEMA_SQL)
    await register_vector(conn)
    return conn


async def fetch_kev_cve_ids(conn: asyncpg.Connection) -> list[str]:
    """Get all CVE IDs from the KEV table."""
    rows = await conn.fetch("SELECT cve_id FROM kev_vulnerabilities ORDER BY cve_id")
    return [row["cve_id"] for row in rows]


async def find_new_cve_ids(conn: asyncpg.Connection) -> list[str]:
    """Return KEV CVE IDs that are not yet in the nvd_vulnerabilities table.

    This lets the script resume where it left off if interrupted.
    """
    cve_ids = await fetch_kev_cve_ids(conn)
    if not cve_ids:
        print("No KEV records found. Run load_kev.py first.")
        return []
    print(f"Found {len(cve_ids)} CVE IDs in KEV table")

    existing = await conn.fetch("SELECT cve_id FROM nvd_vulnerabilities")
    existing_ids = {row["cve_id"] for row in existing}
    new_ids = [cve_id for cve_id in cve_ids if cve_id not in existing_ids]
    print(f"  {len(existing_ids)} already loaded, {len(new_ids)} new to fetch")
    return new_ids


# ---------------------------------------------------------------------------
# NVD API fetching
# ---------------------------------------------------------------------------


async def fetch_nvd_cve(client: httpx.AsyncClient, cve_id: str) -> dict | None:
    """Fetch a single CVE from the NVD API."""
    headers = {}
    if NVD_API_KEY:
        headers["apiKey"] = NVD_API_KEY

    resp = await client.get(NVD_API_URL, params={"cveId": cve_id}, headers=headers)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    data = resp.json()
    vulns = data.get("vulnerabilities", [])
    if not vulns:
        return None
    return vulns[0].get("cve")


async def fetch_nvd_cve_with_retry(
    client: httpx.AsyncClient, cve_id: str
) -> dict | None:
    """Fetch a single CVE, retrying once on 403 (rate limit)."""
    try:
        return await fetch_nvd_cve(client, cve_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            print(f"  Rate limited at {cve_id}, waiting 30s...")
            await asyncio.sleep(30)
            return await fetch_nvd_cve(client, cve_id)
        raise


async def fetch_nvd_batch(
    client: httpx.AsyncClient,
    cve_ids: list[str],
    offset: int,
    total: int,
) -> tuple[list[dict], int]:
    """Fetch a batch of CVEs from the NVD API.

    Returns (records, skipped_count).  Respects the global REQUEST_DELAY
    between calls and retries once on rate-limit (403).
    """
    results = []
    skipped = 0

    for i, cve_id in enumerate(cve_ids):
        try:
            cve_data = await fetch_nvd_cve_with_retry(client, cve_id)
            if cve_data:
                results.append(cve_data)
            else:
                skipped += 1
        except Exception as e:
            print(f"  Error fetching {cve_id}: {e}")
            skipped += 1

        # Progress reporting every 50 CVEs
        absolute = offset + i + 1
        if absolute % 50 == 0:
            print(f"  Fetched {absolute}/{total}")

        await asyncio.sleep(REQUEST_DELAY)

    return results, skipped


# ---------------------------------------------------------------------------
# Embedding generation
# ---------------------------------------------------------------------------


async def generate_embeddings(
    openai_client: AsyncOpenAI, texts: list[str]
) -> list[list[float]]:
    """Generate embeddings in batches of BATCH_SIZE via the OpenAI API."""
    all_embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        resp = await openai_client.embeddings.create(
            model=settings.embedding_model, input=batch
        )
        all_embeddings.extend([item.embedding for item in resp.data])
        print(f"  Embedded {min(i + BATCH_SIZE, len(texts))}/{len(texts)}")
    return all_embeddings


# ---------------------------------------------------------------------------
# Upserting records into PostgreSQL
# ---------------------------------------------------------------------------


def build_upsert_params(cve_data: dict, embedding: list[float]) -> tuple:
    """Build the parameter tuple for the NVD upsert query."""
    metrics = cve_data.get("metrics", {})
    cvss_v31_score, cvss_v31_severity, cvss_v31_vector = extract_cvss_v31(metrics)
    cvss_v2_score, cvss_v2_severity = extract_cvss_v2(metrics)

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
        build_content(cve_data),
        np.array(embedding, dtype=np.float32),
    )


UPSERT_SQL = """
    INSERT INTO nvd_vulnerabilities (
        cve_id, description, cvss_v31_score, cvss_v31_severity,
        cvss_v31_vector, cvss_v2_score, cvss_v2_severity,
        cwes, affected_products, reference_urls,
        published, last_modified, content, embedding
    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
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
        content = EXCLUDED.content,
        embedding = EXCLUDED.embedding
"""


async def upsert_records(
    conn: asyncpg.Connection,
    cve_records: list[dict],
    embeddings: list[list[float]],
) -> None:
    """Upsert NVD records into PostgreSQL one at a time."""
    for i, (cve_data, emb) in enumerate(zip(cve_records, embeddings)):
        params = build_upsert_params(cve_data, emb)
        await conn.execute(UPSERT_SQL, *params)
        if (i + 1) % 500 == 0:
            print(f"  Upserted {i + 1}/{len(cve_records)}")

    print(f"  Upserted {len(cve_records)}/{len(cve_records)} total")


# ---------------------------------------------------------------------------
# Per-batch pipeline: fetch → embed → upsert
# ---------------------------------------------------------------------------


async def process_batch(
    client: httpx.AsyncClient,
    openai_client: AsyncOpenAI,
    dsn: str,
    batch_ids: list[str],
    batch_num: int,
    total_batches: int,
    batch_offset: int,
    total_ids: int,
) -> tuple[int, int]:
    """Run the full fetch → embed → upsert pipeline for one batch.

    Opens a fresh DB connection for the upsert step to avoid idle timeouts
    that occur when the NVD fetch phase takes a long time.

    Returns (loaded_count, skipped_count).
    """
    print(f"Batch {batch_num}/{total_batches}: fetching {len(batch_ids)} CVEs...")

    # Step 1: Fetch CVE data from the NVD API
    cve_records, skipped = await fetch_nvd_batch(
        client, batch_ids, batch_offset, total_ids
    )
    if not cve_records:
        print(f"  No records in batch {batch_num}, skipping embed/upsert.")
        return 0, skipped

    # Step 2: Generate embeddings for the fetched records
    contents = [build_content(cve) for cve in cve_records]
    print(f"  Generating embeddings for {len(cve_records)} records...")
    embeddings = await generate_embeddings(openai_client, contents)

    # Step 3: Upsert into PostgreSQL (fresh connection to avoid idle timeout)
    print(f"  Upserting {len(cve_records)} records...")
    conn = await asyncpg.connect(dsn=dsn)
    await register_vector(conn)
    try:
        await upsert_records(conn, cve_records, embeddings)
    finally:
        await conn.close()

    return len(cve_records), skipped


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    print("Starting NVD ETL (scoped to KEV CVEs)...")
    rate_info = (
        "with API key (50 req/30s)" if NVD_API_KEY else "without API key (5 req/30s)"
    )
    print(f"  Rate limiting: {rate_info}")

    # Initialize database schema and discover which CVEs still need fetching
    print("Connecting to PostgreSQL...")
    dsn = settings.get_database_dsn()
    conn = await init_database(dsn)
    new_ids = await find_new_cve_ids(conn)
    await conn.close()

    if not new_ids:
        print("All NVD records already loaded. Nothing to do.")
        return

    # Process in batches: fetch → embed → upsert, then move to next batch
    print(f"Fetching {len(new_ids)} CVEs from NVD API (batch size: {BATCH_SIZE})...")
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    total_loaded = 0
    total_skipped = 0

    async with httpx.AsyncClient(timeout=30) as client:
        total_batches = (len(new_ids) + BATCH_SIZE - 1) // BATCH_SIZE
        for batch_start in range(0, len(new_ids), BATCH_SIZE):
            batch_ids = new_ids[batch_start : batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1

            loaded, skipped = await process_batch(
                client, openai_client, dsn, batch_ids,
                batch_num, total_batches, batch_start, len(new_ids),
            )
            total_loaded += loaded
            total_skipped += skipped
            if loaded:
                print(f"  Batch {batch_num} complete. Total loaded so far: {total_loaded}")

    print(f"Done! Loaded {total_loaded} NVD records ({total_skipped} skipped).")


if __name__ == "__main__":
    asyncio.run(main())
