from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_local_env(path: Path = Path(".env"), *, override: bool = False) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    db_path: Path
    issuer_feeds_path: Path
    documents_path: Path
    http_timeout: float
    min_request_interval: float
    user_agent: str
    document_allowed_hosts: tuple[str, ...]
    document_max_bytes: int
    extraction_max_input_chars: int
    extraction_max_report_pages: int
    action_timeout: float
    extraction_hard_timeout: float
    worker_poll_interval: float
    mfn_parallel_channels: int
    comment_repair_enabled: bool = False
    deepseek_api_key: str | None = field(default=None, repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_flash_model: str = "deepseek-v4-flash"
    deepseek_flash_thinking: bool = False
    deepseek_flash_max_output_tokens: int = 4096
    deepseek_flash_connect_timeout_seconds: float = 10.0
    deepseek_flash_read_timeout_seconds: float = 120.0
    deepseek_flash_hard_timeout_seconds: float = 180.0
    deepseek_flash_max_concurrency: int = 4
    deepseek_pro_model: str = "deepseek-v4-flash-vision-exp"
    deepseek_pro_thinking: bool = True
    deepseek_pro_reasoning: str = "high"
    deepseek_pro_max_output_tokens: int = 8192
    deepseek_pro_connect_timeout_seconds: float = 10.0
    deepseek_pro_read_timeout_seconds: float = 300.0
    deepseek_pro_hard_timeout_seconds: float = 360.0
    deepseek_pro_max_concurrency: int = 2
    deepseek_pro_extraction_max_output_tokens: int = 16000
    extraction_max_queue: int = 32
    source_fetch_concurrency: int = 4
    source_fetch_max_queue: int = 32
    source_fetch_timeout: float = 120.0
    document_fetch_retries: int = 2
    document_fetch_retry_backoff: float = 0.5
    analysis_worker_threads: int = 2
    auto_analysis_enabled: bool = True
    auto_analysis_interval_seconds: int = 60
    auto_analysis_batch_size: int = 5
    auto_analysis_max_attempts: int = 3
    auto_analysis_queue_target: int = 30
    deepseek_strict_tools_enabled: bool = False
    ai_provider_healthcheck_enabled: bool = True
    ai_cost_tracking_enabled: bool = True
    private_data_external_egress_enabled: bool = False
    transaction_editorial_enabled: bool = True
    transaction_editorial_background_enabled: bool = False
    transaction_audit_enabled: bool = True
    transaction_audit_interval_seconds: int = 86_400
    transaction_audit_max_candidates: int = 25
    research_enabled: bool = False
    research_ddgs_primary_backend: str = "google"
    research_ddgs_fallback_backends: tuple[str, ...] = ("duckduckgo", "bing")
    research_region: str = "se-sv"
    research_safesearch: str = "moderate"
    research_timeout_seconds: float = 8.0
    research_max_results: int = 5
    mfn_monitor_enabled: bool = True
    mfn_monitor_interval_seconds: int = 3600
    git_sha: str = "dev"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_local_env()
        settings = cls(
            db_path=Path(os.getenv("SCREENER_DB_PATH", "data/screener.db")),
            issuer_feeds_path=Path(
                os.getenv(
                    "SCREENER_ISSUER_FEEDS_PATH", "config/issuer_feeds.json"
                )
            ),
            documents_path=Path(
                os.getenv("SCREENER_DOCUMENTS_PATH", "data/documents")
            ),
            http_timeout=float(os.getenv("SCREENER_HTTP_TIMEOUT", "30")),
            min_request_interval=float(
                os.getenv("SCREENER_HTTP_MIN_INTERVAL", "0.75")
            ),
            user_agent=os.getenv(
                "SCREENER_USER_AGENT", "LundbergsCapital-Screener/0.1"
            ),
            document_allowed_hosts=tuple(
                host.strip().casefold()
                for host in os.getenv(
                    "SCREENER_DOCUMENT_ALLOWED_HOSTS",
                    (
                        "storage.mfn.se,www.mfn.se,mfn.se,"
                        "mb.cision.com,cdn.bequoted.com,"
                        "attachment.news.eu.nasdaq.com,"
                        "view.news.eu.nasdaq.com"
                    ),
                ).split(",")
                if host.strip()
            ),
            document_max_bytes=int(
                os.getenv("SCREENER_DOCUMENT_MAX_BYTES", "30000000")
            ),
            extraction_max_input_chars=int(
                os.getenv("SCREENER_EXTRACTION_MAX_INPUT_CHARS", "120000")
            ),
            extraction_max_report_pages=int(
                os.getenv("SCREENER_EXTRACTION_MAX_REPORT_PAGES", "10")
            ),
            action_timeout=float(
                os.getenv("SCREENER_ACTION_TIMEOUT", "300")
            ),
            extraction_hard_timeout=float(
                os.getenv("SCREENER_EXTRACTION_HARD_TIMEOUT", "240")
            ),
            worker_poll_interval=float(
                os.getenv("SCREENER_WORKER_POLL_INTERVAL", "0.5")
            ),
            mfn_parallel_channels=max(
                1,
                min(
                    8,
                    int(os.getenv("SCREENER_MFN_PARALLEL_CHANNELS", "4")),
                ),
            ),
            comment_repair_enabled=(
                os.getenv(
                    "SCREENER_COMMENT_REPAIR_ENABLED",
                    "false",
                ).casefold()
                in {"1", "true", "yes", "on"}
            ),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            deepseek_flash_model=os.getenv(
                "DEEPSEEK_FLASH_MODEL", "deepseek-v4-flash"
            ),
            deepseek_flash_thinking=_env_flag(
                "DEEPSEEK_FLASH_THINKING", default=False
            ),
            deepseek_flash_max_output_tokens=int(
                os.getenv("DEEPSEEK_FLASH_MAX_OUTPUT_TOKENS", "4096")
            ),
            deepseek_flash_connect_timeout_seconds=float(
                os.getenv("DEEPSEEK_FLASH_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            deepseek_flash_read_timeout_seconds=float(
                os.getenv("DEEPSEEK_FLASH_READ_TIMEOUT_SECONDS", "120")
            ),
            deepseek_flash_hard_timeout_seconds=float(
                os.getenv("DEEPSEEK_FLASH_HARD_TIMEOUT_SECONDS", "180")
            ),
            deepseek_flash_max_concurrency=max(
                1,
                int(os.getenv("DEEPSEEK_FLASH_MAX_CONCURRENCY", "4")),
            ),
            deepseek_pro_model=os.getenv(
                "DEEPSEEK_PRO_MODEL", "deepseek-v4-flash-vision-exp"
            ),
            deepseek_pro_thinking=_env_flag(
                "DEEPSEEK_PRO_THINKING", default=True
            ),
            deepseek_pro_reasoning=os.getenv(
                "DEEPSEEK_PRO_REASONING", "high"
            ).casefold(),
            deepseek_pro_max_output_tokens=int(
                os.getenv("DEEPSEEK_PRO_MAX_OUTPUT_TOKENS", "8192")
            ),
            deepseek_pro_connect_timeout_seconds=float(
                os.getenv("DEEPSEEK_PRO_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            deepseek_pro_read_timeout_seconds=float(
                os.getenv("DEEPSEEK_PRO_READ_TIMEOUT_SECONDS", "300")
            ),
            deepseek_pro_hard_timeout_seconds=float(
                os.getenv("DEEPSEEK_PRO_HARD_TIMEOUT_SECONDS", "360")
            ),
            deepseek_pro_max_concurrency=max(
                1,
                int(os.getenv("DEEPSEEK_PRO_MAX_CONCURRENCY", "2")),
            ),
            deepseek_pro_extraction_max_output_tokens=max(
                1024,
                int(os.getenv("DEEPSEEK_PRO_EXTRACTION_MAX_OUTPUT_TOKENS", "16000")),
            ),
            extraction_max_queue=max(
                1,
                int(os.getenv("SCREENER_EXTRACTION_MAX_QUEUE", "32")),
            ),
            source_fetch_concurrency=max(
                1,
                min(
                    8,
                    int(os.getenv("SCREENER_SOURCE_FETCH_CONCURRENCY", "4")),
                ),
            ),
            source_fetch_max_queue=max(
                1,
                int(os.getenv("SCREENER_SOURCE_FETCH_MAX_QUEUE", "32")),
            ),
            source_fetch_timeout=float(
                os.getenv("SCREENER_SOURCE_FETCH_TIMEOUT", "120")
            ),
            document_fetch_retries=max(
                0,
                int(os.getenv("SCREENER_DOCUMENT_FETCH_RETRIES", "2")),
            ),
            document_fetch_retry_backoff=float(
                os.getenv("SCREENER_DOCUMENT_FETCH_RETRY_BACKOFF", "0.5")
            ),
            analysis_worker_threads=max(
                1,
                min(
                    4,
                    int(os.getenv("SCREENER_ANALYSIS_WORKER_THREADS", "2")),
                ),
            ),
            auto_analysis_enabled=_env_flag(
                "SCREENER_AUTO_ANALYSIS_ENABLED", default=True
            ),
            auto_analysis_interval_seconds=max(
                10,
                min(
                    3600,
                    int(os.getenv("SCREENER_AUTO_ANALYSIS_INTERVAL", "60")),
                ),
            ),
            auto_analysis_batch_size=max(
                1,
                min(
                    50,
                    int(os.getenv("SCREENER_AUTO_ANALYSIS_BATCH_SIZE", "5")),
                ),
            ),
            auto_analysis_max_attempts=max(
                1,
                min(
                    10,
                    int(os.getenv("SCREENER_AUTO_ANALYSIS_MAX_ATTEMPTS", "3")),
                ),
            ),
            auto_analysis_queue_target=max(
                1,
                min(
                    50,
                    int(os.getenv("SCREENER_AUTO_ANALYSIS_QUEUE_TARGET", "30")),
                ),
            ),
            deepseek_strict_tools_enabled=_env_flag(
                "DEEPSEEK_STRICT_TOOLS_ENABLED", default=False
            ),
            ai_provider_healthcheck_enabled=_env_flag(
                "AI_PROVIDER_HEALTHCHECK_ENABLED", default=True
            ),
            ai_cost_tracking_enabled=_env_flag(
                "AI_COST_TRACKING_ENABLED", default=True
            ),
            private_data_external_egress_enabled=_env_flag(
                "PRIVATE_DATA_EXTERNAL_EGRESS_ENABLED", default=False
            ),
            transaction_editorial_enabled=_env_flag(
                "SCREENER_TRANSACTION_EDITORIAL_ENABLED", default=True
            ),
            transaction_editorial_background_enabled=_env_flag(
                "SCREENER_TRANSACTION_EDITORIAL_BACKGROUND_ENABLED", default=False
            ),
            transaction_audit_enabled=_env_flag(
                "SCREENER_TRANSACTION_AUDIT_ENABLED", default=True
            ),
            transaction_audit_interval_seconds=max(
                3_600,
                min(
                    86_400,
                    int(os.getenv("SCREENER_TRANSACTION_AUDIT_INTERVAL", "86400")),
                ),
            ),
            transaction_audit_max_candidates=max(
                1,
                min(
                    100,
                    int(os.getenv("SCREENER_TRANSACTION_AUDIT_MAX_CANDIDATES", "25")),
                ),
            ),
            research_enabled=_env_flag("SCREENER_RESEARCH_ENABLED", default=False),
            research_ddgs_primary_backend=os.getenv("SCREENER_RESEARCH_DDGS_PRIMARY_BACKEND", "google").strip().casefold(),
            research_ddgs_fallback_backends=tuple(item.strip().casefold() for item in os.getenv("SCREENER_RESEARCH_DDGS_FALLBACK_BACKENDS", "duckduckgo,bing").split(",") if item.strip()),
            research_region=os.getenv("SCREENER_RESEARCH_REGION", "se-sv").strip().casefold(),
            research_safesearch=os.getenv("SCREENER_RESEARCH_SAFESEARCH", "moderate").strip().casefold(),
            research_timeout_seconds=max(
                1.0, min(20.0, float(os.getenv("SCREENER_RESEARCH_TIMEOUT", "8")))
            ),
            research_max_results=max(
                1, min(10, int(os.getenv("SCREENER_RESEARCH_MAX_RESULTS", "5")))
            ),
            mfn_monitor_enabled=_env_flag(
                "SCREENER_MFN_MONITOR_ENABLED", default=True
            ),
            mfn_monitor_interval_seconds=max(
                300,
                min(
                    86_400,
                    int(os.getenv("SCREENER_MFN_MONITOR_INTERVAL", "3600")),
                ),
            ),
            git_sha=os.getenv("GIT_SHA", "dev"),
        )
        _validate_timeout_order(settings)
        return settings


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _validate_timeout_order(settings: Settings) -> None:
    timeouts = (
        ("SCREENER_HTTP_TIMEOUT", settings.http_timeout),
        ("SCREENER_EXTRACTION_HARD_TIMEOUT", settings.extraction_hard_timeout),
        ("SCREENER_ACTION_TIMEOUT", settings.action_timeout),
    )
    if any(value <= 0 for _, value in timeouts):
        raise ValueError("Alla timeoutvärden måste vara större än noll")
    for (left_name, left), (right_name, right) in zip(
        timeouts,
        timeouts[1:],
    ):
        if left >= right:
            raise ValueError(
                f"{left_name} måste vara lägre än {right_name}"
            )
    if settings.deepseek_api_key:
        for prefix, connect, read, hard in (
            (
                "DEEPSEEK_FLASH",
                settings.deepseek_flash_connect_timeout_seconds,
                settings.deepseek_flash_read_timeout_seconds,
                settings.deepseek_flash_hard_timeout_seconds,
            ),
            (
                "DEEPSEEK_PRO",
                settings.deepseek_pro_connect_timeout_seconds,
                settings.deepseek_pro_read_timeout_seconds,
                settings.deepseek_pro_hard_timeout_seconds,
            ),
        ):
            if not (0 < connect <= read < hard):
                raise ValueError(
                    f"{prefix}_CONNECT/READ/HARD_TIMEOUT_SECONDS måste vara "
                    "positiva och stigande (connect <= read < hard)"
                )
