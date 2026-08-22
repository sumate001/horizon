"""Step 8 — trend scoring (batch job B).

Pure statistics, no LLM. Per active cluster, per 6-hour window:

    frequency    Σ(source_count × credibility_weight) of events new in the window
    velocity     Δfrequency between consecutive windows
    acceleration Δvelocity

Each series is smoothed with an EWMA (span 6 windows ≈ 36 h), then z-scored
against that cluster's own trailing 28 days — a cluster is loud or quiet
relative to its own baseline, never relative to other clusters.

    trend_score = 0.3·z_freq + 0.4·z_vel + 0.3·z_accel

A cluster with less than 14 days of history is `provisional`: its scores are
stored and shown, but breakout publishing is suppressed, because a z-score over
a handful of windows says more about the sample size than the world.

The maths lives in free functions so the unit tests can pin exact values without
a database.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..config import get_settings
from ..db import session_scope
from ..models import Cluster, Event, Score
from ..pipeline.vectors import utcnow

log = logging.getLogger("horizon.batch.trends")

WINDOW_HOURS = 6
EWMA_SPAN = 6
HISTORY_DAYS = 28
PROVISIONAL_DAYS = 14
WINDOWS_PER_DAY = 24 // WINDOW_HOURS


@dataclass
class WindowScore:
    window_start: datetime
    window_end: datetime
    frequency: float
    velocity: float
    acceleration: float
    z_frequency: float | None
    z_velocity: float | None
    z_acceleration: float | None
    trend_score: float | None


@dataclass
class TrendReport:
    clusters: int = 0
    windows_written: int = 0
    breakouts: list[uuid.UUID] | None = None
    provisional: int = 0

    def as_log(self) -> dict:
        return {
            "clusters": self.clusters,
            "windows_written": self.windows_written,
            "breakouts": len(self.breakouts or []),
            "provisional": self.provisional,
        }


# ── maths ────────────────────────────────────────────────────────────────────


def floor_window(moment: datetime, hours: int = WINDOW_HOURS) -> datetime:
    """Snap a timestamp down to the start of its fixed 6-hour window (UTC)."""
    return moment.replace(
        hour=(moment.hour // hours) * hours, minute=0, second=0, microsecond=0
    )


def ewma(values: list[float] | np.ndarray, span: int = EWMA_SPAN) -> np.ndarray:
    """Exponentially weighted moving average, pandas' `adjust=True` convention.

    The adjusted form is unbiased at the start of a series, which matters here
    because a young cluster has only a handful of windows.
    """
    series = np.asarray(values, dtype=np.float64)
    if series.size == 0:
        return series
    alpha = 2.0 / (span + 1.0)
    weights = (1.0 - alpha) ** np.arange(series.size)
    out = np.empty_like(series)
    for i in range(series.size):
        window = series[: i + 1][::-1]
        w = weights[: i + 1]
        out[i] = float(np.dot(window, w) / w.sum())
    return out


def zscore(series: np.ndarray, index: int) -> float | None:
    """Z of `series[index]` against everything strictly before it.

    None until there are at least two prior points, and 0.0 for a flat history —
    a constant series is not anomalous, it is just constant.
    """
    history = series[:index]
    if history.size < 2:
        return None
    std = float(history.std(ddof=1))
    if std < 1e-9:
        return 0.0
    return float((series[index] - history.mean()) / std)


def compute_series(frequencies: list[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Smoothed frequency, velocity and acceleration for a window series.

    Velocity is the first difference of the smoothed frequency and acceleration
    the second, each padded with a leading 0.0 so all three align to the same
    windows.
    """
    smooth_freq = ewma(frequencies)
    velocity = np.concatenate(([0.0], np.diff(smooth_freq)))
    smooth_velocity = ewma(velocity)
    acceleration = np.concatenate(([0.0], np.diff(smooth_velocity)))
    return smooth_freq, smooth_velocity, ewma(acceleration)


def trend_score(
    z_frequency: float | None, z_velocity: float | None, z_acceleration: float | None
) -> float | None:
    """Weighted sum of the three z-scores. None if any component is unavailable."""
    if z_frequency is None or z_velocity is None or z_acceleration is None:
        return None
    settings = get_settings()
    return (
        settings.trend_zfreq_w * z_frequency
        + settings.trend_zvel_w * z_velocity
        + settings.trend_zaccel_w * z_acceleration
    )


