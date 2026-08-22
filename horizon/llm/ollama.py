"""Ollama client — chat (JSON mode) and embeddings.

Points at the shared A5000 box. Only models already resident may be requested:
`gemma4:12b` and `bge-m3`. Never pull, never load anything else — the server runs a
no-swap VRAM policy and an unexpected model would evict a resident one.

Every call is timeout-bounded, retried once, and counted in `horizon.metrics`.
"""

import json
import logging
import re
import time
from functools import lru_cache
from typing import Any

import httpx

from ..config import get_settings
from ..metrics import llm_calls, llm_latency

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class OllamaError(RuntimeError):
    """Ollama was unreachable, timed out, or returned an unusable body."""


def strip_think(text: str) -> str:
    """Drop reasoning blocks that thinking models leak into the content field."""
    return _THINK_RE.sub("", text).strip()


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response.

    `format: json` normally guarantees a clean body, but a model that stops early
    or wraps the object in prose still happens. Fall back to the outermost
    brace-delimited span before giving up.
    """
    cleaned = strip_think(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in response: {cleaned[:200]!r}")
    return json.loads(cleaned[start : end + 1])


class OllamaClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.timeout = timeout or settings.llm_timeout
        self.chat_model = settings.extract_model
        self.embed_model = settings.embed_model
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=httpx.Timeout(self.timeout)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._http()
        response = await client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    async def chat_raw(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "chat",
        model: str | None = None,
    ) -> str:
        """One JSON-mode chat completion, returned as raw text.

        Retries once on *transport* failure only. Malformed JSON is the caller's
        problem — extraction repairs it with a second prompt (see pipeline/extract.py),
        which needs the broken text this returns.
        """
        payload = {
            "model": model or self.chat_model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "think": False,  # ignored by older Ollama builds, harmless
            "options": {"temperature": 0},
        }

        last_error: Exception | None = None
        started = time.perf_counter()
        for attempt in (1, 2):
            try:
                body = await self._post("/api/chat", payload)
                llm_latency.labels(purpose).observe(time.perf_counter() - started)
                llm_calls.labels(purpose, "ok").inc()
                return strip_think(body.get("message", {}).get("content", ""))
            except Exception as exc:  # noqa: BLE001 — one article must never kill a loop
                last_error = exc
                log.warning(
                    "llm call failed",
                    extra={"purpose": purpose, "attempt": attempt, "error": str(exc)},
                )

        llm_latency.labels(purpose).observe(time.perf_counter() - started)
        llm_calls.labels(purpose, "error").inc()
        raise OllamaError(f"{purpose} failed after 2 attempts: {last_error}") from last_error

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "chat",
        model: str | None = None,
    ) -> dict[str, Any]:
        raw = await self.chat_raw(messages, purpose=purpose, model=model)
        return extract_json(raw)

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embed a batch with bge-m3. Returns one vector per input, in order."""
        if not texts:
            return []

        payload = {"model": model or self.embed_model, "input": texts}
        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in (1, 2):
            try:
                body = await self._post("/api/embed", payload)
                vectors = body.get("embeddings")
                if not vectors or len(vectors) != len(texts):
                    got = len(vectors) if vectors else 0
                    raise ValueError(f"expected {len(texts)} embeddings, got {got}")
                llm_latency.labels("embed").observe(time.perf_counter() - started)
                llm_calls.labels("embed", "ok").inc()
                return vectors
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning(
                    "embed call failed", extra={"attempt": attempt, "error": str(exc)}
                )

        llm_calls.labels("embed", "error").inc()
        raise OllamaError(f"embed failed after 2 attempts: {last_error}") from last_error

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]


@lru_cache
def get_ollama() -> OllamaClient:
    return OllamaClient()
