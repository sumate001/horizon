"""Alert dispatch — Telegram and LINE Notify.

Both channels are optional: an unset token means that channel is skipped
silently, not that alerting failed. A channel that errors is logged and the
other still goes out — losing an alert must never cost the pipeline a run.

Message text is Thai; only the numbers and the link are not.

Note on LINE Notify: LINE discontinued the service in 2025. The client is kept
because the spec asks for it and self-hosted relays still speak the same API,
but with LINE_NOTIFY_TOKEN unset — the default — nothing is attempted.
"""

import logging
from dataclasses import dataclass

import httpx

from ..config import get_settings

log = logging.getLogger("horizon.reasoner.alerts")

TELEGRAM_API = "https://api.telegram.org"
LINE_NOTIFY_API = "https://notify-api.line.me/api/notify"
TIMEOUT = 15.0

SIGNAL_LABEL = {
    "weak_signal": "สัญญาณอ่อน",
    "trend_breakout": "แนวโน้มพุ่งผิดปกติ",
}


@dataclass
class AlertContent:
    signal_type: str
    title: str
    score_label: str
    score: float
    summary: str
    categories: list[str]
    dashboard_url: str

    def as_text(self) -> str:
        kind = SIGNAL_LABEL.get(self.signal_type, self.signal_type)
        lines = [
            f"🛰 Horizon — {kind}",
            "",
            self.title or "(ไม่มีหัวข้อ)",
            "",
            f"{self.score_label}: {self.score:.2f}",
        ]
        if self.categories:
            lines.append(f"หมวด: {' · '.join(self.categories)}")
        if self.summary:
            lines += ["", self.summary[:400]]
        lines += ["", self.dashboard_url]
        return "\n".join(lines)


async def send_telegram(content: AlertContent) -> bool:
    settings = get_settings()
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                f"{TELEGRAM_API}/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": content.as_text(),
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
        log.info("telegram alert sent", extra={"signal_type": content.signal_type})
        return True
    except Exception as exc:  # noqa: BLE001 — the other channel must still fire
        log.warning("telegram alert failed", extra={"error": str(exc)})
        return False


async def send_line(content: AlertContent) -> bool:
    settings = get_settings()
    if not settings.line_notify_token:
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                LINE_NOTIFY_API,
                headers={"Authorization": f"Bearer {settings.line_notify_token}"},
                data={"message": "\n" + content.as_text()},
            )
            response.raise_for_status()
        log.info("line alert sent", extra={"signal_type": content.signal_type})
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("line alert failed", extra={"error": str(exc)})
        return False


async def dispatch_alerts(content: AlertContent) -> list[str]:
    """Send to every configured channel. Returns the ones that succeeded."""
    delivered = []
    if await send_telegram(content):
        delivered.append("telegram")
    if await send_line(content):
        delivered.append("line")

    if not delivered:
        log.info(
            "no alert channel configured or all failed",
            extra={"signal_type": content.signal_type},
        )
    return delivered
