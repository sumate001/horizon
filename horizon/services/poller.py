"""horizon-poller — RHYTHM 1, streaming.

Every POLL_INTERVAL_MINUTES: read the source registry, fetch each active source,
persist new articles, enqueue their ids. Articles are durable in Postgres before
they reach the queue, so a Redis flush costs a re-enqueue, not data.

URL uniqueness is enforced by the database (`raw_articles.url UNIQUE`), which
also dedupes the same story arriving from several feeds.
"""

import asyncio
import logging
import signal
import uuid

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..config import get_settings
from ..db import session_scope
from ..logging import setup_logging
from ..metrics import articles_fetched
from ..models import RawArticle, Source
from ..queue import ArticleQueue
from ..sources import FetchedArticle, fetch_fulltext, fetch_source

log = logging.getLogger("horizon.poller")

USER_AGENT = "HorizonBot/0.1 (+news intelligence pipeline)"


async def _fill_bodies(articles: list[FetchedArticle], client: httpx.AsyncClient) -> list[FetchedArticle]:
    """Fetch full pages for entries whose feed body is too thin to extract from."""
    settings = get_settings()
    if not settings.fetch_fulltext:
        return articles

    semaphore = asyncio.Semaphore(settings.fulltext_concurrency)

    async def fill(article: FetchedArticle) -> FetchedArticle:
        if len(article.body) >= settings.fulltext_min_chars:
            return article
        async with semaphore:
            body = await fetch_fulltext(article.url, client)
        if len(body) <= len(article.body):
            return article
        return FetchedArticle(
            url=article.url,
            title=article.title,
            body=body,
            source_id=article.source_id,
            published_at=article.published_at,
            lang=article.lang,
        )

    return list(await asyncio.gather(*(fill(a) for a in articles)))


async def _persist(articles: list[FetchedArticle]) -> list[uuid.UUID]:
    """Insert articles, skipping URLs already seen. Returns the ids actually created."""
    if not articles:
        return []

    # Same URL twice inside one batch would trip ON CONFLICT's single-row rule.
    unique = {a.url: a for a in articles}
    rows = [
        {
            "id": uuid.uuid4(),
            "source_id": a.source_id,
            "url": a.url,
            "title": a.title,
            "body": a.body,
            "lang": a.lang,
            "status": "queued",
        }
        for a in unique.values()
    ]

    statement = (
        insert(RawArticle)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[RawArticle.url])
        .returning(RawArticle.id)
    )
    async with session_scope() as session:
        result = await session.execute(statement)
        return list(result.scalars().all())


async def poll_once() -> int:
    """One full pass over the source registry. Returns the number of articles enqueued."""
    settings = get_settings()
    queue = ArticleQueue()

    depth = await queue.depth()
    if depth > settings.queue_max_depth:
        log.warning("backpressure — skipping poll", extra={"queue_depth": depth})
        return 0

    async with session_scope() as session:
        sources = list(
            (await session.execute(select(Source).where(Source.active.is_(True)))).scalars()
        )

    if not sources:
        log.warning("no active sources configured")
        return 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.fetch_timeout),
        headers={"User-Agent": USER_AGENT},
    ) as client:
        batches = await asyncio.gather(*(fetch_source(s, client) for s in sources))
        for source, batch in zip(sources, batches, strict=True):
            articles_fetched.labels(source.type).inc(len(batch))
            log.info(
                "fetched source", extra={"source": source.name, "articles": len(batch)}
            )

        fetched = [article for batch in batches for article in batch]
        fetched = await _fill_bodies(fetched, client)

    created = await _persist(fetched)
    enqueued = await queue.push_many(created)

    log.info(
        "poll complete",
        extra={
            "sources": len(sources),
            "fetched": len(fetched),
            "new_articles": len(created),
            "enqueued": enqueued,
            "queue_depth": await queue.depth(),
        },
    )
    return enqueued


async def main() -> None:
    settings = get_settings()
    setup_logging("horizon.poller", settings.log_level)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        poll_once,
        "interval",
        minutes=settings.poll_interval_minutes,
        id="poll",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("poller started", extra={"interval_minutes": settings.poll_interval_minutes})

    await poll_once()  # don't wait a full interval for the first batch

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    scheduler.shutdown(wait=False)
    log.info("poller stopped")


if __name__ == "__main__":
    asyncio.run(main())
