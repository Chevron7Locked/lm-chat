# SPDX-License-Identifier: Apache-2.0
"""Application configuration for lm-chat.

``LM_CHAT_SECRET`` is **required** — it is used by
``utils/encryption.py`` to derive the AES-GCM key for TOTP secrets.
An empty or absent secret raises a ``ValidationError`` at startup so the
admin sees a clear diagnostic rather than a runtime ``RuntimeError``
buried inside the first TOTP operation.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and ``.env.local``."""

    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    lm_studio_base_url: str = "http://localhost:1234"
    lm_studio_api_key: str = ""
    # Empty default: the chat UI falls back to the first loaded model from
    # LM Studio's /api/v1/models response.  Only set this env var to pin a
    # SPECIFIC model as the global default — never bake a model ID into
    # source.  (Was hardcoded to a specific model at initial setup; removed
    # 2026-05-27.)
    lm_studio_default_model: str = ""

    database_url: str = "sqlite+aiosqlite:///./lmchat.db"

    # Required — no default; must be set by admin or tests.
    lm_chat_secret: str = Field(default="")
    lm_chat_single_session: bool = True
    lm_chat_trust_forwarded_proto: bool = False
    # Inject the follow-up-suggestions directive into the
    # system prompt server-side so the model emits <!--followups:[...]-->.
    lm_chat_followups_enabled: bool = True
    lm_chat_trusted_proxy: str = ""

    lm_chat_login_rate_limit_per_min: int = Field(default=10, ge=1)
    # Per-IP cap across ALL usernames, consumed on every login request in
    # addition to (not instead of) the per-account bucket above. Closes the
    # username-rotation evasion: without this, an attacker rotating
    # `username` from a single IP gets a fresh per-account bucket every
    # time and never trips the per-account limiter. 30/min is generous for
    # a single local admin's real usage but caps enumeration from one IP.
    lm_chat_login_rate_limit_per_ip_per_min: int = Field(default=30, ge=1)
    # Session lifetime. Default 30 days: this is a self-hosted, single-admin
    # app on your own network — a 24h hard expiry (with no sliding renewal)
    # logged the operator out daily. The cookie's Max-Age is set to match so
    # the login survives browser restarts. Env-overridable.
    lm_chat_session_ttl_seconds: int = Field(default=2592000, ge=60)
    lm_chat_totp_issuer: str = Field(default="lm-chat")

    lm_chat_host: str = "127.0.0.1"
    lm_chat_port: int = Field(default=8000, ge=1, le=65535)
    lm_chat_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    cors_allow_origins: str = ""
    # Comma-separated allowed origins, e.g. "https://a.com,https://b.com".
    # Empty string = no CORS exposure.  CorsMiddleware parses to list[str].

    # Per-user cap for pinned memory insights.
    # v0.5.x used 5 (single-user); v1 defaults to 100 (multi-user).
    # Override via LM_CHAT_PINNED_INSIGHTS_CAP.
    lm_chat_pinned_insights_cap: int = Field(default=100, ge=1)

    # Auto-memory distillation (assistant-style long-term memory).
    # After a completed MAIN, non-incognito assistant turn an out-of-band
    # LLM pass extracts 0-3 durable facts about the user and stores them as
    # AUTO insights (pinned=False) recallable via recall_insights. Disable
    # with LM_CHAT_MEMORY_DISTILLATION_ENABLED=false.
    lm_chat_memory_distillation_enabled: bool = True
    # Extend auto-memory distillation to sub-session turns (/research, /code,
    # tool-mode). Off by default — sub-session turns are ephemeral and may
    # produce noisier facts than main-chat turns. Both this flag AND
    # lm_chat_memory_distillation_enabled must be True for sub-sessions to
    # distill. Enable with LM_CHAT_SUBSESSION_MEMORY_DISTILLATION_ENABLED=true.
    lm_chat_subsession_memory_distillation_enabled: bool = False
    # Per-user cap for AUTO (distilled, pinned=False) memory insights. When a
    # save would exceed the cap the oldest least-recently-used auto rows are
    # faded (state='faded') so they stop surfacing in recall. Distinct from
    # the pinned cap above — pinned insights are admin-chosen and never
    # auto-evicted.
    lm_chat_auto_memory_cap: int = Field(default=200, ge=1)

    # Streaming reaper and idle-timeout settings.
    lm_chat_reaper_finalization_timeout_sec: int = Field(default=5, ge=1)
    lm_chat_reaper_draft_max_age_hours: int = Field(default=24, ge=1)
    # Seconds with no content-bearing OR heartbeat (prompt_processing.*) event
    # before an ``upstream_stall`` frame is emitted. Generous by default: a
    # LOCAL model's prompt-processing (time-to-first-token) scales with context
    # and can take minutes on a large conversation, so a short cap would abort
    # legitimately-slow generation. Env-overridable (raise it further for very
    # large contexts / slow hardware).
    lm_chat_stream_idle_timeout_sec: int = Field(default=300, ge=1)
    # Per-user stream rate limit (POST /api/chat/stream).
    # Default: 30 streams per minute (rate=0.5/s, burst=30).
    lm_chat_stream_rate_limit_per_min: int = Field(default=30, ge=1)

    # Budget for fire-and-forget background aux model calls (auto-title,
    # compaction summary, auto-memory distillation, follow-up chips). Nothing
    # waits on these turns, so give them a generous local-first budget — the
    # only reason to bound at all is so a wedged call can't hold the
    # background-aux queue forever. Override via LM_CHAT_AUX_MODEL_TIMEOUT_SEC.
    lm_chat_aux_model_timeout_sec: float = Field(default=900.0, ge=1)
    # MCP tool-call budget. A slow local tool (or one that itself calls a
    # model) shouldn't be cut at a cloud-latency number. Aligned with
    # lm_chat_stream_idle_timeout_sec since MCP calls are on the turn path.
    # Override via LM_CHAT_MCP_TOOL_CALL_TIMEOUT_SEC.
    lm_chat_mcp_tool_call_timeout_sec: float = Field(default=300.0, ge=1)

    # Per-admin-user rate limit for admin routes.
    # Default: 30 requests per minute (rate=0.5/s, burst=30).
    lm_chat_admin_rate_limit_per_min: int = Field(default=30, ge=1)

    # OpenAPI server URL written into the emitted yaml's "servers"
    # block. Defaults to "/" (same-origin). Override via env when the
    # deployment serves the OpenAPI spec under a non-root path or for an
    # external host. Was previously
    # resolved via getattr(settings, ..., None) with no declared field.
    lm_chat_openapi_server_url: str = Field(default="/")

    # Self-consistency convergence threshold.
    # Convergence fires when the MAX pairwise cosine among SC drafts
    # exceeds this value.  Default 0.85 (Wang et al. 2022).
    # Override via LM_CHAT_SC_THRESHOLD.
    lm_chat_sc_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # Quality-mode (self-consistency / chain-of-verification) dispatch
    # watchdog. A quality turn runs several slow internal generations; the
    # streaming heartbeat deliberately keeps the idle-stall watcher quiet
    # while it runs, so a genuinely-hung quality call would otherwise block
    # the turn forever. This timeout is the upper bound after which the
    # quality task is cancelled and the turn falls back to a normal answer.
    # The default (7200 s = 2 h) is intentionally far beyond any real run so
    # it ONLY catches a true hang — legitimate deep-analysis sessions that
    # run up to an hour are unaffected. Override via
    # LM_CHAT_QUALITY_MODE_TIMEOUT_SEC.
    lm_chat_quality_mode_timeout_sec: int = Field(default=7200, ge=1)

    # RAG document upload limit (bytes). Default 50 MB.
    # Override via LM_CHAT_DOCUMENT_MAX_BYTES.
    lm_chat_document_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1)

    # RRF fusion constant (k). Canonical default is 60 per the
    # information-retrieval literature (Cormack et al. 2009). Making it
    # env-overridable lets admins tune for their workload without a
    # code change. Override via LM_CHAT_RRF_K.
    lm_chat_rrf_k: int = Field(default=60, ge=1)

    # A/B compare per-pane output-token cap.
    # A/B compare doubles inference compute per request; the cap prevents a
    # single user from saturating GPU memory via two full-context completions.
    # Override via LM_CHAT_AB_MAX_OUTPUT_TOKENS.
    lm_chat_ab_max_output_tokens: int = Field(default=32_768, ge=1)

    # Web search provider. "searxng" (default) or "ddg".
    # SearXNG is a meta-search aggregator; default is the public searx.be
    # instance. Admins running their own SearXNG instance should override
    # via LM_CHAT_SEARXNG_URL. DDG is an opt-in HTML-scrape fallback for
    # contexts where a SearXNG instance is unavailable; it is less reliable.
    lm_chat_web_search_provider: str = Field(default="searxng")
    # SearXNG instance URL. Override via LM_CHAT_SEARXNG_URL.
    # Default: https://searx.be (public instance; admins may self-host).
    lm_chat_searxng_url: str = Field(default="https://searx.be")
    # SSRF guard escape hatch.
    # Set to True (LM_CHAT_ALLOW_PRIVATE_SEARXNG=1) to allow SearXNG at a
    # private/loopback URL (e.g. http://127.0.0.1:8888/search for a local
    # docker-compose SearXNG instance).  Defaults to False.
    lm_chat_allow_private_searxng: bool = Field(
        default=False,
        description=(
            "Allow SearXNG URL targeting private/loopback IPs. "
            "Set to True only for self-hosted instances on the same host."
        ),
    )
    # Brave Search API key. When set, "brave" and "brave_llm" (LLM
    # Context — pre-extracted page chunks, same key, different endpoint)
    # become usable web_search_provider values — real keyed search APIs,
    # no HTML scraping. Free tier is ~2k queries/month; get a key at
    # https://brave.com/search/api/. Override via LM_CHAT_BRAVE_API_KEY.
    lm_chat_brave_api_key: str = Field(default="")

    # Long-operation timeout for model load / download upstream calls.
    # Model loading can take 10–120 s (7B–120B range on MLX/GGUF).
    # Download of large models can take many minutes.
    # This timeout governs the httpx request timeout for those endpoints.
    # Unload uses the standard 30 s timeout (fast operation).
    # Override via LM_CHAT_LMSTUDIO_LONG_OP_TIMEOUT_SECONDS.
    lm_chat_lmstudio_long_op_timeout_seconds: float = Field(
        default=600.0,
        ge=1.0,
        description=(
            "httpx timeout (seconds) for long-running LM Studio operations "
            "(model load and download). Default 600 s (10 min)."
        ),
    )

    # MCP integrations list.
    # LM Studio does not expose MCP server enumeration over HTTP.
    # This is the env-var fallback; the DB table takes precedence when populated.
    # The ``NoDecode`` marker bypasses Pydantic-Settings' default JSON
    # parsing for complex types; the ``_split_available_integrations``
    # validator below handles the comma-separated string form:
    #   LM_CHAT_AVAILABLE_INTEGRATIONS=mcp/searxng,mcp/filesystem
    lm_chat_available_integrations: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Admin-supplied list of MCP integration IDs "
            "(e.g. ['mcp/searxng', 'mcp/filesystem']). "
            "Default fallback when the admin DB list is empty. LM Studio does not expose MCP "
            "enumeration over HTTP."
        ),
    )
    # When True (default) the IntegrationsService falls back to reading
    # `~/.lmstudio/mcp.json` if the DB list is empty — same-host
    # deployments get zero-config MCP discovery. Set to False for
    # split-host deployments (LMChat and LM Studio on different boxes
    # — the file is meaningless) or in hermetic test environments
    # where the dev machine's mcp.json would otherwise leak into
    # fixtures.
    #
    # Scope: this flag ONLY gates IntegrationsService's file-discovery
    # tier, which feeds the native-mode composer picker's *display* of
    # LM Studio's own MCP servers (LM Studio runs those itself, host-side
    # — LMChat just surfaces the list; see app.py's `IntegrationsService`
    # construction). It does NOT affect McpHost, which is the separate,
    # Store-only execution engine for cloud/compat agentic tool use —
    # McpHost is unconditionally constructed with `config_path=None` and
    # never reads mcp.json, regardless of this setting.
    lm_chat_local_mcp_discovery_enabled: bool = True

    # When False (default) integrations discovered from `~/.lmstudio/
    # mcp.json` (tier 2) and from the env list (tier 3) are surfaced
    # to the chat composer with `enabled_by_default=False` — they are
    # DISCOVERED and available for the user to pick per-message, but a
    # fresh install (no DB rows yet) injects NO tools into a new chat
    # by default. This is the public-launch-safe posture: tool-calling
    # is never auto-armed from first boot. Explicit DB rows still carry
    # whatever `enabled_by_default` value the admin chose via `PUT
    # /api/integrations/available`, unaffected by this flag. Set True
    # to restore the old behavior where locally-registered MCP servers
    # are pre-selected by default.
    lm_chat_default_integrations_enabled_by_default: bool = False

    # Optional setup-token gating for the bootstrap-admin window.
    # When set, ``POST /api/auth/register`` requires the token (passed as
    # ``?token=...`` query parameter OR the ``X-Setup-Token`` header) UNTIL
    # the first user has registered. After the first user exists the
    # requirement automatically lifts so subsequent self-registrations work
    # normally. Empty string (the default) disables the gate.
    #
    # Use case: an admin who has just deployed a publicly reachable
    # lm-chat does not want a drive-by visitor to claim the bootstrap-admin
    # account before they do. Documented in ``docs/deployment.md``.
    lm_chat_setup_token: str = Field(
        default="",
        description=(
            "Optional setup-token required by POST /api/auth/register until "
            "the first user registers. Pass as ?token=... query or "
            "X-Setup-Token header. Empty string disables the gate."
        ),
    )

    # Incognito chat TTL + periodic purge interval.
    # ``lm_chat_incognito_ttl_seconds`` is the default lifetime of an
    # incognito chat (default: 3600 = 1 hour).  ChatService.create sets
    # ``chats.incognito_expires_at = now + ttl`` when ``incognito=1``.
    # ``lm_chat_incognito_purge_interval_seconds`` is how often the
    # background sweep wakes up to DELETE expired rows (default 300 = 5 min).
    # Privacy invariant lives in MemoryService write paths + chat_service;
    # this is just the housekeeping cadence.
    lm_chat_incognito_ttl_seconds: int = Field(default=3600, ge=60)
    lm_chat_incognito_purge_interval_seconds: int = Field(default=300, ge=10)

    # Background periodic re-probe of LM Studio's model catalog so the
    # in-memory cache stays warm without admin intervention. The lifespan
    # spawns a task that calls ModelsService.refresh() once at startup, then
    # sleeps `models_cache_refresh_interval_seconds` between probes. Manual
    # POST /api/admin/models/refresh remains available for immediate re-probe.
    # Default: 1800 (30 min) — long enough to keep upstream LM Studio chatter
    # quiet, short enough that load/unload changes surface within a single
    # coffee break. Min 60s to keep the loop sane.
    lm_chat_models_cache_refresh_interval_seconds: int = Field(
        default=1800, ge=60
    )

    # Short TTL that guards the resolve-to-loaded path against stale-loaded-set
    # data.  The loaded SET changes frequently (user/idle-TTL unloads), so the
    # resolve gate must re-probe quickly even though the broad catalog cache can
    # stay warm for 30 min.  The broad cache refresh interval above governs the
    # periodic background re-probe; this TTL triggers an on-demand re-probe
    # only inside resolve_to_loaded_or_fallback — the critical-path that
    # determines whether a streaming request uses a live instance id.
    # Default: 5 s — matches _FORCED_REPROBE_MIN_INTERVAL (the existing
    # storm-guard interval), so the on-demand path cannot probe more often
    # than a forced reprobe already can.
    # Override via LM_CHAT_LOADED_MODELS_TTL_SECONDS.
    lm_chat_loaded_models_ttl_seconds: int = Field(
        default=5,
        ge=1,
        description=(
            "TTL (seconds) for the loaded-models set inside "
            "resolve_to_loaded_or_fallback. When the cache age exceeds this "
            "value, a re-probe is triggered before resolving the wire model id. "
            "Default 5 s — keeps the resolve gate fresh without hitting LM "
            "Studio on every single streaming turn."
        ),
    )

    @field_validator("lm_chat_secret")
    @classmethod
    def _validate_secret_present(cls, v: str | None) -> str:
        """Raise if ``LM_CHAT_SECRET`` is absent or empty.

        An empty secret would silently produce deterministic (and thus
        insecure) encryption keys in ``utils/encryption.py``.  Failing
        loudly at startup surfaces misconfiguration before any TOTP
        data is at risk.

        Args:
            v: The raw field value from environment / ``.env.local``.

        Returns:
            The validated secret string.

        Raises:
            ValueError: If ``v`` is ``None`` or an empty string.
        """
        if not v:
            raise ValueError("LM_CHAT_SECRET is required and must be non-empty")
        return v


    @field_validator("lm_chat_available_integrations", mode="before")
    @classmethod
    def _split_available_integrations(cls, v: object) -> object:
        """Parse the env-var CSV form into ``list[str]``.

        Pydantic-Settings treats ``list[str]`` as a JSON-coded "complex"
        type by default, so a raw value like ``mcp/searxng,mcp/filesystem``
        is not parseable as JSON and raises ``SettingsError`` at startup.

        This validator runs in ``mode='before'`` to convert the raw env-var
        string into a list before Pydantic's standard parsing kicks in.
        Behaviour:

        * ``str``  → split on ``,`` and strip whitespace; empty string
          yields ``[]``.
        * ``list`` → pass through unchanged (already parsed).
        * other types → return as-is and let Pydantic raise the standard
          validation error.

        Args:
            v: The raw value from environment, ``.env.local``, or a
               Python literal.

        Returns:
            A ``list[str]`` when input is a string; otherwise ``v``
            unchanged.
        """
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
