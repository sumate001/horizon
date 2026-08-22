"""horizon-batch — RHYTHM 2, every BATCH_INTERVAL_HOURS.

Runs the three analytics jobs in order and then evaluates the threshold gates:

    A  clustering        assigns cluster_id, leaves noise unassigned
    B  trend scoring     6h windows, z-scores, trend_score  → trend_breakout
    C  weak signals      novelty + isolation + burst        → weak_signal

C depends on A (it consumes A's noise points and small clusters) and B depends on
A (it scores A's clusters), so the three run sequentially in a single job rather
than as separate schedules — that also guarantees they never overlap on the DB.

A failure in one job is logged and the next still runs: a bad clustering pass
should not also cost the run its trend scores.
"""

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from ..batch.clustering import run_clustering
from ..batch.signals import publish
from ..batch.trends import run_trend_scoring
from ..batch.weak_signals import run_weak_signal_detection
from ..config import get_settings
from ..db import session_scope
from ..logging import setup_logging
from ..metrics import (
    batch_job_duration,
    batch_job_failures,
    cluster_noise_events,
    clusters_active,
    serve_metrics,
    timed,
    weak_signal_candidates,
)
from ..models import Cluster, Score

log = logging.getLogger("horizon.batch")


async def _publish_breakouts(cluster_ids: list) -> int:
    """Announce trend breakouts, enriching each with its label and score."""
    published = 0
    for cluster_id in cluster_ids:
        async with session_scope() as session:
            cluster = await session.get(Cluster, cluster_id)
            latest = await session.scalar(
                select(Score.trend_score)
                .where(Score.cluster_id == cluster_id)
                .order_by(Score.window_start.desc())
                .limit(1)
            )
        if cluster is None:
            continue
        if await publish(
            "trend_breakout",
            cluster_id,
            title=cluster.label or "",
            trend_score=round(float(latest), 4) if latest is not None else None,
            cluster_id=str(cluster_id),
            event_count=cluster.event_count,
        ):
            published += 1
    return published


async def run_batch() -> None:
    """One full analytics pass. Never raises — the scheduler must keep its job."""
    log.info("batch run started")

    try:
        with timed(batch_job_duration, "clustering"):
            report = await run_clustering()
        clusters_active.set(report.clusters)
        cluster_noise_events.set(report.noise)
    except Exception as exc:
        batch_job_failures.labels("clustering").inc()
        log.exception("clustering failed", extra={"error": str(exc)})

    try:
        with timed(batch_job_duration, "trends"):
            trends = await run_trend_scoring()
        if trends.breakouts:
            published = await _publish_breakouts(trends.breakouts)
            log.info("trend breakouts published", extra={"count": published})
    except Exception as exc:
        batch_job_failures.labels("trends").inc()
        log.exception("trend scoring failed", extra={"error": str(exc)})

    try:
        with timed(batch_job_duration, "weak_signals"):
            weak = await run_weak_signal_detection()
        weak_signal_candidates.set(weak.candidates)
    except Exception as exc:
        batch_job_failures.labels("weak_signals").inc()
        log.exception("weak signal detection failed", extra={"error": str(exc)})

    log.info("batch run finished")


async def main() -> None:
    settings = get_settings()
    setup_logging("horizon.batch", settings.log_level)
    serve_metrics(settings.metrics_port_batch, "batch")

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_batch,
        "interval",
        hours=settings.batch_interval_hours,
        id="batch",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("batch scheduler started", extra={"interval_hours": settings.batch_interval_hours})

    await run_batch()  # don't wait a full interval for the first pass

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    scheduler.shutdown(wait=False)
    log.info("batch scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
