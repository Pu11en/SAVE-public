"""Shared plugin helpers — three tiny utilities deduplicated from
research_tools, self_heal, and recall plugins.

Every plugin handler that registers as a Hermes tool needs to:
  1. Return a JSON-stringified payload to the LLM (capped so a runaway
     response can't blow past Gemini's token budget)
  2. Return an error in the same JSON shape
  3. Build a tool schema dict with the standard {name, description,
     parameters{type, properties, required}} layout

These three helpers (_ok, _err, _schema) were copy-pasted byte-for-byte
across three plugins, totaling ~54 redundant lines. Pulling them here
removes the duplication and gives us one place to evolve the contract
(e.g. tighten the response cap, add a debug envelope).

Usage from a plugin:
    from app.plugins._utils import ok, err, schema

    def h_my_tool(args, **_):
        if not args.get("query"):
            return err("query required")
        return ok({"matches": [...]})

    _TOOLS = (
        (schema("my_tool", "...", {"query": {"type": "string"}}, ["query"]),
         h_my_tool),
        ...
    )
"""
from __future__ import annotations

import json
from typing import Any

# Cap so a runaway tool response can't push Gemini past its context limit.
# 8000 chars ≈ 2000 tokens — enough headroom for normal responses + truncation
# guarantee even when a search returns 50 long results.
_MAX_RESPONSE_CHARS = 8000


def ok(payload: Any) -> str:
    """Serialize a tool result for return to the LLM. Truncates aggressively."""
    try:
        text = json.dumps(payload, default=str)
    except Exception:
        text = json.dumps({"raw": str(payload)[:1000]})
    return text[:_MAX_RESPONSE_CHARS]


def err(msg: str) -> str:
    """Return an error in the same JSON shape as ok() so the LLM can parse it."""
    return json.dumps({"error": msg})


def schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """Build a tool schema dict matching Hermes' ctx.register_tool input shape.

    `properties` is a dict mapping arg name → JSON-schema property (e.g.
    {"query": {"type": "string", "description": "..."}}). `required` is the
    list of property names the LLM must supply for the call to be valid.
    """
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


# Backwards-compat aliases — the plugin files imported these as _ok/_err/_schema
# inside their modules. New code can use the un-underscored names above; the
# aliases here let existing code import either way without a rewrite.
_ok = ok
_err = err
_schema = schema


__all__ = ["ok", "err", "schema", "_ok", "_err", "_schema"]
