import os

# Settings are read at import time via lru_cache — pin them before anything
# imports horizon.config so tests never depend on a developer's .env.
os.environ.setdefault("POSTGRES_URL", "postgresql+asyncpg://horizon:x@localhost:5433/horizon")
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("QDRANT_URL", "http://localhost:6343")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")

import pytest

from horizon.config import get_settings


@pytest.fixture(autouse=True)
def settings():
    get_settings.cache_clear()
    return get_settings()
