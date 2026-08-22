"""Step 4 — two-layer deduplication, ordered cheap → expensive.

L1  MinHash/LSH over title+body character shingles (Jaccard ≥ DEDUP_MINHASH_T).
    A hit is a plain duplicate: bump source_count, raise credibility_weight, stop.
L2  bge-m3 embedding of title+summary searched against Qdrant within a 7-day
    window (cosine ≥ DEDUP_COSINE_T). A hit is either a duplicate or an *update*
    — see `classify_match`.

Thai text has no word boundaries, so shingles are character n-grams rather than
word n-grams; word tokenization would need a Thai segmenter and buy nothing here.

This module makes decisions only. The worker owns every database write, which
keeps the whole layer unit-testable without Postgres or Qdrant.
"""

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import numpy as np
from datasketch import MinHash, MinHashLSH

from ..config import get_settings
from ..metrics import dedup_hits
from .extract import Extraction
from .vectors import VectorStore, utcnow

log = logging.getLogger(__name__)

NUM_PERM = 128
SHINGLE_K = 5

_WS_RE = re.compile(r"\s+")
#: Arabic and Thai digits, including decimals and thousands separators.
_NUM_RE = re.compile(r"[0-9๐-๙][0-9๐-๙.,]*")

MatchAction = Literal["new", "duplicate", "update"]


# ── Text → MinHash ───────────────────────────────────────────────────────────


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def shingles(text: str, k: int = SHINGLE_K) -> set[str]:
    """Character k-grams. Short inputs degrade to a single shingle, not an empty set."""
    normalized = normalize_text(text)
    if len(normalized) <= k:
        return {normalized} if normalized else set()
    return {normalized[i : i + k] for i in range(len(normalized) - k + 1)}


def build_minhash(title: str, body: str, num_perm: int = NUM_PERM) -> MinHash:
    minhash = MinHash(num_perm=num_perm)
    for shingle in shingles(f"{title or ''} {body or ''}"):
        minhash.update(shingle.encode("utf-8"))
    return minhash


def numbers_in(text: str) -> set[str]:
    """Numeric tokens, trailing punctuation stripped — used for the update check."""
    return {match.rstrip(".,") for match in _NUM_RE.findall(text or "")}


# ── Decisions ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """The parts of an existing event needed to decide duplicate vs update."""

    event_id: uuid.UUID
    summary: str
    event_time: datetime | None
    credibility_weight: float
    #: Stored 128-perm signature, used to confirm an approximate L1 hit exactly.
    minhash_sig: bytes | None = None

    def minhash(self, num_perm: int = NUM_PERM) -> MinHash | None:
        if not self.minhash_sig:
            return None
        values = np.frombuffer(self.minhash_sig, dtype=np.uint64)
        if len(values) != num_perm:
            return None
        return MinHash(num_perm=num_perm, hashvalues=values)


@dataclass(frozen=True)
class DedupResult:
    action: MatchAction
    layer: str | None = None
    event_id: uuid.UUID | None = None
    similarity: float = 0.0
    reason: str = ""

    @property
    def is_new(self) -> bool:
        return self.action == "new"


def classify_match(new: Extraction, existing: Candidate) -> tuple[MatchAction, str]:
    """Same story, or the same story with new facts?

    Material change means a different event time, or different numbers in the
    summary (casualty counts, rates, amounts). Either one makes it an update:
    the event keeps its identity but gains a refreshed summary and an audit entry.
    """
    if new.event_time != existing.event_time:
        return "update", "event_time changed"

    if numbers_in(new.summary) != numbers_in(existing.summary):
        return "update", "numeric values changed"

    return "duplicate", "no material difference"


# ── L1 index ─────────────────────────────────────────────────────────────────


