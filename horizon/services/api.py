"""horizon-api — FastAPI on port 8300.

Phase 1 surface: health, Prometheus metrics, source registry CRUD and ingestion
stats for the dashboard. The inbound verdict endpoint arrives in phase 4.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..batch.trends import is_provisional
from ..config import get_settings
from ..db import get_session, session_scope
from ..logging import setup_logging
from ..models import Cluster, Event, RawArticle, Scenario, Score, Source, WeakSignal
from ..queue import ArticleQueue

log = logging.getLogger("horizon.api")

#: Sparkline length on the Trends page — 24 six-hour windows is six days.
TREND_HISTORY_WINDOWS = 24

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Schemas ──────────────────────────────────────────────────────────────────


class SourceIn(BaseModel):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    type: Literal["rss", "searxng", "watchlist_hook"] = "rss"
    credibility_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    active: bool = True


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    url: str | None = Field(default=None, min_length=1)
    type: Literal["rss", "searxng", "watchlist_hook"] | None = None
    credibility_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    active: bool | None = None


class SourceOut(SourceIn):
    id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class Stats(BaseModel):
    queue_depth: int
    articles_by_status: dict[str, int]
    events_total: int
    events_last_24h: int
    events_incomplete: int
    sources_active: int


# ── Auth ─────────────────────────────────────────────────────────────────────


async def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Guards writes. With HORIZON_API_KEY unset the API is open — dev only."""
    expected = get_settings().horizon_api_key
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")


WriteAuth = Depends(require_api_key)


# ── App ──────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging("horizon.api", settings.log_level)
    if not settings.horizon_api_key:
        log.warning("HORIZON_API_KEY is unset — write endpoints are unauthenticated")
    log.info("api started", extra={"osint_desk_enabled": settings.osint_desk_enabled})
    yield


