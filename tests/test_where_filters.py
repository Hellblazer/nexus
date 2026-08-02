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

import pytest

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


# ── same-key equality collision (nexus-1oguj review, bead nexus-4gzc8) ───────
#
# ``--where k=a --where k=b`` used to silently overwrite via dict.update() in
# the flat-merge branch, landing on k=b with zero signal that k=a was ever
# requested. ChromaDB's equality filter has no native OR/$in support for a
# single key at this layer (tracked as the nexus-4gzc8 follow-up), so today
# the only correct way to scope "any of several exact values" is two separate
# queries unioned client-side. Silent overwrite must become a loud error
# naming both values so a caller doesn't mistake "b-only results" for
# "a-or-b results".


def test_same_key_equality_twice_raises() -> None:
    with pytest.raises(ValueError, match="extraction_method"):
        parse_where(["extraction_method=mineru", "extraction_method=mineru+docling-degraded"])


def test_same_key_equality_twice_error_names_both_values() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_where(["k=a", "k=b"])
    msg = str(exc_info.value)
    assert "'a'" in msg or "a" in msg
    assert "'b'" in msg or "b" in msg
    assert "k" in msg


def test_same_key_equality_twice_error_points_at_union_pattern() -> None:
    with pytest.raises(ValueError, match=r"nexus-4gzc8"):
        parse_where(["k=a", "k=b"])
    with pytest.raises(ValueError, match=r"two.*quer|union"):
        parse_where(["k=a", "k=b"])


def test_same_key_equality_three_times_raises() -> None:
    """Not just the pairwise case — any repeat of an equality key is a
    silent-overwrite hazard, however many times it repeats."""
    with pytest.raises(ValueError, match="k"):
        parse_where(["k=a", "k=b", "k=c"])


def test_distinct_keys_equality_still_ands() -> None:
    """The fix must not regress the common multi-field-equality case."""
    assert parse_where(["lang=python", "type=code"]) == {
        "lang": "python", "type": "code",
    }


def test_same_key_same_value_twice_still_raises() -> None:
    """Even an identical repeat is caller error worth surfacing loudly —
    it's never a legitimate way to spell a single filter, and staying
    silent here would mean the guard only fires when values differ."""
    with pytest.raises(ValueError, match="k"):
        parse_where(["k=a", "k=a"])
