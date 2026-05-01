from rag.sql_utils import MAX_CELL_CHARS, apply_row_limit, format_query_results, validate_sql

# ---------------------------------------------------------------------------
# validate_sql
# ---------------------------------------------------------------------------


def test_validate_sql_select_is_valid():
    assert validate_sql("SELECT * FROM t") is None


def test_validate_sql_lowercase_select_is_valid():
    assert validate_sql("select * from t") is None


def test_validate_sql_leading_whitespace_is_valid():
    assert validate_sql("  SELECT * FROM t") is None


def test_validate_sql_drop_table_is_rejected():
    assert validate_sql("DROP TABLE t") is not None


def test_validate_sql_insert_is_rejected():
    assert validate_sql("INSERT INTO t VALUES (1)") is not None


def test_validate_sql_empty_string_is_rejected():
    assert validate_sql("") is not None


# ---------------------------------------------------------------------------
# apply_row_limit
# ---------------------------------------------------------------------------


def test_apply_row_limit_injects_limit_when_absent():
    assert apply_row_limit("SELECT * FROM t", 100) == "SELECT * FROM t LIMIT 100"


def test_apply_row_limit_strips_semicolon_before_injecting():
    assert apply_row_limit("SELECT * FROM t;", 100) == "SELECT * FROM t LIMIT 100"


def test_apply_row_limit_leaves_limit_unchanged_when_within_max():
    result = apply_row_limit("SELECT * FROM t LIMIT 10", 100)
    assert result == "SELECT * FROM t LIMIT 10"


def test_apply_row_limit_rewrites_limit_exceeding_default_max():
    result = apply_row_limit("SELECT * FROM t LIMIT 500", 100)
    assert "LIMIT 100" in result
    assert "LIMIT 500" not in result


def test_apply_row_limit_rewrites_limit_exceeding_custom_max():
    result = apply_row_limit("SELECT * FROM t LIMIT 500", 200)
    assert "LIMIT 200" in result
    assert "LIMIT 500" not in result


# ---------------------------------------------------------------------------
# format_query_results — helpers
# ---------------------------------------------------------------------------


class _Row(dict):
    """Minimal dict subclass that behaves like an asyncpg Record for formatting."""


def _rows(*dicts) -> list[_Row]:
    return [_Row(d) for d in dicts]


# ---------------------------------------------------------------------------
# format_query_results
# ---------------------------------------------------------------------------


def test_format_query_results_contains_headers_data_and_row_count():
    rows = _rows({"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"})
    result = format_query_results(rows)
    assert "id" in result
    assert "name" in result
    assert "Alice" in result
    assert "Bob" in result
    assert "2 row(s) returned." in result


def test_format_query_results_truncates_long_cell_values():
    long_value = "x" * (MAX_CELL_CHARS + 50)
    rows = _rows({"col": long_value})
    result = format_query_results(rows)
    assert "…" in result
    assert long_value not in result


def test_format_query_results_appends_truncation_notice_when_output_too_large():
    rows = _rows(*[{"col": "x" * 100}] * 300)
    result = format_query_results(rows, max_output_chars=1000)
    assert "[Output truncated" in result


def test_format_query_results_no_truncation_notice_when_output_within_limit():
    rows = _rows({"id": 1})
    result = format_query_results(rows)
    assert "[Output truncated" not in result
