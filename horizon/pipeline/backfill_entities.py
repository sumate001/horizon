"""Resolve entities for events that were ingested before this step existed.

Run as a module:

    python -m horizon.pipeline.backfill_entities --limit 200

Only events with no entity links are touched, so the command is safe to re-run
and safe to interrupt — it picks up where it stopped rather than starting over.
There is no attempt to re-extract: this reads `events.actors` as the extractor
already wrote it, which is why it costs one LLM call per event and no more.
"""

import argparse
import asyncio
import logging

from sqlalchemy import func, select

from ..config import get_settings
from ..db import session_scope
from ..logging import setup_logging
from ..models import Event, EventEntity
from .entities import persist, resolve

log = logging.getLogger("horizon.backfill")


async def pending(limit: int) -> list[tuple]:
    """Events that have actors but no links yet, newest first."""
    async with session_scope() as session:
        linked = select(EventEntity.event_id).distinct().scalar_subquery()
        rows = (
            await session.execute(
                select(Event.id, Event.actors, Event.summary)
                .where(Event.id.notin_(linked))
                # `actors` is JSONB, not a PG array — cardinality() does not apply.
                .where(func.jsonb_array_length(Event.actors) > 0)
                .order_by(Event.created_at.desc())
                .limit(limit)
            )
        ).all()
    return [tuple(row) for row in rows]


async def run(limit: int) -> dict[str, int]:
    settings = get_settings()
    events = await pending(limit)
    log.info("backfill starting", extra={"events": len(events)})

    totals = {
        "events": 0,
        "entities_linked": 0,
        "entities_new": 0,
        "generic_skipped": 0,
        "needs_review": 0,
        "failed": 0,
    }
    for index, (event_id, actors, summary) in enumerate(events, 1):
        try:
            resolutions = await resolve(
                list(actors), context=summary, model=settings.entity_model
            )
            async with session_scope() as session:
                counts = await persist(
                    session,
                    event_id,
                    resolutions,
                    context=summary,
                    model=settings.entity_model,
                )
        except Exception as exc:  # noqa: BLE001 — one bad event must not end the run
            totals["failed"] += 1
            log.warning(
                "backfill event failed",
                extra={"event_id": str(event_id), "error": str(exc)},
            )
            continue
        totals["events"] += 1
        for key, value in counts.items():
            totals[key] += value
        if index % 10 == 0:
            log.info("backfill progress", extra={"done": index, "of": len(events), **totals})
    log.info("backfill complete", extra=totals)
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="how many events to resolve")
    args = parser.parse_args()
    setup_logging("horizon.backfill", get_settings().log_level)
    totals = asyncio.run(run(args.limit))
    print(
        f"events={totals['events']} entities_new={totals['entities_new']} "
        f"linked={totals['entities_linked']} generic_skipped={totals['generic_skipped']} "
        f"needs_review={totals['needs_review']} failed={totals['failed']}"
    )


if __name__ == "__main__":
    main()
