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

import hashlib
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
_COLLECTIONS = {"main": "knowledge__scn-main__bge-base-en-v15-768__v1", "other": "knowledge__scn-other__bge-base-en-v15-768__v1"}


def _cases() -> list[dict]:
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


def _scenarios() -> list[dict]:
    return json.loads(_TABLE.read_text(encoding="utf-8"))["scenarios"]


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


# ── scenarios: several documents, chunks and manifest rows ──────────────────


class _ScenarioReader:
    """The live documents of each collection and the manifest rows of each document, as the catalog answers them."""

    def __init__(self, scenario: dict) -> None:
        self._docs = {
            d["key"]: SimpleNamespace(
                tumbler=f"1.{i + 1}.1", content_type=d["content_type"], file_path=d["file_path"],
                source_uri=d["source_uri"], meta=d["meta"], collection=_COLLECTIONS[d["collection"]],
            )
            for i, d in enumerate(scenario["documents"])
        }
        chunk_collection = {c["key"]: _collections_of(c) for c in scenario["chunks"]}
        self._manifests: dict[str, list] = {}
        for row in scenario["manifest"]:
            doc = self._docs[row["doc"]]
            collection = row.get("collection") or next(
                d["collection"] for d in scenario["documents"] if d["key"] == row["doc"]
            )
            assert collection in chunk_collection[row["chunk"]], row
            self._manifests.setdefault(doc.tumbler, []).append(
                (row["position"], SimpleNamespace(chash=_chash(scenario, row["chunk"]), collection=_COLLECTIONS[collection]))
            )

    def list_by_collection(self, name: str) -> list:
        return [d for d in self._docs.values() if d.collection == name]

    def get_manifests(self, tumblers: list[str]) -> dict:
        return {t: [r for _, r in sorted(self._manifests[t], key=lambda x: x[0])] for t in tumblers if t in self._manifests}

    def close(self) -> None:
        pass


def _collections_of(chunk: dict) -> list[str]:
    """The collection(s) a scenario chunk is a row of: one name, or a list when the same chunk is in several."""
    value = chunk["collection"]
    return value if isinstance(value, list) else [value]


def _chash(scenario: dict, key: str) -> str:
    return hashlib.sha256(f"{scenario['id']}/{key}".encode()).hexdigest()


class _ScenarioT3:
    def __init__(self, scenario: dict) -> None:
        self._by_collection = {name: {} for name in _COLLECTIONS.values()}
        for c in scenario["chunks"]:
            for which in _collections_of(c):
                self._by_collection[_COLLECTIONS[which]][_chash(scenario, c["key"])] = c["metadata"]

    def get_or_create_collection(self, name: str) -> "_ScenarioCollection":
        return _ScenarioCollection(self._by_collection[name])


class _ScenarioCollection:
    def __init__(self, rows: dict) -> None:
        self._rows = rows

    def get(self, ids: list[str], include: list[str], limit: int, sensitivity: str) -> dict:
        found = [i for i in ids if i in self._rows]
        return {"ids": found, "metadatas": [self._rows[i] for i in found]}


def test_the_scenarios_are_read_whole() -> None:
    scenarios = _scenarios()
    ids = [s["id"] for s in scenarios]
    assert len(scenarios) >= 8 and len(set(ids)) == len(ids), ids
    assert any(len(s["expect"]["main"]) >= 2 for s in scenarios), "no scenario names two owners"
    assert any(s["expect"]["main"] == {} or s["expect"].get("other") == {} for s in scenarios), "no scenario counts nothing"


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s["id"])
def test_the_client_rule_counts_each_scenarios_owners(scenario: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.catalog import factory

    monkeypatch.setattr(factory, "make_catalog_reader", lambda: _ScenarioReader(scenario))
    t3 = _ScenarioT3(scenario)
    for which, want in scenario["expect"].items():
        stubs, coverage = health._pdf_stubs_in_collection(t3, _COLLECTIONS[which], 300)
        assert stubs == want, (which, stubs, want)
        assert not coverage.truncated


@pytest.mark.parametrize(
    "name,prefix",
    [
        ("knowledge__owner__bge-base-en-v15-768__v1", "knowledge"),
        ("docs__owner__voyage-context-3__v1", "docs"),
        ("knowledge__distributed-systems", "knowledge"),
        ("docs__legacy-repo-abcd1234", "docs"),
        ("code__owner__voyage-code-3__v1", "code"),
        ("rdr__owner__voyage-context-3__v1", "rdr"),
    ],
)
def test_a_collection_registered_by_the_client_gets_its_names_first_segment_as_the_registry_content_type(
    name: str, prefix: str,
) -> None:
    """The engine route examines the registry's knowledge and docs collections; the client scans the name prefixes.

    ``register_collection`` derives the content type with ``collection_registration_kwargs``, the first segment of the
    name, so the two sets agree for every prefixed name (the engine cannot parse names: CollectionParseGateTest).
    """
    from nexus.corpus import collection_registration_kwargs

    assert collection_registration_kwargs(name)["content_type"] == prefix


def test_a_bare_name_registers_as_knowledge_though_the_clients_prefix_scan_skips_it() -> None:
    """The one place the sets differ, in the engine's favour: the engine can examine a collection the client does not."""
    from nexus.corpus import collection_registration_kwargs

    name = "my-notes"
    assert not name.startswith(health._PDF_STUB_SCAN_PREFIXES)
    assert collection_registration_kwargs(name)["content_type"] == "knowledge"
