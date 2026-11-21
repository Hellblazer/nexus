# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 (Sam, 2026-11-21): a collection's owner is its SOURCE, set once at registration. The ``.nxexp``
import derives it in this order, never refusing a collection: (1) the bundle's owner header, (2) a repo-synth
name stem ``<slug>-<hash8>``, (3) a docs corpus's curator, (4) the knowledge curator with a loud warning that
the import summary counts. The bulk import used to put every collection with no usable row under the
knowledge curator, so catalog-044 derived the curator as a repo's owner (gate-xr789, nexus-wbfpw.33).

Tenant-free on purpose (``tests/storage_touch_ceiling.toml`` allows python_unit no new tenant test): the
resolver (``nexus.nxexp_owner``) takes a catalog reader and writer, so its cases run against ``_FakeCatalog``,
an in-memory catalog that implements only the methods the resolver, the export header and the importer's
owner step call. The importer is driven through its real ``plan`` (``_OwnerImport`` with the page writing
stubbed out), and the root backfill through the real ``HttpCatalogClient`` with its HTTP layer stubbed. The
engine-backed end to end of the same behaviour is ``tests/test_wbfpw31_nxexp_owner.py``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import msgpack
import numpy as np
import pytest
from click.testing import CliRunner

import nexus.exporter as exporter_mod
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.tumbler import Tumbler
from nexus.commands.store import import_cmd
from nexus.db.http_vector_client import HttpVectorClient
from nexus.exporter import _resolve_import_owner_tumbler, export_collection, import_collection
from nexus.repo_identity import _repo_identity_with_main

_MODEL = "bge-base-en-v15-768"
_DIM = 768


def _name(content_type: str, stem: str) -> str:
    return f"{content_type}__{stem}__{_MODEL}__v1"


def _fallbacks() -> tuple[list[str], Any]:
    seen: list[str] = []
    return seen, seen.append


class _FakeCatalog:
    """An in-memory catalog that is both reader and writer, implementing what the owner derivation calls.
    ``register_owner`` converges on an existing ``(name, owner_type)`` the way the engine does, and
    allocates the next prefix otherwise. ``registered`` records every call, so a test can say an owner was
    found and not registered a second time."""

    def __init__(self) -> None:
        self.owners: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}
        self.registered: list[str] = []

    # writer
    def register_owner(
        self, name: str, owner_type: str = "repo", *, repo_hash: str | None = None,
        repo_root: Any = None, **_kw: Any,
    ) -> Tumbler:
        self.registered.append(name)
        for prefix, row in self.owners.items():
            if (row["name"], row["owner_type"]) == (name, owner_type):
                if repo_hash:
                    row["repo_hash"] = repo_hash
                return Tumbler.parse(prefix)
        prefix = f"1.{len(self.owners) + 1}"
        self.owners[prefix] = {
            "tumbler_prefix": prefix, "name": name, "owner_type": owner_type,
            "repo_hash": repo_hash or "", "repo_root": str(repo_root or ""),
        }
        return Tumbler.parse(prefix)

    def add_collection(self, name: str, *, content_type: str, owner_id: str = "") -> None:
        self.collections[name] = {
            "name": name, "content_type": content_type, "owner_id": owner_id,
            "lifecycle_state": "live",
        }

    # reader
    def get_collection(self, name: str) -> dict | None:
        return self.collections.get(name)

    def list_collections(self, *, lifecycle_state: str = "", content_type: str = "", **_kw: Any) -> list[dict]:
        return [
            c for c in self.collections.values()
            if (not lifecycle_state or c["lifecycle_state"] == lifecycle_state)
            and (not content_type or c["content_type"] == content_type)
        ]

    def list_by_collection(self, name: str, limit: int = 0) -> list:
        return []

    def get_owner_by_prefix(self, prefix: str) -> dict | None:
        return self.owners.get(prefix)

    def list_owners_by_type(self, owner_type: str, *, include_deactivated: bool = False) -> list[dict]:
        return [r for r in self.owners.values() if r["owner_type"] == owner_type]

    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        for prefix, row in self.owners.items():
            if row["owner_type"] == "repo" and row["repo_hash"] == repo_hash:
                return Tumbler.parse(prefix)
        return None

    def curator_owner_tumbler_by_name(self, name: str) -> Tumbler | None:
        for prefix, row in self.owners.items():
            if row["owner_type"] == "curator" and row["name"] == name:
                return Tumbler.parse(prefix)
        return None

    def owner_row(self, tumbler: Tumbler) -> dict:
        return self.owners[str(tumbler)]


@pytest.fixture
def cat() -> _FakeCatalog:
    return _FakeCatalog()


