"""Redis work queue between poller and worker.

A plain Redis list: LPUSH from the poller, BRPOP in the worker. Article rows are
already durable in Postgres before the id is enqueued, so a lost queue entry
costs a reprocess (`status='queued'` rows can be re-enqueued), never data.
"""

import logging
import uuid
from functools import lru_cache

import redis.asyncio as redis

from .config import get_settings
from .metrics import queue_depth

log = logging.getLogger(__name__)


@lru_cache
def get_redis() -> redis.Redis:
    return redis.from_url(get_settings().redis_url, decode_responses=True)


class ArticleQueue:
    def __init__(self, client: redis.Redis | None = None, key: str | None = None) -> None:
        settings = get_settings()
        self.client = client or get_redis()
        self.key = key or settings.queue_key

    async def push(self, article_id: uuid.UUID) -> None:
        await self.client.lpush(self.key, str(article_id))

    async def push_many(self, article_ids: list[uuid.UUID]) -> int:
        if not article_ids:
            return 0
        await self.client.lpush(self.key, *[str(i) for i in article_ids])
        return len(article_ids)

    async def pop(self, timeout: int = 5) -> uuid.UUID | None:
        """Blocking pop. None on timeout so the caller can check its shutdown flag."""
        item = await self.client.brpop(self.key, timeout=timeout)
        if item is None:
            return None
        try:
            return uuid.UUID(item[1])
        except ValueError:
            log.warning("dropping non-uuid queue entry", extra={"raw": item[1]})
            return None

    async def depth(self) -> int:
        value = int(await self.client.llen(self.key))
        queue_depth.set(value)
        return value
