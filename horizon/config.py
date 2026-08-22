"""Central settings. Every threshold and weight lives here — never hardcode one."""

from datetime import timedelta, timezone
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Storage and comparison are UTC everywhere; this is only for reading and
#: rendering Thai-language dates, which are always local time.
BANGKOK = timezone(timedelta(hours=7))

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
    # Distance added per day of separation between two events.
    #
    # Measured on real Thai news: bge-m3 cosine distance between events occupies
    # [0.13, 0.87] — mean 0.66, sd 0.07 — not the theoretical [0, 2]. The spec's
    # 0.15/day was calibrated against the theoretical range, so in practice one
    # day of separation cost 2.2 standard deviations of semantic distance and
    # clustering degenerated into "group by timestamp".
    #
    # 0.065 satisfies both ends of the real scale: 14 days apart costs 0.91,
    # past the most dissimilar pair ever observed, while one day costs about one
    # standard deviation — a nudge, not a verdict.
    temporal_weight: float = 0.065
    #: "leaf" takes the finest coherent groups; "eom" (HDBSCAN's default) merges
    #: by excess of mass and collapsed 210 events into one 195-event blob.
    cluster_selection_method: str = "leaf"
    cluster_window_days: int = 30
    #: Run-to-run label stability: centroid cosine at or above this reuses the id.
    cluster_match_t: float = 0.9
    #: Clustering builds an n×n float64 distance matrix — 6000 events ≈ 288 MB.
    max_cluster_events: int = 6000
    #: A cluster with no new events for this long goes dormant.
    cluster_dormant_days: int = 7
    weak_signal_t: float = 0.65
    trend_breakout_t: float = 2.5

    weak_novelty_w: float = 0.4
    weak_isolation_w: float = 0.3
    weak_burst_w: float = 0.3

    trend_zfreq_w: float = 0.3
    trend_zvel_w: float = 0.4
    trend_zaccel_w: float = 0.3

    gate_min_credibility: float = 0.2

    # ── Reasoner ─────────────────────────────────────────────────────────────
    #: Rivals the triggered cluster is compared against. Cost is quadratic:
    #: n=1+ahp_top_clusters alternatives means n(n−1)/2 LLM calls per force.
    ahp_top_clusters: int = 5
    #: Cluster event summaries fed to the scenario prompt, newest first.
    scenario_event_cap: int = 50
    scenario_trend_windows: int = 10
    scenario_related_clusters: int = 3
    #: Skip anything already reasoned about this recently — the batch republishes
    #: the same cluster every run until its score falls back under threshold.
    reasoner_cooldown_hours: int = 6

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
    qdrant_centroid_collection: str = "horizon_centroids"
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
