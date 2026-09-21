"""horizon-api — FastAPI on port 8300.

Phase 1 surface: health, Prometheus metrics, source registry CRUD and ingestion
stats for the dashboard. The inbound verdict endpoint arrives in phase 4.
"""

import json
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

from ..batch.trends import PROVISIONAL_DAYS, is_provisional
from ..config import get_settings
from ..db import get_session, session_scope
from ..logging import setup_logging
from ..models import (
    Cluster,
    Dispatch,
    Entity,
    Event,
    EventEntity,
    RawArticle,
    Scenario,
    Score,
    Source,
    Verdict,
    WeakSignal,
)
from ..pipeline.triage import TriageSettings
from ..pipeline.triage import rescore as rescore_triage
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


class Detection(BaseModel):
    """Whether the engine is producing anything, and if not, why not.

    Ingestion health said "everything is fine" for three days while the detector
    emitted nothing, because nothing reported on the detector at all. The two
    numbers that matter are not counts of signals — they are the reasons there
    are none: a cluster under 14 days old cannot break out however loud it gets,
    and that is a system still warming up, not a system that is broken.
    """

    weak_signals_open: int
    clusters_total: int
    #: Too young for a z-score to mean anything. Breakout publishing is
    #: suppressed for these, so all-provisional means no breakout is possible yet.
    clusters_provisional: int
    #: When the oldest cluster stops being provisional — the date this engine can
    #: first report a trend breakout. Null once at least one has matured.
    trend_ready_at: datetime | None
    breakouts_last_24h: int


class Stats(BaseModel):
    queue_depth: int
    articles_by_status: dict[str, int]
    #: Share of the last 24h of articles that never became an event, 0–100.
    #: `articles_by_status` holds lifetime totals, and a number that only ever
    #: grows cannot say whether anything is wrong *now*: extraction failed on
    #: 39–72% of articles for over a week, on a dashboard reporting a five-digit
    #: "failed" count that looked the same on a good day as on a bad one.
    failure_rate_24h: float
    articles_failed_24h: int
    events_total: int
    events_last_24h: int
    events_incomplete: int
    sources_active: int
    detection: Detection


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

    clusters = (
        await session.execute(select(Cluster.first_seen).where(Cluster.status == "active"))
    ).scalars().all()
    now = datetime.now(UTC)
    provisional = [f for f in clusters if is_provisional(f, now)]
    # The oldest provisional cluster matures first, so it sets the date.
    oldest = min((f for f in provisional if f is not None), default=None)

    recent_failed = (
        await session.scalar(
            select(func.count())
            .select_from(RawArticle)
            .where(RawArticle.fetched_at >= since, RawArticle.status == "failed")
        )
        or 0
    )
    recent_decided = (
        await session.scalar(
            select(func.count())
            .select_from(RawArticle)
            .where(
                RawArticle.fetched_at >= since,
                RawArticle.status.in_(("failed", "processed")),
            )
        )
        or 0
    )

    return Stats(
        queue_depth=await ArticleQueue().depth(),
        articles_by_status=by_status,
        # Against decided articles only. Counting the queue in the denominator
        # would make a healthy backlog look like improving quality.
        failure_rate_24h=round(100.0 * recent_failed / recent_decided, 1)
        if recent_decided
        else 0.0,
        articles_failed_24h=recent_failed,
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
        detection=Detection(
            weak_signals_open=await session.scalar(
                select(func.count())
                .select_from(WeakSignal)
                .where(WeakSignal.status == "candidate")
            )
            or 0,
            clusters_total=len(clusters),
            clusters_provisional=len(provisional),
            trend_ready_at=(
                oldest + timedelta(days=PROVISIONAL_DAYS)
                if oldest is not None and len(provisional) == len(clusters)
                else None
            ),
            breakouts_last_24h=await session.scalar(
                select(func.count())
                .select_from(Dispatch)
                .where(Dispatch.signal_type == "trend_breakout", Dispatch.created_at >= since)
            )
            or 0,
        ),
    )


# ── Triage tuning ────────────────────────────────────────────────────────────


def _stored_dimensions(event: Event) -> dict[str, float]:
    return {
        "relevance": event.score_relevance or 0.0,
        "urgency": event.score_urgency or 0.0,
        "impact": event.score_impact or 0.0,
        "novelty": event.score_novelty or 0.0,
        "reliability": event.score_reliability or 0.0,
        "sensitivity": event.score_sensitivity or 0.0,
        "actionability": event.score_actionability or 0.0,
    }


