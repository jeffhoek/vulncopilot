"""Read-only /admin dashboard: per-user usage and estimated LLM cost (PR 3).

Protected by HTTP Basic Auth rather than a bearer token: browsers don't set an
`Authorization` header on plain navigation, so a bearer scheme would 403 every
in-browser visit. Basic Auth makes the browser show a native credential prompt,
so the page is reachable with no frontend JavaScript. The username is ignored;
only the password (``settings.admin_secret``) is checked, with
``secrets.compare_digest`` to avoid leaking the secret via timing.

**HTTPS is required** — Basic Auth sends the secret as base64 plaintext. Never
expose /admin over plain HTTP. See docs/public-access-setup.md.
"""

import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from config import settings
from rag.database import get_pool
from rag.usage import get_usage_stats

security = HTTPBasic()
# Resolve the template dir relative to this file, not the process CWD, so the
# dashboard renders regardless of where `chainlit run` is launched from.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))  # autoescapes .html


async def admin_dashboard(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),  # noqa: B008 — FastAPI dependency idiom
):
    """Render the usage table after validating the HTTP Basic password."""
    # compare_digest on bytes; the username is intentionally not checked.
    ok = secrets.compare_digest(credentials.password.encode(), settings.admin_secret.encode())
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Forbidden",
            headers={"WWW-Authenticate": 'Basic realm="Admin"'},
        )

    rows = await get_usage_stats(
        get_pool(),
        settings.llm_input_cost_per_million,
        settings.llm_output_cost_per_million,
    )
    return templates.TemplateResponse(request, "dashboard.html", {"rows": rows})
