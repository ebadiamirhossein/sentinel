"""The wire schema is part of the prompt. It has to carry what the model needs.

Structured outputs accepts only a subset of JSON Schema: numeric and length
bounds are stripped before sending. That is safe for *validation* -- Pydantic
still enforces them here -- but it means the schema alone never tells the model a
limit exists. The first live analyst call overran all three string caps at once
and cost a full retry, so every capped field now states its cap in prose, and
this module fails if the prose and the constraint drift apart.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from sentinel.analyst.models import AnalystReportPayload
from sentinel.llm.schema import json_schema_for
from sentinel.screener.models import ScreenerBatch


def capped_fields(model: type[BaseModel]) -> dict[str, int]:
    """Every field with a max_length, and its limit."""
    found: dict[str, int] = {}
    for name, field in model.model_fields.items():
        for meta in field.metadata:
            limit = getattr(meta, "max_length", None)
            if limit is not None:
                found[name] = limit
    return found


def test_there_are_capped_fields_to_check() -> None:
    """Guard against this whole module silently passing on an empty set."""
    assert set(capped_fields(AnalystReportPayload)) == {
        "thesis",
        "counter_thesis",
        "invalidation_text",
    }


@pytest.mark.parametrize(("field", "limit"), sorted(capped_fields(AnalystReportPayload).items()))
def test_schema_states_every_length_limit(field: str, limit: int) -> None:
    """The model cannot respect a bound nobody told it about."""
    description = json_schema_for(AnalystReportPayload)["properties"][field].get("description", "")
    assert str(limit) in description, (
        f"{field} is capped at {limit} but its description does not say so; "
        "structured outputs strips maxLength, so the description is the only channel"
    )


def test_stripped_keywords_are_really_absent() -> None:
    """Sending maxLength/ge/le would be a 400 from the schema compiler."""
    body = json.dumps(json_schema_for(AnalystReportPayload))
    for keyword in ("maxLength", "minLength", "minimum", "maximum", "exclusiveMinimum"):
        assert f'"{keyword}"' not in body


def test_numeric_bounds_are_still_enforced_client_side() -> None:
    """Stripping them from the wire must not loosen them here.

    ``confidence`` still *rejects*: it is a number the gate compares against
    ``min_confidence``, so a value outside 0-100 is a broken answer, not a long one.
    """
    from pydantic import ValidationError

    from tests.market_double import valid_report_json

    with pytest.raises(ValidationError):
        AnalystReportPayload.model_validate_json(valid_report_json(confidence=101))


@pytest.mark.parametrize(("field", "limit"), [("thesis", 600), ("counter_thesis", 300)])
def test_over_long_prose_is_truncated_rather_than_rejected(field: str, limit: int) -> None:
    """M8.2, owner ruling 2026-08-19 — the change, stated as a test.

    Three of 21 live calls on 2026-08-19 were discarded for length, one of them by a
    single character, at ~$0.27 each. These fields are prose for a card; nothing
    computes on them. So an overrun costs a truncation and a log line, not the
    analysis. ``confidence`` above is the contrast: numbers still reject.
    """
    from tests.market_double import valid_report_json

    payload = AnalystReportPayload.model_validate_json(
        valid_report_json(**{field: "x" * (limit + 1)})
    )
    assert len(getattr(payload, field)) == limit


def test_truncation_keeps_the_beginning_not_the_end() -> None:
    """A thesis leads with its conclusion, so the tail is the part to lose."""
    from tests.market_double import valid_report_json

    text = "LEAD. " + ("filler " * 200)
    payload = AnalystReportPayload.model_validate_json(valid_report_json(thesis=text))
    assert payload.thesis.startswith("LEAD.")


def test_every_field_is_required() -> None:
    """A NO_SETUP must say "stop": null, not omit the key."""
    schema = json_schema_for(AnalystReportPayload)
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_prices_are_plain_numbers_not_anyof() -> None:
    """One type per field is one fewer thing for the model to get wrong."""
    schema = json_schema_for(AnalystReportPayload)
    assert schema["$defs"]["EntryZonePayload"]["properties"]["low"] == {"type": "number"}


def test_screener_schema_is_an_object_wrapper() -> None:
    """specs/PROMPTS.md §1 asks for an array; structured outputs needs an object."""
    schema = json_schema_for(ScreenerBatch)
    assert schema["type"] == "object"
    assert schema["properties"]["verdicts"]["type"] == "array"
