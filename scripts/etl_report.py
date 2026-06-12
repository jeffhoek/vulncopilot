"""Structured result a loader returns to the ETL orchestrator (run_etl.py).

Each loader's orchestrator entrypoint returns a LoaderReport so the results email
can render real metrics instead of grepping the loader's stdout.
"""

from dataclasses import dataclass, field


@dataclass
class LoaderReport:
    """One loader's outcome: a human one-liner plus structured counts."""

    summary: str  # e.g. "Synced 1060 CVEs (298 new, 762 modified)"
    metrics: dict[str, int] = field(default_factory=dict)
