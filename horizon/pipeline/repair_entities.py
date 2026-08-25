"""Recompute stored entities with the current rules.

    python -m horizon.pipeline.repair_entities            # report only
    python -m horizon.pipeline.repair_entities --apply    # write

Names and aliases were derived once, at ingest, by whatever the rules were that
day. When a rule is wrong the damage stays in the store after the rule is fixed,
so this reruns the derivation over each entity's own surface forms — which are
kept verbatim precisely so a decision can be redone.

Three outcomes, and only the first two are automatic:

  renamed   the display name or the alias set changed. Safe: both are derived,
            and the surface forms they came from are untouched.

  merged    the corrected aliases now overlap another entity, which means the
            old rules split one thing in two — "อนุทิน ชาญวีรกูล" and "อนุทิน
            ชาญวีรกูล , นายกรัฐมนตรี" were two people, and only one of them ever
            got a Wikidata id.

  flagged   the entity's own surface forms no longer touch each other, so it is
            holding two different things — one man had absorbed the whole agency
            he runs. Splitting is not done here. It needs someone who can read
            the article, and the review queue already exists for that.

A merge is refused, and the pair flagged instead, when the two disagree in a way
no spelling rule can settle: different Wikidata items, or different kinds of
thing. The first dry run over 1,396 stored entities tried to fold a member of
parliament into his party and an agency into the man who runs it, both because a
bracket carried the other's name. An entity already flagged is never a merge
target either — whatever is wrong inside it should not spread.
"""

import argparse
import asyncio
import logging
import uuid
from collections import defaultdict

from sqlalchemy import delete, select

from ..config import get_settings
from ..db import session_scope
from ..logging import setup_logging
from ..models import Entity, EventEntity
from .entities import (
    RISK_DIFFERENT_ITEMS,
    RISK_TYPE_MISMATCH,
    RISK_UNRELATED_NAMES,
    UNIDENTIFIABLE_RISKS,
    display_name,
    group_by_rules,
    parse,
)

log = logging.getLogger("horizon.repair_entities")


#: Types that cannot be the same thing however much their names overlap. A
#: person and the organisation they lead share brackets constantly, and that is
#: the shape both wrong merges in the first dry run had.
_PEOPLE, _THINGS = {"person"}, {"org", "place", "team"}


def _may_merge(left: Entity, right: Entity) -> str | None:
    """Why these two must not be folded together, or None if they may be."""
    if left.qid and right.qid and left.qid != right.qid:
        return RISK_DIFFERENT_ITEMS
    kinds = {left.entity_type, right.entity_type}
    if kinds & _PEOPLE and kinds & _THINGS:
        return RISK_TYPE_MISMATCH
    return None


def _derive(forms: list[str]) -> tuple[str, list[str]]:
    """The name and aliases these surface forms would produce today."""
    aliases = sorted({key for raw in forms for key in parse(raw).keys})
    return display_name(forms), aliases


async def _surface_forms(session) -> dict[uuid.UUID, list[str]]:
    rows = (
        await session.execute(
            select(EventEntity.entity_id, EventEntity.surface_form).distinct()
        )
    ).all()
    forms: dict[uuid.UUID, list[str]] = defaultdict(list)
    for entity_id, surface in rows:
        forms[entity_id].append(surface)
    return forms


async def _absorb(session, survivor: Entity, victim: Entity) -> None:
    """Move everything the victim knows onto the survivor and delete it.

    Event links move rather than being recreated so nothing is lost, and the
    unique index on (event, entity, surface_form) is respected by dropping the
    links that would collide — the survivor already records those.
    """
    survivor.aliases = sorted(set(survivor.aliases) | set(victim.aliases))
    survivor.mention_count += victim.mention_count
    survivor.confidence = max(survivor.confidence, victim.confidence)
    survivor.first_seen = min(survivor.first_seen, victim.first_seen)
    survivor.last_seen = max(survivor.last_seen, victim.last_seen)
    if survivor.entity_type == "unknown" and victim.entity_type != "unknown":
        survivor.entity_type = victim.entity_type
    if not survivor.qid and victim.qid:
        survivor.qid = victim.qid
        survivor.qid_status = victim.qid_status
        survivor.qid_confidence = victim.qid_confidence
        survivor.qid_reason = victim.qid_reason
        survivor.qid_checked_at = victim.qid_checked_at

    kept = {
        (event_id, surface)
        for event_id, surface in (
            await session.execute(
                select(EventEntity.event_id, EventEntity.surface_form).where(
                    EventEntity.entity_id == survivor.id
                )
            )
        ).all()
    }
    moving = (
        await session.execute(
            select(EventEntity).where(EventEntity.entity_id == victim.id)
        )
    ).scalars()
    for link in moving:
        if (link.event_id, link.surface_form) in kept:
            await session.execute(delete(EventEntity).where(EventEntity.id == link.id))
        else:
            link.entity_id = survivor.id
    await session.flush()
    await session.execute(delete(Entity).where(Entity.id == victim.id))


