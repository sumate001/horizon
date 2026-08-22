"""Qdrant wrapper for the `horizon_events` collection.

One point per event, keyed by the event UUID. `created_at` is stored as an epoch
second so range filters (the 7-day dedup window, the 30-day clustering window)
are plain integer comparisons.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from qdrant_client import AsyncQdrantClient, models

from ..config import get_settings

log = logging.getLogger(__name__)

#: bge-m3 output dimensionality.
EMBEDDING_DIM = 1024


@dataclass(frozen=True)
class VectorHit:
    event_id: uuid.UUID
    score: float
    payload: dict


class VectorStore:
    def __init__(self, client: AsyncQdrantClient | None = None, collection: str | None = None):
        settings = get_settings()
        self.collection = collection or settings.qdrant_collection
        self._client = client or AsyncQdrantClient(url=settings.qdrant_url)
        self._ready = False

    async def ensure_collection(self, dim: int = EMBEDDING_DIM) -> None:
        """Idempotent — safe to call on every worker start."""
        if self._ready:
            return
        if not await self._client.collection_exists(self.collection):
            await self._client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=dim, distance=models.Distance.COSINE
                ),
            )
            await self._client.create_payload_index(
                collection_name=self.collection,
                field_name="created_at",
                field_schema=models.PayloadSchemaType.INTEGER,
            )
            log.info("created qdrant collection", extra={"collection": self.collection})
        self._ready = True

    async def upsert_event(
        self,
        event_id: uuid.UUID,
        vector: list[float],
        *,
        created_at: datetime,
        categories: list[str] | None = None,
        summary: str = "",
    ) -> uuid.UUID:
        await self.ensure_collection(len(vector))
        await self._client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=str(event_id),
                    vector=vector,
                    payload={
                        "event_id": str(event_id),
                        "created_at": int(created_at.timestamp()),
                        "categories": categories or [],
                        "summary": summary,
                    },
                )
            ],
        )
        return event_id

    async def search(
        self, vector: list[float], *, limit: int = 10, since: datetime | None = None
    ) -> list[VectorHit]:
        await self.ensure_collection(len(vector))
        query_filter = None
        if since is not None:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="created_at",
                        range=models.Range(gte=int(since.timestamp())),
                    )
                ]
            )

        response = await self._client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )
        return [
            VectorHit(
                event_id=uuid.UUID(str(point.id)),
                score=float(point.score),
                payload=point.payload or {},
            )
            for point in response.points
        ]

    async def delete_event(self, event_id: uuid.UUID) -> None:
        await self._client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=[str(event_id)]),
        )

    async def aclose(self) -> None:
        await self._client.close()


@lru_cache
def get_vector_store() -> VectorStore:
    return VectorStore()


def utcnow() -> datetime:
    return datetime.now(UTC)
