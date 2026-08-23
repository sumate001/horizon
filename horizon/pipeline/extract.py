"""Steps 1 + 3 — event extraction and classification in a single LLM call.

Failure policy (from the spec): a bad JSON body gets one repair prompt; if that
also fails the article is marked `failed` and the worker moves on. Nothing here
may raise past `extract_event` except `ExtractionError`.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import BANGKOK, CATEGORIES, get_settings
from ..llm.ollama import OllamaClient, extract_json, get_ollama
from ..llm.prompts import extraction_messages, repair_messages

log = logging.getLogger(__name__)

MAX_CATEGORIES = 3
SUMMARY_LIMIT = 280


class ExtractionError(RuntimeError):
    """Extraction failed after the repair attempt — mark the article `failed`."""


@dataclass
class Extraction:
    actors: list[str] = field(default_factory=list)
    action: str | None = None
    location: str | None = None
    event_time: datetime | None = None
    categories: list[str] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    #: Raw editorial scores as the model returned them. Turned into a
    #: TriageResult by the worker, which supplies the source's credibility.
    triage_scores: dict = field(default_factory=dict)

    @property
    def incomplete(self) -> bool:
        """Incomplete events are stored but excluded from clustering and scoring."""
        return not self.actors or not self.action


def parse_event_time(value: object) -> datetime | None:
    """ISO-8601 → aware UTC datetime. Anything unparseable becomes None.

    The prompt forbids guessing dates, so a missing or junk value is expected and
    must not fail the article.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip().replace("Z", "+00:00")
    # Tolerate "2026-08-22 14:30:00" and date-only values.
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BANGKOK)
        return parsed.astimezone(UTC)

    log.debug("unparseable event_time", extra={"value": value})
    return None


def _clean_actors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: dict[str, None] = {}
    for item in value:
        if isinstance(item, str) and item.strip():
            seen.setdefault(item.strip(), None)
    return list(seen)


def _clean_categories(value: object) -> list[str]:
    """Keep only labels from the closed list, deduped, at most MAX_CATEGORIES."""
    if not isinstance(value, list):
        return []
    kept: dict[str, None] = {}
    for item in value:
        if isinstance(item, str) and item.strip() in CATEGORIES:
            kept.setdefault(item.strip(), None)
    return list(kept)[:MAX_CATEGORIES]


def _clean_confidence(value: object) -> float:
    try:
        return min(1.0, max(0.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _clean_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def normalize(payload: dict) -> Extraction:
    """Coerce a raw model response into an Extraction. Never raises."""
    summary = _clean_text(payload.get("summary")) or ""
    return Extraction(
        actors=_clean_actors(payload.get("actors")),
        action=_clean_text(payload.get("action")),
        location=_clean_text(payload.get("location")),
        event_time=parse_event_time(payload.get("time")),
        categories=_clean_categories(payload.get("categories")),
        summary=summary[:SUMMARY_LIMIT],
        confidence=_clean_confidence(payload.get("confidence")),
        # Passed through unvalidated; triage.score() clamps and fills the gaps,
        # so a model that omits a dimension costs that dimension, not the article.
        triage_scores={
            key: payload.get(key)
            for key in (
                "relevance",
                "urgency",
                "impact",
                "novelty",
                "sensitivity",
                "actionability",
            )
        },
    )


async def extract_event(
    title: str,
    body: str,
    *,
    published_at: datetime | None = None,
    client: OllamaClient | None = None,
) -> Extraction:
    """Extract + classify one article. Raises ExtractionError if unrecoverable.

    `published_at` anchors relative dates in the text; see `extraction_messages`.
    """
    llm = client or get_ollama()
    settings = get_settings()

    raw = await llm.chat_raw(
        extraction_messages(title, body, published_at=published_at),
        purpose="extract",
        model=settings.extract_model,
    )

    try:
        return normalize(extract_json(raw))
    except (ValueError, TypeError) as first_error:
        log.warning("extraction JSON invalid, attempting repair", extra={"error": str(first_error)})

    try:
        repaired = await llm.chat_raw(
            repair_messages(raw), purpose="extract_repair", model=settings.extract_model
        )
        return normalize(extract_json(repaired))
    except Exception as exc:
        # Terminal for this article only — the worker marks it `failed` and moves on.
        raise ExtractionError(f"repair failed: {exc}") from exc
