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
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db import session_scope
from ..logging import setup_logging
from ..models import Entity, Event, EventEntity
from .entities import RISK_SHARED_QID, UNIDENTIFIABLE_RISKS
from .wikidata import WikidataClient, link_entity

log = logging.getLogger("horizon.backfill_wikidata")


async def _record_duplicate(entity_id, attempted_qid: str | None) -> str:
    """Mark a Q-number collision and name the entity already holding it.

    Takes the attempted qid as an argument rather than re-reading it: the
    session that hit the constraint was rolled back, so the row no longer
    remembers what the lookup decided. The qid is deliberately not written — it
    belongs to the other row — so what gets recorded is the finding and who to
    compare against. Merging two entities is not reversible, so this stops at
    telling a human.
    """
    async with session_scope() as session:
        entity = await session.get(Entity, entity_id)
        if entity is None:
            return "(หายไประหว่างทาง)"
        taken_by = (
            await session.scalar(
                select(Entity.canonical_name).where(
                    Entity.qid == attempted_qid, Entity.id != entity.id
                )
            )
            if attempted_qid
            else None
        )
        entity.qid_status = "duplicate"
        entity.qid_reason = f"{RISK_SHARED_QID}: {taken_by}" if taken_by else RISK_SHARED_QID
        if entity.review_status == "auto":
            entity.review_status = "needs_review"
            entity.risk = entity.risk or entity.qid_reason
        return entity.canonical_name


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
    totals = {
        "checked": 0,
        "linked": 0,
        "no_match": 0,
        "unavailable": 0,
        "duplicate": 0,
        "queued": 0,
    }
    wikidata = WikidataClient()
    try:
        for index, entity_id in enumerate(pending, 1):
            # Bound before the block: the commit that raises happens on the way
            # out of the session scope, after these are set, but link_entity
            # could in principle fail earlier.
            name, qid = "(ไม่ทราบชื่อ)", None
            try:
                async with session_scope() as session:
                    entity = await session.get(Entity, entity_id)
                    if entity is None:
                        continue
                    context = await _context_for(session, entity_id)
                    before_review = entity.review_status
                    await link_entity(
                        entity, context=context, wikidata=wikidata, model=settings.entity_model
                    )
                    name, qid = entity.canonical_name, entity.qid
                    outcome = entity.qid_status
                    requeued = entity.review_status != before_review
                # Counted only once the commit has actually gone through. Doing
                # it inside the block counted a collision as `linked` first and
                # as `duplicate` again in the handler, so the totals added up to
                # more than the number of entities looked at.
                totals["checked"] += 1
                totals[outcome] += 1
                if requeued:
                    totals["queued"] += 1
            except IntegrityError:
                # `ix_entities_qid` is unique because one Wikidata item is one
                # entity — the property that lets a Q-number merge spelling
                # variants. So this is the lookup finding that two rows are the
                # same subject, and it used to end the run: the whole backfill
                # died on the first collision and 14,113 entities stayed
                # `pending` because the job never reached them.
                # `checked` counts everything looked at, so the outcomes sum
                # back to it: linked + no_match + unavailable + duplicate.
                totals["checked"] += 1
                totals["duplicate"] += 1
                name = await _record_duplicate(entity_id, qid)
                log.info(
                    "wikidata item already claimed by another entity",
                    extra={"done": index, "of": len(pending), "entity_name": name, "qid": qid},
                )
                continue
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
