# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gnrow (critique of 4l80g): range-operand typing in parse_where.

The service bridge is operand-typed — numeric operand → jsonb_typeof-guarded
numeric compare; string operand → LEXICAL compare ('9' > '10'). The old
8-field ``NUMERIC_FIELDS``-only coercion shipped numeric-looking range values
as strings for every other field: plausible-looking, silently WRONG results.
Range operators now coerce unambiguous numeric literals for ANY field; a
quoted value forces the string/lexical path (ISO dates etc.).
"""
from __future__ import annotations

from nexus.filters import parse_where


def test_range_on_whitelisted_field_is_numeric():
    assert parse_where(["bib_year>=2020"]) == {"bib_year": {"$gte": 2020}}


def test_range_on_any_field_with_numeric_literal_is_numeric():
    """The gnrow gap: custom_field>=80 must NOT take the lexical path."""
    assert parse_where(["custom_score>=80"]) == {"custom_score": {"$gte": 80}}
    assert parse_where(["threshold<0.5"]) == {"threshold": {"$lt": 0.5}}


def test_range_with_quoted_value_forces_string_lexical():
    """Quoting is the explicit escape for ordered-string compares (ISO dates)."""
    assert parse_where(["created>='2026-01-01'"]) == {"created": {"$gte": "2026-01-01"}}
    assert parse_where(['rank>"10"']) == {"rank": {"$gt": "10"}}


def test_range_with_non_numeric_literal_stays_string():
    assert parse_where(["created>=2026-01-01"]) == {"created": {"$gte": "2026-01-01"}}


def test_equality_semantics_unchanged():
    """Equality stays field-list-coerced only — the bridge's text-rendered
    equality is type-agnostic, so widening coercion there buys nothing and
    risks churn. Range ops are where operand type changes results."""
    assert parse_where(["custom_score=80"]) == {"custom_score": "80"}
    assert parse_where(["bib_year=2020"]) == {"bib_year": 2020}


def test_ne_is_not_a_range_op():
    assert parse_where(["custom_score!=80"]) == {"custom_score": {"$ne": "80"}}
