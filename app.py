import os

import chainlit as cl
from chainlit.server import app as fastapi_app
from fastapi.responses import HTMLResponse
from openai import AsyncOpenAI
from pydantic_ai import Agent

if os.getenv("LOGFIRE_ENABLED", "").lower() == "true":
    import logfire

    logfire.configure(scrubbing=False)
    logfire.instrument_pydantic_ai()
    logfire.instrument_openai()

from config import settings
from mcp_server.server import McpRouterMiddleware, set_mcp_context
from rag.agent import Deps, rag_agent
from rag.database import init_db
from rag.etl_stats import get_recent_runs, render_etl_stats_html
from rag.vector_store import PgVectorStore

if os.getenv("LANGFUSE_PUBLIC_KEY"):
    from langfuse import get_client

    get_client()
    Agent.instrument_all()

fastapi_app.add_middleware(McpRouterMiddleware)


@fastapi_app.get("/etl-stats", response_class=HTMLResponse)
async def etl_stats_page() -> str:
    """Public, always-on ETL run-history page.

    Mounted directly on the FastAPI app, so it bypasses Chainlit's
    password_auth_callback (which only gates the chat UI) and is reachable by
    logged-out visitors. Read-only: app_readonly's SELECT is enough.
    """
    pool = await init_db()  # idempotent — returns the existing pool if already up
    runs = await get_recent_runs(pool, limit=50)
    return render_etl_stats_html(runs)


def _prioritize_route(path: str) -> None:
    """Move a route ahead of Chainlit's SPA catch-all so it isn't shadowed.

    Chainlit registers a greedy "/{full_path:path}" route at import time. Starlette
    matches routes in registration order, so a route appended afterwards never wins —
    the catch-all serves the frontend instead and the client redirects to "/". Re-order
    our route to sit just before the catch-all.
    """
    routes = fastapi_app.router.routes
    ours = next(r for r in routes if getattr(r, "path", None) == path)
    routes.remove(ours)
    idx = next(
        (i for i, r in enumerate(routes) if getattr(r, "path", None) == "/{full_path:path}"),
        len(routes),
    )
    routes.insert(idx, ours)


_prioritize_route("/etl-stats")


@cl.on_app_startup
async def on_app_startup() -> None:
    """Initialise shared resources and inject context into the MCP server."""
    pool = await init_db()
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    set_mcp_context(pool, openai_client)


@cl.password_auth_callback
def auth_callback(username: str, password: str):
    expected_username = os.getenv("APP_USERNAME", "admin")
    expected_password = os.getenv("APP_PASSWORD")

    if not expected_password:
        return None

    if username == expected_username and password == expected_password:
        return cl.User(identifier=username)

    return None


def _quick_query_actions() -> list[cl.Action]:
    return [cl.Action(name="quick_query", label=label, payload={"query": label}) for label in settings.action_buttons]


@cl.action_callback("quick_query")
async def on_quick_query(action: cl.Action) -> None:
    query = action.payload["query"]
    deps = cl.user_session.get("deps")
    if deps is None:
        await cl.Message(content="Error: Knowledge base not initialized. Please refresh the page.").send()
        return
    history = cl.user_session.get("message_history", [])
    result = await rag_agent.run(query, deps=deps, message_history=history)
    cl.user_session.set("message_history", result.all_messages()[-settings.max_history_messages :])
    await cl.Message(content=result.output, actions=_quick_query_actions()).send()


@cl.on_chat_start
async def on_chat_start() -> None:
    """Initialize the RAG system on chat start."""
    pool = await init_db()
    vector_store = PgVectorStore(pool)
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key)

    deps = Deps(openai_client=openai_client, vector_store=vector_store)
    existing_history = cl.user_session.get("message_history")
    cl.user_session.set("deps", deps)

    if existing_history is None:
        cl.user_session.set("message_history", [])
        doc_count = await vector_store.get_document_count()
        await cl.Message(
            content=f"Ready! {doc_count} vulnerability records available.",
            actions=_quick_query_actions(),
        ).send()
    else:
        cl.user_session.set("message_history", existing_history)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Handle incoming messages."""
    deps = cl.user_session.get("deps")

    if deps is None:
        await cl.Message(content="Error: Knowledge base not initialized. Please refresh the page.").send()
        return

    history = cl.user_session.get("message_history", [])
    result = await rag_agent.run(message.content, deps=deps, message_history=history)
    cl.user_session.set("message_history", result.all_messages()[-settings.max_history_messages :])
    await cl.Message(content=result.output, actions=_quick_query_actions()).send()
