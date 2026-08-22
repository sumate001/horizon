"""Step 9 — scenario reasoning.

Retrieval-augmented: the prompt sees only what the database holds about this
cluster — its events, the force assessments just computed, its recent trend
windows, and the clusters nearest it by centroid. Nothing is invented, and every
scenario records the exact events it was built from so an analyst can audit it.

Scenarios are possibilities under stated conditions, never forecasts. The UI
carries that warning; the prompt enforces it.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..llm.ollama import OllamaClient, OllamaError, get_ollama
from ..llm.prompts import scenario_messages
from ..models import Cluster, DrivingForce, Event, ForceAssessment, Scenario, Score
from ..pipeline.vectors import VectorStore, get_vector_store

log = logging.getLogger("horizon.reasoner.scenario")

WATCH_TYPES = {
    "นโยบาย",
    "เศรษฐกิจ",
    "สังคม",
    "เทคโนโลยี",
    "สิ่งแวดล้อม",
    "กฎหมาย",
    "ความมั่นคง",
}
DEFAULT_WATCH_TYPE = "สังคม"
MAX_INDICATORS = 5


@dataclass
class ScenarioContext:
    label: str
    event_ids: list[uuid.UUID]
    summaries: list[str]
    forces: list[str]
    trend: list[float]
    related: list[str]

    @property
    def usable(self) -> bool:
        """One event is not a scenario. Below this the model would be guessing."""
        return len(self.summaries) >= 2


async def build_context(
    cluster_id: uuid.UUID, *, vectors: VectorStore | None = None
) -> ScenarioContext | None:
    settings = get_settings()
    store = vectors or get_vector_store()

    async with session_scope() as session:
        cluster = await session.get(Cluster, cluster_id)
        if cluster is None:
            return None

        events = (
            await session.execute(
                select(Event.id, Event.summary)
                .where(Event.cluster_id == cluster_id, Event.summary.isnot(None))
                .order_by(Event.created_at.desc())
                .limit(settings.scenario_event_cap)
            )
        ).all()

        assessments = (
            await session.execute(
                select(DrivingForce.name, ForceAssessment.impact, ForceAssessment.uncertainty)
                .join(ForceAssessment, ForceAssessment.force_id == DrivingForce.id)
                .where(ForceAssessment.cluster_id == cluster_id)
                .order_by(ForceAssessment.assessed_at.desc(), ForceAssessment.impact.desc())
                .limit(20)
            )
        ).all()

        trend_rows = (
            await session.execute(
                select(Score.frequency)
                .where(Score.cluster_id == cluster_id)
                .order_by(Score.window_start.desc())
                .limit(settings.scenario_trend_windows)
            )
        ).scalars()
        trend = list(trend_rows)[::-1]

    # Only the newest assessment per force — a cluster reasoned about twice would
    # otherwise show the same force with two impact numbers.
    seen: set[str] = set()
    forces: list[str] = []
    for name, impact, uncertainty in assessments:
        if name in seen:
            continue
        seen.add(name)
        forces.append(f"{name}: impact {impact:.2f}, uncertainty {uncertainty:.2f}")

    related: list[str] = []
    if cluster.centroid_id:
        hits = await store.search_centroids(
            (await _centroid_vector(store, cluster_id)) or [],
            limit=settings.scenario_related_clusters + 1,
        )
        async with session_scope() as session:
            for hit in hits:
                if hit.event_id == cluster_id:
                    continue
                other = await session.get(Cluster, hit.event_id)
                if other and other.label:
                    related.append(f"{other.label} (ความคล้าย {hit.score:.2f})")
                if len(related) >= settings.scenario_related_clusters:
                    break

    return ScenarioContext(
        label=cluster.label or "",
        event_ids=[row.id for row in events],
        summaries=[row.summary for row in events if row.summary],
        forces=forces,
        trend=trend,
        related=related,
    )


async def _centroid_vector(store: VectorStore, cluster_id: uuid.UUID) -> list[float] | None:
    """Read a cluster's own centroid back out of the centroid collection."""
    try:
        return await store.get_centroid(cluster_id)
    except Exception as exc:  # noqa: BLE001 — related clusters are a nicety
        log.debug(
            "centroid lookup failed",
            extra={"cluster_id": str(cluster_id), "error": str(exc)},
        )
        return None


def normalize_indicators(raw: object) -> list[dict[str, str]]:
    """Coerce the model's indicator list into the shape the UI and DB expect."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        watch_type = str(item.get("watch_type", "")).strip()
        out.append(
            {
                "description": description,
                "watch_type": watch_type if watch_type in WATCH_TYPES else DEFAULT_WATCH_TYPE,
            }
        )
    return out[:MAX_INDICATORS]


async def generate_scenario(
    cluster_id: uuid.UUID,
    *,
    client: OllamaClient | None = None,
    vectors: VectorStore | None = None,
) -> uuid.UUID | None:
    """Write one scenario for a cluster. Returns its id, or None if skipped."""
    settings = get_settings()
    llm = client or get_ollama()

    context = await build_context(cluster_id, vectors=vectors)
    if context is None:
        log.warning("cluster vanished before reasoning", extra={"cluster_id": str(cluster_id)})
        return None
    if not context.usable:
        log.info(
            "too little evidence for a scenario",
            extra={"cluster_id": str(cluster_id), "events": len(context.summaries)},
        )
        return None

    try:
        payload = await llm.chat_json(
            scenario_messages(
                label=context.label,
                events=context.summaries,
                forces=context.forces,
                trend=context.trend,
                related=context.related,
            ),
            purpose="scenario",
        )
    except (OllamaError, ValueError) as exc:
        log.warning(
            "scenario generation failed",
            extra={"cluster_id": str(cluster_id), "error": str(exc)},
        )
        return None

    scenario_id = uuid.uuid4()
    async with session_scope() as session:
        session.add(
            Scenario(
                id=scenario_id,
                cluster_id=cluster_id,
                best_case=(payload.get("best_case") or None),
                worst_case=(payload.get("worst_case") or None),
                likely_case=(payload.get("likely_case") or None),
                indicators=normalize_indicators(payload.get("indicators")),
                # Auditability: exactly which events this reasoning stood on.
                source_event_ids=context.event_ids,
                model=settings.extract_model,
            )
        )

    log.info(
        "scenario written",
        extra={
            "cluster_id": str(cluster_id),
            "scenario_id": str(scenario_id),
            "source_events": len(context.event_ids),
            "forces": len(context.forces),
        },
    )
    return scenario_id
