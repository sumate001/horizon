"""Attach Wikidata identifiers to entities that have not been looked up yet.

    python -m horizon.pipeline.backfill_wikidata --limit 50

Deliberately a command rather than part of ingestion. Linking needs a network
round trip to a free API that asks callers to keep the rate modest, so it runs
where it can be paced and interrupted — not on the path an article takes to
becoming an event.

Safe to re-run: only `qid_status = 'pending'` is touched, and every outcome
including "Wikidata has nothing" is written back, so a name is asked about once.
`--retry-unavailable` re-opens the ones that failed on the network, which is the
only status worth a second attempt.
"""

import argparse
import asyncio
import logging

from sqlalchemy import or_, select

from ..config import get_settings
from ..db import session_scope
from ..logging import setup_logging
from ..models import Entity, Event, EventEntity
from .entities import UNIDENTIFIABLE_RISKS
from .wikidata import WikidataClient, link_entity

log = logging.getLogger("horizon.backfill_wikidata")


async def _context_for(session, entity_id) -> str | None:
    """One article summary mentioning this entity, as disambiguating context.

    This is what separates the country from the empire: "กัมพูชา" in a story
    about a border clash is Q424, and in a story about Angkor it is not.
    """
    return (
        await session.execute(
            select(Event.summary)
            .join(EventEntity, EventEntity.event_id == Event.id)
            .where(EventEntity.entity_id == entity_id)
            .where(Event.summary.isnot(None))
            .order_by(Event.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def run(limit: int, *, retry_unavailable: bool = False) -> dict[str, int]:
    settings = get_settings()
    wanted = ["pending", "unavailable"] if retry_unavailable else ["pending"]

    async with session_scope() as session:
        pending = list(
            (
                await session.execute(
                    select(Entity.id)
                    .where(Entity.qid_status.in_(wanted))
                    .where(Entity.entity_type != "generic")
                    # An entity flagged as holding two different things has no
                    # single answer, so Wikidata will confidently supply one for
                    # whichever half its name currently reads as. The NULL check
                    # is not optional: `NOT IN` against a NULL risk is NULL, and
                    # would silently exclude every entity that has no risk at all.
                    .where(
                        or_(
                            Entity.risk.is_(None),
                            Entity.risk.notin_(UNIDENTIFIABLE_RISKS),
                        )
                    )
                    # Most-mentioned first: those matter most downstream and are
                    # the ones Wikidata is most likely to know.
                    .order_by(Entity.mention_count.desc(), Entity.last_seen.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    log.info("wikidata backfill starting", extra={"entities": len(pending)})
    totals = {"checked": 0, "linked": 0, "no_match": 0, "unavailable": 0, "queued": 0}
    wikidata = WikidataClient()
    try:
        for index, entity_id in enumerate(pending, 1):
            async with session_scope() as session:
                entity = await session.get(Entity, entity_id)
                if entity is None:
                    continue
                context = await _context_for(session, entity_id)
                before_review = entity.review_status
                await link_entity(
                    entity, context=context, wikidata=wikidata, model=settings.entity_model
                )
                totals["checked"] += 1
                totals[entity.qid_status if entity.qid_status != "linked" else "linked"] += 1
                if entity.review_status != before_review:
                    totals["queued"] += 1
                name, qid = entity.canonical_name, entity.qid
            log.info(
                "wikidata checked",
                extra={"done": index, "of": len(pending), "entity_name": name, "qid": qid},
            )
    finally:
        await wikidata.aclose()

    log.info("wikidata backfill complete", extra=totals)
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--retry-unavailable",
        action="store_true",
        help="also retry entities whose lookup failed on the network",
    )
    args = parser.parse_args()
    setup_logging("horizon.backfill_wikidata", get_settings().log_level)
    totals = asyncio.run(run(args.limit, retry_unavailable=args.retry_unavailable))
    print(
        f"checked={totals['checked']} linked={totals['linked']} "
        f"no_match={totals['no_match']} unavailable={totals['unavailable']} "
        f"queued_for_review={totals['queued']}"
    )


if __name__ == "__main__":
    main()
