# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ax49 (Sam's ruling, 2026-11-01): a re-index fills a document's catalog ``tags`` and ``category`` only
when they are empty, and never overwrites a non-empty value.

The pure rule, storage-free: :func:`nexus.catalog.indexed_meta.fill_only_meta`. The seams that apply it (the
repo catalog pass, the markdown hook, the PDF hook) are pinned against the real engine in
``tests/integration/test_3ax49_fill_only_reindex.py``.
"""
from __future__ import annotations

import pytest

from nexus.catalog.indexed_meta import FILL_ONLY_KEYS, document_register_meta, fill_only_meta

STAMP = {"tags": ["py"], "category": "code", "source_format": "code", "content_hash": "h"}


def test_the_fill_only_keys_are_tags_and_category() -> None:
    assert set(FILL_ONLY_KEYS) == {"tags", "category"}


@pytest.mark.parametrize("empty", [None, "", []], ids=["none", "empty-string", "empty-list"])
def test_an_empty_or_absent_value_is_filled(empty) -> None:
    for held in ({}, {"tags": empty, "category": empty}, None):
        assert fill_only_meta(STAMP, held) == STAMP, held


def test_a_non_empty_value_is_kept_and_so_is_each_key_on_its_own() -> None:
    assert fill_only_meta(STAMP, {"tags": ["mine"], "category": "mine"}) == {"source_format": "code", "content_hash": "h"}
    assert fill_only_meta(STAMP, {"tags": ["mine"]}) == {"category": "code", "source_format": "code", "content_hash": "h"}
    assert fill_only_meta(STAMP, {"tags": [], "category": "mine"}) == {
        "tags": ["py"], "source_format": "code", "content_hash": "h",
    }


def test_a_held_value_equal_to_the_derived_one_is_still_a_held_value() -> None:
    assert "tags" not in fill_only_meta(STAMP, {"tags": ["py"], "category": "code"})


def test_a_legacy_string_tag_is_a_value_and_is_kept() -> None:
    assert "tags" not in fill_only_meta(STAMP, {"tags": "a, b"})


def test_the_other_keys_are_never_gated() -> None:
    held = {"source_format": "old", "content_hash": "old", "rdr_id": "RDR-1"}
    assert fill_only_meta(STAMP, held) == STAMP


def test_the_input_is_not_mutated_and_a_stamp_with_neither_key_comes_back_equal() -> None:
    stamp = document_register_meta("code", "Makefile")  # no extension: no tags key
    before = dict(stamp)
    assert fill_only_meta(stamp, {"tags": ["x"]}) == {"category": "code", "source_format": "code"}
    assert fill_only_meta(stamp, {"category": "x"}) == {"source_format": "code"}
    assert stamp == before
    assert fill_only_meta({"source_format": "pdf"}, {"tags": ["x"]}) == {"source_format": "pdf"}
