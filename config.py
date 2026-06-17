from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API Keys (anthropic is optional — not needed by ETL scripts)
    anthropic_api_key: str | None = None
    openai_api_key: str
    nvd_api_key: str | None = None

    # PostgreSQL Configuration
    # Use PG_DATABASE_URL (not DATABASE_URL) to avoid Chainlit auto-activating its data layer
    pg_database_url: str | None = None
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgresuser"
    pg_password: str = ""
    pg_database: str = "inventory"

    # When False, init_db() skips schema DDL and only connects/reads. Set this for
    # the live app when it uses a read-only role; schema is created by the
    # admin/ETL connection instead. See docs/supabase-readonly-role.md.
    db_init_schema: bool = True

    def get_database_dsn(self) -> str:
        if self.pg_database_url:
            return self.pg_database_url
        return f"postgresql://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_database}"

    # RAG Configuration
    top_k: int = 5
    max_history_messages: int = 50
    embedding_model: str = "text-embedding-3-small"
    llm_model: str = "anthropic:claude-haiku-4-5-20251001"
    system_prompt: str = (
        "You are a security analyst assistant with access to the CISA Known "
        "Exploited Vulnerabilities (KEV) database and NIST National "
        "Vulnerability Database (NVD).\n\n"
        "## Database Schema\n\n"
        "TABLE: kev_vulnerabilities (\n"
        "  cve_id VARCHAR(20),\n"
        "  vendor_project TEXT,\n"
        "  product TEXT,\n"
        "  vulnerability_name TEXT,\n"
        "  short_description TEXT,\n"
        "  required_action TEXT,\n"
        "  notes TEXT,\n"
        "  date_added DATE,\n"
        "  due_date DATE,\n"
        "  known_ransomware_campaign_use VARCHAR(20),\n"
        "  cwes TEXT[]\n"
        ")\n\n"
        "TABLE: nvd_vulnerabilities (\n"
        "  cve_id VARCHAR(20),\n"
        "  description TEXT,\n"
        "  cvss_v31_score NUMERIC(3,1),\n"
        "  cvss_v31_severity VARCHAR(10),\n"
        "  cvss_v31_vector TEXT,\n"
        "  cvss_v2_score NUMERIC(3,1),\n"
        "  cvss_v2_severity VARCHAR(10),\n"
        "  cwes TEXT[],\n"
        "  affected_products TEXT[],\n"
        "  reference_urls TEXT[],\n"
        "  published DATE,\n"
        "  last_modified DATE,\n"
        "  raw_json JSONB -- full NVD API response, query with -> and ->> operators\n"
        ")\n\n"
        "TABLE: cwe_definitions (\n"
        "  cwe_id VARCHAR(20),       -- e.g., 'CWE-79'\n"
        "  name TEXT,                -- human-readable weakness name\n"
        "  abstraction VARCHAR(20),  -- Pillar, Class, Base, Variant, Compound\n"
        "  description TEXT,\n"
        "  url TEXT\n"
        ")\n\n"
        "JOIN tables on cve_id to cross-reference KEV and NVD data.\n"
        "JOIN cwe_definitions using: cwe_id = ANY(nvd_vulnerabilities.cwes) "
        "or cwe_id = ANY(kev_vulnerabilities.cwes) to resolve CWE IDs to names.\n\n"
        "## Tools\n\n"
        "- **retrieve**: semantic search across both datasets. Use for "
        "conceptual questions (e.g. 'tell me about Log4j').\n"
        "- **query**: execute SQL. Use for counts, top-N, date filters, "
        "grouping, listing, JOINs across tables, and specific CVE ID lookups. "
        "For CVE ID lookups, always query BOTH kev_vulnerabilities AND "
        "nvd_vulnerabilities before concluding a CVE is not found — a CVE "
        "may exist in NVD without appearing in KEV.\n\n"
        "Answer concisely. If the answer is not in the data, say so. "
        "When the user asks a follow-up question, use the conversation history "
        "to resolve references (e.g., 'it', 'that CVE', 'the one you just described') "
        "before querying the database."
    )

    # OAuth
    oauth_github_client_id: str | None = None
    oauth_github_client_secret: str | None = None
    # oauth_google_client_id / oauth_google_client_secret (optional alternative — see Step 4)

    # Authorization
    # pydantic-settings parses list[str] env vars as JSON arrays (e.g. ALLOWED_EMAILS=["a@x.com"]),
    # the same convention as the existing ACTION_BUTTONS field — not comma-separated.
    allowed_email_domains: list[str] = []  # e.g. ["mycompany.com"]
    allowed_emails: list[str] = []  # explicit email addresses only
    allowed_logins: list[str] = []  # GitHub usernames (login field)
    open_registration: bool = False  # True = any OAuth user allowed

    # Rate Limiting
    daily_query_limit: int = 20
    # Elevated cap for admin/trusted users, keyed by stable GitHub identifier
    # (e.g. ADMIN_USER_IDENTIFIERS=["github:12345678"]). Identifiers not listed
    # get daily_query_limit. JSON-array env var, like the allow-list fields.
    admin_daily_query_limit: int = 100000
    admin_user_identifiers: list[str] = []

    # MCP Server
    mcp_api_key: str | None = None

    # Action Buttons (optional)
    action_buttons: list[str] = []

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