def _flag(entity: Entity, reason: str, *, apply: bool, **detail) -> None:
    """Hand this entity to the review queue rather than deciding it here."""
    log.warning(
        "entity needs a human",
        extra={"entity_name": entity.canonical_name, "reason": reason, **detail},
    )
    if not apply:
        return
    entity.review_status = "needs_review"
    # A reason that says *what* is wrong supersedes the one written at ingest:
    # "this holds two different things" tells a reviewer what to decide, while
    # "joined on a bracket" only tells them to look.
    if reason in UNIDENTIFIABLE_RISKS or not entity.risk:
        entity.risk = reason
    if reason in UNIDENTIFIABLE_RISKS and entity.qid:
        # The Q-number answered a question this entity is not yet able to ask.
        # นันทพงศ์ สุวรรณรัตน์ merged with the agency he works for was linked to
        # the agency's item while typed a person — half right, and stored as if
        # it were settled.
        entity.qid = entity.qid_confidence = entity.qid_reason = None
        entity.qid_checked_at = None
        entity.qid_status = "pending"


async def run(*, apply: bool = False) -> dict[str, int]:
    counts = {"checked": 0, "renamed": 0, "merged": 0, "flagged": 0, "refused": 0}

    async with session_scope() as session:
        forms_by_entity = await _surface_forms(session)
        entities = list((await session.execute(select(Entity))).scalars())

        # Most-mentioned first, so a merge keeps the entity an analyst has
        # already seen and the newcomer is the one that disappears.
        entities.sort(key=lambda e: (-e.mention_count, e.canonical_name))
        by_alias: dict[str, Entity] = {}

        for entity in entities:
            counts["checked"] += 1
            forms = forms_by_entity.get(entity.id, [])
            if not forms:
                for alias in entity.aliases:
                    by_alias.setdefault(alias, entity)
                continue

            name, aliases = _derive(forms)
            changed = name != entity.canonical_name or aliases != list(entity.aliases)
            if changed:
                counts["renamed"] += 1
                log.info(
                    "entity rederived",
                    extra={
                        "entity_name": entity.canonical_name,
                        "becomes": name,
                        "dropped": sorted(set(entity.aliases) - set(aliases)),
                    },
                )
                if apply:
                    entity.canonical_name, entity.aliases = name, aliases
                    # "Wikidata has nothing" was a verdict about the old string.
                    # "ผู้อำนวยการ สำนักข่าวกรองแห่งชาติ" has no item and never
                    # will; the man it was hiding might. Ask again about the name
                    # we now believe in — and only that one, so the 1,300 entities
                    # this pass did not touch are not re-queried for nothing.
                    if entity.qid_status == "no_match":
                        entity.qid_status = "pending"

            if len(group_by_rules(forms, respect_qualifiers=False)) > 1:
                # Nothing merges into this: whatever is wrong inside it would
                # spread, and its aliases are exactly the ones not to trust.
                counts["flagged"] += 1
                _flag(
                    entity,
                    RISK_UNRELATED_NAMES,
                    apply=apply,
                    surface_forms=sorted(forms),
                )
                continue

            twin = next((by_alias[a] for a in aliases if a in by_alias), None)
            if twin is not None and twin.id != entity.id:
                refusal = _may_merge(twin, entity)
                if refusal:
                    counts["refused"] += 1
                    counts["flagged"] += 1
                    _flag(entity, refusal, apply=apply, into=twin.canonical_name)
                    continue
                counts["merged"] += 1
                log.info(
                    "merging duplicate entity",
                    extra={"entity_name": name, "into": twin.canonical_name},
                )
                if apply:
                    await _absorb(session, twin, entity)
                for alias in aliases:
                    by_alias.setdefault(alias, twin)
                continue

            for alias in aliases:
                by_alias.setdefault(alias, entity)

        if not apply:
            # Nothing above touched the session in report mode, but be explicit:
            # a repair that writes when asked to look is not a repair.
            await session.rollback()

    log.info("entity repair complete", extra=counts)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write the changes (default: report only)"
    )
    args = parser.parse_args()
    setup_logging("horizon.repair_entities", get_settings().log_level)
    counts = asyncio.run(run(apply=args.apply))
    print(
        f"{'applied' if args.apply else 'dry run'}: checked={counts['checked']} "
        f"renamed={counts['renamed']} merged={counts['merged']} "
        f"flagged={counts['flagged']} refused={counts['refused']}"
    )


if __name__ == "__main__":
    main()
