"""Threshold gate output — publish to the `horizon:signals` pub/sub channel.

The reasoner (phase 3) subscribes. Publishing is fire-and-forget by design: if
nothing is listening the message is dropped, and the batch job must not care —
the underlying rows are already in Postgres and the next run re-evaluates.
"""

import json
import logging
import uuid
from typing import Literal

from ..config import get_settings
from ..metrics import signals_published
from ..pipeline.vectors import utcnow
from ..queue import get_redis

log = logging.getLogger("horizon.batch.signals")

SignalType = Literal["weak_signal", "trend_breakout"]


async def publish(signal_type: SignalType, ref_id: uuid.UUID, **payload) -> int:
    """Announce one signal. Returns how many subscribers heard it — never raises.

    The count, not a bool: Redis pub/sub has no queue behind it, so publishing
    while the reasoner happens to be restarting succeeds and reaches nobody.
    A caller that has already written down "sent" on the strength of a True
    will never retry, and the story is gone. Callers must treat 0 as not sent.
    """
    settings = get_settings()
    message = {
        "signal_type": signal_type,
        "ref_id": str(ref_id),
        "published_at": utcnow().isoformat(),
        **payload,
    }
    try:
        receivers = await get_redis().publish(
            settings.signals_channel, json.dumps(message, ensure_ascii=False)
        )
        signals_published.labels(signal_type).inc()
        log.info(
            "signal published",
            extra={"signal_type": signal_type, "ref_id": str(ref_id), "receivers": receivers},
        )
        if not receivers:
            log.warning(
                "signal published to nobody — no subscriber was listening",
                extra={"signal_type": signal_type, "ref_id": str(ref_id)},
            )
        return int(receivers)
    except Exception as exc:  # noqa: BLE001 — alerting must not block the batch
        log.warning(
            "signal publish failed",
            extra={"signal_type": signal_type, "ref_id": str(ref_id), "error": str(exc)},
        )
        return 0
