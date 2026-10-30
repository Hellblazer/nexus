# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.16 (RDR-236 Phase 2): the extraction-provenance readers take ``extraction_method``,
``quality_gate_overridden`` and ``extraction_source`` from the OWNING document's catalog ``meta``, not from the
shared chunk row.

Two PDFs that share a chunk share one row, and the row holds whichever indexed last. Before this change a clean
re-index of B left the row saying B's method and, through ``delete_keys``, erased A's ``quality_gate_overridden``
marker; ``store_get`` named the wrong extractor for A, the doctor's stub scan judged A by B's row, and the
documented blast-radius audit (``--where quality_gate_overridden=true``) could not list A. Since nexus-6pfgb.6 each
document's catalog ``meta`` carries its own. The world is the same everywhere below: degraded A, clean B, B's row
last; the catalog is a stub (the real-engine twin is ``tests/integration/test_6pfgb16_extraction_readers.py``).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import nexus.catalog.factory as catalog_factory
import nexus.catalog.store_hook as store_hook
import nexus.commands.store as store_cmd
import nexus.mcp_infra as mcp_infra
from nexus import health
from nexus.catalog.store_hook import extraction_facts
from nexus.cli import main
from nexus.corpus import t3_collection_name
from nexus.mcp import core as mcp_core

H_S = "5" * 64  # the chunk A and B share
H_A = "a" * 64  # A's own chunk
H_K = "c" * 64  # a chunk held by one document with no extraction meta (indexed before nexus-6pfgb.6)
H_K2 = "d" * 64  # a chunk held by two such documents

DEGRADED = {"mineru-no-formula": [3, 7]}
#: B indexed last and clean: the row says B's method and carries no override marker at all.
ROW_S = {"id": H_S, "title": "Paper B", "content": "shared page text", "extraction_method": "mineru"}


class _StubT3:
    def __init__(self) -> None:
        self.name = t3_collection_name("art")
        self.rows = [{"name": self.name, "content_type": "knowledge", "owner_id": "art",
                      "embedding_model": "local-model", "lifecycle_state": "live", "count": 4}]
        self.docs = {
            H_S: dict(ROW_S),
            H_A: {"id": H_A, "title": "Paper A", "content": "a only", "extraction_method": "mineru"},
            H_K: {"id": H_K, "title": "Old Paper", "content": "old text", "extraction_method": "pymupdf_normalized"},
            H_K2: {"id": H_K2, "title": "Two Old", "content": "two old", "extraction_method": "pymupdf_normalized"},
        }

    def collection_exists(self, name: str) -> bool:
        return name == self.name

    def list_collections(self) -> list[dict]:
        return [dict(r) for r in self.rows]

    def collection_info(self, name: str) -> dict:
        return {"count": len(self.docs), "metadata": {}}

    def get_by_id(self, name: str, doc_id: str) -> dict | None:
        return dict(self.docs[doc_id]) if name == self.name and doc_id in self.docs else None

    def find_ids_by_title(self, name: str, title: str) -> list[str]:
        return []


class _StubReader:
    def __init__(self, state) -> None:
        self.state = state

    def docs_for_chashes(self, chashes):
        self.state.owner_calls += 1
        return {c: list(self.state.owners[c]) for c in chashes if c in self.state.owners}

    def resolve_many(self, tumblers):
        self.state.resolve_calls += 1
        return {t: self.state.entries[t] for t in tumblers if t in self.state.entries}

    def close(self) -> None:
        pass


def _entry(tumbler: str, title: str, meta: dict, **kw) -> SimpleNamespace:
    return SimpleNamespace(tumbler=tumbler, title=title, meta=meta, indexed_at="", physical_collection="",
                           source_uri=kw.get("source_uri", f"file:///papers/{title}.pdf"), file_path="", content_type="paper")