@app.get("/api/v1/triage/simulate")
async def simulate_triage(
    session: SessionDep,
    sensitivity_coefficient: float | None = None,
    priority_total: float | None = None,
    investigate_total: float | None = None,
):
    """What the verdict split would look like under different settings.

    The six dimensions are already stored, so trying a coefficient costs a
    division rather than re-reading every article. Tune here, then apply with
    /triage/rescore.
    """
    current = TriageSettings.from_env()
    proposed = TriageSettings(
        sensitivity_coefficient=(
            current.sensitivity_coefficient
            if sensitivity_coefficient is None
            else sensitivity_coefficient
        ),
        priority_total=current.priority_total if priority_total is None else priority_total,
        priority_urgency=current.priority_urgency,
        fasttrack_impact=current.fasttrack_impact,
        fasttrack_reliability=current.fasttrack_reliability,
        investigate_total=(
            current.investigate_total if investigate_total is None else investigate_total
        ),
    )

    events = list(
        (
            await session.execute(select(Event).where(Event.triage_total.isnot(None)))
        ).scalars()
    )
    if not events:
        return {"events": 0, "current": {}, "proposed": {}}

    def distribution(settings: TriageSettings) -> dict:
        verdicts: dict[str, int] = {}
        totals = []
        capped = 0
        for event in events:
            total, verdict = rescore_triage(_stored_dimensions(event), settings)
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            totals.append(total)
            if total >= 10.0:
                capped += 1
        return {
            "verdicts": verdicts,
            "mean_total": round(sum(totals) / len(totals), 2),
            # The number that exposed the problem: a formula that pins many
            # stories to the ceiling has stopped ranking them.
            "at_ceiling": capped,
            "at_ceiling_pct": round(100 * capped / len(events)),
        }

    return {
        "events": len(events),
        "settings": {
            "current": current.__dict__,
            "proposed": proposed.__dict__,
        },
        "current": distribution(current),
        "proposed": distribution(proposed),
    }


@app.post("/api/v1/triage/rescore", dependencies=[WriteAuth])
async def rescore_triage_endpoint(session: SessionDep):
    """Re-apply the configured formula to every scored event.

    Needed because the verdict is computed at write time. No LLM calls: the
    dimensions the model produced are already on the row.
    """
    events = list(
        (
            await session.execute(select(Event).where(Event.triage_total.isnot(None)))
        ).scalars()
    )
    settings = TriageSettings.from_env()
    changed = 0
    for event in events:
        total, verdict = rescore_triage(_stored_dimensions(event), settings)
        if event.triage_total != total or event.triage_verdict != verdict:
            event.triage_total, event.triage_verdict = total, verdict
            changed += 1

    log.info("triage rescored", extra={"events": len(events), "changed": changed})
    return {"events": len(events), "changed": changed, "settings": settings.__dict__}


# ── Verdict feedback from OSINT//DESK (phase 4) ──────────────────────────────


class VerdictIn(BaseModel):
    """Mirrors contracts/verdict.schema.json — do not rename fields."""

    model_config = {"extra": "forbid"}

    signal_id: uuid.UUID
    osint_signal_id: str
    verdict: Literal["true_signal", "false_signal", "inconclusive", "off_topic"]
    analyst_note: str | None = None
    closed_at: datetime


#: Only a decided verdict moves the weak signal on; "inconclusive" leaves it
#: dispatched, because an analyst who could not tell has not told us anything.
#: "off_topic" is absent for a different reason: it says nothing about whether
#: the detection was right, only that the newsroom does not cover the subject,
#: so it must not mark the signal verified either way.
VERDICT_TO_STATUS = {"true_signal": "verified_true", "false_signal": "verified_false"}


