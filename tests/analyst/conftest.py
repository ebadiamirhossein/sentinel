"""Analyst-specific fixtures. Shared market and LLM doubles live in the root conftest."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sentinel.ingestion.models import MarketSnapshot, SentimentContext


@pytest.fixture
def sentiment_snapshot(snapshot: MarketSnapshot) -> MarketSnapshot:
    return snapshot.model_copy(
        update={
            "sentiment": SentimentContext(
                source="alternative.me",
                fetched_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
                value=41,
                classification="Fear",
                previous_value=51,
            )
        }
    )
