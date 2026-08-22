"""Central settings. Every threshold and weight lives here — never hardcode one."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

CATEGORIES: tuple[str, ...] = (
    "การเมือง",
    "เศรษฐกิจ",
    "ความมั่นคง",
    "เทคโนโลยี",
    "สังคม",
    "สิ่งแวดล้อม",
    "ต่างประเทศ",
    "พลังงาน",
    "บันเทิง/กีฬา",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── LLM ──────────────────────────────────────────────────────────────────
    ollama_base_url: str = "http://100.94.37.18:11434"
    extract_model: str = "gemma4:12b"
    embed_model: str = "bge-m3"
    llm_timeout: float = 60.0

    # ── Thresholds ───────────────────────────────────────────────────────────
    dedup_minhash_t: float = 0.85
    dedup_cosine_t: float = 0.88
    dedup_window_days: int = 7
    min_cluster_size: int = 4
    temporal_weight: float = 0.15
    weak_signal_t: float = 0.65
    trend_breakout_t: float = 2.5

    weak_novelty_w: float = 0.4
    weak_isolation_w: float = 0.3
    weak_burst_w: float = 0.3

    trend_zfreq_w: float = 0.3
    trend_zvel_w: float = 0.4
    trend_zaccel_w: float = 0.3

    gate_min_credibility: float = 0.2

    # ── Integration ──────────────────────────────────────────────────────────
    osint_desk_base_url: str = ""
    osint_desk_api_key: str = ""
    horizon_api_key: str = ""

    # ── Alerting ─────────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    line_notify_token: str = ""

    # ── Infrastructure ───────────────────────────────────────────────────────
    postgres_url: str = "postgresql+asyncpg://horizon:change_me@postgres:5432/horizon"
    redis_url: str = "redis://redis:6379/0"
    queue_key: str = "horizon:articles"
    signals_channel: str = "horizon:signals"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "horizon_events"
    searxng_url: str = ""

    # ── Runtime ──────────────────────────────────────────────────────────────
    poll_interval_minutes: int = 15
    worker_concurrency: int = 2
    queue_max_depth: int = 2000
    fetch_timeout: float = 20.0
    # RSS teasers are too thin to extract from — pull the page when the entry is short
    fetch_fulltext: bool = True
    fulltext_min_chars: int = 600
    fulltext_concurrency: int = 5
    batch_interval_hours: int = 3
    dashboard_base_url: str = "http://localhost:8301"
    log_level: str = "INFO"

    @property
    def osint_desk_enabled(self) -> bool:
        return bool(self.osint_desk_base_url.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