@pytest.fixture
def world(monkeypatch):
    t3 = _StubT3()
    state = SimpleNamespace(owners={}, entries={}, t3=t3, owner_calls=0, resolve_calls=0, title_hits={})
    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    mcp_infra.prime_collections_cache(t3.rows)
    monkeypatch.setattr(store_cmd, "_t3", lambda **_: t3)
    monkeypatch.setattr(mcp_core, "_get_t3", lambda: t3)
    monkeypatch.setattr(store_hook, "split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(store_cmd, "_split_note_text", lambda *a, **k: None)
    monkeypatch.setattr(
        store_hook, "catalog_chashes_for_title", lambda col, title: [[c] for c in state.title_hits.get(title, [])][:1],
    )
    monkeypatch.setattr(catalog_factory, "make_catalog_reader", lambda: _StubReader(state))
    state.owners = {H_S: ["1.1.1", "1.1.2"], H_A: ["1.1.1"], H_K: ["1.1.3"], H_K2: ["1.1.4", "1.1.5"]}
    state.entries = {
        # A: degraded, quality gate overridden, text from DEVONthink
        "1.1.1": _entry("1.1.1", "Paper A", {
            "extraction_method": "mineru+degraded", "quality_gate_overridden": True,
            "extraction_source": "dt_content", "extraction_pages": DEGRADED,
        }),
        # B: clean, explicit false (the catalog merge never retracts, so a clean index writes false)
        "1.1.2": _entry("1.1.2", "Paper B", {
            "extraction_method": "mineru", "quality_gate_overridden": False, "extraction_source": "file",
            "extraction_pages": {},
        }),
        "1.1.3": _entry("1.1.3", "Old Paper", {}),
        "1.1.4": _entry("1.1.4", "Two Old A", {}),
        "1.1.5": _entry("1.1.5", "Two Old B", {}),
    }
    state.title_hits = {"Paper A": [H_S], "Paper B": [H_S]}
    yield state
    mcp_infra.invalidate_collections_cache()


def _mcp_get(doc_id: str) -> str:
    return mcp_core.store_get(doc_id=doc_id, collection="art")


def _cli_get(doc_id: str, *extra: str) -> str:
    return CliRunner().invoke(main, ["store", "get", doc_id, "--collection", "art", *extra], catch_exceptions=False).output


def _lines(text: str, prefix: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith(prefix)]


# ── store_get: each document's own extractor, override and source ─────────────────────────────


def test_get_by_title_names_each_documents_own_extractor_though_b_indexed_last(world) -> None:
    a, b = _mcp_get("Paper A"), _mcp_get("Paper B")
    assert _lines(a, "Extractor:") == ["Extractor:  mineru+degraded"], "A's own method, not the row's mineru"
    assert _lines(a, "Gate:") == ["Gate:       overridden"], "the marker B's clean index erased from the row"
    assert _lines(a, "Source:") == ["Source:     dt_content"]
    assert "Degraded:   mineru-no-formula 3,7" in a.splitlines()
    assert _lines(b, "Extractor:") == ["Extractor:  mineru"]
    assert _lines(b, "Gate:") == [] and _lines(b, "Source:") == [] and _lines(b, "Degraded:") == [], "clean, from a file"


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_get_by_id_of_the_shared_chunk_labels_each_owners_line(world, get) -> None:
    out = get(H_S)
    assert _lines(out, "Extractor:") == [
        "Extractor:  Paper A: mineru+degraded", "Extractor:  Paper B: mineru",
    ]
    assert _lines(out, "Gate:") == ["Gate:       Paper A: overridden"], "only the owner that overrode it is named"
    assert _lines(out, "Source:") == ["Source:     Paper A: dt_content"]


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_owners_that_agree_print_one_unlabelled_line(world, get) -> None:
    world.entries["1.1.1"].meta.update(extraction_method="mineru", quality_gate_overridden=False, extraction_source="file")
    world.entries["1.1.1"].meta["extraction_pages"] = {}
    out = get(H_S)
    assert _lines(out, "Extractor:") == ["Extractor:  mineru"]
    assert _lines(out, "Gate:") == [] and _lines(out, "Source:") == []


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_a_document_with_no_catalog_method_prints_no_extractor_line_whatever_the_row_says(world, get) -> None:
    """Absent is unknown. A PDF indexed before nexus-6pfgb.6 has no extraction meta (``nx catalog
    backfill-meta`` or its next index run records it), and the chunk row's method is the last writer's:
    sole owner or shared, it is not shown as the document's."""
    assert _lines(get(H_K), "Extractor:") == [], "the row says pymupdf_normalized; the sole owner's meta says nothing"
    assert _lines(get(H_K2), "Extractor:") == []


@pytest.mark.parametrize("get", [_mcp_get, _cli_get], ids=["mcp", "cli"])
def test_an_unreadable_catalog_prints_no_extractor_line_never_the_rows(world, get, monkeypatch) -> None:
    def boom():
        raise ConnectionError("catalog down")

    monkeypatch.setattr(catalog_factory, "make_catalog_reader", boom)
    out = get(H_S)
    assert _lines(out, "Extractor:") == [] and "shared page text" in out, "get still answers; the row's mineru is B's"


def test_one_owner_lookup_serves_the_whole_get(world) -> None:
    _mcp_get(H_S)
    _cli_get(H_S)
    assert (world.owner_calls, world.resolve_calls) == (2, 2), "one docs_for_chashes and one resolve_many per get"


def test_cli_json_lists_each_owners_extraction_facts_and_keeps_the_row(world) -> None:
    out = json.loads(_cli_get(H_S, "--json"))
    assert out["extraction_method"] == "mineru", "the chunk row as it is, for scripts that read it"
    assert out["extraction"] == [
        {"tumbler": "1.1.1", "title": "Paper A", "extraction_method": "mineru+degraded",
         "quality_gate_overridden": True, "extraction_source": "dt_content"},
        {"tumbler": "1.1.2", "title": "Paper B", "extraction_method": "mineru",
         "quality_gate_overridden": False, "extraction_source": "file"},
    ]
    assert [d["tumbler"] for d in out["degraded_pages"]] == ["1.1.1"]


def test_an_absent_extraction_source_means_file() -> None:
    owners = [_entry("1.1.1", "P", {"extraction_method": "mineru"})]
    (facts,) = extraction_facts(owners)
    assert facts["extraction_source"] == "file" and facts["quality_gate_overridden"] is None, (
        "absent source is a file; an absent override is unknown, not False"
    )


@pytest.mark.parametrize("source", [None, "", "file"])
def test_both_readers_read_an_empty_extraction_source_as_file(source) -> None:
    """nexus-6pfgb.48 (Low-6): ``meta_matches`` took ``""`` for a stated value that is not ``file`` while
    ``extraction_facts`` took it for no value (so ``file``); one reading now, the key's default."""
    from nexus.catalog.meta_filter import meta_matches

    meta = {"extraction_method": "mineru", "extraction_source": source}
    (facts,) = extraction_facts([_entry("1.1.1", "P", meta)])

    assert facts["extraction_source"] == "file"
    assert meta_matches(meta, [("extraction_source", "file")]) is True
    assert meta_matches(meta, [("extraction_source", "dt_content")]) is False


def test_an_empty_value_for_a_key_with_no_default_still_matches_itself() -> None:
    """The default applies to ``extraction_source`` only; elsewhere an explicit empty value is a value."""
    from nexus.catalog.meta_filter import meta_matches

    assert meta_matches({"extraction_method": ""}, [("extraction_method", "")]) is True
    assert meta_matches({}, [("extraction_method", "")]) is False


# ── the doctor's PDF stub scan: a document is a stub by its own meta ──────────────────────────


class _Col:
    """``col.get`` over canned pages, as the engine answers ``where={"content_type": "pdf", "title": ""}``."""

    def __init__(self, ids, metas, uris) -> None:
        self.ids, self.metas, self.uris = ids, metas, uris
        self.calls = 0

    def get(self, *, where=None, include=None, limit=300, offset=0, include_source_uri=False):
        self.calls += 1
        sl = slice(offset, offset + limit)
        out = {"ids": self.ids[sl], "metadatas": self.metas[sl]}
        if include_source_uri:
            out["source_uris"] = self.uris[sl]
        return out


class _T3:
    def __init__(self, col) -> None:
        self.col = col

    def get_or_create_collection(self, name):
        return self.col


def _scan(world, ids, metas):
    col = _Col(ids, metas, ["file:///engine/one.pdf"] * len(ids))
    return health._pdf_stubs_in_collection(_T3(col), world.t3.name, 300)


def test_the_scan_judges_each_owner_by_its_own_meta_when_the_row_is_a_shared_placeholder(world) -> None:
    """A's stub upload wrote the shared row last (empty title, no method); B's catalog meta records its method."""
    world.entries["1.1.1"].meta.pop("extraction_method")  # A's post-pass never landed
    stubs, cut = _scan(world, [H_S], [{"content_type": "pdf", "title": "", "content_hash": "h" * 64}])
    assert stubs == {"file:///papers/Paper A.pdf": 1}, "A is the stub; B (method in its meta) is not named"
    assert cut is False


def test_a_catalog_method_clears_a_sole_owner_whose_row_still_looks_like_a_stub(world) -> None:
    stubs, _ = _scan(world, [H_A], [{"content_type": "pdf", "title": ""}])
    assert stubs == {}, "A's own meta says extraction ran"


def test_a_document_with_no_catalog_method_is_named_whatever_the_row_says(world) -> None:
    """A PDF indexed before nexus-6pfgb.6 has no method in its meta until ``nx catalog backfill-meta`` or
    its next index run; the row's method is the last writer's and does not clear it."""
    stubs, _ = _scan(world, [H_K], [{"content_type": "pdf", "title": "", "extraction_method": "pymupdf_normalized"}])
    assert stubs == {"file:///papers/Old Paper.pdf": 1}


def test_the_scan_makes_one_owner_lookup_per_page_and_reads_no_catalog_for_an_empty_page(world) -> None:
    _scan(world, [H_S, H_A, H_K], [{"content_type": "pdf", "title": ""}] * 3)
    assert (world.owner_calls, world.resolve_calls) == (1, 1)
    world.owner_calls = world.resolve_calls = 0
    _scan(world, [], [])
    assert (world.owner_calls, world.resolve_calls) == (0, 0)


def test_an_unreadable_catalog_makes_the_collection_not_checked_never_clean(world, monkeypatch) -> None:
    def boom():
        raise ConnectionError("catalog down")

    monkeypatch.setattr(catalog_factory, "make_catalog_reader", boom)
    with pytest.raises(RuntimeError, match="catalog"):
        _scan(world, [H_S], [{"content_type": "pdf", "title": ""}])


@pytest.mark.parametrize(
    ("meta", "document_has_method", "flagged"),
    [
        ({"title": ""}, False, True),
        ({"title": ""}, True, False),
        ({"title": "T"}, False, False),
        ({}, False, True),
        ({"title": "", "extraction_method": "mineru"}, False, True),  # the row's method is the last writer's
    ],
)
def test_the_predicate_is_the_row_placeholder_and_the_documents_own_method(meta, document_has_method, flagged) -> None:
    assert health._is_pdf_stub_metadata(meta, document_has_method=document_has_method) is flagged


# ── the audit: documents by their own meta ────────────────────────────────────────────────────


@pytest.fixture
def catalog_cli(world, monkeypatch):
    docs = [
        world.entries["1.1.1"], world.entries["1.1.2"],
        _entry("1.1.3", "Old Paper", {}),  # indexed before nexus-6pfgb.6: no extraction meta at all
    ]
    for d in docs:
        d.year, d.author = 0, ""

    class _Cat:
        def __init__(self):
            self.calls = []

        def all_documents(self, limit=0, *, content_type="", offset=0):
            self.calls.append((limit, content_type, offset))
            return [d for d in docs if not content_type or d.content_type == content_type]

    cat = _Cat()
    import nexus.commands.catalog as catalog_cmd

    monkeypatch.setattr(catalog_cmd, "_get_catalog", lambda: cat)
    return cat


def _audit(*args: str) -> list[str]:
    out = CliRunner().invoke(main, ["catalog", "list", *args], catch_exceptions=False).output
    return [ln.split()[0] for ln in out.splitlines() if ln.strip() and ln.split()[0][0].isdigit()]


def test_the_audit_lists_degraded_a_and_not_clean_b(catalog_cli) -> None:
    assert _audit("--meta", "quality_gate_overridden=true") == ["1.1.1"]
    assert _audit("--meta", "quality_gate_overridden=false") == ["1.1.2"], "an explicit false; absent is unknown, not false"
    assert _audit("--meta", "extraction_method=mineru+degraded") == ["1.1.1"]
    assert _audit("--meta", "extraction_method=mineru") == ["1.1.2"]


def test_filters_are_anded_and_an_absent_source_reads_as_file(catalog_cli) -> None:
    assert _audit("--meta", "extraction_source=dt_content", "--meta", "quality_gate_overridden=true") == ["1.1.1"]
    assert _audit("--meta", "extraction_source=file") == ["1.1.2", "1.1.3"], "an absent source is a file, as for the pre-nexus-6pfgb.6 document"
    assert _audit("--meta", "extraction_method=mineru", "--meta", "quality_gate_overridden=true") == []


def test_a_malformed_filter_is_a_usage_error_not_a_silent_scan(catalog_cli) -> None:
    result = CliRunner().invoke(main, ["catalog", "list", "--meta", "no-equals-sign"])
    assert result.exit_code != 0 and "KEY=VALUE" in result.output
