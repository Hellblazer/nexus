# SPDX-License-Identifier: AGPL-3.0-or-later
"""The catalog TTL CHECK reads a ``ttl_days`` the way the expiry sweep does (nexus-6pfgb.44, RDR-236).

The engine's CHECK on ``nexus.catalog_documents`` ("a note TTL implies a ``put_at`` stamp") and its census count the
entries that carry a TTL by one SQL predicate (``CatalogTtlCheckAdmin.ttlRequiresPutAt``). The reader that decides
whether a note expires is :func:`nexus.catalog.note_ttl.note_ttl_days`. Both are driven by the same corpus,
``service/src/test/resources/catalog-ttl-check-shapes.json``: the engine test
(``CatalogTtlCheckAdminIntegrationTest``) holds the predicate to ``carries_ttl`` on a real PostgreSQL, and this test
holds the reader to the same column. So the three cannot drift apart unseen.

The corpus names the one place they differ on purpose: a digit string Python's ``int()`` takes and the CHECK does not
(``"1_0"``, a non-ASCII decimal digit). Such an entry has a ``reader_ttl`` that disagrees with ``carries_ttl``; the
CHECK does not count it, and :func:`nexus.catalog.note_ttl.clock_start` falls back to ``indexed_at`` for it, which
expires it late and never early.

DECISION (written here and in ``CatalogTtlCheckAdmin``): a string of ASCII decimal digits, with an optional ``+``,
leading zeros and surrounding whitespace, IS a TTL when its value is in 1..999999999, because ``note_ttl_days`` reads
``"7"`` as 7 and the sweep expires on it. No writer produces a string TTL; the rule guards hand edits and imports.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.catalog.note_ttl import note_ttl_days

CORPUS = Path(__file__).resolve().parent.parent / "service" / "src" / "test" / "resources" / "catalog-ttl-check-shapes.json"


def _shapes() -> list[dict]:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def test_the_corpus_is_read_and_is_not_empty() -> None:
    shapes = _shapes()
    assert len(shapes) >= 30, "the corpus is the pin; an empty or truncated one proves nothing"
    assert len({s["name"] for s in shapes}) == len(shapes)
    assert any(s["carries_ttl"] for s in shapes) and any(not s["carries_ttl"] for s in shapes)


@pytest.mark.parametrize("shape", _shapes(), ids=lambda s: s["name"])
def test_the_reader_agrees_with_the_check_on_every_shape_that_is_not_a_named_difference(shape: dict) -> None:
    meta = json.loads(shape["meta_json"])
    reads_as_ttl = note_ttl_days(meta) > 0

    if "differs_from_reader" in shape:
        assert reads_as_ttl is shape["reader_ttl"], shape["name"]
        assert reads_as_ttl is not shape["carries_ttl"], "a named difference must be a difference"
        assert shape["differs_from_reader"]
    else:
        assert reads_as_ttl is shape["carries_ttl"], (
            f"{shape['name']}: note_ttl_days reads {meta!r} as a TTL={reads_as_ttl}, "
            f"the CHECK's corpus says {shape['carries_ttl']}")


def test_the_named_differences_are_exactly_the_digit_forms_the_check_does_not_read() -> None:
    differing = {s["name"] for s in _shapes() if "differs_from_reader" in s}

    assert differing == {"string-underscore-digits", "string-arabic-indic-digit"}


def test_a_plain_digit_string_is_a_ttl_to_the_reader_and_to_the_check() -> None:
    by_name = {s["name"]: s for s in _shapes()}

    assert note_ttl_days({"ttl_days": "7"}) == 7
    assert by_name["string-7"]["carries_ttl"] is True
    assert "differs_from_reader" not in by_name["string-7"]
