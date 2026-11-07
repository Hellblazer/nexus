# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qmrfi (review of c58372716): ``manifest_span`` answers the shared span case table.

The engine's ``ChunkKeySweep`` counts a manifest row's missing span by the value its chunk row holds, with
``manifest_span``'s rule (positive lines, a positive char end, a char start that is positive or is an integer beside a
recorded end, numbers only). The two halves are written in different languages, so one table
(``service/src/test/resources/manifest-span-cases.json``) is read by both: the Java
``ChunkKeySweepIntegrationTest#nullSpanRowsFollowsTheSharedSpanCaseTable`` holds the SQL to it, and this test holds
``manifest_span`` to it. A change to either rule that the other does not follow fails one of the two.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.catalog.types import manifest_span

_TABLE = Path(__file__).resolve().parents[2] / "service/src/test/resources/manifest-span-cases.json"
_CHUNK_KEY_FOR_COLUMN = {
    "line_start": "line_start", "line_end": "line_end", "char_start": "chunk_start_char", "char_end": "chunk_end_char",
}


def _cases() -> list[dict]:
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


def test_the_table_is_read_whole() -> None:
    """A table that lost its rows would pass the parametrized test below with nothing to check."""
    cases = _cases()
    ids = [c["id"] for c in cases]
    assert len(cases) >= 20 and len(set(ids)) == len(ids), ids
    assert any(any(v is not None for v in c["recorded"].values()) for c in cases), "no case records a span"
    assert any(all(v is None for v in c["recorded"].values()) for c in cases), "no case records nothing"


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_manifest_span_returns_the_tables_recorded_values(case: dict) -> None:
    chunk = case["chunk"]
    got = manifest_span(
        chunk.get("line_start"), chunk.get("line_end"), chunk.get("chunk_start_char"), chunk.get("chunk_end_char"))
    assert {_CHUNK_KEY_FOR_COLUMN[column]: value for column, value in got.items()} == case["recorded"]
