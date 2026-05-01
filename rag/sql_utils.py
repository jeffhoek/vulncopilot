import re

MAX_QUERY_ROWS = 100
MAX_CELL_CHARS = 200
MAX_OUTPUT_CHARS = 20_000


def validate_sql(sql: str) -> str | None:
    """Return an error string if sql is not a SELECT, else None."""
    if not sql.strip().upper().startswith("SELECT"):
        return "Error: Only SELECT statements are permitted."
    return None


def apply_row_limit(sql: str, max_rows: int = MAX_QUERY_ROWS) -> str:
    """Cap or inject a LIMIT clause, returning the rewritten SQL."""
    limit_match = re.search(r"\bLIMIT\s+(\d+)\b", sql, re.IGNORECASE)
    if limit_match:
        if int(limit_match.group(1)) > max_rows:
            sql = sql[: limit_match.start(1)] + str(max_rows) + sql[limit_match.end(1) :]
    else:
        sql = sql.rstrip().rstrip(";") + f" LIMIT {max_rows}"
    return sql


def format_query_results(
    rows,
    max_cell_chars: int = MAX_CELL_CHARS,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> str:
    """Format asyncpg rows as a pipe-delimited table, truncating as needed."""
    headers = list(rows[0].keys())
    lines = [" | ".join(headers)]
    lines.append("-" * len(lines[0]))
    for row in rows:
        lines.append(
            " | ".join(
                s if len(s) <= max_cell_chars else s[:max_cell_chars] + "…" for s in (str(v) for v in row.values())
            )
        )
    lines.append(f"\n{len(rows)} row(s) returned.")
    result = "\n".join(lines)
    if len(result) > max_output_chars:
        result = result[:max_output_chars] + (
            "\n\n[Output truncated: result exceeded size limit. "
            "Re-query without STRING_AGG or large aggregated columns, "
            "or narrow the result set.]"
        )
    return result
