# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.6 (nexus-p2m1x.26), client half: ``validate_collection_name`` is the label rule.

A collection's ``name`` is a label (RDR-229 item 7): what a person types and reads, not a key and not a
ChromaDB identifier. The rule is non-empty, valid UTF-8, no control character, at most 255 UTF-8 bytes, not of
the minted form ``c`` + ASCII digits (the engine's own, item 8), and no ``/`` (Sam's ruling, T2
``nexus/sam-ruling-rdr229-label-slash-2026-11-21``). The engine twin is ``dev.nexus.service.db.CollectionLabel.validateUserLabel``;
``service/src/test/java/dev/nexus/service/db/CollectionLabelTest.java`` reads the same case table,
``service/src/test/resources/label-rule-cases.json`` (nexus-mrmqw).

The conformant-shape check for a NEW collection (``is_conformant_collection_name``) is a different gate and is
not exercised here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus import corpus
from nexus.corpus import validate_collection_name


#: The case table both halves read (nexus-mrmqw): the engine's CollectionLabelTest reads the same file, so the two
#: rules cannot drift apart case by case.
CASES_PATH = Path(__file__).resolve().parents[1] / "service" / "src" / "test" / "resources" / "label-rule-cases.json"
_DOC = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _label(case: dict) -> str:
    return case["label"] if "label" in case else "".join(text * times for text, times in case["parts"])


_CASES = [pytest.param(_label(c), c["rule"], id=f"{c['rule'] or 'legal'}:{_label(c)!r}"[:48]) for c in _DOC["cases"]]


def test_the_case_table_is_not_empty_and_covers_every_rule() -> None:
    assert len(_DOC["cases"]) >= 60
    assert {c["rule"] for c in _DOC["cases"]} == set(_DOC["rules"]) | {None}


def test_the_client_checks_the_clauses_in_the_tables_order() -> None:
    """nexus-pqirj: the engine's CollectionLabelTest pins its own order to the same list."""
    assert corpus.LABEL_RULES == tuple(_DOC["rules"])


@pytest.mark.parametrize("label, rule", _CASES)
def test_each_case_breaks_the_rule_the_table_names(label: str, rule: str | None) -> None:
    breach = corpus.label_breach(label)
    assert (breach and breach[0]) == rule, breach
    if rule is None:
        validate_collection_name(label)
    else:
        with pytest.raises(ValueError):
            validate_collection_name(label)


def test_the_minted_form_refusal_names_the_reason() -> None:
    with pytest.raises(ValueError, match="minted"):
        validate_collection_name("c42")


def test_the_byte_limit_refusal_names_the_byte_count() -> None:
    with pytest.raises(ValueError, match="256 bytes"):
        validate_collection_name("é" * 128)


def test_a_label_over_the_old_63_character_cap_no_longer_carries_the_overflow_hint() -> None:
    """GH #1060's remedy hint belonged to the 63-character cap, which is gone."""
    validate_collection_name("code__conductor-sys-monitoring-b25083f0__bge-base-en-v15-768__v1")
    with pytest.raises(ValueError) as ei:
        validate_collection_name("a" * 256)
    assert "nx catalog collection-name" not in str(ei.value)


# The '/' clause is Sam's ruling (T2 nexus/sam-ruling-rdr229-label-slash-2026-11-21): NoteUri.collectionOf ends a
# collection at the first '/', and file paths are built from a label. It is one named clause, LABEL_REFUSES_SLASH in
# src/nexus/corpus.py and CollectionLabel.REFUSE_SLASH in the engine; a later ruling flips both and changes this test.
def test_a_label_holding_a_slash_is_refused() -> None:
    assert corpus.LABEL_REFUSES_SLASH is True
    with pytest.raises(ValueError, match="/"):
        validate_collection_name("a/b")
    with pytest.raises(ValueError, match="/"):
        validate_collection_name("/")


# The ',' and edge-whitespace clause is Sam's ruling on ask rdr229-label-comma-space (2026-11-21): MCP search and plan
# confinement read a corpus list with split(",") then strip(), so such a label could not be named there. One named
# clause, LABEL_REFUSES_COMMA_SPACE here and CollectionLabel.REFUSE_COMMA_AND_EDGE_SPACE in the engine.
@pytest.mark.parametrize("label", ["a,b", ",", " notes", "notes ", " ", "\u00a0notes", "notes\u3000", "\u2003"])
def test_a_label_holding_a_comma_or_edge_whitespace_is_refused(label: str) -> None:
    assert corpus.LABEL_REFUSES_COMMA_SPACE is True
    with pytest.raises(ValueError, match="','|whitespace"):
        validate_collection_name(label)


@pytest.mark.parametrize("label", ["notes 50%: café", "a b", "a\u00a0b", "notes\u200d"])
def test_inner_whitespace_and_an_edge_format_character_are_allowed(label: str) -> None:
    validate_collection_name(label)
