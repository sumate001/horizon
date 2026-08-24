"""Step 3.6 — give an entity a Wikidata identifier, when one honestly fits.

An entity id of our own is stable inside Horizon. A Q-number is stable *outside*
it: OSINT//DESK, a future graph, and anyone reading an export can all agree that
Q16139757 is the same person without comparing Thai spellings. That is the whole
reason to reach for an external registry.

Three things measured on this corpus shaped the design, and each rules out the
obvious approach:

1. **Coverage is a long-tail problem.** Wikidata knew 75% of the names that
   repeat here and 33% of the ones seen once. A village headman or a patrol
   officer will never be in it. So "no match" is a normal outcome, not a
   failure, and nothing may block on getting one.

2. **The first search hit is wrong 13–20% of the time.** "กัมพูชา" returns the
   Khmer Empire, "แคนาดา" returns a reality show, "กระทรวงการคลัง" returns the
   UK Treasury. Taking `search[0]` would have attached confidently wrong
   identifiers to one entity in six.

3. **Prefix search alone cannot find the right answer at all.** Wikidata's Thai
   label for Cambodia is "ประเทศกัมพูชา" and the bare "กัมพูชา" is not among its
   aliases, so the country is absent from the entire prefix result list. Full
   text search finds it at rank 1. Both are queried and merged, because prefix
   is better for exact personal names and full text is better for anything
   Wikidata titles more formally than a newsroom does.

The model then picks from the merged candidates *with the article as context*,
and "none of these" is a first-class answer — point 3 means the right item is
sometimes simply not there, and a chooser that must choose would invent a link.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass

import httpx

from ..config import get_settings
from ..llm.ollama import OllamaClient, OllamaError, get_ollama
from ..llm.prompts import wikidata_messages

log = logging.getLogger(__name__)

API = "https://www.wikidata.org/w/api.php"

#: Wikimedia asks automated clients to identify themselves and to keep the rate
#: modest. Exceeding it earns a 429, which this module treats as a reason to
#: slow down rather than to give up on the name.
MIN_INTERVAL_SECONDS = 1.0
#: Once throttled, every later call waits this much longer too. A 429 means the
#: whole client is going too fast, not that one request was unlucky.
MAX_PENALTY_SECONDS = 30.0
MAX_CANDIDATES = 6

#: A Wikidata description that means the item is a work *about* something, not
#: the thing itself. "แคนาดา" matching a reality show is the failure this
#: catches before an LLM call is spent on it.
WORK_MARKERS = (
    "album", "song", "film", "movie", "television series", "tv series",
    "reality show", "season of", "video game", "book", "novel", "single by",
    "disambiguation", "แก้ความกำกวม", "หน้าหมวดหมู่", "wikimedia category",
    "wikimedia disambiguation", "category page", "list of", "รายชื่อ",
)


#: A name written only in Latin script is better searched as English — Wikidata's
#: Thai search ranks Thai labels first, which buries "Canada" under "แคนาดา…".
_LATIN_ONLY = re.compile(r"^[A-Za-z0-9 .\-'&]+$")


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the server asked us to wait, if it said."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; the caller's backoff covers it


class WikidataUnavailable(RuntimeError):
    """The API could not be reached. The caller carries on without a Q-number."""


@dataclass(frozen=True)
class Candidate:
    qid: str
    label: str
    description: str
    #: Which query surfaced it. Useful when reviewing a bad link by hand.
    found_by: str

    @property
    def looks_like_a_work(self) -> bool:
        text = f"{self.label} {self.description}".lower()
        return any(marker in text for marker in WORK_MARKERS)


class WikidataClient:
    """Read-only Wikidata access, rate limited and retried.

    One client per process. The interval lock is per-instance, so sharing it is
    what keeps the whole worker under the rate limit rather than each caller
    politely waiting on its own.
    """

    def __init__(self, user_agent: str | None = None, timeout: float = 25.0) -> None:
        settings = get_settings()
        self.user_agent = user_agent or settings.wikidata_user_agent
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._penalty = 0.0

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, params: dict) -> dict:
        """One API call, never faster than MIN_INTERVAL_SECONDS after the last."""
        client = await self._http()
        params = {**params, "format": "json"}
        for attempt in (1, 2, 3):
            async with self._lock:
                wait = (MIN_INTERVAL_SECONDS + self._penalty) - (
                    time.monotonic() - self._last_call
                )
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.monotonic()
                try:
                    response = await client.get(API, params=params)
                except httpx.HTTPError as exc:
                    if attempt == 3:
                        raise WikidataUnavailable(str(exc)) from exc
                    continue
            if response.status_code == 429:
                # Backing off inside the retry rather than failing: the name is
                # still worth resolving, we were just going too fast. When the
                # server says how long to wait, that beats guessing — and the
                # penalty applies to every later call, not just this retry.
                delay = _retry_after(response) or 2 ** attempt
                self._penalty = max(self._penalty, min(delay, MAX_PENALTY_SECONDS))
                log.warning(
                    "wikidata rate limited", extra={"wait": delay, "attempt": attempt}
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 500:
                if attempt == 3:
                    raise WikidataUnavailable(f"HTTP {response.status_code}")
                await asyncio.sleep(attempt)
                continue
            if response.status_code >= 400:
                raise WikidataUnavailable(f"HTTP {response.status_code}")
            return response.json()
        raise WikidataUnavailable("rate limited after 3 attempts")

    async def _prefix_search(self, name: str, language: str) -> list[tuple[str, str]]:
        """Label and alias prefix match. Precise for names written as Wikidata has them."""
        data = await self._get(
            {
                "action": "wbsearchentities",
                "search": name,
                "language": language,
                "uselang": language,
                "limit": MAX_CANDIDATES,
            }
        )
        return [(hit["id"], "prefix") for hit in data.get("search", [])]

    async def _fulltext_search(self, name: str) -> list[tuple[str, str]]:
        """Whole-item search. Finds "ประเทศกัมพูชา" when the article said "กัมพูชา"."""
        data = await self._get(
            {
                "action": "query",
                "list": "search",
                "srsearch": name,
                "srlimit": MAX_CANDIDATES,
                "srnamespace": 0,
            }
        )
        return [
            (hit["title"], "fulltext")
            for hit in data.get("query", {}).get("search", [])
            if hit["title"].startswith("Q")
        ]

    async def _describe(self, qids: list[str]) -> dict[str, tuple[str, str]]:
        if not qids:
            return {}
        data = await self._get(
            {
                "action": "wbgetentities",
                "ids": "|".join(qids[:50]),
                "props": "labels|descriptions",
                "languages": "th|en",
            }
        )
        out: dict[str, tuple[str, str]] = {}
        for qid, entity in (data.get("entities") or {}).items():
            if "labels" not in entity:  # a redirect or a deleted item
                continue
            labels, descriptions = entity["labels"], entity.get("descriptions", {})
            label = (labels.get("th") or labels.get("en") or {}).get("value", qid)
            description = (descriptions.get("th") or descriptions.get("en") or {}).get("value", "")
            out[qid] = (label, description)
        return out

    async def candidates(
        self, name: str, *, also: list[str] | None = None, language: str = "th"
    ) -> list[Candidate]:
        """Merged prefix and full-text candidates, in order, deduplicated.

        Prefix hits come first: when the newsroom spelling matches Wikidata's
        label exactly, that is the strongest evidence available.

        `also` carries the entity's other surface forms, and it is what makes
        the hard cases findable. Searching Thai "แคนาดา" returns a reality show
        and an archipelago but never the country, because Wikidata's Thai label
        is "ประเทศแคนาดา" and the bare form is not an alias. The same entity is
        also written "Canada" in our own store, and that finds Q16 at rank one.
        The corpus already carries both scripts — this just uses them.
        """
        queries: list[tuple[str, str]] = [(name, language)]
        for extra in also or []:
            extra = extra.strip()
            if not extra or extra.lower() == name.lower():
                continue
            queries.append((extra, "en" if _LATIN_ONLY.match(extra) else language))

        found: list[tuple[str, str]] = []
        for term, lang in queries[:3]:  # two spellings is plenty; three is the cap
            for coroutine in (self._prefix_search(term, lang), self._fulltext_search(term)):
                try:
                    found += await coroutine
                except WikidataUnavailable as exc:
                    log.warning(
                        "wikidata search failed",
                        extra={"entity_name": term, "error": str(exc)},
                    )

        ordered: dict[str, str] = {}
        for qid, source in found:
            ordered.setdefault(qid, source)
        if not ordered:
            return []

        described = await self._describe(list(ordered)[: MAX_CANDIDATES * 2])
        return [
            Candidate(qid=qid, label=label, description=description, found_by=ordered[qid])
            for qid, (label, description) in described.items()
        ]


@dataclass
class Link:
    """The outcome of trying to attach a Q-number to one entity."""

    qid: str | None
    confidence: float
    reason: str
    candidates_seen: int = 0

    @property
    def linked(self) -> bool:
        return self.qid is not None


NO_MATCH = "ไม่พบรายการที่ตรงกันใน Wikidata"


def _plausible(candidate: Candidate, entity_type: str) -> bool:
    """Cheap filter before the model is asked.

    A song called "กัมพูชา" is never the country in a border story, and a
    disambiguation page is never any entity at all. Filtering these keeps the
    candidate list short enough for the model to weigh the real options.
    """
    if candidate.looks_like_a_work:
        return entity_type in ("unknown",)
    return True


async def choose(
    name: str,
    candidates: list[Candidate],
    *,
    entity_type: str = "unknown",
    context: str | None = None,
    client: OllamaClient | None = None,
    model: str | None = None,
) -> Link:
    """Pick the candidate the article actually means, or none of them.

    Returning nothing is the right answer more often than it looks: measured on
    this corpus, two thirds of the names seen once have no Wikidata item at all.
    A chooser that must choose would rather invent a link than admit that.
    """
    usable = [c for c in candidates if _plausible(c, entity_type)]
    if not usable:
        return Link(None, 0.0, NO_MATCH, candidates_seen=len(candidates))

    client = client or get_ollama()
    try:
        payload = await client.chat_json(
            wikidata_messages(
                name,
                [(c.qid, c.label, c.description) for c in usable],
                entity_type=entity_type,
                context=context,
            ),
            purpose="wikidata",
            model=model,
        )
    except (OllamaError, ValueError) as exc:
        log.warning("wikidata choice failed", extra={"entity_name": name, "error": str(exc)})
        return Link(None, 0.0, f"เลือกไม่สำเร็จ: {exc}", candidates_seen=len(usable))

    qid = payload.get("qid")
    confidence = payload.get("confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    reason = str(payload.get("reason") or "")[:200]

    if not qid or qid in ("none", "None", "-1"):
        return Link(None, confidence, reason or NO_MATCH, candidates_seen=len(usable))
    # The model occasionally answers with a Q-number that was not on the list.
    # Accepting it would mean linking to an item nobody checked.
    if qid not in {c.qid for c in usable}:
        log.warning("wikidata choice not among candidates", extra={"entity_name": name, "qid": qid})
        return Link(None, 0.0, "โมเดลตอบ Q-id ที่ไม่ได้อยู่ในตัวเลือก", candidates_seen=len(usable))

    return Link(qid, confidence, reason, candidates_seen=len(usable))


async def link_name(
    name: str,
    *,
    aliases: list[str] | None = None,
    entity_type: str = "unknown",
    context: str | None = None,
    wikidata: WikidataClient | None = None,
    client: OllamaClient | None = None,
    model: str | None = None,
) -> Link:
    """Search, then choose. The whole path for one entity."""
    wikidata = wikidata or WikidataClient()
    try:
        found = await wikidata.candidates(name, also=aliases)
    except WikidataUnavailable as exc:
        return Link(None, 0.0, f"เชื่อมต่อ Wikidata ไม่ได้: {exc}")
    if not found:
        return Link(None, 0.0, NO_MATCH)
    return await choose(
        name, found, entity_type=entity_type, context=context, client=client, model=model
    )


# ── linking the entity store ─────────────────────────────────────────────────


async def link_entity(entity, *, context=None, wikidata=None, client=None, model=None) -> Link:
    """Resolve one stored entity and record the outcome on it.

    Generic nouns are skipped outright: "ตำรวจ" has a perfectly good Wikidata
    item for the concept of police, and attaching it would claim the article was
    about that concept rather than about some officers in Narathiwat.
    """
    from datetime import UTC, datetime

    settings = get_settings()
    if entity.entity_type == "generic":
        entity.qid_status = "no_match"
        entity.qid_reason = "คำนามทั่วไป ไม่ผูกกับรายการใด"
        entity.qid_checked_at = datetime.now(UTC)
        return Link(None, 0.0, entity.qid_reason)

    link = await link_name(
        entity.canonical_name,
        aliases=list(entity.aliases or []),
        entity_type=entity.entity_type,
        context=context,
        wikidata=wikidata,
        client=client,
        model=model,
    )

    entity.qid_checked_at = datetime.now(UTC)
    entity.qid_confidence = link.confidence
    entity.qid_reason = link.reason or None

    if link.reason.startswith("เชื่อมต่อ Wikidata ไม่ได้"):
        # Distinct from "no match" so a network outage does not permanently
        # mark half the store as unlinkable.
        entity.qid_status = "unavailable"
        return link
    if not link.linked:
        entity.qid_status = "no_match"
        return link

    if link.confidence < settings.wikidata_min_confidence:
        # Recorded but queued: a wrong Q-number is worse than none, because it
        # travels to OSINT//DESK and to every export while looking authoritative.
        entity.qid = link.qid
        entity.qid_status = "linked"
        if entity.review_status == "auto":
            entity.review_status = "needs_review"
            entity.risk = entity.risk or f"ผูกกับ {link.qid} แบบไม่มั่นใจ ({link.confidence:.2f})"
        return link

    entity.qid = link.qid
    entity.qid_status = "linked"
    return link
