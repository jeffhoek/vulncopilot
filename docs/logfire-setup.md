# Logfire Observability

[Logfire](https://pydantic.dev/logfire) is Pydantic's hosted observability platform with first-class PydanticAI and OpenAI instrumentation.

## How It Works

Logfire initialization in `app.py` is conditional — it only activates when `LOGFIRE_ENABLED=true` is set. This means:

- **Local dev**: Set `LOGFIRE_ENABLED=true` in `.env` and authenticate once via the CLI. Credentials are stored in `~/.logfire/` and picked up automatically.
- **Production**: Set both `LOGFIRE_ENABLED=true` and `LOGFIRE_TOKEN` as environment variables.

When active, three instrumentations are enabled:

- `logfire.instrument_pydantic_ai()` — traces agent runs, tool calls, token counts, and latency
- `logfire.instrument_openai()` — traces OpenAI API calls, including embedding generation

## Setup

### Local Dev

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

### Production

Set these environment variables in your deployment:

```
LOGFIRE_ENABLED=true
LOGFIRE_TOKEN=your-logfire-token
```

Retrieve the token from your Logfire project settings under **API Tokens**.

## Dependencies

`logfire>=4.27.0` is listed in `pyproject.toml`.