async def require_inbound_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Guards the verdict endpoint. Unlike the dashboard writes, this one is
    reachable from another system, so an unset key is refused rather than open."""
    expected = get_settings().horizon_api_key
    if not expected or x_api_key != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")


@app.post(
    "/api/v1/verdicts", status_code=status.HTTP_200_OK, dependencies=[Depends(require_inbound_key)]
)
async def receive_verdict(payload: VerdictIn, session: SessionDep) -> dict:
    """Record an analyst's verdict on a signal we sent.

    These labels are the feedback corpus for future threshold tuning, so a
    revised verdict replaces the old one rather than appending a second row.
    """
    dispatch = await session.get(Dispatch, payload.signal_id)
    if dispatch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown signal_id")

    existing = await session.scalar(
        select(Verdict).where(Verdict.dispatch_id == payload.signal_id)
    )
    if existing is None:
        session.add(
            Verdict(
                dispatch_id=payload.signal_id,
                verdict=payload.verdict,
                analyst_note=payload.analyst_note,
                received_at=payload.closed_at,
            )
        )
    else:
        log.info(
            "verdict revised",
            extra={
                "signal_id": str(payload.signal_id),
                "from": existing.verdict,
                "to": payload.verdict,
            },
        )
        existing.verdict = payload.verdict
        existing.analyst_note = payload.analyst_note
        existing.received_at = payload.closed_at

    if not dispatch.osint_desk_signal_id:
        dispatch.osint_desk_signal_id = payload.osint_signal_id

    new_status = VERDICT_TO_STATUS.get(payload.verdict)
    if new_status and dispatch.signal_type == "weak_signal":
        weak = await session.get(WeakSignal, dispatch.ref_id)
        if weak is not None:
            weak.status = new_status

    log.info(
        "verdict received",
        extra={
            "signal_id": str(payload.signal_id),
            "verdict": payload.verdict,
            "signal_type": dispatch.signal_type,
        },
    )
    return {"ok": True}


@app.get("/api/v1/verdicts/export")
async def export_verdicts(session: SessionDep) -> Response:
    """The labelled corpus as JSONL, one signal per line, for offline analysis.

    Each line pairs the payload we sent with the verdict that came back, which
    is what threshold tuning needs — the scores alongside the ground truth.
    """
    rows = (
        await session.execute(
            select(Dispatch, Verdict)
            .join(Verdict, Verdict.dispatch_id == Dispatch.id)
            .order_by(Verdict.received_at)
        )
    ).all()

    def line(dispatch: Dispatch, verdict: Verdict) -> str:
        payload = dispatch.payload or {}
        return json.dumps(
            {
                "signal_id": str(dispatch.id),
                "signal_type": dispatch.signal_type,
                "ref_id": str(dispatch.ref_id),
                "osint_signal_id": dispatch.osint_desk_signal_id,
                "combined_score": payload.get("combined_score"),
                "trend_score": payload.get("trend_score"),
                "categories": payload.get("categories", []),
                "title": payload.get("title"),
                "force_assessments": payload.get("force_assessments", []),
                "source_count": len(payload.get("top_events", [])),
                "verdict": verdict.verdict,
                "analyst_note": verdict.analyst_note,
                "dispatched_at": dispatch.created_at.isoformat(),
                "closed_at": verdict.received_at.isoformat(),
            },
            ensure_ascii=False,
        )

    body = "\n".join(line(d, v) for d, v in rows)
    return Response(
        content=body + ("\n" if body else ""),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="horizon-verdicts.jsonl"'},
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


def _event_json(event: Event, source_name: str | None = None, url: str | None = None) -> dict:
    """One event as OSINT//DESK's feed page expects it.

    Horizon is the only thing ingesting now, so this shape is a public interface
    between the two systems, not an internal convenience.
    """
    return {
        "id": str(event.id),
        "summary": event.summary,
        "actors": event.actors,
        "action": event.action,
        "location": event.location,
        "event_time": event.event_time,
        "categories": event.categories,
        "source_count": event.source_count,
        "source_name": source_name,
        "url": url,
        "credibility_weight": event.credibility_weight,
        "incomplete": event.incomplete,
        "updates": len(event.event_updates or []),
        "cluster_id": str(event.cluster_id) if event.cluster_id else None,
        "created_at": event.created_at,
        "triage": {
            "verdict": event.triage_verdict,
            "total": event.triage_total,
            "relevance": event.score_relevance,
            "urgency": event.score_urgency,
            "impact": event.score_impact,
            "novelty": event.score_novelty,
            "reliability": event.score_reliability,
            "sensitivity": event.score_sensitivity,
            "actionability": event.score_actionability,
        },
    }


@app.get("/api/v1/events")
async def list_events(
    session: SessionDep,
    limit: int = 50,
    offset: int = 0,
    verdict: str | None = None,
    order: Literal["recent", "score"] = "recent",
):
    """The inbound stream, with editorial scores. Backs the DESK feed page."""
    limit = min(limit, 200)
    query = (
        select(Event, Source.name, RawArticle.url)
        .join(RawArticle, RawArticle.id == Event.raw_article_id, isouter=True)
        .join(Source, Source.id == RawArticle.source_id, isouter=True)
    )
    if verdict:
        query = query.where(Event.triage_verdict == verdict)
    query = query.order_by(
        Event.triage_total.desc().nullslast()
        if order == "score"
        else Event.created_at.desc()
    )

    rows = (await session.execute(query.limit(limit).offset(offset))).all()
    return [_event_json(row[0], row[1], row[2]) for row in rows]


@app.get("/api/v1/events/counts")
async def event_counts(session: SessionDep):
    """Verdict tallies for the filter tabs, so the UI need not fetch to count."""
    rows = (
        await session.execute(
            select(Event.triage_verdict, func.count().label("n"))
            .where(Event.triage_verdict.isnot(None))
            .group_by(Event.triage_verdict)
        )
    ).all()
    counts = {row.triage_verdict: row.n for row in rows}
    scored = sum(counts.values())
    # ALL is the unfiltered total, not the sum of the verdicts, because the ALL
    # tab lists every event — including ones ingested before editorial scoring
    # existed. Summing verdicts here would put a badge of 88 on a tab that then
    # shows 644 rows.
    counts["ALL"] = (await session.execute(select(func.count(Event.id)))).scalar_one()
    counts["UNSCORED"] = counts["ALL"] - scored
    return counts


@app.get("/api/v1/clusters/{cluster_id}/timeline")
async def cluster_timeline(cluster_id: uuid.UUID, session: SessionDep, limit: int = 200):
    """Every event in a cluster, oldest first.

    This is what makes a handover worth accepting: an analyst opening a case
    should inherit the whole thread Horizon has been assembling for days, not
    just the few most credible pieces of it.
    """
    cluster = await session.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cluster not found")

    rows = (
        await session.execute(
            select(Event, Source.name, RawArticle.url)
            .join(RawArticle, RawArticle.id == Event.raw_article_id, isouter=True)
            .join(Source, Source.id == RawArticle.source_id, isouter=True)
            .where(Event.cluster_id == cluster_id)
            # Ordered by when it happened, falling back to when we saw it —
            # a timeline is only useful in the order things occurred.
            .order_by(func.coalesce(Event.event_time, Event.created_at))
            .limit(min(limit, 500))
        )
    ).all()

    return {
        "cluster_id": str(cluster.id),
        "label": cluster.label,
        "event_count": cluster.event_count,
        "first_seen": cluster.first_seen,
        "last_seen": cluster.last_seen,
        "status": cluster.status,
        "timeline": [_event_json(row[0], row[1], row[2]) for row in rows],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8300)


# ── Entity review queue ──────────────────────────────────────────────────────


def _entity_json(entity: Entity, surfaces: list[str]) -> dict:
    return {
        "id": str(entity.id),
        "canonical_name": entity.canonical_name,
        "entity_type": entity.entity_type,
        "aliases": entity.aliases,
        "qid": entity.qid,
        "qid_status": entity.qid_status,
        "qid_confidence": entity.qid_confidence,
        # The model's own narrative. Useful when reviewing, but it is prose the
        # model wrote about its choice — the Q-number is the part that was
        # checked against Wikidata, and the two can disagree.
        "qid_reason": entity.qid_reason,
        "confidence": entity.confidence,
        "review_status": entity.review_status,
        "risk": entity.risk,
        "decided_by": entity.decided_by,
        "mention_count": entity.mention_count,
        # What the articles actually said. When a merge is wrong this is the
        # only way an analyst can see it without reading the pipeline.
        "surface_forms": surfaces,
        "first_seen": entity.first_seen.isoformat() if entity.first_seen else None,
        "last_seen": entity.last_seen.isoformat() if entity.last_seen else None,
    }


async def _surfaces(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(EventEntity.entity_id, EventEntity.surface_form)
            .where(EventEntity.entity_id.in_(ids))
            .distinct()
        )
    ).all()
    out: dict[uuid.UUID, list[str]] = {}
    for entity_id, surface in rows:
        out.setdefault(entity_id, []).append(surface)
    return {k: sorted(v) for k, v in out.items()}


@app.get("/api/v1/entities")
async def list_entities(
    session: SessionDep,
    status: str | None = None,
    entity_type: str | None = None,
    qid_status: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """The entity store, filtered. `status=needs_review` backs the review queue."""
    limit = min(limit, 500)
    query = select(Entity)
    if status:
        query = query.where(Entity.review_status == status)
    if entity_type:
        query = query.where(Entity.entity_type == entity_type)
    if qid_status:
        query = query.where(Entity.qid_status == qid_status)
    entities = list(
        (
            await session.execute(
                query.order_by(Entity.mention_count.desc(), Entity.last_seen.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
    )
    surfaces = await _surfaces(session, [entity.id for entity in entities])
    return [_entity_json(entity, surfaces.get(entity.id, [])) for entity in entities]


@app.get("/api/v1/entities/counts")
async def entity_counts(session: SessionDep):
    """Review backlog, type mix, and how far Wikidata linking has got."""
    rows = (
        await session.execute(
            select(Entity.review_status, func.count().label("n")).group_by(Entity.review_status)
        )
    ).all()
    counts = {row.review_status: row.n for row in rows}
    counts["ALL"] = sum(counts.values())
    by_type = (
        await session.execute(
            select(Entity.entity_type, func.count().label("n")).group_by(Entity.entity_type)
        )
    ).all()
    by_qid = (
        await session.execute(
            select(Entity.qid_status, func.count().label("n")).group_by(Entity.qid_status)
        )
    ).all()
    return {
        "review": counts,
        "types": {row.entity_type: row.n for row in by_type},
        "wikidata": {row.qid_status: row.n for row in by_qid},
    }


class MentionSplit(BaseModel):
    """The mentions an analyst says are not this entity."""

    model_config = {"extra": "forbid"}

    #: event_entities.id values. Empty is rejected rather than treated as "all":
    #: an accidental empty selection must not silently dismantle an entity.
    mention_ids: list[uuid.UUID] = Field(min_length=1)
    reviewed_by: str | None = None


class EntityReview(BaseModel):
    """An analyst's verdict on a proposed entity."""

    model_config = {"extra": "forbid"}

    decision: Literal["confirmed", "rejected"]
    canonical_name: str | None = None
    entity_type: Literal["person", "org", "place", "team", "generic", "unknown"] | None = None
    qid: str | None = None
    reviewed_by: str | None = None


