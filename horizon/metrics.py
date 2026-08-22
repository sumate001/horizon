"""Prometheus metrics.

Each service is its own process with its own registry, so each one serves its
own `/metrics`. A single endpoint on horizon-api would only ever report the API
process's counters — the worker's dedup hit rate and the reasoner's LLM failures
live in different processes entirely and would silently read as zero.

Ports: api 8300 (on the main app), poller 9101, worker 9102, batch 9103,
reasoner 9104. `infra/prometheus.yml` has a ready scrape config.
"""

import logging
import time
from contextlib import contextmanager

from prometheus_client import Counter, Gauge, Histogram, start_http_server

log = logging.getLogger(__name__)

# ── LLM ──────────────────────────────────────────────────────────────────────

llm_calls = Counter(
    "horizon_llm_calls_total", "LLM calls by purpose and outcome", ["purpose", "outcome"]
)
llm_latency = Histogram(
    "horizon_llm_latency_seconds",
    "LLM call latency",
    ["purpose"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 45, 60, 120),
)

# ── Ingestion (poller / worker) ──────────────────────────────────────────────

articles_fetched = Counter(
    "horizon_articles_fetched_total", "Articles fetched by the poller", ["source_type"]
)
source_fetch_failures = Counter(
    "horizon_source_fetch_failures_total", "Source fetches that returned nothing", ["source"]
)
poll_duration = Histogram(
    "horizon_poll_duration_seconds",
    "Wall time of one full pass over the source registry",
    buckets=(1, 5, 10, 30, 60, 120, 300, 600),
)
articles_processed = Counter(
    "horizon_articles_processed_total",
    "Articles leaving the worker, by terminal status",
    ["status"],
)
dedup_hits = Counter(
    "horizon_dedup_hits_total", "Dedup matches by layer and kind", ["layer", "kind"]
)
queue_depth = Gauge("horizon_queue_depth", "Pending articles in the Redis queue")
extraction_latency = Histogram(
    "horizon_extraction_latency_seconds",
    "End-to-end worker time per article",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)

# ── Batch analytics ──────────────────────────────────────────────────────────

batch_job_duration = Histogram(
    "horizon_batch_job_duration_seconds",
    "Wall time of one batch job",
    ["job"],
    buckets=(1, 5, 15, 30, 60, 180, 300, 600, 1800),
)
batch_job_failures = Counter(
    "horizon_batch_job_failures_total", "Batch jobs that raised", ["job"]
)
clusters_active = Gauge("horizon_clusters_active", "Clusters in the active state")
cluster_noise_events = Gauge(
    "horizon_cluster_noise_events", "Events the last clustering run left unassigned"
)
weak_signal_candidates = Gauge(
    "horizon_weak_signal_candidates", "Candidates evaluated by the last weak signal run"
)
signals_published = Counter(
    "horizon_signals_published_total", "Signals published to the pub/sub channel", ["signal_type"]
)

# ── Reasoner ─────────────────────────────────────────────────────────────────

signals_handled = Counter(
    "horizon_signals_handled_total",
    "Signals the reasoner processed, by outcome",
    ["signal_type", "outcome"],
)
reasoning_duration = Histogram(
    "horizon_reasoning_duration_seconds",
    "Wall time of one reasoning stage",
    ["stage"],
    buckets=(5, 15, 30, 60, 180, 300, 600, 1200, 1800),
)
ahp_inconsistent = Counter(
    "horizon_ahp_inconsistent_total",
    "Force assessments whose pairwise answers breached the consistency cutoff",
)

# ── Outbound integration ─────────────────────────────────────────────────────

delivery_attempts = Counter(
    "horizon_delivery_attempts_total",
    "OSINT//DESK delivery attempts by resulting status",
    ["status"],
)
deliveries_pending = Gauge(
    "horizon_deliveries_pending", "Dispatches awaiting delivery or retry"
)


@contextmanager
def timed(histogram: Histogram, *labels: str):
    """Observe wall time into a histogram, whether or not the block raises."""
    started = time.perf_counter()
    try:
        yield
    finally:
        target = histogram.labels(*labels) if labels else histogram
        target.observe(time.perf_counter() - started)


def serve_metrics(port: int, service: str) -> None:
    """Expose this process's registry on `port`.

    Failure to bind is logged, not raised: a missing metrics endpoint is a
    monitoring problem, and taking the pipeline down over it would be worse.
    """
    try:
        start_http_server(port)
        log.info("metrics endpoint listening", extra={"service": service, "port": port})
    except OSError as exc:
        log.warning(
            "could not start metrics endpoint",
            extra={"service": service, "port": port, "error": str(exc)},
        )
