"""horizon-reasoner — RHYTHM 3, event driven.

Idles on the `horizon:signals` channel and wakes only when the batch job finds
something. Per signal:

    driving force scoring (PESTEL pairwise → AHP) → scenario (RAG) →
    alerts (Telegram / LINE) → dispatch record

This is the expensive path — roughly 100 LLM calls per signal — which is exactly
why it is gated behind a threshold instead of running on a schedule.

A cooldown stops the same cluster being re-reasoned every batch run: the batch
republishes a cluster on every pass until its score drops back under threshold,
and paying for that repeatedly buys nothing.
"""

import asyncio
import contextlib
import json
import logging
import signal as signalmod
import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select

from ..batch.beats import republish, undispatched_matches
from ..config import get_settings
from ..db import session_scope
from ..integration.osint_desk import deliver, due_dispatches
from ..logging import setup_logging
from ..metrics import (
    ahp_inconsistent,
    deliveries_pending,
    reasoning_duration,
    serve_metrics,
    signals_handled,
    timed,
)
from ..models import SIGNAL_TYPES, Dispatch, WeakSignal
from ..pipeline.vectors import utcnow
from ..queue import get_redis
from ..reasoner.dispatch import SignalRef, record_dispatch
from ..reasoner.forces import score_driving_forces
from ..reasoner.scenario import generate_scenario

log = logging.getLogger("horizon.reasoner")

#: How often to look for retries that have come due. The shortest backoff step
#: is 30s, so anything much longer would add latency to every retry.
DELIVERY_SWEEP_SECONDS = 20


def parse_signal(raw: str) -> SignalRef | None:
    """Turn a pub/sub message into a resolved reference, or None if unusable."""
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("signal is not JSON", extra={"raw": raw[:200]})
        return None

    signal_type = message.get("signal_type")
    if signal_type not in SIGNAL_TYPES:
        log.warning("unknown signal type", extra={"signal_type": str(signal_type)[:40]})
        return None

    try:
        ref_id = uuid.UUID(str(message["ref_id"]))
    except (KeyError, ValueError):
        log.warning("signal has no usable ref_id", extra={"raw": raw[:200]})
        return None

    def as_uuid(value) -> uuid.UUID | None:
        try:
            return uuid.UUID(str(value)) if value else None
        except ValueError:
            return None

    return SignalRef(
        signal_type=signal_type,
        ref_id=ref_id,
        cluster_id=as_uuid(message.get("cluster_id")),
        event_id=as_uuid(message.get("event_id")),
        title=str(message.get("title") or ""),
        combined_score=float(message.get("combined_score") or 0.0),
        trend_score=float(message.get("trend_score") or 0.0),
        beat_id=as_uuid(message.get("beat_id")),
        beat_name=(str(message["beat_name"]) if message.get("beat_name") else None),
        beat_reason=(str(message["beat_reason"]) if message.get("beat_reason") else None),
    )


async def _recently_reasoned(signal: SignalRef) -> bool:
    """Has this cluster already been through the reasoner inside the cooldown?

    `dispatches.ref_id` is the cluster itself for a trend breakout, and the weak
    signal row for a weak signal — which in turn carries the cluster. Both reach
    the same cluster in one query.
    """
    settings = get_settings()
    if signal.cluster_id is None:
        return False

    cutoff = utcnow() - timedelta(hours=settings.reasoner_cooldown_hours)
    weak_for_cluster = select(WeakSignal.id).where(WeakSignal.cluster_id == signal.cluster_id)

    async with session_scope() as session:
        seen = await session.scalar(
            select(Dispatch.id)
            .where(
                Dispatch.created_at >= cutoff,
                or_(
                    Dispatch.ref_id == signal.cluster_id,
                    Dispatch.ref_id.in_(weak_for_cluster),
                ),
            )
            .limit(1)
        )
    return seen is not None


async def _already_dispatched(ref_id: uuid.UUID) -> bool:
    """Has this exact match already left the building?"""
    async with session_scope() as session:
        return (
            await session.scalar(
                select(func.count()).select_from(Dispatch).where(Dispatch.ref_id == ref_id)
            )
        ) > 0


async def _mark_dispatched(signal: SignalRef) -> None:
    """Weak signal rows move candidate → dispatched once they leave the building."""
    if signal.signal_type != "weak_signal":
        return
    async with session_scope() as session:
        weak = await session.get(WeakSignal, signal.ref_id)
        if weak is not None and weak.status == "candidate":
            weak.status = "dispatched"


