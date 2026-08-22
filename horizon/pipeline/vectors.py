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
        self.centroid_collection = settings.qdrant_centroid_collection
        self._client = client or AsyncQdrantClient(url=settings.qdrant_url)
        self._ready = False
        self._centroids_ready = False

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

    async def scroll_events(
        self, *, since: datetime | None = None, limit: int = 20000
    ) -> list[tuple[uuid.UUID, list[float]]]:
        """Every stored vector in the window, oldest page first.

        Used by the clustering job, which needs the raw embeddings rather than
        nearest neighbours. Returns at most `limit` points.
        """
        await self.ensure_collection()
        scroll_filter = None
        if since is not None:
            scroll_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="created_at", range=models.Range(gte=int(since.timestamp()))
                    )
                ]
            )

        out: list[tuple[uuid.UUID, list[float]]] = []
        offset = None
        while len(out) < limit:
            points, offset = await self._client.scroll(
                collection_name=self.collection,
                scroll_filter=scroll_filter,
                limit=min(1024, limit - len(out)),
                offset=offset,
                with_vectors=True,
                with_payload=False,
            )
            out.extend((uuid.UUID(str(p.id)), list(p.vector)) for p in points if p.vector)
            if offset is None or not points:
                break
        return out

    async def upsert_centroid(
        self, centroid_id: uuid.UUID, vector: list[float], *, cluster_id: uuid.UUID
    ) -> None:
        """Cluster centroids live in their own collection.

        Run-to-run label stability and weak-signal novelty both need to search
        centroids without event vectors polluting the results.
        """
        await self.ensure_centroids(len(vector))
        await self._client.upsert(
            collection_name=self.centroid_collection,
            points=[
                models.PointStruct(
                    id=str(centroid_id),
                    vector=vector,
                    payload={"cluster_id": str(cluster_id)},
                )
            ],
        )

    async def ensure_centroids(self, dim: int = EMBEDDING_DIM) -> None:
        if self._centroids_ready:
            return
        if not await self._client.collection_exists(self.centroid_collection):
            await self._client.create_collection(
                collection_name=self.centroid_collection,
                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
            )
            log.info("created qdrant collection", extra={"collection": self.centroid_collection})
        self._centroids_ready = True

    async def search_centroids(self, vector: list[float], *, limit: int = 5) -> list[VectorHit]:
        await self.ensure_centroids(len(vector))
        response = await self._client.query_points(
            collection_name=self.centroid_collection,
            query=vector,
            limit=limit,
            with_payload=True,
        )
        return [
            VectorHit(
                event_id=uuid.UUID(str(point.payload["cluster_id"])),
                score=float(point.score),
                payload=point.payload or {},
            )
            for point in response.points
            if point.payload and point.payload.get("cluster_id")
        ]

    async def get_centroid(self, cluster_id: uuid.UUID) -> list[float] | None:
        await self.ensure_centroids()
        points = await self._client.retrieve(
            collection_name=self.centroid_collection,
            ids=[str(cluster_id)],
            with_vectors=True,
        )
        if not points or not points[0].vector:
            return None
        return list(points[0].vector)

    async def clear_centroids(self) -> None:
        """Centroids are fully rebuilt each clustering run — stale ones would
        keep matching clusters that no longer exist."""
        if await self._client.collection_exists(self.centroid_collection):
            await self._client.delete_collection(self.centroid_collection)
        self._centroids_ready = False

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
