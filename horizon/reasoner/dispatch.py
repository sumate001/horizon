"""Dispatch records and the outbound signal payload.

The payload built here is the shared contract with OSINT//DESK
(`contracts/signal_inbound.schema.json`). It is assembled and stored now even
though nothing sends it yet: phase 4 adds the HTTP client with retry, and it
should have nothing left to compute — just POST `dispatches.payload`.

With OSINT_DESK_BASE_URL unset the row is written with status `disabled`, which
is a deliberate state rather than a failure.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from ..config import get_settings
from ..db import session_scope
from ..models import Cluster, Dispatch, Event, RawArticle, Source
from ..pipeline.vectors import utcnow
from .alerts import AlertContent, dispatch_alerts

log = logging.getLogger("horizon.reasoner.dispatch")

TOP_EVENTS_CAP = 10


@dataclass
class SignalRef:
    """What the batch job published, resolved against the database."""

    signal_type: str
    ref_id: uuid.UUID
    cluster_id: uuid.UUID | None
    event_id: uuid.UUID | None
    title: str
    combined_score: float
    trend_score: float
    #: Only on beat_match. Carried through rather than looked up again so the
    #: reason the editor reads is the one the matcher actually gave.
    beat_id: uuid.UUID | None = None
    beat_name: str | None = None
    beat_reason: str | None = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _top_events(cluster_id: uuid.UUID | None, event_id: uuid.UUID | None) -> list[dict]:
    """Most credible events first, capped, with their source article and outlet."""
    async with session_scope() as session:
        query = (
            select(
                Event.summary,
                Event.event_time,
                Event.credibility_weight,
                Event.location,
                RawArticle.url,
                Source.name.label("source_name"),
            )
            .join(RawArticle, RawArticle.id == Event.raw_article_id, isouter=True)
            .join(Source, Source.id == RawArticle.source_id, isouter=True)
            .order_by(Event.credibility_weight.desc(), Event.created_at.desc())
            .limit(TOP_EVENTS_CAP)
        )
        query = (
            query.where(Event.cluster_id == cluster_id)
            if cluster_id
            else query.where(Event.id == event_id)
        )
        rows = (await session.execute(query)).all()

    return [
        {
            "summary": row.summary or "",
            "url": row.url or "",
            "source_name": row.source_name or "",
            "credibility_weight": float(row.credibility_weight),
            "event_time": _iso(row.event_time),
            # Extracted since the beginning and never sent: it was absent from
            # the contract, so a place reached 80% of events and then stopped.
            "location": row.location or None,
        }
        for row in rows
    ]


async def _force_assessments(cluster_id: uuid.UUID | None) -> list[dict]:
    if cluster_id is None:
        return []
    from ..models import DrivingForce, ForceAssessment

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(DrivingForce.name, ForceAssessment.impact, ForceAssessment.uncertainty)
                .join(ForceAssessment, ForceAssessment.force_id == DrivingForce.id)
                .where(ForceAssessment.cluster_id == cluster_id)
                .order_by(ForceAssessment.assessed_at.desc())
            )
        ).all()

    # Newest assessment per force; a re-reasoned cluster has several rounds.
    seen: set[str] = set()
    out = []
    for name, impact, uncertainty in rows:
        if name in seen:
            continue
        seen.add(name)
        out.append(
            {"force": name, "impact": float(impact), "uncertainty": float(uncertainty)}
        )
    return sorted(out, key=lambda f: -f["impact"])


async def _categories(cluster_id: uuid.UUID | None, event_id: uuid.UUID | None) -> list[str]:
    async with session_scope() as session:
        query = (
            select(func.unnest(Event.categories))
            .group_by(func.unnest(Event.categories))
            .order_by(func.count().desc())
            .limit(3)
        )
        query = (
            query.where(Event.cluster_id == cluster_id)
            if cluster_id
            else query.where(Event.id == event_id)
        )
        return list((await session.execute(query)).scalars())


async def build_payload(
    signal: SignalRef, dispatch_id: uuid.UUID, scenario_id: uuid.UUID | None
) -> dict:
    """The OSINT//DESK contract payload. Field names are frozen by contracts/."""
    top_events = await _top_events(signal.cluster_id, signal.event_id)
    summary = top_events[0]["summary"] if top_events else ""

    return {
        "signal_id": str(dispatch_id),
        "signal_type": signal.signal_type,
        "title": signal.title or summary,
        "combined_score": round(signal.combined_score, 4),
        "trend_score": round(signal.trend_score, 4),
        "categories": await _categories(signal.cluster_id, signal.event_id),
        "summary": summary,
        "top_events": top_events,
        "force_assessments": await _force_assessments(signal.cluster_id),
        # Lets the receiver come back for the full timeline at the moment an
        # analyst accepts, which is later and fuller than this snapshot.
        "cluster_id": str(signal.cluster_id) if signal.cluster_id else None,
        "scenario_id": str(scenario_id) if scenario_id else None,
        # Null on a detection, set on a beat_match. The receiver files it
        # straight into the beat rather than asking its own model to decide
        # again — and a second opinion here could only contradict the reason
        # shown next to it.
        "beat_id": str(signal.beat_id) if signal.beat_id else None,
        "beat_name": signal.beat_name,
        "beat_reason": signal.beat_reason,
        "created_at": utcnow().isoformat(),
    }


async def record_dispatch(
    signal: SignalRef, scenario_id: uuid.UUID | None
) -> tuple[uuid.UUID, dict]:
    """Build the payload, alert the configured channels, and store the record."""
    settings = get_settings()
    dispatch_id = uuid.uuid4()
    payload = await build_payload(signal, dispatch_id, scenario_id)

    async with session_scope() as session:
        cluster_label = None
        if signal.cluster_id:
            cluster = await session.get(Cluster, signal.cluster_id)
            cluster_label = cluster.label if cluster else None

    channels = await dispatch_alerts(
        AlertContent(
            signal_type=signal.signal_type,
            title=cluster_label or signal.title or payload["summary"],
            score_label=(
                "คะแนนรวม" if signal.signal_type == "weak_signal" else "trend score"
            ),
            score=(
                signal.combined_score
                if signal.signal_type == "weak_signal"
                else signal.trend_score
            ),
            summary=payload["summary"],
            categories=payload["categories"],
            dashboard_url=(
                f"{settings.dashboard_base_url.rstrip('/')}/"
                f"{'weak-signals' if signal.signal_type == 'weak_signal' else 'trends'}"
            ),
        )
    )

    # phase 4 flips `pending` to delivered/failed; `disabled` stays as-is.
    osint_status = "pending" if settings.osint_desk_enabled else "disabled"

    async with session_scope() as session:
        session.add(
            Dispatch(
                id=dispatch_id,
                signal_type=signal.signal_type,
                ref_id=signal.ref_id,
                channels=channels,
                osint_desk_status=osint_status,
                payload=payload,
            )
        )

    log.info(
        "dispatch recorded",
        extra={
            "dispatch_id": str(dispatch_id),
            "signal_type": signal.signal_type,
            "channels": channels,
            "osint_desk_status": osint_status,
            "top_events": len(payload["top_events"]),
            "forces": len(payload["force_assessments"]),
        },
    )
    return dispatch_id, payload