async def handle_signal(signal: SignalRef) -> uuid.UUID | None:
    """Full reasoning pass for one signal. Never raises."""
    log.info(
        "signal received",
        extra={
            "signal_type": signal.signal_type,
            "ref_id": str(signal.ref_id),
            "cluster_id": str(signal.cluster_id) if signal.cluster_id else None,
        },
    )

    # A beat match is a standing request being served, not a detection. The
    # cooldown exists so one cluster cannot produce a run of near-identical
    # detections; applying it here would silently drop the story the newsroom
    # explicitly asked to be told about, because something else on the same
    # cluster happened to fire first.
    # One match, one dispatch, whatever happens on the wire. The sweeper
    # re-announces anything without a dispatch every 20 seconds, and while this
    # consumer was stuck inside another signal's reasoning it kept announcing
    # the same match — then drained the backlog and made a dispatch for every
    # copy. Two matches became 61 and 64 deliveries, and an editor following
    # "สงครามโลกครั้งที่ 3" opened the inbox to the same Gaza story 61 times.
    #
    # The check belongs here rather than in the sweeper: the duplicate is
    # created on this side, so this is where it can actually be prevented.
    # Not a blanket rule on ref_id — a cluster breaking out again weeks later is
    # a real second trend_breakout, but a match is one event on one beat, once.
    if signal.signal_type == "beat_match" and await _already_dispatched(signal.ref_id):
        signals_handled.labels(signal.signal_type, "duplicate").inc()
        log.info("match already dispatched — ignoring the repeat announcement",
                 extra={"ref_id": str(signal.ref_id)})
        return None

    if signal.signal_type != "beat_match" and await _recently_reasoned(signal):
        signals_handled.labels(signal.signal_type, "cooldown").inc()
        log.info(
            "skipping — cluster reasoned about recently",
            extra={"cluster_id": str(signal.cluster_id)},
        )
        return None

    # Scenario reasoning is skipped for beat matches, and it is a cost decision
    # rather than a judgement about worth: forces + scenario is two model passes
    # per signal, and one beat can match up to MAX_MATCHES_PER_BEAT events in a
    # single run. An editor following a subject wants the story; if one turns
    # out to need modelling, accepting it in OSINT//DESK opens a case and the
    # cluster is right there.
    scenario_id = None
    if signal.cluster_id is not None and signal.signal_type != "beat_match":
        # Forces first: the scenario prompt reads the assessments they produce.
        try:
            with timed(reasoning_duration, "forces"):
                report = await score_driving_forces(signal.cluster_id)
            ahp_inconsistent.inc(sum(1 for r in report.results if not r.trustworthy))
        except Exception as exc:
            log.exception("driving force scoring failed", extra={"error": str(exc)})

        try:
            with timed(reasoning_duration, "scenario"):
                scenario_id = await generate_scenario(signal.cluster_id)
        except Exception as exc:
            log.exception("scenario generation failed", extra={"error": str(exc)})

    dispatch_id, _ = await record_dispatch(signal, scenario_id)
    await _mark_dispatched(signal)

    # First delivery attempt is immediate; the sweeper owns every retry after it.
    if get_settings().osint_desk_enabled:
        await deliver(dispatch_id)

    signals_handled.labels(signal.signal_type, "reasoned").inc()
    return dispatch_id


async def delivery_loop(stop: asyncio.Event) -> None:
    """Retry outbound deliveries whose backoff has elapsed.

    Runs beside the subscriber rather than as its own service: the reasoner
    already owns dispatch rows, and a delivery that fails must not hold up the
    next signal.
    """
    settings = get_settings()
    if not settings.osint_desk_enabled:
        log.info("OSINT_DESK_BASE_URL unset — outbound delivery disabled")
        return

    log.info("delivery sweeper started", extra={"interval_s": DELIVERY_SWEEP_SECONDS})
    #: Matches this process has already put on the wire. Bounded by the fact
    #: that a match leaves the set the moment it has a dispatch — and a restart
    #: clears it, which is correct: after a restart nothing is in flight.
    announced: set[uuid.UUID] = set()
    while not stop.is_set():
        try:
            due = await due_dispatches()
            deliveries_pending.set(len(due))
            for dispatch_id in due:
                await deliver(dispatch_id)
        except Exception as exc:
            log.exception("delivery sweep failed", extra={"error": str(exc)})

        # Matches that were announced while this consumer was busy. pub/sub has
        # no queue and PUBLISH counts subscribers rather than readers, so a
        # message sent while the loop is inside one signal's reasoning is simply
        # gone — 51 matches were recorded and never reached the editor that way.
        # The stored row is what guarantees delivery; the message is the fast
        # path, not the mechanism.
        try:
            # Announced at most once per pass. The consumer may be minutes
            # behind — it is the same loop — so re-announcing every 20s piles up
            # copies of a message it has not reached yet. Remembering what was
            # already sent keeps the sweep a recovery mechanism rather than a
            # source of load, and _already_dispatched catches whatever slips.
            missed = [m for m in await undispatched_matches() if m not in announced]
            if missed:
                log.info("re-announcing matches nobody read", extra={"count": len(missed)})
            for match_id in missed:
                if await republish(match_id):
                    announced.add(match_id)
        except Exception as exc:
            log.exception("beat match sweep failed", extra={"error": str(exc)})

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=DELIVERY_SWEEP_SECONDS)


async def consume(stop: asyncio.Event) -> None:
    settings = get_settings()
    pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(settings.signals_channel)
    log.info("subscribed", extra={"channel": settings.signals_channel})

    try:
        while not stop.is_set():
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=5.0
                )
            except Exception as exc:  # noqa: BLE001 — Redis blip; resubscribe below
                log.warning("pubsub read failed", extra={"error": str(exc)})
                await asyncio.sleep(2)
                continue

            if message is None:
                continue

            parsed = parse_signal(message.get("data") or "")
            if parsed is None:
                continue

            try:
                await handle_signal(parsed)
            except Exception as exc:
                log.exception(
                    "signal handling failed",
                    extra={"ref_id": str(parsed.ref_id), "error": str(exc)},
                )
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(settings.signals_channel)
            await pubsub.aclose()


async def main() -> None:
    settings = get_settings()
    setup_logging("horizon.reasoner", settings.log_level)
    serve_metrics(settings.metrics_port_reasoner, "reasoner")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signalmod.SIGINT, signalmod.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "reasoner started",
        extra={
            "ahp_top_clusters": settings.ahp_top_clusters,
            "cooldown_hours": settings.reasoner_cooldown_hours,
            "osint_desk_enabled": settings.osint_desk_enabled,
            "telegram": bool(settings.telegram_bot_token),
            "line": bool(settings.line_notify_token),
        },
    )
    await asyncio.gather(consume(stop), delivery_loop(stop))
    log.info("reasoner stopped")


if __name__ == "__main__":
    asyncio.run(main())
