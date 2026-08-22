"""RSS / Atom fetcher."""

import logging
from datetime import datetime, timezone

import feedparser
import httpx
from selectolax.parser import HTMLParser

from ..models import Source
from .types import FetchedArticle

log = logging.getLogger(__name__)


def _text(html_or_text: str) -> str:
    if not html_or_text:
        return ""
    if "<" not in html_or_text:
        return html_or_text.strip()
    return HTMLParser(html_or_text).text(separator=" ", strip=True)


def _published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _body(entry) -> str:
    contents = entry.get("content") or []
    if contents:
        return _text(contents[0].get("value", ""))
    return _text(entry.get("summary", "") or entry.get("description", ""))


async def fetch_rss(source: Source, client: httpx.AsyncClient) -> list[FetchedArticle]:
    response = await client.get(source.url, follow_redirects=True)
    response.raise_for_status()

    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        log.warning(
            "unparseable feed",
            extra={"source": source.name, "error": str(feed.get("bozo_exception"))},
        )
        return []

    articles: list[FetchedArticle] = []
    for entry in feed.entries:
        url = (entry.get("link") or "").strip()
        title = _text(entry.get("title", ""))
        if not url or not title:
            continue
        articles.append(
            FetchedArticle(
                url=url,
                title=title,
                body=_body(entry),
                source_id=source.id,
                published_at=_published(entry),
                lang=feed.feed.get("language"),
            )
        )
    return articles