def _resolve(cat: _FakeCatalog, name: str, **kw: Any) -> Tumbler:
    return _resolve_import_owner_tumbler(name, cat, cat, **kw)


# ── (1) the bundle's owner ────────────────────────────────────────────────────


def test_a_bundle_owner_names_the_repo_owner_existing_or_registered(cat):
    existing = cat.register_owner("r229-bundle-existing", "repo", repo_hash="b1b1b1b1", repo_root="/src/a")
    seen, hook = _fallbacks()
    cat.registered.clear()

    got = _resolve(
        cat, _name("code", "r229bundle-x"), content_type="code", on_fallback=hook,
        bundle_owner={"name": "ignored", "repo_hash": "b1b1b1b1", "repo_root": "/src/other"})
    assert got == existing and cat.registered == []         # an existing owner is found by repo_hash, not re-registered

    got = _resolve(
        cat, _name("code", "r229bundle-y"), content_type="code", on_fallback=hook,
        bundle_owner={"name": "r229-bundle-new", "repo_hash": "b2b2b2b2", "repo_root": "/src/b"})
    row = cat.owner_row(got)
    assert (row["owner_type"], row["repo_hash"], row["repo_root"], row["name"]) == (
        "repo", "b2b2b2b2", "", "r229-bundle-new")          # empty root: the header's path is the source machine's;
    assert seen == []                                       # `nx index repo` here backfills it. A source was found: no fallback


# ── (2) the repo-synth stem ───────────────────────────────────────────────────


def test_a_repo_stem_names_the_repo_owner_for_a_code_collection(cat):
    existing = cat.register_owner("r229-stem-existing", "repo", repo_hash="2ad2825c", repo_root="/src/arc")
    seen, hook = _fallbacks()

    got = _resolve(cat, _name("code", "arcaneum-2ad2825c"), content_type="code", on_fallback=hook)
    assert got == existing

    fresh = _resolve(cat, _name("code", "r229stem-0badf00d"), content_type="code", on_fallback=hook)
    row = cat.owner_row(fresh)
    assert (row["owner_type"], row["repo_hash"], row["repo_root"], row["name"]) == (
        "repo", "0badf00d", "", "r229stem")                 # no root: the source machine's path is not this one's
    assert seen == []


def test_a_docs_collection_with_a_code_sibling_takes_the_repo_owner(cat):
    seen, hook = _fallbacks()
    cat.add_collection(_name("code", "r229sib-5eed5eed"), content_type="code", owner_id="1-1")

    got = _resolve(cat, _name("docs", "r229sib-5eed5eed"), content_type="docs", on_fallback=hook)
    row = cat.owner_row(got)
    assert (row["owner_type"], row["repo_hash"]) == ("repo", "5eed5eed")
    assert seen == []


# ── (3) a docs corpus's curator ───────────────────────────────────────────────


def test_a_docs_only_corpus_takes_its_corpus_curator(cat):
    seen, hook = _fallbacks()

    got = _resolve(cat, _name("docs", "r229-wow-addon-dev"), content_type="docs", on_fallback=hook)
    row = cat.owner_row(got)
    assert (row["owner_type"], row["name"]) == ("curator", "r229-wow-addon-dev")
    # the way the doc indexer names it: the second call finds the same curator
    assert _resolve(cat, _name("docs", "r229-wow-addon-dev"), content_type="docs", on_fallback=hook) == got
    assert seen == []


def test_a_docs_collection_whose_repo_owner_exists_takes_it_with_no_code_collection(cat):
    """The widened case 2 (lead ruling): the target catalog already holds a repo owner with the stem's
    hash (an earlier ``nx index repo`` or a code import), and no ``code__`` collection of the stem
    exists anywhere, so the docs and rdr collections still resolve to that repo owner."""
    repo = cat.register_owner("r229-known-repo", "repo", repo_hash="7e7e7e7e", repo_root="/src/known")
    seen, hints = _fallbacks()
    hint_seen, on_hint = _fallbacks()
    for ctype in ("docs", "rdr"):
        got = _resolve(
            cat, _name(ctype, "r229known-7e7e7e7e"), content_type=ctype, on_fallback=hints, on_hint=on_hint)
        assert got == repo
    assert seen == [] and hint_seen == []                   # a source was found: no fallback, no hint


