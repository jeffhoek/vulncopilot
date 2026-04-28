"""ETL script: Scrape NVD reference URLs, extract content, embed, and store in cve_references.

Fetches unprocessed (or stale) reference URLs from nvd_vulnerabilities, extracts main-text
content via trafilatura, optionally summarizes long pages with Claude Haiku, generates
embeddings, and upserts results into the cve_references table.

Usage:
  uv run python scripts/scrape_references.py                    # scrape all unprocessed
  uv run python scripts/scrape_references.py --refresh          # re-scrape stale (>30 days)
  uv run python scripts/scrape_references.py --cve CVE-2021-44228  # single CVE
"""

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
import asyncpg
import httpx
import numpy as np
import trafilatura
from openai import AsyncOpenAI
from pgvector.asyncpg import register_vector
from robotexclusionrulesparser import RobotExclusionRulesParser

from config import settings

EMBED_BATCH_SIZE = 500
SCRAPE_BATCH_SIZE = 100
SUMMARIZE_THRESHOLD = 8_000  # chars; pages longer than this get Haiku summarization
REFRESH_DAYS = 30

# Domains to skip immediately — low signal or duplicate of structured NVD data
DENYLIST_DOMAINS = {
    "nvd.nist.gov",
    "web.nvd.nist.gov",
    "cve.mitre.org",
    "twitter.com",
    "x.com",
    "facebook.com",
    "youtube.com",
    "linkedin.com",
    "t.co",
}

# Per-domain concurrency and inter-request delay (seconds)
# Unlisted domains use "default" values
DOMAIN_CONCURRENCY: dict[str, int] = {
    "microsoft.com": 1,
    "cisco.com": 1,
    "apple.com": 1,
    "default": 3,
}
DOMAIN_DELAY: dict[str, float] = {
    "microsoft.com": 3.0,
    "cisco.com": 2.0,
    "default": 1.0,
}

UPSERT_SQL = """
    INSERT INTO cve_references (
        url, cve_id, domain, title, scraped_text, summary, content,
        embedding, http_status, scraped_at, content_hash, skip_reason
    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW(),$10,$11)
    ON CONFLICT (url, cve_id) DO UPDATE SET
        domain       = EXCLUDED.domain,
        title        = EXCLUDED.title,
        scraped_text = EXCLUDED.scraped_text,
        summary      = EXCLUDED.summary,
        content      = EXCLUDED.content,
        embedding    = EXCLUDED.embedding,
        http_status  = EXCLUDED.http_status,
        scraped_at   = EXCLUDED.scraped_at,
        content_hash = EXCLUDED.content_hash,
        skip_reason  = EXCLUDED.skip_reason
"""

# --- Robots.txt cache ---------------------------------------------------------

_robots_cache: dict[str, RobotExclusionRulesParser] = {}


async def is_robots_allowed(client: httpx.AsyncClient, url: str) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _robots_cache:
        parser = RobotExclusionRulesParser()
        try:
            r = await client.get(f"{origin}/robots.txt", timeout=10, follow_redirects=True)
            if r.status_code == 200:
                parser.parse(r.text)
        except Exception as exc:
            print(f"  robots.txt fetch failed for {origin}: {exc} (allowing)")
        _robots_cache[origin] = parser
    return _robots_cache[origin].is_allowed("*", url)


# --- Domain helpers -----------------------------------------------------------


def registered_domain(hostname: str) -> str:
    """Return eTLD+1 (e.g. 'sub.microsoft.com' → 'microsoft.com')."""
    parts = hostname.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def domain_semaphore(semaphores: dict[str, asyncio.Semaphore], hostname: str) -> asyncio.Semaphore:
    rd = registered_domain(hostname)
    if rd not in semaphores:
        limit = DOMAIN_CONCURRENCY.get(rd, DOMAIN_CONCURRENCY["default"])
        semaphores[rd] = asyncio.Semaphore(limit)
    return semaphores[rd]


def domain_delay(hostname: str) -> float:
    rd = registered_domain(hostname)
    return DOMAIN_DELAY.get(rd, DOMAIN_DELAY["default"])


# --- Content extraction -------------------------------------------------------


def extract_text(html: str) -> tuple[str, str]:
    """Return (title, body_text) from raw HTML."""
    body = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta and meta.title else "") or ""
    return title, body


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- Summarization ------------------------------------------------------------