app = FastAPI(title="Horizon API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """Liveness plus a dependency probe — used by the compose healthcheck."""
    checks: dict[str, str] = {}

    try:
        async with session_scope() as session:
            await session.execute(select(1))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"error: {exc}"

    try:
        await ArticleQueue().depth()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"

    healthy = all(value == "ok" for value in checks.values())
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@app.get("/metrics")
async def metrics() -> Response:
    await ArticleQueue().depth()  # refresh the gauge at scrape time
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Sources ──────────────────────────────────────────────────────────────────


@app.get("/api/v1/sources", response_model=list[SourceOut])
async def list_sources(session: SessionDep, active: bool | None = None):
    query = select(Source).order_by(Source.name)
    if active is not None:
        query = query.where(Source.active.is_(active))
    return list((await session.execute(query)).scalars())


@app.post(
    "/api/v1/sources",
    response_model=SourceOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[WriteAuth],
)
async def create_source(payload: SourceIn, session: SessionDep):
    source = Source(**payload.model_dump())
    session.add(source)
    await session.flush()
    return source


@app.patch("/api/v1/sources/{source_id}", response_model=SourceOut, dependencies=[WriteAuth])
async def update_source(source_id: uuid.UUID, payload: SourcePatch, session: SessionDep):
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    await session.flush()
    return source


@app.delete(
    "/api/v1/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[WriteAuth],
)
async def delete_source(source_id: uuid.UUID, session: SessionDep):
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    await session.delete(source)


# ── Stats ────────────────────────────────────────────────────────────────────


@app.get("/api/v1/stats", response_model=Stats)
async def stats(session: SessionDep) -> Stats:
    by_status = {
        row.status: row.count
        for row in (
            await session.execute(
                select(RawArticle.status, func.count().label("count")).group_by(
                    RawArticle.status
                )
            )
        ).all()
    }
    since = datetime.now(UTC) - timedelta(hours=24)

    return Stats(
        queue_depth=await ArticleQueue().depth(),
        articles_by_status=by_status,
        events_total=await session.scalar(select(func.count()).select_from(Event)) or 0,
        events_last_24h=await session.scalar(
            select(func.count()).select_from(Event).where(Event.created_at >= since)
        )
        or 0,
        events_incomplete=await session.scalar(
            select(func.count()).select_from(Event).where(Event.incomplete.is_(True))
        )
        or 0,
        sources_active=await session.scalar(
            select(func.count()).select_from(Source).where(Source.active.is_(True))
        )
        or 0,
    )


# ── Analytics (phase 2) ──────────────────────────────────────────────────────


@app.get("/api/v1/trends")
async def list_trends(session: SessionDep, limit: int = 50):
    """Active clusters ranked by their newest trend score.

    `provisional` mirrors the batch job's rule: under 14 days of history the
    z-scores are reported but must not be read as a breakout.
    """
    limit = min(limit, 200)
    clusters = (
        await session.execute(
            select(Cluster).where(Cluster.status == "active").order_by(Cluster.last_seen.desc())
        )
    ).scalars()

    now = datetime.now(UTC)
    rows = []
    for cluster in clusters:
        history = list(
            (
                await session.execute(
                    select(Score)
                    .where(Score.cluster_id == cluster.id)
                    .order_by(Score.window_start.desc())
                    .limit(TREND_HISTORY_WINDOWS)
                )
            ).scalars()
        )[::-1]
        latest = history[-1] if history else None

        categories = list(
            (
                await session.execute(
                    select(func.unnest(Event.categories))
                    .where(Event.cluster_id == cluster.id)
                    .group_by(func.unnest(Event.categories))
                    .order_by(func.count().desc())
                    .limit(3)
                )
            ).scalars()
        )

        rows.append(
            {
                "cluster_id": str(cluster.id),
                "label": cluster.label,
                "event_count": cluster.event_count,
                "status": cluster.status,
                "first_seen": cluster.first_seen,
                "last_seen": cluster.last_seen,
                "trend_score": latest.trend_score if latest else None,
                "z_frequency": latest.z_frequency if latest else None,
                "z_velocity": latest.z_velocity if latest else None,
                "z_acceleration": latest.z_acceleration if latest else None,
                "provisional": is_provisional(cluster.first_seen, now),
                "history": [s.frequency for s in history],
                "categories": categories,
            }
        )

    rows.sort(key=lambda r: (r["trend_score"] is None, -(r["trend_score"] or 0)))
    return rows[:limit]


@app.get("/api/v1/weak-signals")
async def list_weak_signals(session: SessionDep, limit: int = 100, status: str | None = None):
    limit = min(limit, 200)
    query = select(WeakSignal).order_by(WeakSignal.combined_score.desc()).limit(limit)
    if status:
        query = query.where(WeakSignal.status == status)
    signals = list((await session.execute(query)).scalars())

    rows = []
    for signal in signals:
        title, categories = None, []
        if signal.event_id:
            event = await session.get(Event, signal.event_id)
            if event:
                title, categories = event.summary, list(event.categories or [])
        elif signal.cluster_id:
            cluster = await session.get(Cluster, signal.cluster_id)
            if cluster:
                title = cluster.label
            categories = list(
                (
                    await session.execute(
                        select(func.unnest(Event.categories))
                        .where(Event.cluster_id == signal.cluster_id)
                        .group_by(func.unnest(Event.categories))
                        .order_by(func.count().desc())
                        .limit(3)
                    )
                ).scalars()
            )

        rows.append(
            {
                "id": str(signal.id),
                "event_id": str(signal.event_id) if signal.event_id else None,
                "cluster_id": str(signal.cluster_id) if signal.cluster_id else None,
                "title": title,
                "novelty_score": signal.novelty_score,
                "isolation_score": signal.isolation_score,
                "burst_score": signal.burst_score,
                "combined_score": signal.combined_score,
                "status": signal.status,
                "created_at": signal.created_at,
                "categories": categories,
            }
        )
    return rows


@app.get("/api/v1/scenarios")
async def list_scenarios(session: SessionDep, limit: int = 50):
    """Populated by horizon-reasoner in phase 3; returns [] until then."""
    limit = min(limit, 200)
    scenarios = list(
        (
            await session.execute(
                select(Scenario).order_by(Scenario.created_at.desc()).limit(limit)
            )
        ).scalars()
    )
    rows = []
    for scenario in scenarios:
        cluster = await session.get(Cluster, scenario.cluster_id)
        rows.append(
            {
                "id": str(scenario.id),
                "cluster_id": str(scenario.cluster_id),
                "label": cluster.label if cluster else None,
                "best_case": scenario.best_case,
                "worst_case": scenario.worst_case,
                "likely_case": scenario.likely_case,
                "indicators": scenario.indicators or [],
                "source_event_ids": [str(i) for i in (scenario.source_event_ids or [])],
                "model": scenario.model,
                "created_at": scenario.created_at,
            }
        )
    return rows


@app.get("/api/v1/events")
async def list_events(session: SessionDep, limit: int = 50, offset: int = 0):
    """Recent events — the raw feed behind the dashboard, useful for smoke tests."""
    limit = min(limit, 200)
    events = list(
        (
            await session.execute(
                select(Event).order_by(Event.created_at.desc()).limit(limit).offset(offset)
            )
        ).scalars()
    )
    return [
        {
            "id": str(event.id),
            "summary": event.summary,
            "actors": event.actors,
            "action": event.action,
            "location": event.location,
            "event_time": event.event_time,
            "categories": event.categories,
            "source_count": event.source_count,
            "credibility_weight": event.credibility_weight,
            "incomplete": event.incomplete,
            "updates": len(event.event_updates or []),
            "created_at": event.created_at,
        }
        for event in events
    ]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8300)