def test_a_repo_shaped_docs_stem_with_no_repo_stays_with_its_curator_and_hints_once(cat):
    seen, hook = _fallbacks()
    hints, on_hint = _fallbacks()
    stem = "r229lone-cafe1234"
    got = _resolve(cat, _name("docs", stem), content_type="docs", on_fallback=hook, on_hint=on_hint)
    row = cat.owner_row(got)
    assert (row["owner_type"], row["name"]) == ("curator", stem)
    assert seen == []                                       # not the knowledge curator: no fallback
    assert hints == [
        f"{stem} looks like a repo's collection; it was given its corpus curator as owner. To give it "
        "the repo's owner, import that repo's code__ bundle, then remove this collection and import it again."]
    # a docs stem with no hash is plainly a corpus: no hint
    hints.clear()
    _resolve(cat, _name("docs", "r229-plain-corpus"), content_type="docs", on_hint=on_hint)
    assert hints == []


def test_a_tumbler_shaped_docs_stem_is_not_a_corpus_name_and_falls_to_the_knowledge_curator(cat):
    """A ``docs__1-14__...`` name carries an owner tumbler, not a corpus name: registering a curator
    literally named ``1-14`` is wrong, so the import takes the loud knowledge-curator fallback."""
    curator = cat.register_owner("knowledge", "curator")
    seen, hook = _fallbacks()
    hints, on_hint = _fallbacks()

    got = _resolve(cat, _name("docs", "1-14"), content_type="docs", on_fallback=hook, on_hint=on_hint)

    assert got == curator
    assert len(seen) == 1 and "docs" in seen[0]
    assert hints == []
    assert cat.curator_owner_tumbler_by_name("1-14") is None   # no curator was registered under the tumbler


# ── (4) the knowledge curator, loudly ─────────────────────────────────────────


def test_no_source_falls_back_to_the_knowledge_curator_and_says_so(cat):
    curator = cat.register_owner("knowledge", "curator")
    seen, hook = _fallbacks()

    got = _resolve(cat, _name("code", "r229nohash"), content_type="code", on_fallback=hook)
    assert got == curator
    assert len(seen) == 1 and "code" in seen[0]

    # a knowledge collection keeps its curator and is not a fallback
    seen.clear()
    assert _resolve(cat, _name("knowledge", "r229-subject"), content_type="knowledge", on_fallback=hook) == curator
    assert seen == []


# ── the importer and the CLI ──────────────────────────────────────────────────


def _write_bundle(path: Path, collection: str, texts: list[str], *, owner: dict | None = None) -> None:
    header = {
        "format_version": 1, "collection_name": collection, "database_type": collection.split("__")[0],
        "embedding_model": _MODEL, "record_count": len(texts), "embedding_dim": _DIM,
        "exported_at": "2026-01-01T00:00:00+00:00", "pipeline_version": "nexus-1",
    }
    if owner is not None:
        header["owner"] = owner
    rng = np.random.default_rng(seed=229)
    with open(path, "wb") as f:
        f.write(json.dumps(header).encode() + b"\n")
        with gzip.GzipFile(fileobj=f, mode="wb") as gz:
            for t in texts:
                chash = hashlib.sha256(t.encode()).hexdigest()
                gz.write(msgpack.packb({
                    "id": chash, "document": t, "metadata": {"chunk_text_hash": chash},
                    "embedding": rng.standard_normal(_DIM).astype(np.float32).tobytes(),
                }, use_bin_type=True))


class _OwnerStepOnly(exporter_mod._OwnerImport):
    """The real ``_OwnerImport`` with its engine writes stubbed out: ``plan`` (the owner derivation and the
    ``on_fallback`` / ``on_hint`` wiring) is the real one, over the fake catalog; the page writes are not."""

    catalog: _FakeCatalog | None = None

    def _ensure(self) -> None:
        self._reader = self._writer = type(self).catalog
        self._import_writer = object()

    def flush(self, page: list) -> None:
        pass

    def finish(self) -> dict:
        return {"owned_count": 0, "unowned_count": 0, "unowned_documents": [], "sweep_skipped": 0}

    def close(self) -> None:
        pass

    def abort(self, error: str) -> None:
        pass

    def progress(self) -> tuple[int, int]:
        return (0, 0)


@pytest.fixture
def importer(cat, monkeypatch) -> list[Tumbler]:
    """Runs ``import_collection`` and the CLI over the fake catalog; returns the list of owners the importer
    handed to its document registration (one per owner group)."""
    handed: list[Tumbler] = []
    _OwnerStepOnly.catalog = cat
    monkeypatch.setattr(exporter_mod, "_OwnerImport", _OwnerStepOnly)
    monkeypatch.setattr(
        exporter_mod, "_resolve_owner_document",
        lambda group, collection, owner, *a, **kw: handed.append(owner) or "1.1.1")
    # RDR-229 P3.1: ``plan`` registers the target collection before the first document, which reads the engine's embedding
    # profile; this fake catalog is the owner derivation's, and registering a collection is not what these cases are about.
    monkeypatch.setattr(exporter_mod, "ensure_collection_registered", lambda *a, **kw: None)
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name, **kw: cat.get_collection(name))
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **kw: cat)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **kw: cat)
    return handed