async def summarize(anthropic_client: anthropic.AsyncAnthropic, text: str) -> str:
    """Summarize a long security page with Claude Haiku."""
    msg = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    "Summarize the following security advisory or vulnerability write-up "
                    "in 3-5 sentences. Focus on: what is vulnerable, how it can be exploited, "
                    "what the impact is, and how to remediate. "
                    "Do not include boilerplate or disclaimers.\n\n"
                    f"{text[:12000]}"
                ),
            }
        ],
    )
    return msg.content[0].text if msg.content else ""


# --- Database queries ---------------------------------------------------------


async def fetch_unprocessed_pairs(conn: asyncpg.Connection, cve_id: str | None) -> list[tuple[str, str]]:
    """Return (cve_id, url) pairs that have no row in cve_references yet."""
    if cve_id:
        rows = await conn.fetch(
            """
            SELECT n.cve_id, u.url
            FROM nvd_vulnerabilities n,
                 UNNEST(n.reference_urls) AS u(url)
            WHERE n.cve_id = $1
              AND NOT EXISTS (
                  SELECT 1 FROM cve_references r
                  WHERE r.cve_id = n.cve_id AND r.url = u.url
              )
            """,
            cve_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT n.cve_id, u.url
            FROM nvd_vulnerabilities n,
                 UNNEST(n.reference_urls) AS u(url)
            WHERE NOT EXISTS (
                SELECT 1 FROM cve_references r
                WHERE r.cve_id = n.cve_id AND r.url = u.url
            )
            ORDER BY n.cve_id
            """
        )
    return [(row["cve_id"], row["url"]) for row in rows]


async def fetch_stale_pairs(conn: asyncpg.Connection, cve_id: str | None) -> list[tuple[str, str]]:
    """Return (cve_id, url) pairs that are older than REFRESH_DAYS and not dead."""
    if cve_id:
        rows = await conn.fetch(
            """
            SELECT cve_id, url FROM cve_references
            WHERE cve_id = $1
              AND (http_status IS NULL OR http_status < 400)
              AND (scraped_at IS NULL OR scraped_at < NOW() - INTERVAL '30 days')
            """,
            cve_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT cve_id, url FROM cve_references
            WHERE (http_status IS NULL OR http_status < 400)
              AND (scraped_at IS NULL OR scraped_at < NOW() - INTERVAL '30 days')
            ORDER BY cve_id
            """
        )
    return [(row["cve_id"], row["url"]) for row in rows]


# --- Core scrape + embed pipeline ---------------------------------------------