def score_windows(
    window_starts: list[datetime], frequencies: list[float]
) -> list[WindowScore]:
    """Turn a per-window frequency series into fully scored windows."""
    freq, velocity, acceleration = compute_series(frequencies)
    scores: list[WindowScore] = []
    for i, start in enumerate(window_starts):
        z_f, z_v, z_a = zscore(freq, i), zscore(velocity, i), zscore(acceleration, i)
        scores.append(
            WindowScore(
                window_start=start,
                window_end=start + timedelta(hours=WINDOW_HOURS),
                frequency=float(freq[i]),
                velocity=float(velocity[i]),
                acceleration=float(acceleration[i]),
                z_frequency=z_f,
                z_velocity=z_v,
                z_acceleration=z_a,
                trend_score=trend_score(z_f, z_v, z_a),
            )
        )
    return scores


def window_grid(start: datetime, end: datetime) -> list[datetime]:
    """Every window start from `start` to `end`, inclusive of the one holding `end`."""
    grid, cursor = [], floor_window(start)
    last = floor_window(end)
    while cursor <= last:
        grid.append(cursor)
        cursor += timedelta(hours=WINDOW_HOURS)
    return grid


# ── data access ──────────────────────────────────────────────────────────────


async def _cluster_frequencies(
    cluster_id: uuid.UUID, since: datetime, until: datetime
) -> tuple[list[datetime], list[float]]:
    """Frequency per window: Σ(source_count × credibility_weight) of new events.

    Bucketed by created_at — when the story reached us — because velocity is
    about the rate of reporting, not about when the underlying event happened.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Event.created_at, Event.source_count, Event.credibility_weight).where(
                    Event.cluster_id == cluster_id,
                    Event.created_at >= since,
                    Event.created_at < until,
                )
            )
        ).all()

    buckets: dict[datetime, float] = {}
    for created_at, source_count, credibility in rows:
        bucket = floor_window(created_at)
        buckets[bucket] = buckets.get(bucket, 0.0) + source_count * credibility

    grid = window_grid(since, until - timedelta(seconds=1))
    return grid, [buckets.get(start, 0.0) for start in grid]


async def _persist(cluster_id: uuid.UUID, scores: list[WindowScore]) -> int:
    """Upsert scored windows. Re-running a batch must not duplicate rows."""
    if not scores:
        return 0
    rows = [
        {
            "id": uuid.uuid4(),
            "cluster_id": cluster_id,
            "window_start": s.window_start,
            "window_end": s.window_end,
            "frequency": s.frequency,
            "velocity": s.velocity,
            "acceleration": s.acceleration,
            "z_frequency": s.z_frequency,
            "z_velocity": s.z_velocity,
            "z_acceleration": s.z_acceleration,
            "trend_score": s.trend_score,
        }
        for s in scores
    ]
    statement = insert(Score).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[Score.cluster_id, Score.window_start],
        set_={
            "window_end": statement.excluded.window_end,
            "frequency": statement.excluded.frequency,
            "velocity": statement.excluded.velocity,
            "acceleration": statement.excluded.acceleration,
            "z_frequency": statement.excluded.z_frequency,
            "z_velocity": statement.excluded.z_velocity,
            "z_acceleration": statement.excluded.z_acceleration,
            "trend_score": statement.excluded.trend_score,
        },
    )
    async with session_scope() as session:
        await session.execute(statement)
    return len(rows)


def is_provisional(first_seen: datetime | None, now: datetime) -> bool:
    """True until the cluster has PROVISIONAL_DAYS of history behind it."""
    if first_seen is None:
        return True
    return (now - first_seen) < timedelta(days=PROVISIONAL_DAYS)


# ── entry point ──────────────────────────────────────────────────────────────


async def run_trend_scoring() -> TrendReport:
    settings = get_settings()
    report = TrendReport(breakouts=[])
    now = utcnow()
    since = floor_window(now - timedelta(days=HISTORY_DAYS))
    until = floor_window(now) + timedelta(hours=WINDOW_HOURS)

    async with session_scope() as session:
        clusters = (
            await session.execute(
                select(Cluster.id, Cluster.first_seen, Cluster.label).where(
                    Cluster.status == "active"
                )
            )
        ).all()

    for cluster_id, first_seen, label in clusters:
        grid, frequencies = await _cluster_frequencies(cluster_id, since, until)
        if not grid:
            continue

        scores = score_windows(grid, frequencies)
        report.windows_written += await _persist(cluster_id, scores)
        report.clusters += 1

        latest = scores[-1]
        provisional = is_provisional(first_seen, now)
        if provisional:
            report.provisional += 1
            continue

        if latest.trend_score is not None and latest.trend_score >= settings.trend_breakout_t:
            report.breakouts.append(cluster_id)
            log.info(
                "trend breakout",
                extra={
                    "cluster_id": str(cluster_id),
                    "label": label,
                    "trend_score": round(latest.trend_score, 3),
                },
            )

    log.info("trend scoring complete", extra=report.as_log())
    return report
