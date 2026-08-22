"""horizon-worker — RHYTHM 1, streaming.

Pops article ids off the Redis queue and runs each through:

    gate → extraction + classification → dedup L1 → dedup L2 → persist

Every article is wrapped: a failure marks that row `failed` and the loop keeps
going. Nothing an article can do may kill the worker.
"""

import asyncio
import logging
import signal
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..llm.ollama import OllamaError, get_ollama
from ..logging import setup_logging
from ..metrics import articles_processed, extraction_latency
from ..models import Event, RawArticle, Source
from ..pipeline.dedup import Candidate, Deduplicator, MinHashIndex, build_minhash
from ..pipeline.extract import Extraction, ExtractionError, extract_event
from ..pipeline.gate import build_gate
from ..pipeline.vectors import get_vector_store
from ..queue import ArticleQueue

log = logging.getLogger("horizon.worker")

DEFAULT_CREDIBILITY = 0.5


class ArticleWorker:
    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.queue = ArticleQueue()
        self.gate = build_gate()
        self.ollama = get_ollama()
        self.vectors = get_vector_store()
        self.index = MinHashIndex(redis_url=settings.redis_url)
        self.dedup = Deduplicator(self.index, self.vectors, self._load_candidate)
        self._stop = asyncio.Event()

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _load_candidate(self, event_id: uuid.UUID) -> Candidate | None:
        async with session_scope() as session:
            event = await session.get(Event, event_id)
            if event is None:
                return None
            return Candidate(
                event_id=event.id,
                summary=event.summary or "",
                event_time=event.event_time,
                credibility_weight=event.credibility_weight,
                minhash_sig=event.minhash_sig,
            )

    async def _finish(self, article_id: uuid.UUID, status: str) -> None:
        async with session_scope() as session:
            article = await session.get(RawArticle, article_id)
            if article is not None:
                article.status = status
        articles_processed.labels(status).inc()

    # ── main path ────────────────────────────────────────────────────────────

    async def process(self, article_id: uuid.UUID) -> str:
        """Run one article end to end. Returns the terminal raw_articles.status."""
        async with session_scope() as session:
            article = await session.get(RawArticle, article_id)
            if article is None:
                log.warning("article vanished", extra={"article_id": str(article_id)})
                return "failed"
            if article.status != "queued":
                log.debug(
                    "already processed",
                    extra={"article_id": str(article_id), "status": article.status},
                )
                return article.status

            title, body, url = article.title or "", article.body or "", article.url
            credibility = DEFAULT_CREDIBILITY
            if article.source_id:
                source = await session.get(Source, article.source_id)
                if source is not None:
                    credibility = source.credibility_weight

        # Step 2 — fake news gate (phase 1: source credibility only)
        decision = self.gate.evaluate(title=title, body=body, credibility_weight=credibility)
        if not decision.passed:
            log.info("gated", extra={"url": url, "reason": decision.reason})
            await self._finish(article_id, decision.status)
            return decision.status

        # Steps 1 + 3 — extraction and classification
        try:
            extraction = await extract_event(title, body, client=self.ollama)
        except ExtractionError as exc:
            log.warning("extraction failed", extra={"url": url, "error": str(exc)})
            await self._finish(article_id, "failed")
            return "failed"

        # Step 4 — dedup. L2 needs an embedding of title + summary.
        try:
            embedding = await self.ollama.embed_one(f"{title}\n{extraction.summary}")
        except OllamaError as exc:
            # Retryable: leave the row queued-but-failed so it can be re-enqueued.
            log.warning("embedding failed", extra={"url": url, "error": str(exc)})
            await self._finish(article_id, "failed")
            return "failed"

        minhash = build_minhash(title, body)
        result = await self.dedup.check(
            title=title,
            body=body,
            extraction=extraction,
            embedding=embedding,
            minhash=minhash,
        )

        if result.action == "duplicate":
            await self._merge_duplicate(result.event_id, credibility)
            log.info(
                "duplicate",
                extra={
                    "url": url,
                    "layer": result.layer,
                    "event_id": str(result.event_id),
                    "similarity": round(result.similarity, 4),
                },
            )
            await self._finish(article_id, "dropped_duplicate")
            return "dropped_duplicate"

        if result.action == "update":
            await self._merge_update(result.event_id, extraction, credibility, article_id, result)
            log.info(
                "event updated",
                extra={
                    "url": url,
                    "event_id": str(result.event_id),
                    "similarity": round(result.similarity, 4),
                    "reason": result.reason,
                },
            )
            await self._finish(article_id, "processed")
            return "processed"

        event_id = await self._insert_event(article_id, extraction, credibility, minhash)
        await self.vectors.upsert_event(
            event_id,
            embedding,
            created_at=datetime.now(timezone.utc),
            categories=extraction.categories,
            summary=extraction.summary,
        )
        self.dedup.register(event_id, minhash)
        log.info(
            "event created",
            extra={
                "url": url,
                "event_id": str(event_id),
                "categories": extraction.categories,
                "incomplete": extraction.incomplete,
            },
        )
        await self._finish(article_id, "processed")
        return "processed"

    # ── persistence ──────────────────────────────────────────────────────────

    async def _insert_event(
        self,
        article_id: uuid.UUID,
        extraction: Extraction,
        credibility: float,
        minhash,
    ) -> uuid.UUID:
        event_id = uuid.uuid4()
        async with session_scope() as session:
            session.add(
                Event(
                    id=event_id,
                    raw_article_id=article_id,
                    actors=extraction.actors,
                    action=extraction.action,
                    location=extraction.location,
                    event_time=extraction.event_time,
                    categories=extraction.categories,
                    summary=extraction.summary,
                    extraction_confidence=extraction.confidence,
                    incomplete=extraction.incomplete,
                    source_count=1,
                    credibility_weight=credibility,
                    # One vector per event, keyed by the event id itself.
                    embedding_id=event_id,
                    event_updates=[],
                    minhash_sig=minhash.hashvalues.tobytes(),
                )
            )
        return event_id

    async def _merge_duplicate(self, event_id: uuid.UUID | None, credibility: float) -> None:
        if event_id is None:
            return
        async with session_scope() as session:
            event = await session.get(Event, event_id)
            if event is None:
                return
            event.source_count += 1
            event.credibility_weight = max(event.credibility_weight, credibility)

    async def _merge_update(
        self,
        event_id: uuid.UUID | None,
        extraction: Extraction,
        credibility: float,
        article_id: uuid.UUID,
        result,
    ) -> None:
        """Same event, new facts: refresh the summary and keep an audit entry."""
        if event_id is None:
            return
        async with session_scope() as session:
            event = await session.get(Event, event_id)
            if event is None:
                return
            entry = {
                "at": datetime.now(timezone.utc).isoformat(),
                "raw_article_id": str(article_id),
                "reason": result.reason,
                "similarity": round(result.similarity, 4),
                "previous_summary": event.summary,
                "previous_event_time": event.event_time.isoformat() if event.event_time else None,
            }
            event.source_count += 1
            event.credibility_weight = max(event.credibility_weight, credibility)
            event.summary = extraction.summary or event.summary
            if extraction.event_time is not None:
                event.event_time = extraction.event_time
            # JSONB columns are not mutation-tracked — rebind the list.
            event.event_updates = [*(event.event_updates or []), entry]

    # ── loop ─────────────────────────────────────────────────────────────────

    async def _consume(self, worker_no: int) -> None:
        log.info("consumer started", extra={"worker": worker_no})
        while not self._stop.is_set():
            try:
                article_id = await self.queue.pop(timeout=5)
            except Exception as exc:  # noqa: BLE001 — Redis blip, back off and retry
                log.warning("queue pop failed", extra={"error": str(exc)})
                await asyncio.sleep(2)
                continue

            if article_id is None:
                continue

            started = asyncio.get_running_loop().time()
            try:
                await self.process(article_id)
            except Exception as exc:  # noqa: BLE001 — never let one article kill the loop
                log.exception(
                    "unhandled article failure",
                    extra={"article_id": str(article_id), "error": str(exc)},
                )
                try:
                    await self._finish(article_id, "failed")
                except Exception:  # noqa: BLE001
                    log.exception("could not mark article failed")
            finally:
                extraction_latency.observe(asyncio.get_running_loop().time() - started)

        log.info("consumer stopped", extra={"worker": worker_no})

    async def run(self) -> None:
        await self.vectors.ensure_collection()
        log.info(
            "worker started",
            extra={
                "concurrency": self.settings.worker_concurrency,
                "l1_backend": self.index.backend,
                "minhash_t": self.settings.dedup_minhash_t,
                "cosine_t": self.settings.dedup_cosine_t,
            },
        )
        await asyncio.gather(
            *(self._consume(i) for i in range(self.settings.worker_concurrency))
        )

    def stop(self) -> None:
        self._stop.set()


STUCK_AFTER = timedelta(hours=1)


async def requeue_stuck() -> int:
    """Re-enqueue rows left `queued` by a crash or a Redis flush.

    Only rows older than STUCK_AFTER, so articles still sitting in a healthy
    queue are not enqueued a second time and processed concurrently.
    """
    queue = ArticleQueue()
    cutoff = datetime.now(timezone.utc) - STUCK_AFTER
    async with session_scope() as session:
        ids = list(
            (
                await session.execute(
                    select(RawArticle.id)
                    .where(RawArticle.status == "queued", RawArticle.fetched_at < cutoff)
                    .limit(5000)
                )
            ).scalars()
        )
    if ids:
        await queue.push_many(ids)
        log.info("requeued stuck articles", extra={"count": len(ids)})
    return len(ids)


async def main() -> None:
    settings = get_settings()
    setup_logging("horizon.worker", settings.log_level)

    worker = ArticleWorker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)

    await requeue_stuck()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
