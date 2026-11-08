# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (nexus-rfvyy.8), item 6: the output posture fails closed.

An engine that sends no ``sensitive`` flag (one that predates the field) plus a failed owner lookup leaves nothing to
say a hit is clean, so the hit is treated as sensitive. An engine that sends the flag stays the authority, and a
lookup that was not attempted (no catalog) marks nothing.
"""
from __future__ import annotations

import pytest

from nexus.search_engine import OWNER_LOOKUP_FAILED_KEY, _attach_doc_ids_from_catalog
from nexus.sensitive_output import hit_facts
from nexus.types import SearchResult

CH = "a" * 64


def _hit(**meta) -> SearchResult:
    return SearchResult(id="x", content="body", distance=0.1, collection="knowledge__c",
                        metadata={"chunk_text_hash": CH, **meta})


class _Catalog:
    def __init__(self, positions) -> None:
        self._positions = positions

    def chash_positions(self, chashes):
        if isinstance(self._positions, Exception):
            raise self._positions
        return self._positions

    def docs_for_chashes(self, chashes):
        return {}


def test_a_failed_owner_lookup_marks_a_hit_the_engine_did_not_flag() -> None:
    hit = _hit()
    _attach_doc_ids_from_catalog([hit], _Catalog(RuntimeError("catalog down")))
    assert hit.metadata[OWNER_LOOKUP_FAILED_KEY] is True
    assert hit_facts(hit.metadata) == (True, ())


def test_the_engines_flag_stays_the_authority_when_the_lookup_fails() -> None:
    clean, secret = _hit(sensitive=False), _hit(sensitive=True)
    _attach_doc_ids_from_catalog([clean, secret], _Catalog(RuntimeError("catalog down")))
    assert OWNER_LOOKUP_FAILED_KEY not in clean.metadata and hit_facts(clean.metadata)[0] is False
    assert hit_facts(secret.metadata)[0] is True


def test_a_lookup_that_answers_marks_nothing() -> None:
    hit = _hit()
    _attach_doc_ids_from_catalog([hit], _Catalog([{"chash": CH, "doc_id": "1.1.1", "collection": "knowledge__c",
                                                   "position": 0, "chunk_count": 1}]))
    assert OWNER_LOOKUP_FAILED_KEY not in hit.metadata and hit_facts(hit.metadata)[0] is False


def test_no_catalog_is_not_a_failed_lookup() -> None:
    hit = _hit()
    _attach_doc_ids_from_catalog([hit], None)
    assert OWNER_LOOKUP_FAILED_KEY not in hit.metadata


@pytest.mark.parametrize("meta, expected", [
    ({"_owner_lookup_failed": True}, True),
    ({"_owner_lookup_failed": True, "sensitive": False}, False),
    ({"sensitive": False}, False),
    ({}, False),
])
def test_hit_facts_reads_the_marker_only_without_an_engine_flag(meta, expected) -> None:
    assert hit_facts(meta)[0] is expected
