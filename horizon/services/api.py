"""horizon-api — FastAPI on port 8300.

Phase 1 surface: health, Prometheus metrics, source registry CRUD and ingestion
stats for the dashboard. The inbound verdict endpoint arrives in phase 4.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session, session_scope
from ..logging import setup_logging
from ..models import Event, RawArticle, Source
from ..queue import ArticleQueue

log = logging.getLogger("horizon.api")

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
    since = datetime.now(timezone.utc) - timedelta(hours=24)

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
