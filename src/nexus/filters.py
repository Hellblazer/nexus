# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared where-filter parsing for ChromaDB metadata queries.

Used by both MCP tools and CLI commands.
"""
from __future__ import annotations

import re

NUMERIC_FIELDS = frozenset({
    "bib_year", "bib_citation_count", "page_number", "page_count",
    "chunk_index", "chunk_count", "chunk_start_char", "chunk_end_char",
})

_WHERE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(>=|<=|!=|>|<|=)(.*)$")
_OP_MAP: dict[str, str | None] = {
    ">=": "$gte", "<=": "$lte", "!=": "$ne",
    ">": "$gt", "<": "$lt", "=": None,
}


def coerce_value(key: str, value: str, *, strict: bool = False) -> str | int | float:
    """Coerce *value* to int/float for known numeric metadata fields.

    When *strict* is True, raises ``ValueError`` if a numeric field
    has a non-numeric value. Otherwise returns the original string.
    """
    if key in NUMERIC_FIELDS:
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                if strict:
                    raise ValueError(
                        f"field {key!r} requires a numeric value, got {value!r}"
                    )
    return value


def parse_where(
    pairs: list[str] | tuple[str, ...],
    *,
    strict: bool = False,
) -> dict | None:
    """Parse ``KEY{op}VALUE`` strings into a ChromaDB where dict.

    Supported operators: ``=``, ``>=``, ``<=``, ``>``, ``<``, ``!=``.
    Known numeric fields are auto-coerced to int/float.

    Multiple pairs are ANDed via ``$and`` when operator nesting is present,
    or merged into a flat dict for simple equality filters.
    Returns ``None`` when *pairs* is empty.

    Raises ``ValueError`` on invalid format or empty values.
    """
    if not pairs:
        return None
    parts: list[dict] = []
    for pair in pairs:
        pair = pair.strip()
        if not pair:
            continue
        m = _WHERE_RE.match(pair)
        if not m:
            raise ValueError(f"Invalid where format: {pair!r}. Use KEY=VALUE or KEY>=VALUE")
        key, op_str, raw_value = m.group(1), m.group(2), m.group(3)
        if not raw_value:
            raise ValueError(f"empty value in where clause: {pair!r}")
        value = coerce_value(key, raw_value, strict=strict)
        chroma_op = _OP_MAP[op_str]
        if chroma_op is None:
            parts.append({key: value})
        else:
            parts.append({key: {chroma_op: value}})
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    if all(not isinstance(v, dict) for p in parts for v in p.values()):
        merged: dict = {}
        for p in parts:
            merged.update(p)
        return merged
    return {"$and": parts}


def parse_where_str(where_str: str) -> dict | None:
    """Parse a comma-separated where string. Convenience for MCP tools."""
    if not where_str.strip():
        return None
    return parse_where(where_str.split(","))
