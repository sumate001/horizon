from .base import FetchedArticle, fetch_source
from .fulltext import extract_main_text, fetch_fulltext

__all__ = ["FetchedArticle", "fetch_source", "extract_main_text", "fetch_fulltext"]
