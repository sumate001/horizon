"""Outbound signal delivery to OSINT//DESK.

    POST {OSINT_DESK_BASE_URL}/api/v1/signals/inbound
    X-API-Key: {OSINT_DESK_API_KEY}

Fire-and-forget with retry. The contract says a 202 carries `osint_signal_id`;
anything else is a failure that gets rescheduled on the 30s → 2m → 10m → 1h
backoff and then gives up. Retry state lives on the `dispatches` row, so a
restart mid-schedule resumes instead of losing the delivery.

Nothing here may raise into the caller. OSINT//DESK being down is its problem;
Horizon keeps ingesting, scoring, reasoning and alerting regardless.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import Dispatch
from ..pipeline.vectors import utcnow

log = logging.getLogger("horizon.integration.osint_desk")

#: Delay before each retry, in seconds. Attempt 1 is immediate; after it fails
#: the next runs 30s later, then 2m, then 10m, then 1h. Exhausting the list
#: means the signal is marked failed and left for an operator.
BACKOFF_SECONDS: tuple[int, ...] = (30, 120, 600, 3600)

INBOUND_PATH = "/api/v1/signals/inbound"
TIMEOUT = 20.0
#: A 4xx other than these means OSINT//DESK rejected the payload itself —
#: retrying an unchanged body would just repeat the rejection.
RETRYABLE_CLIENT_ERRORS = frozenset({408, 425, 429})


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str  # delivered | pending | failed | disabled
    osint_signal_id: str | None = None
    error: str | None = None
    next_attempt_at: datetime | None = None


def next_attempt(attempts: int, *, now: datetime | None = None) -> datetime | None:
    """When to try again after `attempts` failures, or None once exhausted."""
    if attempts < 1 or attempts > len(BACKOFF_SECONDS):
        return None
    return (now or utcnow()) + timedelta(seconds=BACKOFF_SECONDS[attempts - 1])


def is_retryable(status_code: int) -> bool:
    """Server trouble and rate limits are worth another go; a rejection is not."""
    if status_code >= 500:
        return True
    return status_code in RETRYABLE_CLIENT_ERRORS


async def post_signal(payload: dict, *, client: httpx.AsyncClient | None = None) -> DeliveryOutcome:
    """One delivery attempt. Never raises — every failure comes back as an outcome."""
    settings = get_settings()
    if not settings.osint_desk_enabled:
        return DeliveryOutcome(status="disabled")

    url = f"{settings.osint_desk_base_url.rstrip('/')}{INBOUND_PATH}"
    headers = {
        "X-API-Key": settings.osint_desk_api_key,
        "Content-Type": "application/json",
    }

    owned = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        response = await http.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001 — transport trouble is retryable
        return DeliveryOutcome(status="pending", error=f"transport: {exc}")
    finally:
        if owned:
            await http.aclose()

    if response.status_code == 202:
        try:
            osint_signal_id = str(response.json().get("osint_signal_id") or "")
        except ValueError:
            osint_signal_id = ""
        if not osint_signal_id:
            # 202 without the id still means accepted; the id is what we lose.
            log.warning("OSINT//DESK accepted the signal without an osint_signal_id")
        return DeliveryOutcome(status="delivered", osint_signal_id=osint_signal_id or None)

    detail = f"HTTP {response.status_code}: {response.text[:200]}"
    return DeliveryOutcome(
        status="pending" if is_retryable(response.status_code) else "failed", error=detail
    )


async def deliver(dispatch_id: uuid.UUID, *, client: httpx.AsyncClient | None = None) -> str:
    """Attempt one delivery for a dispatch row and record the result.

    Returns the resulting `osint_desk_status`.
    """
    async with session_scope() as session:
        dispatch = await session.get(Dispatch, dispatch_id)
        if dispatch is None:
            return "failed"
        payload = dict(dispatch.payload or {})
        attempts = dispatch.delivery_attempts

    outcome = await post_signal(payload, client=client)

    async with session_scope() as session:
        dispatch = await session.get(Dispatch, dispatch_id)
        if dispatch is None:
            return "failed"

        if outcome.status == "disabled":
            dispatch.osint_desk_status = "disabled"
            dispatch.next_attempt_at = None
            return "disabled"

        if outcome.status == "delivered":
            dispatch.osint_desk_status = "delivered"
            dispatch.osint_desk_signal_id = outcome.osint_signal_id
            dispatch.next_attempt_at = None
            dispatch.last_error = None
            dispatch.delivery_attempts = attempts + 1
            log.info(
                "signal delivered to OSINT//DESK",
                extra={
                    "dispatch_id": str(dispatch_id),
                    "osint_signal_id": outcome.osint_signal_id,
                    "attempts": dispatch.delivery_attempts,
                },
            )
            return "delivered"

        dispatch.delivery_attempts = attempts + 1
        dispatch.last_error = outcome.error
        retry_at = (
            next_attempt(dispatch.delivery_attempts) if outcome.status == "pending" else None
        )

        if retry_at is None:
            dispatch.osint_desk_status = "failed"
            dispatch.next_attempt_at = None
            log.warning(
                "giving up on OSINT//DESK delivery",
                extra={
                    "dispatch_id": str(dispatch_id),
                    "attempts": dispatch.delivery_attempts,
                    "error": outcome.error,
                },
            )
            return "failed"

        dispatch.osint_desk_status = "pending"
        dispatch.next_attempt_at = retry_at
        log.info(
            "OSINT//DESK delivery failed, retry scheduled",
            extra={
                "dispatch_id": str(dispatch_id),
                "attempt": dispatch.delivery_attempts,
                "retry_at": retry_at.isoformat(),
                "error": outcome.error,
            },
        )
        return "pending"


async def due_dispatches(limit: int = 20) -> list[uuid.UUID]:
    """Pending deliveries whose backoff has elapsed, oldest first."""
    now = utcnow()
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Dispatch.id)
                .where(
                    Dispatch.osint_desk_status == "pending",
                    (Dispatch.next_attempt_at.is_(None)) | (Dispatch.next_attempt_at <= now),
                )
                .order_by(Dispatch.created_at)
                .limit(limit)
            )
        ).scalars()
        return list(rows)
