"""ETL orchestrator for the scheduled refresh job.

Runs the three loaders in the correct order, captures each step's output and
timing, then emails a results summary via Azure Communication Services.

Order matters: the full NVD incremental runs FIRST so the KEV-scoped loaders
(which write recent last_modified/published dates into nvd_vulnerabilities)
don't poison the high-water mark the incremental derives its start from.

Email is best-effort and optional: if the ACS_* / ETL_EMAIL_TO env vars are
unset (e.g. local runs), the email step is skipped. The process exit code
reflects the ETL outcome only — a failed email never masks a successful sync,
and a failed sync always exits non-zero so the platform records the failure.

Env:
    ACS_ENDPOINT    Azure Communication Services endpoint (https://<host>)
    ACS_SENDER      Verified sender address (e.g. donotreply@<domain>.azurecomm.net)
    ETL_EMAIL_TO    Comma-separated recipient address(es)
    AZURE_CLIENT_ID Client ID of the user-assigned managed identity (for auth)
"""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime

# (label, argv) — full NVD incremental first, then KEV catalog, then KEV-scoped NVD.
STEPS: list[tuple[str, list[str]]] = [
    ("NVD full incremental", [sys.executable, "scripts/load_nvd_full.py", "--incremental"]),
    ("KEV catalog", [sys.executable, "scripts/load_kev.py"]),
    ("NVD enrichment (KEV-scoped)", [sys.executable, "scripts/load_nvd.py"]),
]

# Lines worth surfacing in the email body without dumping the entire log.
HIGHLIGHTS = ("Done!", "Synced", "Upserted", "new CVEs", "modified", "Error", "Traceback", "Failed")


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def run_step(label: str, argv: list[str]) -> dict:
    """Run one loader, capturing combined output, exit code, and duration."""
    print(f"\n=== {label} ===", flush=True)
    started = time.time()
    proc = subprocess.run(argv, capture_output=True, text=True)
    elapsed = time.time() - started

    output = (proc.stdout or "") + (proc.stderr or "")
    print(output, end="", flush=True)  # echo to job logs

    highlights = [line for line in output.splitlines() if any(h in line for h in HIGHLIGHTS)]
    return {
        "label": label,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "elapsed": elapsed,
        "highlights": highlights[-15:],  # cap to keep the email readable
    }


def run_pipeline(steps: list[tuple[str, list[str]]], runner=run_step) -> list[dict]:
    """Run steps in order, stopping at the first failure.

    Stopping early is what protects the incremental's high-water mark: load_nvd.py
    (KEV-scoped) writes recent last_modified dates into nvd_vulnerabilities, so
    letting it run after a failed NVD full incremental would poison the mark the
    next incremental derives its start from — silently skipping everything.
    Returns one result dict per step actually run.
    """
    results: list[dict] = []
    for label, argv in steps:
        result = runner(label, argv)
        results.append(result)
        if not result["ok"]:
            break
    return results


def build_email(results: list[dict], total_elapsed: float) -> tuple[str, str]:
    """Return (subject, plain-text body) summarizing the run."""
    all_ok = all(r["ok"] for r in results)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    status = "SUCCESS" if all_ok else "FAILED"
    subject = f"[{status}] NVD/KEV ETL — {stamp}"

    lines = [f"ETL run {status} at {stamp} (total {_fmt_duration(total_elapsed)})", ""]
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        lines.append(f"[{mark}] {r['label']} ({_fmt_duration(r['elapsed'])}, exit {r['returncode']})")
        for h in r["highlights"]:
            lines.append(f"        {h}")
        lines.append("")
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    """Send the summary via Azure Communication Services (best-effort)."""
    endpoint = os.getenv("ACS_ENDPOINT")
    sender = os.getenv("ACS_SENDER")
    recipients = os.getenv("ETL_EMAIL_TO")
    if not (endpoint and sender and recipients):
        print("Email not configured (ACS_ENDPOINT/ACS_SENDER/ETL_EMAIL_TO unset) — skipping.")
        return

    try:
        from azure.communication.email import EmailClient
        from azure.identity import DefaultAzureCredential

        client = EmailClient(endpoint, DefaultAzureCredential())
        message = {
            "senderAddress": sender,
            "recipients": {"to": [{"address": a.strip()} for a in recipients.split(",") if a.strip()]},
            "content": {"subject": subject, "plainText": body},
        }
        poller = client.begin_send(message)
        result = poller.result()
        print(f"Email sent (status: {result['status']}).")
    except Exception as exc:  # never let email failure mask the ETL result
        print(f"WARNING: failed to send results email: {exc}")


def main() -> int:
    overall_start = time.time()
    results = run_pipeline(STEPS)
    total_elapsed = time.time() - overall_start

    subject, body = build_email(results, total_elapsed)
    print(f"\n{subject}\n{body}")
    send_email(subject, body)

    return 0 if results and all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
