# PostgreSQL Hosting Options

The full NVD dataset (~250k CVEs) with pgvector embeddings requires ~3.5-5.5 GB of storage, which exceeds most free tiers. This document summarizes hosting options evaluated in March 2026.

## Storage breakdown (estimated, ~250k CVEs)

| Component | Per row | Total |
|---|---|---|
| Embeddings (1536 x float32) | ~6 KB | ~1.5 GB |
| `raw_json` (full CVE JSON) | ~2-5 KB avg | ~0.5-1.2 GB |
| Text columns + content | ~1-2 KB | ~250-500 MB |
| HNSW index | overhead | ~1-2 GB |
| GIN indexes on jsonb | | ~200-500 MB |
| **Total** | | **~3.5-5.5 GB** |

## Cloud-managed PostgreSQL

| Service | Instance | Monthly cost | Notes |
|---|---|---|---|
| Timescale Cloud (current) | Base plan | ~$30 | Native pgvector, 25 GB storage. Free tier limited to 750 MB per service |
| Neon | Launch plan | $19 | 10 GB storage, pgvector supported |
| Supabase | Pro plan | $25 | 8 GB storage, pgvector supported |
| **AWS RDS** (db.t4g.micro) | 1 vCPU, 1 GB RAM, 20 GB gp3 | ~$13-15 | pgvector supported on PostgreSQL 15.4+. **12-month free tier eligible** |
| AWS RDS (db.t4g.small) | 2 vCPU, 2 GB RAM, 20 GB gp3 | ~$30 | More headroom for HNSW index builds |
| Aurora Serverless v2 | Pay per I/O | ~$50+ | Overkill, unpredictable costs with bulk loads |

### RDS notes

- pgvector is a supported extension as of RDS PostgreSQL 15.4+
- 1 GB RAM (t4g.micro) is tight for HNSW index builds on 250k vectors — may need to tune `maintenance_work_mem` or temporarily use t4g.small during backfill then downsize
- Automated backups, patching, point-in-time recovery included
- Free tier covers the instance for 12 months; storage beyond 20 GB is ~$0.10/GB/mo

## Self-hosted VM

| Provider | Instance | Specs | Monthly cost | Notes |
|---|---|---|---|---|
| **Oracle Cloud** (Ampere A1) | VM.Standard.A1.Flex | 4 vCPU, 24 GB RAM | **Always free** | Generous permanent free tier, ARM |
| Hetzner (CPX11) | Shared | 2 vCPU, 2 GB RAM | $4.50 | Cheapest paid option |
| AWS EC2 (t4g.micro) | Burstable | 1 vCPU, 1 GB RAM | ~$6 + EBS | 12-month free tier |
| AWS EC2 (t4g.small) | Burstable | 2 vCPU, 2 GB RAM | ~$12 + EBS | Better for HNSW builds |
| Azure (B1s) | Burstable | 1 vCPU, 1 GB RAM | ~$7.50 + disk | 750hr/mo free for 12 months |
| Azure (B2s) | Burstable | 2 vCPU, 4 GB RAM | ~$30 | Comfortable for pgvector |

### Self-hosted trade-offs

**Pros:**
- Full control over PostgreSQL config (`shared_buffers`, `maintenance_work_mem`, `work_mem`)
- No storage limits beyond disk size
- Oracle Cloud free tier gives 24 GB RAM permanently — luxurious for this workload

**Cons:**
- You manage backups (cron + `pg_dump` to S3/blob storage)
- You manage OS and PostgreSQL patching
- Need to install and maintain pgvector extension
- Run PostgreSQL under systemd for process management

### Self-hosted setup (high-level)

1. Provision VM (Ubuntu 22.04+ or Amazon Linux 2023)
2. Install PostgreSQL 16+ and pgvector from packages
3. Configure `postgresql.conf` (tune memory for available RAM)
4. Enable and start via systemd
5. Set up firewall rules (restrict port 5432 to app server IPs)
6. Set up automated backups (cron + `pg_dump` to object storage)
7. Update `PG_DATABASE_URL` in app `.env` to point at the VM

## Data size reduction options

If storage is the primary constraint, these can reduce the footprint:

- **Drop `raw_json` column** — saves ~0.5-1.2 GB. The useful fields are already extracted into dedicated columns. Raw JSON can be re-fetched from NVD API if needed.
- **Reduce embedding dimensions** — `text-embedding-3-small` supports `dimensions` parameter (e.g., 512 instead of 1536). Cuts embedding + index storage by ~3x. Requires regenerating all embeddings and updating the schema.

## Recommendations

1. **Cheapest long-term:** Oracle Cloud free tier VM — 24 GB RAM, always free, self-managed
2. **Least operational burden:** AWS RDS db.t4g.micro — managed service, free for 12 months, ~$15/mo after
3. **Current:** Timescale Cloud base plan — ~$30/mo, no migration needed