@app.post("/api/v1/entities/{entity_id}/review", dependencies=[WriteAuth])
async def review_entity(entity_id: uuid.UUID, payload: EntityReview, session: SessionDep):
    """Confirm or reject a proposed entity.

    A rejection is kept rather than deleted: `_find_existing` skips rejected
    rows, so the same bad merge does not come straight back on the next article
    that mentions the name.
    """
    entity = await session.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")

    entity.review_status = payload.decision
    entity.reviewed_by = payload.reviewed_by
    entity.reviewed_at = datetime.now(UTC)
    if payload.canonical_name:
        entity.canonical_name = payload.canonical_name
    if payload.entity_type:
        entity.entity_type = payload.entity_type
    if payload.qid:
        entity.qid = payload.qid
    if payload.decision == "confirmed":
        # A human looked: downstream should stop treating this as provisional.
        entity.confidence = 1.0

    log.info(
        "entity reviewed",
        extra={"entity_id": str(entity_id), "decision": payload.decision},
    )
    return _entity_json(entity, [])


@app.get("/api/v1/entities/{entity_id}/mentions")
async def entity_mentions(entity_id: uuid.UUID, session: SessionDep, limit: int = 60):
    """Every article this entity was read out of, and how each one wrote the name.

    The review queue could not be worked without this. It showed the surface
    forms as bare chips — "Fed (เฟด)", "ธนาคารกลางสหรัฐ (Fed)" — with no way to
    see which article each came from, so the one question a reviewer has to
    answer ("are these two the same thing?") had to be answered by guessing.
    The data was always here; `_surfaces` applied .distinct() and dropped the
    event_id on the way out.

    Ordered newest first: a wrong merge usually shows up in the most recent
    article, which is the one the analyst has context for.
    """
    if await session.get(Entity, entity_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity not found")

    rows = (
        await session.execute(
            select(EventEntity, Event, RawArticle.url, RawArticle.title)
            .join(Event, Event.id == EventEntity.event_id)
            .outerjoin(RawArticle, RawArticle.id == Event.raw_article_id)
            .where(EventEntity.entity_id == entity_id)
            .order_by(Event.created_at.desc())
            .limit(limit)
        )
    ).all()

    return [
        {
            # The mention, not the entity: this is what a split acts on.
            "id": str(link.id),
            "surface_form": link.surface_form,
            "event_id": str(event.id),
            "summary": event.summary or "",
            "categories": list(event.categories or []),
            "occurred_at": event.created_at.isoformat() if event.created_at else None,
            "article_title": title,
            "article_url": url,
        }
        for link, event, url, title in rows
    ]


@app.post("/api/v1/entities/{entity_id}/split", dependencies=[WriteAuth])
async def split_entity(entity_id: uuid.UUID, payload: MentionSplit, session: SessionDep):
    """Move the named mentions onto an entity of their own.

    "รวมผิด" used to only set review_status='rejected', which stops *new*
    articles attaching but leaves everything already merged exactly as it was.
    The wrong history stayed in the store and still travelled to OSINT//DESK.
    The button said "wrong merge" and meant "stop adding to it".

    Splitting is the operation the queue actually needs, because the mistake is
    per-mention: twelve of fourteen articles are usually right and two are not,
    and a decision at entity level can only keep all of them or throw all of
    them away.

    The new entity carries the surface form the articles used and goes to the
    queue in turn — it is a name nobody has identified yet, not a finding.
    """
    entity = await session.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity not found")

    links = list(
        (
            await session.execute(
                select(EventEntity)
                .where(EventEntity.id.in_(payload.mention_ids))
                .where(EventEntity.entity_id == entity_id)
            )
        ).scalars()
    )
    if not links:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "none of those mentions belong to this entity"
        )

    total = await session.scalar(
        select(func.count()).select_from(EventEntity).where(EventEntity.entity_id == entity_id)
    )
    if len(links) >= (total or 0):
        # Splitting everything off would leave an entity with no evidence behind
        # it and produce a duplicate of itself. That is "rejected", not a split.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "that is every mention — use รวมผิด instead of splitting all of them",
        )

    surface = links[0].surface_form
    moved = Entity(
        id=uuid.uuid4(),
        canonical_name=surface,
        entity_type="unknown",
        aliases=sorted({link.surface_form for link in links}),
        # Deliberately below the review threshold: this is a name a human has
        # separated out, not a name anything has identified yet.
        confidence=0.0,
        review_status="needs_review",
        decided_by="human",
        risk=f"แยกออกจาก \"{entity.canonical_name}\" เพราะไม่ใช่สิ่งเดียวกัน",
        mention_count=len(links),
    )
    session.add(moved)
    await session.flush()

    for link in links:
        link.entity_id = moved.id
    entity.mention_count = max(0, entity.mention_count - len(links))
    entity.reviewed_by = payload.reviewed_by
    entity.reviewed_at = datetime.now(UTC)

    log.info(
        "mentions split off an entity",
        extra={
            "entity_id": str(entity_id),
            "new_entity_id": str(moved.id),
            "mentions": len(links),
        },
    )
    return {
        "entity_id": str(entity_id),
        "new_entity_id": str(moved.id),
        "moved": len(links),
        "new_canonical_name": moved.canonical_name,
    }


