"""Prometheus metrics shared by all services. Exposed on /metrics by horizon-api."""

from prometheus_client import Counter, Gauge, Histogram

llm_calls = Counter(
    "horizon_llm_calls_total", "LLM calls by purpose and outcome", ["purpose", "outcome"]
)
llm_latency = Histogram(
    "horizon_llm_latency_seconds",
    "LLM call latency",
    ["purpose"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 45, 60, 120),
)

articles_fetched = Counter(
    "horizon_articles_fetched_total", "Articles fetched by the poller", ["source_type"]
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
