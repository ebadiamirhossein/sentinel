"""Pydantic model -> Anthropic structured-outputs JSON schema.

Structured outputs accept a useful subset of JSON Schema. Numeric bounds, string
lengths and array-size constraints are **not** in it, and sending them is an
error — so they are stripped here. Nothing is loosened by that: the same model
still validates the response client-side, where those constraints do apply. The
API guarantees the *shape*; Pydantic enforces the *ranges*.

Every object is also forced to ``additionalProperties: false`` and to require all
of its properties. Requiring everything is a prompt-design choice as much as a
schema one: the analyst must consciously emit ``"stop": null`` for a NO_SETUP
rather than quietly omitting the field, which makes an incomplete answer visible
instead of ambiguous.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

#: Keys the structured-outputs schema compiler rejects. Enforced by Pydantic on
#: the way back in, so dropping them here costs nothing.
UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "default",
    }
)

#: Allowed, but pure noise: Pydantic derives ``title`` from the field name the
#: model can already see. Dropping it is a few hundred input tokens per call.
#: ``description`` is kept -- that one is genuinely instructive.
NOISE_KEYWORDS = frozenset({"title"})

#: The string formats structured outputs does accept.
SUPPORTED_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Build the wire schema for ``model``."""
    cleaned = _clean(model.model_json_schema(mode="validation"))
    if not isinstance(cleaned, dict):  # pragma: no cover - a model schema is an object
        raise TypeError(f"{model.__name__} did not produce an object schema")
    return cleaned


def _clean(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in UNSUPPORTED_KEYWORDS or key in NOISE_KEYWORDS:
            continue
        if key == "format" and value not in SUPPORTED_FORMATS:
            continue
        out[key] = _clean(value)

    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])

    return out
