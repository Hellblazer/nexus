# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.6 (nexus-p2m1x.26), client half: ``validate_collection_name`` is the label rule.

A collection's ``name`` is a label (RDR-229 item 7): what a person types and reads, not a key and not a
ChromaDB identifier. The rule is non-empty, valid UTF-8, no control character, at most 255 UTF-8 bytes, not of
the minted form ``c`` + ASCII digits (the engine's own, item 8), and no ``/`` (Sam's ruling, T2
``nexus/sam-ruling-rdr229-label-slash-2026-11-21``). The engine twin is ``dev.nexus.service.db.CollectionLabel.validateUserLabel``;
``service/src/test/java/dev/nexus/service/db/CollectionLabelTest.java`` pins the same table.

The conformant-shape check for a NEW collection (``is_conformant_collection_name``) is a different gate and is
not exercised here.
"""
from __future__ import annotations

import pytest

from nexus import corpus
from nexus.corpus import validate_collection_name


@pytest.mark.parametrize(
    "label",
    [
        "a" * 100,
        "My Notes",
        "notes 50%: café",
        "日本語のノート",
        "ab",
        "a",
        "-leading-hyphen",
        "trailing_",
        "a.b",
        "code__myrepo",
        "docs__1-1__model-docs__v1",
        "c",
        "C1",
        "c1a",
        "cc1",
        "c-1",
        "ac1",
        "c١",  # c + ARABIC-INDIC DIGIT ONE: not an ASCII digit, so not the minted form
        "é" * 127,  # 254 bytes
        "é" * 127 + "a",  # exactly 255 bytes
        "a" * 255,
        "x‍y",  # ZERO WIDTH JOINER (Cf) is allowed: Persian, Indic scripts and emoji sequences need it
        "a b",
        " leading space",
    ],
    ids=lambda s: repr(s)[:24],
)
def test_a_label_the_rule_allows_is_accepted(label: str) -> None:
    validate_collection_name(label)


@pytest.mark.parametrize(
    "label, why",
    [
        ("", "empty"),
        ("a\x00b", "NUL"),
        ("a\nb", "newline"),
        ("a\tb", "tab"),
        ("a\x1fb", "unit separator"),
        ("a\x7fb", "DEL"),
        ("a\x85b", "C1 control NEL"),
        ("a b", "LINE SEPARATOR"),
        ("a b", "PARAGRAPH SEPARATOR"),
        ("a\ud800b", "lone surrogate: not valid UTF-8"),
        ("a" * 256, "256 bytes"),
        ("é" * 128, "256 bytes in 128 characters"),
        ("é" * 127 + "ab", "256 bytes"),
        ("c1", "minted form"),
        ("c0", "minted form"),
        ("c007", "minted form, leading zeros reserved"),
        ("c1234567890123456789", "minted form, long"),
    ],
    ids=lambda v: repr(v)[:24],
)
def test_a_label_the_rule_refuses_raises_value_error(label: str, why: str) -> None:
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