@pytest.fixture
def db() -> HttpVectorClient:
    """A service-backed handle (``_owners_apply`` is an ``isinstance`` test) that reads no row and holds nothing."""
    client = object.__new__(HttpVectorClient)
    client._resolve_collection_row = lambda collection, strict=False: None  # type: ignore[method-assign]
    client.find_collection = lambda *a, **kw: None  # type: ignore[method-assign]
    client._routing_prefetch = None
    client.collection_exists = lambda name: False  # type: ignore[method-assign]
    return client


def test_the_import_summary_counts_a_fallback(cat, importer, db, tmp_path):
    curator = cat.register_owner("knowledge", "curator")
    nohash = _name("code", "r229summary")
    f = tmp_path / "nohash.nxexp"
    _write_bundle(f, nohash, ["r229 summary chunk"])
    result = import_collection(db=db, input_path=f, target_collection=nohash)
    assert result["owner_fallback_count"] == 1 and "code" in result["owner_fallback_reason"]
    assert importer == [curator]                            # the document goes under the knowledge curator

    stemmed = _name("code", "r229summary-1a2b3c4d")
    g = tmp_path / "stem.nxexp"
    _write_bundle(g, stemmed, ["r229 summary stem chunk"])
    importer.clear()
    result = import_collection(db=db, input_path=g, target_collection=stemmed)
    assert result["owner_fallback_count"] == 0 and result["owner_fallback_reason"] == ""
    assert result["owner_hint"] == ""
    assert len(importer) == 1 and cat.owner_row(importer[0])["repo_hash"] == "1a2b3c4d"   # under its source


