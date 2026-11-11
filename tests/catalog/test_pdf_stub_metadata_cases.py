# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the client's PDF placeholder-metadata rule answers the shared case table.

``nx doctor`` finds placeholder chunks of PDF documents with the client rule
(``nexus.health._pdf_stubs_in_collection``, through ``_is_pdf_stub_metadata`` and ``catalog_source_format``). The engine
route ``GET /v1/catalog/stub-metadata`` answers the same question in one bounded statement. The two halves are written
in different languages, so one table (``service/src/test/resources/pdf-stub-metadata-cases.json``) is read by both: the
Java ``CatalogStubMetadataParityTest`` holds the SQL to it, and this test holds the client to it, case by case, through
the real function a doctor run calls. A change to either rule that the other does not follow fails one of the two.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus import health
from nexus.tables.chunk_metadata_keys import non_chunk_row_keys

_TABLE = Path(__file__).resolve().parents[2] / "service/src/test/resources/pdf-stub-metadata-cases.json"
_JAVA_KEYS = (
    Path(__file__).resolve().parents[2]
    / "service/src/main/java/dev/nexus/service/vectors/ChunkMetadataKeys.java"
)
_COLLECTION = "knowledge__parity__bge-base-en-v15-768__v1"


def _cases() -> list[dict]:
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


class _Reader:
    """One live document in the collection and its one manifest row: what the doctor reads from the catalog."""

    def __init__(self, case: dict) -> None:
        d = case["document"]
        self._doc = SimpleNamespace(
            tumbler="1.1.1", content_type=d["content_type"], file_path=d["file_path"],
            source_uri=d["source_uri"], meta=d["meta"],
        )
        self._row = SimpleNamespace(chash="aa" * 32, collection=_COLLECTION)

    def list_by_collection(self, name: str) -> list:
        assert name == _COLLECTION
        return [self._doc]

    def get_manifests(self, tumblers: list[str]) -> dict:
        return {"1.1.1": [self._row]}

    def close(self) -> None:
        pass


class _Collection:
    def __init__(self, case: dict) -> None:
        self._meta = case["chunk"]

    def get(self, ids: list[str], include: list[str], limit: int, sensitivity: str) -> dict:
        assert include == ["metadatas"] and sensitivity == "include"
        return {"ids": list(ids), "metadatas": [self._meta for _ in ids]}


class _T3:
    def __init__(self, case: dict) -> None:
        self._case = case

    def get_or_create_collection(self, name: str) -> _Collection:
        assert name == _COLLECTION
        return _Collection(self._case)


def test_the_table_is_read_whole() -> None:
    """A table that lost its rows would pass the parametrized test below with nothing to check."""
    cases = _cases()
    ids = [c["id"] for c in cases]
    assert len(cases) >= 40 and len(set(ids)) == len(ids), ids
    assert sum(1 for c in cases if c["expect"]["stub"]) >= 15, "too few stub cases to hold a rule to"
    assert sum(1 for c in cases if not c["expect"]["stub"]) >= 15, "too few non-stub cases to hold a rule to"
    for c in cases:
        assert (c["expect"]["source"] is not None) == c["expect"]["stub"], c["id"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_the_client_rule_counts_exactly_the_tables_stubs(case: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.catalog import factory

    monkeypatch.setattr(factory, "make_catalog_reader", lambda: _Reader(case))
    stubs, coverage = health._pdf_stubs_in_collection(_T3(case), _COLLECTION, 300)
    want = {case["expect"]["source"]: 1} if case["expect"]["stub"] else {}
    assert stubs == want
    assert not coverage.truncated


def test_the_engine_key_classes_are_the_clients() -> None:
    """The route's key set is ChunkMetadataKeys.{DOCUMENT,POSITION,LEGACY}; it must be the client's non_chunk_row_keys().

    ``ChunkMetadataKeysTest`` pins the Java file to the key table; this pins the same union the rule reads, so a class
    moved between CHUNK/ROW and the other three fails here as well as in the Java test.
    """
    import re

    text = _JAVA_KEYS.read_text(encoding="utf-8")
    java: set[str] = set()
    for name in ("DOCUMENT", "POSITION", "LEGACY"):
        block = re.search(rf"public static final Set<String> {name} = Set\.of\((.*?)\);", text, re.S)
        assert block, name
        java |= set(re.findall(r'"([a-z_]+)"', block.group(1)))
    assert java == set(non_chunk_row_keys())
