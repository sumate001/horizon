import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FetchedArticle:
    url: str
    title: str
    body: str
    source_id: uuid.UUID
    published_at: datetime | None = None
    lang: str | None = None
