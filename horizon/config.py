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
    entity_model: str = "gemma4:12b"
    beat_model: str = "gemma4:12b"
    #: How far back the beat matcher looks on each batch run. Wider than the
    #: batch interval so a run that fails or is skipped does not leave a hole in
    #: coverage — `beat_matches` has a unique constraint, so overlap re-reads
    #: rather than re-sends.
    beat_lookback_hours: int = 12
    #: Events scored below this are not offered to a beat at all. Not a quality
    #: judgement — a floor that keeps the matcher off the long tail of trivia,
    #: which is most of the corpus and none of what a beat is for.
    beat_min_triage_total: float = 4.0
    #: Off until beats exist to match against. Turning it on with an empty
    #: `beats` table costs nothing and does nothing.
    beat_matching_enabled: bool = True
    #: Off by default. It adds one LLM call per event, and a half-populated
    #: entity store is worse than none: downstream code would read "never seen
    #: before" when the truth is "not resolved yet".
    entity_resolution_enabled: bool = False
    #: Below this a resolution is still written, but flagged for a human.
    #: Merging two people wrongly writes false history into a case file, so this
    #: is set to be cautious rather than to keep the queue quiet.
    entity_review_threshold: float = 0.75
    #: Wikimedia asks automated clients to identify themselves with a contact
    #: address; an anonymous agent gets rate limited harder or blocked.
    #: A real, reachable URL — the placeholder here was "https://github.com/"
    #: with no repository after it, which identifies nobody and is the kind of
    #: agent the policy exists to refuse.
    wikidata_user_agent: str = (
        "horizon-newsroom/0.1 (https://github.com/sumate001/horizon; contact via repo issues)"
    )
    wikidata_enabled: bool = False
    #: Below this the Q-number is recorded but the entity goes to the review
    #: queue. A wrong identifier is permanent and invisible — it travels to
    #: OSINT//DESK and to every export — so this sits higher than the threshold
    #: for merging names.
    wikidata_min_confidence: float = 0.8

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
    #: Measured, not inherited. At 0.65 the detector produced nothing for three
    #: days while reporting 994 candidates — and it was not being strict, it was
    #: unreachable: the best candidate in the whole corpus scored 0.364, because
    #: novelty tops out near 0.55 (bge-m3 cosine on Thai news never spans the
    #: theoretical range) and burst is 0 for the single events that are 99% of
    #: the field. The same miscalibration as `temporal_weight` above, from the
    #: same cause — a constant chosen against a range the data never occupies.
    #:
    #: The binding filter is now "must be growing" (see batch/weak_signals.py);
    #: this is a floor beneath it. The seven genuinely emerging candidates on
    #: three days of data scored 0.225–0.317, so 0.20 lets them through while
    #: still excluding a degenerate one.
    weak_signal_t: float = 0.20

    #: Measured, deliberately NOT retuned — the stored distribution and the one
    #: the detector can actually see are two different distributions, and only
    #: the second one is a basis for this number.
    #:
    #: Across 136,197 scored windows: median 0, p95 0.253, p99 5.23, max 66.9,
    #: and 3,172 windows at or above 2.5. That reads like a reachable threshold
    #: and is not, for two reasons. 2,714 of those windows (86%) belong to
    #: clusters that have since gone dormant, which run_trend_scoring does not
    #: consider at all. And `_persist` upserts every window on every run, so a
    #: stored score is the value computed in hindsight, with the window complete
    #: and later windows surrounding it.
    #:
    #: The live check reads `scores[-1]`, the window still being filled. Over
    #: the 97 clusters past their provisional period, that window scored max
    #: 1.434, median -0.115, and reached 2.5 exactly zero times. So the live
    #: ceiling sits below the threshold — one breakout in three days is that,
    #: not a quiet news week.
    #:
    #: Lowering it needs the live-window distribution over time, not this one
    #: snapshot: how many breakouts per day a given value sends to the newsroom
    #: is an editorial quantity, and a number picked off a single reading is the
    #: guess this comment exists to prevent. Evaluating a completed window
    #: instead was tried and measured worse (live max 1.434 → 0.942): a surge
    #: shows up in the window still being filled, which is the point of it.
    trend_breakout_t: float = 2.5

    weak_novelty_w: float = 0.4
    weak_isolation_w: float = 0.3
    weak_burst_w: float = 0.3

    trend_zfreq_w: float = 0.3
    trend_zvel_w: float = 0.4
    trend_zaccel_w: float = 0.3

    gate_min_credibility: float = 0.2

    # ── Editorial triage (tunable) ───────────────────────────────────────────
    # How hard sensitivity lifts the score: total = mean(6 dims) × (1 + s × k).
    #
    # OSINT//DESK used k = 0.1, which makes the multiplier run 1.0–2.0 and
    # saturates the scale: measured on the first 20 real events, half hit the
    # cap of 10 and 65% came out PRIORITY, which is the same as having no
    # verdict at all. At 0.03 the multiplier tops out at 1.3 and sensitivity
    # still ranks a story up without swamping the other five dimensions.
    #
    # Tune against real data with GET /api/v1/triage/simulate, then apply with
    # POST /api/v1/triage/rescore — no LLM calls needed, the dimension scores
    # are already stored.
    triage_sensitivity_coefficient: float = 0.03
    triage_priority_total: float = 7.5
    triage_priority_urgency: float = 9.0
    triage_fasttrack_impact: float = 8.0
    triage_fasttrack_reliability: float = 7.0
    triage_investigate_total: float = 5.5

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

    # ── Observability ────────────────────────────────────────────────────────
    # One port per service: every process keeps its own Prometheus registry, so
    # a single shared endpoint would report only that process's counters.
    metrics_port_poller: int = 9101
    metrics_port_worker: int = 9102
    metrics_port_batch: int = 9103
    metrics_port_reasoner: int = 9104
    #: How often the worker refreshes the queue-depth gauge.
    queue_gauge_interval: int = 15

    @property
    def osint_desk_enabled(self) -> bool:
        return bool(self.osint_desk_base_url.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
