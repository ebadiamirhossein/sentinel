"""REST clients for news, sentiment, macro and FX (specs/DATA_SOURCES.md §2.2-§2.4)."""

from sentinel.ingestion.clients.fx import FxClient
from sentinel.ingestion.clients.macro import MacroClient
from sentinel.ingestion.clients.news import NewsClient
from sentinel.ingestion.clients.sentiment import SentimentClient

__all__ = ["FxClient", "MacroClient", "NewsClient", "SentimentClient"]
