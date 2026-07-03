# Observability

Two optional observability integrations are available: self-hosted **Langfuse** (local dev, via Compose) and cloud-hosted **Logfire** (Pydantic's hosted tracing platform).

---

## Langfuse

Self-hosted [Langfuse](https://langfuse.com/) stack running alongside the chatbot via Podman Compose.

### How It Works

Langfuse initialization in `app.py` is conditional — it only activates when `LANGFUSE_PUBLIC_KEY` is set. This means:

- **Podman Compose** (local dev): Langfuse env vars are loaded from `.env`, so tracing is enabled automatically.
- **Cloud Run** (production): No Langfuse vars are set, so the app starts cleanly without any telemetry overhead.

When active, `Agent.instrument_all()` enables OpenTelemetry instrumentation on all PydanticAI agents. Traces capture agent runs, tool calls, token counts, and latency.

### Dependencies

The `docker-compose.yaml` at the project root defines a 7-service Langfuse stack (PostgreSQL, ClickHouse, MinIO, Redis, Langfuse server, worker) plus the chatbot service.

The Python dependency `langfuse>=3.0.0` is listed in `pyproject.toml`.

### Setup

1. Add Langfuse keys to your `.env` (see `.env.example` for the template variables).

2. Start all services:

   ```bash
   podman compose up --build -d
   ```

3. Wait ~30s for Langfuse health checks to pass.

4. Open `http://localhost:3000` and log in with `admin@local.dev` / `password`.

5. Open `http://localhost:8080`, log in to the chatbot, and send a message.

6. Back in the Langfuse UI, check the **Traces** page — you should see a trace with the PydanticAI agent run including the `retrieve` tool call, token counts, and latency.

---

## Logfire

[Logfire](https://pydantic.dev/logfire) is Pydantic's hosted observability platform with first-class PydanticAI and OpenAI instrumentation.

### How It Works

Logfire initialization in `app.py` is conditional — it only activates when `LOGFIRE_ENABLED=true` is set. This means:

- **Local dev**: Set `LOGFIRE_ENABLED=true` in `.env` and authenticate once via the CLI. Credentials are stored in `~/.logfire/` and picked up automatically.
- **Production**: Set both `LOGFIRE_ENABLED=true` and `LOGFIRE_TOKEN` as environment variables.

When active, two instrumentations are enabled:

- `logfire.instrument_pydantic_ai()` — traces agent runs, tool calls, token counts, and latency
- `logfire.instrument_openai()` — traces OpenAI API calls, including embedding generation

### Setup

#### Local Dev

1. Install the Logfire CLI and authenticate:

   ```bash
   uv run logfire auth
   ```

2. Select or create a project when prompted.

3. Add to your `.env`:

   ```
   LOGFIRE_ENABLED=true
   ```

4. Start the chatbot:

   ```bash
   uv run chainlit run app.py
   ```

5. Send a message — traces appear in your [Logfire dashboard](https://logfire.pydantic.dev) within seconds.

#### Production

The app reads `LOGFIRE_ENABLED` and `LOGFIRE_TOKEN` from environment variables at startup.

**Azure App Service**: `LOGFIRE_TOKEN` is sourced from Key Vault automatically. `LOGFIRE_ENABLED` is controlled by `logfireEnabled` in `infra/parameters.dev.bicepparam` — set it to `true` there to enable. See the [Azure deployment guide](deploy-azure-app-service.md) (Step 4.1) for secret provisioning.

**EKS**: `LOGFIRE_ENABLED` is set in `k8s/configmap.yaml`. `LOGFIRE_TOKEN` is synced into the `rag-secrets` secret by the External Secrets Operator from SSM Parameter Store — put the token at `/rag/LOGFIRE_TOKEN` (see `k8s/external-secret.yaml` and the [EKS runbook](eks-runbook.md), Step 6).

**Other environments**: set these environment variables directly:

```
LOGFIRE_ENABLED=true
LOGFIRE_TOKEN=your-logfire-token
```

Retrieve the token from your Logfire project settings under **Write Tokens**.

### Dependencies

`logfire>=4.27.0` is listed in `pyproject.toml`.
