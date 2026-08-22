"""Metrics wiring — names, labels and the timing helper.

These guard against the two failure modes that matter for observability: a
metric that silently stops being emitted, and a metric renamed out from under a
dashboard or alert.
"""

import time
from contextlib import contextmanager

import pytest
from prometheus_client import REGISTRY, Histogram

from horizon import metrics
from horizon.config import get_settings


def sample(name: str, **labels) -> float:
    """Current value, or 0 when the label set has never been touched."""
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


@contextmanager
def delta(name: str, **labels):
    """Yield a one-element list that ends up holding the change in a counter.

    Counters are process-global and other tests in the suite touch the same
    label sets, so asserting on absolute values passes alone and fails in a full
    run. Only the change this test caused is its own.
    """
    before = sample(name, **labels)
    box: list[float] = [0.0]
    yield box
    box[0] = sample(name, **labels) - before


# ── the names dashboards and alerts depend on ────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "horizon_llm_calls_total",
        "horizon_llm_latency_seconds",
        "horizon_articles_fetched_total",
        "horizon_articles_processed_total",
        "horizon_dedup_hits_total",
        "horizon_queue_depth",
        "horizon_extraction_latency_seconds",
        "horizon_poll_duration_seconds",
        "horizon_batch_job_duration_seconds",
        "horizon_batch_job_failures_total",
        "horizon_clusters_active",
        "horizon_signals_published_total",
        "horizon_signals_handled_total",
        "horizon_reasoning_duration_seconds",
        "horizon_delivery_attempts_total",
        "horizon_deliveries_pending",
    ],
)
def test_the_metric_is_registered(name):
    collected = {
        metric.name for metric in REGISTRY.collect()
    }
    # Counters register as `<name>` with a `_total` sample; histograms as `<name>`.
    assert name.removesuffix("_total") in collected or name in collected


def test_every_rhythm_of_the_pipeline_is_measured():
    """Spec asks for queue depth, extraction latency, dedup hit rate and LLM
    failure rate — the four numbers that say whether the pipeline is healthy."""
    assert metrics.queue_depth is not None
    assert metrics.extraction_latency is not None
    assert metrics.dedup_hits is not None
    assert metrics.llm_calls is not None


# ── labels ───────────────────────────────────────────────────────────────────


def test_llm_calls_separate_success_from_failure():
    """LLM failure rate is a ratio, so both outcomes must be counted."""
    with (
        delta("horizon_llm_calls_total", purpose="test_purpose", outcome="ok") as ok,
        delta("horizon_llm_calls_total", purpose="test_purpose", outcome="error") as bad,
    ):
        metrics.llm_calls.labels("test_purpose", "ok").inc()
        metrics.llm_calls.labels("test_purpose", "error").inc(2)
    assert (ok[0], bad[0]) == (1.0, 2.0)


def test_dedup_hits_distinguish_the_layer_and_the_kind():
    """L1 duplicates and L2 updates are different events with different meaning."""
    with (
        delta("horizon_dedup_hits_total", layer="l1", kind="duplicate") as l1,
        delta("horizon_dedup_hits_total", layer="l2", kind="update") as l2,
    ):
        metrics.dedup_hits.labels("l1", "duplicate").inc()
        metrics.dedup_hits.labels("l2", "update").inc()
    assert (l1[0], l2[0]) == (1.0, 1.0)


def test_delivery_attempts_are_counted_per_outcome():
    with delta("horizon_delivery_attempts_total", status="failed") as failed:
        for status in ("delivered", "pending", "failed", "disabled"):
            metrics.delivery_attempts.labels(status).inc()
    assert failed[0] == 1.0


def test_reasoning_is_timed_per_stage():
    """Twelve minutes a signal is only actionable if you know which stage owns it."""
    with (
        delta("horizon_reasoning_duration_seconds_count", stage="forces") as forces,
        delta("horizon_reasoning_duration_seconds_sum", stage="scenario") as scenario,
    ):
        metrics.reasoning_duration.labels("forces").observe(1.0)
        metrics.reasoning_duration.labels("scenario").observe(2.0)
    assert (forces[0], scenario[0]) == (1.0, 2.0)


# ── the timing helper ────────────────────────────────────────────────────────


def test_timed_records_elapsed_time():
    histogram = Histogram("horizon_test_timed_seconds", "fixture")
    with metrics.timed(histogram):
        time.sleep(0.02)
    assert sample("horizon_test_timed_seconds_count") == 1.0
    assert sample("horizon_test_timed_seconds_sum") >= 0.02


def test_timed_still_records_when_the_block_raises():
    """A job that blew up took time too — and that is exactly the case you want
    on the dashboard."""
    histogram = Histogram("horizon_test_timed_raises_seconds", "fixture", ["job"])
    with pytest.raises(ValueError), metrics.timed(histogram, "boom"):
        raise ValueError("boom")
    assert sample("horizon_test_timed_raises_seconds_count", job="boom") == 1.0


# ── ports ────────────────────────────────────────────────────────────────────


def test_each_service_gets_its_own_metrics_port():
    """One shared endpoint would only ever report one process's registry."""
    settings = get_settings()
    ports = [
        settings.metrics_port_poller,
        settings.metrics_port_worker,
        settings.metrics_port_batch,
        settings.metrics_port_reasoner,
    ]
    assert len(set(ports)) == len(ports)
    assert 8300 not in ports  # the API already serves its own on the app port


def test_a_busy_port_does_not_take_the_service_down(caplog):
    """Losing metrics is a monitoring problem; losing the pipeline is an outage."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        taken = sock.getsockname()[1]
        metrics.serve_metrics(taken, "test")  # must not raise
