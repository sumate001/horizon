"""HTML → readable text.

RSS feeds usually carry a teaser, not the article. Extraction quality depends
heavily on having the body, so short entries get a follow-up page fetch.
"""

import logging

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)

_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "figure",
)

#: Tried in order; first non-trivial match wins.
_CONTENT_SELECTORS = (
    "article",
    "main",
    '[itemprop="articleBody"]',
    ".entry-content",
    ".article-content",
    ".post-content",
    "#content",
)


def extract_main_text(html: str, *, min_chars: int = 200) -> str:
    """Best-effort main-content text. Falls back to the whole body."""
    if not html:
        return ""

    tree = HTMLParser(html)
    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    for selector in _CONTENT_SELECTORS:
        nodes = tree.css(selector)
        if not nodes:
            continue
        text = "\n".join(node.text(separator="\n", strip=True) for node in nodes)
        if len(text) >= min_chars:
            return text

    body = tree.body
    return body.text(separator="\n", strip=True) if body else ""


async def fetch_fulltext(url: str, client: httpx.AsyncClient) -> str:
    """Fetch and extract one article body. Returns "" on any failure."""
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        if "html" not in response.headers.get("content-type", "").lower():
            return ""
        return extract_main_text(response.text)
    except Exception as exc:  # noqa: BLE001
        log.debug("fulltext fetch failed", extra={"url": url, "error": str(exc)})
        return ""