@app.get("/api/v1/clusters/{cluster_id}/entities")
async def cluster_entities(cluster_id: uuid.UUID, session: SessionDep, limit: int = 50):
    """Who and what a cluster is about, resolved and counted.

    This is the handover that makes cross-case memory possible on the DESK side.
    An analyst accepting a signal gets not just the story but the cast, already
    merged across spellings and — where one honestly fits — carrying a Wikidata
    identifier that means the same thing in both systems.

    Generic nouns are excluded: "ตำรวจ" appears in half the crime stories and
    would link every case to every other one.
    """
    if await session.get(Cluster, cluster_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cluster not found")

    rows = (
        await session.execute(
            select(
                Entity,
                func.count(func.distinct(EventEntity.event_id)).label("events"),
            )
            .join(EventEntity, EventEntity.entity_id == Entity.id)
            .join(Event, Event.id == EventEntity.event_id)
            .where(Event.cluster_id == cluster_id)
            .where(Entity.entity_type != "generic")
            # A rejected merge is one an analyst already said was wrong; passing
            # it on would re-import the mistake into a case file.
            .where(Entity.review_status != "rejected")
            .group_by(Entity.id)
            .order_by(func.count(func.distinct(EventEntity.event_id)).desc())
            .limit(min(limit, 200))
        )
    ).all()

    return {
        "cluster_id": str(cluster_id),
        "entities": [
            {
                "entity_id": str(entity.id),
                "canonical_name": entity.canonical_name,
                "entity_type": entity.entity_type,
                "qid": entity.qid,
                "events": events,
                # So the DESK side can show that a name is provisional rather
                # than presenting an unreviewed merge as established fact.
                "review_status": entity.review_status,
            }
            for entity, events in rows
        ],
    }
