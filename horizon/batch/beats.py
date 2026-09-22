"""Match new events against the beats the newsroom asked to follow.

This is the half of the OSINT//DESK integration that did not exist. A beat
defined over there could only filter what arrived, and what arrives is whatever
the anomaly detectors flag — 246 of 9,939 events, 2.5%. So an editor could
define "อิสราเอลในประเทศไทย", 90 matching events could sit in this database, 21
of them from the last two days, and not one would travel. Nothing was broken.
Nothing had asked for them.

`weak_signal` and `trend_breakout` say the engine noticed something. This says
the newsroom asked for something and here it is. The two are not graded the
same, which is why `beat_match` is its own signal type rather than a flag.

Rules, in order, and the order is the point:

  1. Categories narrow, they never decide. A beat with categories is only
     considered for events sharing one; a beat with none is considered for
     everything. This is the same rule OSINT//DESK uses on its side, and for
     the same reason: ความมั่นคง covers the southern insurgency, a cyber
     incident and a land dispute equally well.
  2. The description decides, read by the model. A subject is always narrower
     than a category.
  3. One beat per call. Asking about several at once turns a yes/no into a
     ranking the newsroom did not ask for, and an event can sit on two beats.

Asking the model about every (beat × event) pair does not scale and was the
first thing measured: 4 beats against one 12-hour window is ~1,300 questions,
45 minutes to 3.5 hours, on a cycle that repeats every 3. So the beat
description is embedded once and the existing event vectors — already there for
clustering — shortlist the candidates. The model then answers a few dozen
questions instead of a few thousand, and it answers them about the events that
could plausibly be on the beat rather than about the whole day.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db import session_scope
from ..llm.ollama import OllamaError, get_ollama
from ..llm.prompts import beat_match_messages
from ..models import Beat, BeatMatch, Dispatch, Event
from ..pipeline.vectors import get_vector_store, utcnow
from .signals import publish

log = logging.getLogger("horizon.batch.beats")

#: Cap per run. A beat that suddenly matches everything is a badly written beat,
#: and the failure mode to avoid is flooding an editor's inbox before anyone
#: notices. The cap is per beat, not per run, so one loose beat cannot crowd out
#: a well-written one.
MAX_MATCHES_PER_BEAT = 25

#: How many nearest events the vector search offers a beat before the model is
#: asked anything. The embedding shortlists — it never accepts.
#:
#: 60 was too narrow and it cost real stories. Measured on
#: "อิสราเอลในประเทศไทย": of the on-subject events in the store, 60 reached 22
#: and 150 reached 29. What crowded the list out was near-miss noise — Middle
#: East war coverage shares the word อิสราเอล with a deportation from Thailand
#: and scores close to it — so the Chabad centre in Phuket and the bodies found
#: in the Jewish cemetery at Bang Khla never got in front of the model at all.
#: Asked about them directly, the model matched all three correctly.
#:
#: Cost is bounded elsewhere: events are asked about BATCH at a time, and one
#: beat stops at MAX_MATCHES_PER_BEAT however long its list is.
SHORTLIST = 150

#: Events per question. Under load one question takes 35–120 seconds, so asking
#: per event would put a single beat's shortlist at an hour on a cycle that
#: repeats every three. Batched, a run is a handful of questions.
BATCH = 20


@dataclass
class BeatReport:
    beats: int = 0
    events_considered: int = 0
    pairs_asked: int = 0
    matched: int = 0
    published: int = 0
    already_matched: int = 0
    #: Matched, but nobody was listening when it was announced. Rolled back so
    #: a later run tries again rather than treating the story as handled.
    dropped: int = 0
    #: Beats that got their one wide pass over the archive on this run.
    backfilled: list[str] = field(default_factory=list)
    capped: list[str] = field(default_factory=list)
    model_failures: int = 0

    def as_log(self) -> dict:
        return {
            "beats": self.beats,
            "events_considered": self.events_considered,
            "pairs_asked": self.pairs_asked,
            "matched": self.matched,
            "published": self.published,
            "already_matched": self.already_matched,
            "dropped": self.dropped,
            "backfilled": self.backfilled,
            "capped": self.capped,
            "model_failures": self.model_failures,
        }


def is_candidate(beat: Beat, categories: list[str]) -> bool:
    """Whether this beat should even be asked about this event.

    A beat with no categories is asked about everything — that is what leaving
    it blank means, and it must not silently mean "nothing".
    """
    if not beat.categories:
        return True
    return bool(set(beat.categories) & set(categories or []))


async def run_beat_matching() -> BeatReport:
    """One pass: ask each open beat about the events it could plausibly want."""
    settings = get_settings()
    report = BeatReport()
    if not settings.beat_matching_enabled:
        log.info("beat matching disabled")
        return report

    now = utcnow()
    async with session_scope() as session:
        beat_rows = [
            (b.id, b.name, b.description, list(b.categories or []), b.backfilled_at is None)
            for b in (
                await session.execute(select(Beat).where(Beat.active.is_(True)).order_by(Beat.name))
            ).scalars()
        ]
        seen = {
            (row.beat_id, row.event_id)
            for row in (await session.execute(select(BeatMatch))).scalars()
        }

    report.beats = len(beat_rows)
    if not beat_rows:
        log.info("beat matching complete", extra=report.as_log())
        return report

    client = get_ollama()
    store = get_vector_store()
    considered: set[uuid.UUID] = set()

    for beat_id, beat_name, description, beat_categories, first_run in beat_rows:
        # A beat's first run reaches back over the archive; after that it only
        # has to keep up. Doing the wide pass every cycle would re-read weeks of
        # events forever to find the handful that arrived since.
        since = now - (
            timedelta(days=settings.beat_backfill_days)
            if first_run
            else timedelta(hours=settings.beat_lookback_hours)
        )
        if first_run:
            report.backfilled.append(beat_name)

        # The beat's own words are the query. Name included: a beat called
        # "อิสราเอลในประเทศไทย" carries meaning the description may assume.
        try:
            vector = await client.embed_one(f"{beat_name}\n{description}")
            hits = await store.search(vector, limit=SHORTLIST, since=since)
        except Exception as exc:  # noqa: BLE001 — one beat must not cost the run
            report.model_failures += 1
            log.warning(
                "could not shortlist for this beat",
                extra={"beat": beat_name, "error": str(exc)[:160]},
            )
            continue

        event_ids = [hit.event_id for hit in hits if (beat_id, hit.event_id) not in seen]
        report.already_matched += len(hits) - len(event_ids)
        if not event_ids:
            continue

        async with session_scope() as session:
            candidates = [
                (e.id, e.summary or "", list(e.categories or []), e.cluster_id)
                for e in (
                    await session.execute(
                        select(Event)
                        .where(Event.id.in_(event_ids))
                        .where(Event.triage_total >= settings.beat_min_triage_total)
                    )
                ).scalars()
            ]

        matched_here = 0
        failed_here = 0
        eligible = [
            (event_id, summary, categories, cluster_id)
            for event_id, summary, categories, cluster_id in candidates
            if is_candidate(
                Beat(
                    id=beat_id,
                    name=beat_name,
                    description=description,
                    categories=beat_categories,
                ),
                categories,
            )
        ]
        considered.update(event_id for event_id, *_ in eligible)

        for start_at in range(0, len(eligible), BATCH):
            if matched_here >= MAX_MATCHES_PER_BEAT:
                report.capped.append(beat_name)
                break
            chunk = eligible[start_at : start_at + BATCH]
            report.pairs_asked += len(chunk)
            try:
                answer = await client.chat_json(
                    beat_match_messages(
                        name=beat_name,
                        description=description,
                        events=[
                            (index, summary, categories)
                            for index, (_, summary, categories, _) in enumerate(chunk, 1)
                        ],
                    ),
                    purpose="beat_match",
                    model=settings.beat_model,
                )
            except (OllamaError, Exception) as exc:  # noqa: BLE001
                # One unanswered question must not cost the run. These events
                # stay unmatched and the next cycle asks again.
                report.model_failures += 1
                failed_here += 1
                log.warning(
                    "beat match question went unanswered",
                    extra={"beat": beat_name, "error": str(exc)[:120]},
                )
                continue

            for hit in answer.get("matches") or []:
                try:
                    position = int(hit["n"]) - 1
                except (KeyError, TypeError, ValueError):
                    continue
                if not 0 <= position < len(chunk):
                    # A number for an event that was not offered. Dropped rather
                    # than clamped: guessing which story was meant is how the
                    # wrong one ends up in an editor's inbox with a reason
                    # written about a different story.
                    log.warning(
                        "beat match names an event that was not offered",
                        extra={"beat": beat_name, "n": str(hit.get("n"))[:20]},
                    )
                    continue
                if matched_here >= MAX_MATCHES_PER_BEAT:
                    report.capped.append(beat_name)
                    break

                event_id, summary, categories, cluster_id = chunk[position]
                reason = str(hit.get("reason") or "").strip()[:400]
                match_id = uuid.uuid4()
                try:
                    async with session_scope() as session:
                        session.add(
                            BeatMatch(
                                id=match_id, beat_id=beat_id, event_id=event_id, reason=reason
                            )
                        )
                except IntegrityError:
                    # Another run got there first. Not an error — the unique
                    # constraint is doing exactly what it is for.
                    report.already_matched += 1
                    continue

                heard = await publish(
                    "beat_match",
                    match_id,
                    title=summary[:160],
                    combined_score=0.0,
                    categories=categories,
                    cluster_id=str(cluster_id) if cluster_id else None,
                    event_id=str(event_id),
                    beat_id=str(beat_id),
                    beat_name=beat_name,
                    beat_reason=reason,
                )
                if not heard:
                    # Redis pub/sub has no queue behind it: publishing while the
                    # reasoner restarts reaches nobody and raises nothing. The
                    # row has to go back, because keeping it would mark this
                    # story as handled and no run would ever look at it again —
                    # the story would be lost, silently, forever.
                    async with session_scope() as session:
                        stale = await session.get(BeatMatch, match_id)
                        if stale is not None:
                            await session.delete(stale)
                    report.dropped += 1
                    continue

                report.matched += 1
                matched_here += 1
                report.published += 1

        if first_run and failed_here:
            # "Nothing in the archive" and "could not ask" are not the same
            # answer, and stamping both spends the one wide pass a beat gets.
            # It happened: all four beats were backfilled during a run where
            # llm_timeout was too short, so every question failed and the pass
            # was recorded as complete. Three recovered because news kept
            # arriving on their subjects and the 12-hour window caught it;
            # "อิสราเอลในประเทศไทย" is sporadic, so it simply never got another
            # chance and stayed empty with 42 matching events in the store.
            log.warning(
                "not recording the archive pass — the model could not answer",
                extra={"beat": beat_name, "unanswered": failed_here},
            )
        elif first_run:
            async with session_scope() as session:
                beat = await session.get(Beat, beat_id)
                if beat is not None:
                    beat.backfilled_at = utcnow()

    report.events_considered = len(considered)
    log.info("beat matching complete", extra=report.as_log())
    return report


async def undispatched_matches(limit: int = 50) -> list[uuid.UUID]:
    """Matches that were recorded but never turned into a dispatch.

    Redis pub/sub has no queue behind it and PUBLISH counts *subscribers*, not
    readers. The reasoner is a single consumer that can block for minutes on one
    signal's scenario reasoning, and everything announced while it is busy goes
    to a socket nobody is reading and is gone. Measured: 51 matches recorded,
    dispatched, and never delivered — the row said the story had been handled
    and the editor never saw it.

    So the row is the queue and the message is only the fast path. Anything with
    no dispatch against it is picked up here, however it was missed.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(BeatMatch.id)
                .where(
                    ~select(Dispatch.id)
                    .where(Dispatch.ref_id == BeatMatch.id)
                    .exists()
                )
                .order_by(BeatMatch.created_at)
                .limit(limit)
            )
        ).scalars()
        return list(rows)


async def republish(match_id: uuid.UUID) -> bool:
    """Announce one recorded match again. Returns whether anyone was listening."""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(BeatMatch, Beat, Event)
                .join(Beat, Beat.id == BeatMatch.beat_id)
                .join(Event, Event.id == BeatMatch.event_id)
                .where(BeatMatch.id == match_id)
            )
        ).first()
        if row is None:
            return False
        match, beat, event = row
        payload = {
            "title": (event.summary or "")[:160],
            "categories": list(event.categories or []),
            "cluster_id": str(event.cluster_id) if event.cluster_id else None,
            "event_id": str(event.id),
            "beat_id": str(beat.id),
            "beat_name": beat.name,
            "beat_reason": match.reason,
        }

    return bool(await publish("beat_match", match_id, combined_score=0.0, **payload))
