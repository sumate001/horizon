"""SearXNG fetcher.

`sources.url` holds the query for this source (e.g. "ประเทศไทย เศรษฐกิจ"); the
instance itself comes from SEARXNG_URL so it can be moved without touching rows.
"""

import logging
from datetime import UTC, datetime

import httpx

from ..config import get_settings
from ..models import Source
from .types import FetchedArticle

log = logging.getLogger(__name__)


def _published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def fetch_searxng(source: Source, client: httpx.AsyncClient) -> list[FetchedArticle]:
    base_url = get_settings().searxng_url.strip()
    if not base_url:
        log.debug("SEARXNG_URL unset, skipping", extra={"source": source.name})
        return []

    response = await client.get(
        f"{base_url.rstrip('/')}/search",
        params={
            "q": source.url,
            "format": "json",
            "categories": "news",
            "language": "th",
            "time_range": "day",
        },
    )
    response.raise_for_status()

    articles: list[FetchedArticle] = []
    for result in response.json().get("results", []):
        url = (result.get("url") or "").strip()
        title = (result.get("title") or "").strip()
        if not url or not title:
            continue
        articles.append(
            FetchedArticle(
                url=url,
                title=title,
                body=result.get("content") or "",
                source_id=source.id,
                published_at=_published(result.get("publishedDate")),
                lang="th",
            )
        )
    return articles
