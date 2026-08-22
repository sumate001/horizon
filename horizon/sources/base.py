"""Source dispatch — one fetcher per `sources.type`."""

import logging

import httpx

from ..models import Source
from .rss import fetch_rss
from .searxng import fetch_searxng
from .types import FetchedArticle

log = logging.getLogger(__name__)


async def fetch_source(source: Source, client: httpx.AsyncClient) -> list[FetchedArticle]:
    """Fetch one source. Returns [] on any failure — a dead feed must not stop the poll."""
    try:
        if source.type == "rss":
            return await fetch_rss(source, client)
        if source.type == "searxng":
            return await fetch_searxng(source, client)
        if source.type == "watchlist_hook":
            # Pushed in by OSINT//DESK rather than polled — nothing to fetch.
            return []
        log.warning("unknown source type", extra={"source": source.name, "type": source.type})
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "source fetch failed",
            extra={"source": source.name, "url": source.url, "error": str(exc)},
        )
        return []