async def process_pairs(
    pairs: list[tuple[str, str]],
    client: httpx.AsyncClient,
    openai_client: AsyncOpenAI,
    anthropic_client: anthropic.AsyncAnthropic,
    dsn: str,
) -> tuple[int, int, int]:
    """Scrape, summarize, embed, and upsert a list of (cve_id, url) pairs.

    Returns (scraped, skipped, embedded).
    """
    semaphores: dict[str, asyncio.Semaphore] = {}
    scraped = skipped = embedded = 0

    # Accumulate rows ready for embedding
    pending_embed: list[dict] = []

    async def flush_pending() -> None:
        nonlocal embedded
        if not pending_embed:
            return
        texts = [r["content"] for r in pending_embed]
        print(f"  Embedding {len(texts)} pages...")
        # Batch embed
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            resp = await openai_client.embeddings.create(model=settings.embedding_model, input=batch)
            all_embeddings.extend([item.embedding for item in resp.data])

        conn = await asyncpg.connect(dsn=dsn)
        await register_vector(conn)
        for row, emb in zip(pending_embed, all_embeddings, strict=True):
            await conn.execute(
                UPSERT_SQL,
                row["url"],
                row["cve_id"],
                row["domain"],
                row["title"],
                row["scraped_text"],
                row["summary"],
                row["content"],
                np.array(emb, dtype=np.float32),
                row["http_status"],
                row["content_hash"],
                row["skip_reason"],
            )
        await conn.close()
        embedded += len(pending_embed)
        pending_embed.clear()

    async def upsert_skip(cve_id: str, url: str, domain: str, reason: str) -> None:
        conn = await asyncpg.connect(dsn=dsn)
        await register_vector(conn)
        await conn.execute(
            UPSERT_SQL,
            url,
            cve_id,
            domain,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            reason,
        )
        await conn.close()

    for idx, (cve_id, url) in enumerate(pairs):
        parsed = urlparse(url)
        hostname = parsed.netloc.lower()
        rd = registered_domain(hostname)

        # 1. Denylist check
        if hostname in DENYLIST_DOMAINS or rd in DENYLIST_DOMAINS:
            await upsert_skip(cve_id, url, rd, f"denylist:{rd}")
            skipped += 1
            if (idx + 1) % 100 == 0:
                print(f"  Processed {idx + 1}/{len(pairs)} (skipped denylist)")
            continue

        # 2. Robots.txt check
        if not await is_robots_allowed(client, url):
            await upsert_skip(cve_id, url, rd, "robots_txt")
            skipped += 1
            continue

        # 3. Fetch with per-domain rate limiting
        sem = domain_semaphore(semaphores, hostname)
        async with sem:
            try:
                resp = await client.get(url, timeout=30, follow_redirects=True, max_redirects=5)
                http_status = resp.status_code
            except Exception as exc:
                reason = f"fetch_error:{type(exc).__name__}"
                await upsert_skip(cve_id, url, rd, reason)
                skipped += 1
                continue
            finally:
                await asyncio.sleep(domain_delay(hostname))

        # 4. Non-200 responses
        if http_status != 200:
            conn = await asyncpg.connect(dsn=dsn)
            await register_vector(conn)
            await conn.execute(
                UPSERT_SQL,
                url,
                cve_id,
                rd,
                None,
                None,
                None,
                None,
                None,
                http_status,
                None,
                f"http_{http_status}",
            )
            await conn.close()
            skipped += 1
            continue

        # 5. Extract text
        title, body = extract_text(resp.text)
        if not body:
            conn = await asyncpg.connect(dsn=dsn)
            await register_vector(conn)
            await conn.execute(
                UPSERT_SQL,
                url,
                cve_id,
                rd,
                title,
                None,
                None,
                None,
                None,
                http_status,
                None,
                "no_content",
            )
            await conn.close()
            skipped += 1
            continue

        # 6. Hash for change detection
        chash = compute_hash(body)

        # 7. Two-tier summarization
        summary: str | None = None
        if len(body) >= SUMMARIZE_THRESHOLD:
            try:
                summary = await summarize(anthropic_client, body)
                content = summary
            except Exception as exc:
                print(f"  Summarization failed for {url}: {exc}")
                content = body[:8000]
        else:
            content = body

        pending_embed.append(
            {
                "url": url,
                "cve_id": cve_id,
                "domain": rd,
                "title": title,
                "scraped_text": body,
                "summary": summary,
                "content": content,
                "http_status": http_status,
                "content_hash": chash,
                "skip_reason": None,
            }
        )
        scraped += 1

        # Flush embedding batch
        if len(pending_embed) >= SCRAPE_BATCH_SIZE:
            await flush_pending()

        if (idx + 1) % 50 == 0:
            print(f"  Processed {idx + 1}/{len(pairs)} (scraped={scraped}, skipped={skipped})")

    await flush_pending()
    return scraped, skipped, embedded


# --- Main ---------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape NVD reference URLs")
    parser.add_argument("--refresh", action="store_true", help="Re-scrape stale entries (>30 days)")
    parser.add_argument("--cve", metavar="CVE_ID", help="Limit to a single CVE")
    args = parser.parse_args()

    dsn = settings.get_database_dsn()
    conn = await asyncpg.connect(dsn=dsn)
    await register_vector(conn)

    if args.refresh:
        pairs = await fetch_stale_pairs(conn, args.cve)
        mode = "refresh"
    else:
        pairs = await fetch_unprocessed_pairs(conn, args.cve)
        mode = "initial"

    await conn.close()

    scope = f"CVE {args.cve}" if args.cve else "all CVEs"
    print(f"Starting reference scrape ({mode}, {scope}): {len(pairs)} URL pairs to process")

    if not pairs:
        print("Nothing to do.")
        return

    openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; CVE-reference-indexer/1.0; "
            "+https://github.com/jeffhoek/chainlit-pydanticai-postgres)"
        )
    }

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        scraped, skipped, embedded = await process_pairs(pairs, client, openai_client, anthropic_client, dsn)

    print(f"Done. scraped={scraped}, skipped={skipped}, embedded={embedded}")


if __name__ == "__main__":
    asyncio.run(main())