def test_the_cli_warns_of_a_fallback_and_says_nothing_for_a_source_it_found(cat, importer, db, tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.commands.store._t3", lambda: db)
    cat.register_owner("knowledge", "curator")
    lone = _name("code", "r229summarycli")
    h = tmp_path / "nohash_cli.nxexp"
    _write_bundle(h, lone, ["r229 summary cli chunk"])
    out = CliRunner().invoke(import_cmd, [str(h), "--collection", lone])
    assert out.exit_code == 0, out.output
    assert "Warning:" in out.output and "knowledge curator" in out.output and "code" in out.output
    found = _name("code", "r229summarycli-2b3c4d5e")
    k = tmp_path / "stem_cli.nxexp"
    _write_bundle(k, found, ["r229 summary cli stem chunk"])
    out = CliRunner().invoke(import_cmd, [str(k), "--collection", found])
    assert out.exit_code == 0, out.output
    assert "knowledge curator" not in out.output


def test_the_import_summary_and_cli_carry_the_repo_looking_docs_hint(cat, importer, db, tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.commands.store._t3", lambda: db)
    cat.register_owner("knowledge", "curator")
    docs = _name("docs", "r229hint-9a9a9a9a")
    f = tmp_path / "hint.nxexp"
    _write_bundle(f, docs, ["r229 hint chunk"])
    result = import_collection(db=db, input_path=f, target_collection=docs)
    hint = ("r229hint-9a9a9a9a looks like a repo's collection; it was given its corpus curator as owner. "
            "To give it the repo's owner, import that repo's code__ bundle, then remove this collection "
            "and import it again.")
    assert result["owner_hint"] == hint and result["owner_fallback_count"] == 0

    again = _name("docs", "r229hint2-8b8b8b8b")
    g = tmp_path / "hint2.nxexp"
    _write_bundle(g, again, ["r229 hint chunk two"])
    out = CliRunner().invoke(import_cmd, [str(g), "--collection", again])
    assert out.exit_code == 0, out.output
    assert ("  Note: r229hint2-8b8b8b8b looks like a repo's collection; it was given its corpus curator as owner. "
            "To give it the repo's owner") in out.output


class _FakeVectors:
    """The slice of the vector store an export reads: one collection of one chunk with its vector."""

    def __init__(self, text: str) -> None:
        self.text, self.chash = text, hashlib.sha256(text.encode()).hexdigest()

    def get_collection(self, name: str) -> Any:
        me = self
        return SimpleNamespace(
            count=lambda: 1,
            get=lambda **kw: {"ids": [me.chash] if not kw.get("offset") else [], "documents": [me.text],
                              "metadatas": [{"chunk_text_hash": me.chash}]},
        )

    def get_embeddings(self, name: str, ids: list[str]) -> np.ndarray:
        return np.zeros((len(ids), _DIM), dtype=np.float32)


def test_an_export_carries_the_repo_owner_and_an_import_reads_it(cat, importer, db, tmp_path):
    owner = cat.register_owner("r229-roundtrip", "repo", repo_hash="c3c3c3c3", repo_root="/r229")
    src = _name("code", "r229roundtrip-c3c3c3c3")
    cat.add_collection(src, content_type="code", owner_id=str(owner).replace(".", "-"))
    vectors = _FakeVectors("r229 roundtrip chunk")
    for attr in ("get_collection", "get_embeddings"):
        setattr(db, attr, getattr(vectors, attr))
    cat.docs_for_chashes = lambda chashes: {}               # no manifest rows: the header is what is under test
    out = tmp_path / "rt.nxexp"
    export_collection(db=db, collection_name=src, output_path=out)
    with open(out, "rb") as fh:
        header = json.loads(fh.readline().decode())
    assert header["owner"] == {"name": "r229-roundtrip", "repo_hash": "c3c3c3c3", "repo_root": "/r229"}

    # a rename onto a name with no stem still lands under the bundle's repo owner, with no fallback
    dst = _name("code", "r229roundtrip-renamed")
    result = import_collection(db=db, input_path=out, target_collection=dst)
    assert result["owner_fallback_count"] == 0
    assert importer == [owner]


# ── TASK 2: the root backfill ─────────────────────────────────────────────────


class _OwnersOverHttp:
    """The engine's owner routes behind a real ``HttpCatalogClient``: the client's ``_get`` and ``_post`` are
    stubbed. An upsert that names the owner by identity converges on the row and keeps the columns its payload
    omits, as the engine does; one that names it by ``tumbler_prefix`` would assign every column."""

    def __init__(self, row: dict) -> None:
        self.row = dict(row)
        self.upserts: list[dict] = []

    def get(self, path: str, *, params: dict | None = None, **_kw: Any) -> Any:
        assert path == "/v1/catalog/owners/by_repo", path
        return dict(self.row)

    def post(self, path: str, body: dict | None = None, **_kw: Any) -> Any:
        assert path == "/owners/upsert", path
        self.upserts.append(dict(body or {}))
        assert "tumbler_prefix" not in (body or {}), "an upsert naming the row by prefix blanks the omitted columns"
        self.row.update({k: v for k, v in (body or {}).items() if k in ("name", "owner_type", "repo_hash", "repo_root")})
        return {"tumbler_prefix": self.row["tumbler_prefix"]}


def _http_client(engine: _OwnersOverHttp) -> HttpCatalogClient:
    client = object.__new__(HttpCatalogClient)
    client._resolve_path_owner_cache = {}
    # the mixin's own ``_get`` is reached through the catalog override, which prefixes ``/v1/catalog``
    client._get = lambda path, **kw: engine.get(f"/v1/catalog{path}", params=kw)    # type: ignore[method-assign]
    client._post = lambda path, body=None, **kw: engine.post(path, body, **kw)      # type: ignore[method-assign]
    return client


def test_ensure_owner_for_repo_backfills_an_empty_root_and_keeps_the_rest(tmp_path):
    name, repo_hash, main_repo = _repo_identity_with_main(tmp_path)
    engine = _OwnersOverHttp({
        "tumbler_prefix": "1.7", "name": name, "owner_type": "repo", "repo_hash": repo_hash, "repo_root": "",
        "description": "kept", "head_hash": "abc123",
    })

    assert _http_client(engine).ensure_owner_for_repo(tmp_path) == Tumbler.parse("1.7")
    assert len(engine.upserts) == 1
    assert engine.upserts[0] == {"name": name, "owner_type": "repo", "repo_hash": repo_hash, "repo_root": str(main_repo)}
    row = engine.row
    assert row["repo_root"] == str(main_repo)
    assert (row["description"], row["head_hash"], row["name"]) == ("kept", "abc123", name)
    # the upsert names no description or head_hash, so the engine keeps the stored ones
    assert "description" not in engine.upserts[0] and "head_hash" not in engine.upserts[0]


def test_ensure_owner_for_repo_leaves_an_existing_root_alone(tmp_path):
    name, repo_hash, _main = _repo_identity_with_main(tmp_path)
    engine = _OwnersOverHttp({
        "tumbler_prefix": "1.7", "name": name, "owner_type": "repo", "repo_hash": repo_hash,
        "repo_root": "/already/set",
    })

    assert _http_client(engine).ensure_owner_for_repo(tmp_path) == Tumbler.parse("1.7")
    assert engine.upserts == []
    assert engine.row["repo_root"] == "/already/set"