class MinHashIndex:
    """LSH index over recent events.

    Backed by Redis so it survives restarts and is shared across worker replicas.
    Falls back to an in-process index if the Redis backend cannot be constructed —
    dedup still works, it just degrades to per-process state until Redis returns.

    Entries are never expired here; the batch job prunes keys older than the
    dedup window (see `prune_before`).
    """

    def __init__(
        self,
        threshold: float | None = None,
        num_perm: int = NUM_PERM,
        redis_url: str | None = None,
        basename: bytes = b"horizon_l1",
    ) -> None:
        settings = get_settings()
        self.threshold = settings.dedup_minhash_t if threshold is None else threshold
        self.num_perm = num_perm
        self.backend = "memory"
        self._lsh = self._build(redis_url, basename)

    def _build(self, redis_url: str | None, basename: bytes) -> MinHashLSH:
        if redis_url is None:
            return MinHashLSH(threshold=self.threshold, num_perm=self.num_perm)

        try:
            from urllib.parse import urlparse

            parsed = urlparse(redis_url)
            lsh = MinHashLSH(
                threshold=self.threshold,
                num_perm=self.num_perm,
                storage_config={
                    "type": "redis",
                    "basename": basename,
                    "redis": {"host": parsed.hostname or "redis", "port": parsed.port or 6379},
                },
            )
            self.backend = "redis"
            return lsh
        except Exception as exc:  # noqa: BLE001 — degrade, never fail startup
            log.warning(
                "redis-backed LSH unavailable, using in-process index",
                extra={"error": str(exc)},
            )
            return MinHashLSH(threshold=self.threshold, num_perm=self.num_perm)

    def query(self, minhash: MinHash) -> list[uuid.UUID]:
        return [uuid.UUID(str(key)) for key in self._lsh.query(minhash)]

    def insert(self, event_id: uuid.UUID, minhash: MinHash) -> None:
        key = str(event_id)
        if key in self._lsh:
            return
        self._lsh.insert(key, minhash)

    def remove(self, event_id: uuid.UUID) -> None:
        key = str(event_id)
        if key in self._lsh:
            self._lsh.remove(key)


# ── Orchestration ────────────────────────────────────────────────────────────

CandidateLoader = Callable[[uuid.UUID], Awaitable[Candidate | None]]


class Deduplicator:
    def __init__(
        self,
        index: MinHashIndex,
        vectors: VectorStore,
        load_candidate: CandidateLoader,
        *,
        minhash_threshold: float | None = None,
        cosine_threshold: float | None = None,
        window_days: int | None = None,
    ) -> None:
        settings = get_settings()
        self.index = index
        self.vectors = vectors
        self.load_candidate = load_candidate
        self.minhash_threshold = (
            settings.dedup_minhash_t if minhash_threshold is None else minhash_threshold
        )
        self.cosine_threshold = (
            settings.dedup_cosine_t if cosine_threshold is None else cosine_threshold
        )
        self.window_days = settings.dedup_window_days if window_days is None else window_days

    async def check(
        self,
        *,
        title: str,
        body: str,
        extraction: Extraction,
        embedding: list[float],
        minhash: MinHash | None = None,
    ) -> DedupResult:
        """Run L1 then L2. Returns `new` when nothing matches."""
        result = await self._check_l1(title, body, minhash)
        if not result.is_new:
            return result
        return await self._check_l2(extraction, embedding)

    async def _check_l1(
        self, title: str, body: str, minhash: MinHash | None
    ) -> DedupResult:
        minhash = minhash or build_minhash(title, body)

        for event_id in self.index.query(minhash):
            candidate = await self.load_candidate(event_id)
            if candidate is None:
                self.index.remove(event_id)  # index outlived the event
                continue

            # LSH querying is approximate, so confirm against the stored signature.
            # A candidate with no signature is trusted — the index put it there.
            stored = candidate.minhash()
            similarity = minhash.jaccard(stored) if stored is not None else self.minhash_threshold
            if similarity < self.minhash_threshold:
                continue

            # The spec treats an L1 hit as a plain duplicate: at Jaccard ≥ 0.85 over
            # character shingles the texts are near-identical, so there is nothing
            # new to merge. Material-change detection is L2's job.
            dedup_hits.labels("l1", "duplicate").inc()
            return DedupResult(
                action="duplicate",
                layer="l1",
                event_id=event_id,
                similarity=similarity,
                reason="minhash jaccard above threshold",
            )

        return DedupResult(action="new")

    async def _check_l2(self, extraction: Extraction, embedding: list[float]) -> DedupResult:
        if not embedding:
            return DedupResult(action="new", reason="no embedding")

        since = utcnow() - timedelta(days=self.window_days)
        hits = await self.vectors.search(embedding, limit=5, since=since)

        for hit in hits:
            if hit.score < self.cosine_threshold:
                break  # results are score-ordered — nothing further can match

            candidate = await self.load_candidate(hit.event_id)
            if candidate is None:
                # Vector outlived its event (deleted row). Drop the stale point.
                log.info("stale vector, deleting", extra={"event_id": str(hit.event_id)})
                await self.vectors.delete_event(hit.event_id)
                continue

            action, reason = classify_match(extraction, candidate)
            dedup_hits.labels("l2", action).inc()
            return DedupResult(
                action=action,
                layer="l2",
                event_id=candidate.event_id,
                similarity=hit.score,
                reason=reason,
            )

        return DedupResult(action="new")

    def register(self, event_id: uuid.UUID, minhash: MinHash) -> None:
        """Add a freshly inserted event to the L1 index."""
        self.index.insert(event_id, minhash)
