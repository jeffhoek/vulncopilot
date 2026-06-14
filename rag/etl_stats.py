"""Read and render ETL run history for the public stats page (PR 2).

`get_recent_runs` reads the `etl_runs` table (the live app's `app_readonly` role
already has SELECT, so no privilege change is needed). `render_etl_stats_html`
renders an always-on, scrollable, newest-first run-history page.

Public-exposure hardening: the per-loader `error` field is raw exception text and
can leak internal detail (paths, connection strings), so this page **never** echoes
it — it shows status, counts, and durations, and a generic note for a failed loader.
All stored text is rendered through an autoescaping Jinja2 template to prevent HTML
injection from anything that lands in the database.
"""

import json

import asyncpg
from jinja2 import Environment, select_autoescape


async def get_recent_runs(pool: asyncpg.Pool, limit: int = 50) -> list[dict]:
    """Return the most recent ETL runs, newest first (bounded by `limit`).

    The `etl_runs_run_at_idx` index keeps the ordered scan fast, and the LIMIT keeps
    the page responsive regardless of table size (no prune job needed).
    """
    rows = await pool.fetch(
        "SELECT run_at, status, total_elapsed, results FROM etl_runs ORDER BY run_at DESC LIMIT $1",
        limit,
    )
    runs: list[dict] = []
    for r in rows:
        results = r["results"]
        # asyncpg returns a JSONB column as a JSON string unless a codec is set.
        if isinstance(results, str):
            results = json.loads(results)
        runs.append(
            {
                "run_at": r["run_at"],
                "status": r["status"],
                "total_elapsed": float(r["total_elapsed"]),
                "results": results,
            }
        )
    return runs


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def _shape_loader(loader: dict) -> dict:
    """Build a public-safe view of one loader's outcome — never the raw `error`."""
    ok = bool(loader.get("ok"))
    return {
        "label": loader.get("label", ""),
        "ok": ok,
        "elapsed": _fmt_duration(loader.get("elapsed", 0) or 0),
        # The loader's own summary line is safe, descriptive text on success; on
        # failure we show a generic note rather than the raw exception string.
        "detail": (loader.get("summary") or "") if ok else "failed",
    }


def _shape_run(run: dict) -> dict:
    return {
        "run_at": run["run_at"].strftime("%Y-%m-%d %H:%M UTC"),
        "status": run["status"],
        "ok": run["status"] == "SUCCESS",
        "total_elapsed": _fmt_duration(run["total_elapsed"]),
        "loaders": [_shape_loader(loader) for loader in run["results"]],
    }


_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))

_TEMPLATE = _env.from_string(
    """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <!-- Refresh an open tab so freshly-loaded data shows up after each ETL run. -->
  <meta http-equiv="refresh" content="300">
  <title>ETL run history</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
           margin: 0; padding: 1.5rem; line-height: 1.4; }
    h1 { font-size: 1.4rem; margin: 0 0 0.25rem; }
    p.sub { margin: 0 0 1.25rem; opacity: 0.7; }
    .scroll { overflow: auto; max-height: 80vh; border: 1px solid #8884; border-radius: 8px; }
    table { border-collapse: collapse; width: 100%; font-size: 0.92rem; }
    th, td { text-align: left; padding: 0.55rem 0.8rem; border-bottom: 1px solid #8883;
             vertical-align: top; }
    th { position: sticky; top: 0; background: #8881; backdrop-filter: blur(4px); }
    .badge { display: inline-block; padding: 0.1rem 0.55rem; border-radius: 999px;
             font-size: 0.78rem; font-weight: 600; }
    .ok { background: #1a7f3722; color: #1a7f37; }
    .fail { background: #c0283322; color: #c02833; }
    ul.loaders { margin: 0; padding-left: 1.1rem; }
    ul.loaders li { margin: 0.1rem 0; }
    .empty { padding: 2rem; text-align: center; opacity: 0.7; }
    time, .dur { font-variant-numeric: tabular-nums; white-space: nowrap; }
  </style>
</head>
<body>
  <h1>ETL run history</h1>
  <p class="sub">CISA KEV &amp; NIST NVD refresh — newest first. Auto-refreshes every 5 min.</p>
  {% if runs %}
  <div class="scroll">
    <table>
      <thead>
        <tr><th>Status</th><th>Run (UTC)</th><th>Total</th><th>Loaders</th></tr>
      </thead>
      <tbody>
      {% for run in runs %}
        <tr>
          <td><span class="badge {{ 'ok' if run.ok else 'fail' }}">{{ run.status }}</span></td>
          <td><time>{{ run.run_at }}</time></td>
          <td class="dur">{{ run.total_elapsed }}</td>
          <td>
            <ul class="loaders">
            {% for loader in run.loaders %}
              <li>{{ '✓' if loader.ok else '✗' }} <strong>{{ loader.label }}</strong>
                  <span class="dur">({{ loader.elapsed }})</span>
                  {%- if loader.detail %} — {{ loader.detail }}{% endif %}</li>
            {% endfor %}
            </ul>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="empty">No ETL runs recorded yet.</div>
  {% endif %}
</body>
</html>"""
)


def render_etl_stats_html(runs: list[dict]) -> str:
    """Render the run-history page. `runs` is the output of `get_recent_runs`."""
    return _TEMPLATE.render(runs=[_shape_run(r) for r in runs])
