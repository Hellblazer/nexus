# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 (Sam, 2026-11-21): a collection's owner is its SOURCE, set once at registration. The ``.nxexp``
import derives it in this order, never refusing a collection: (1) the bundle's owner header, (2) a repo-synth
name stem ``<slug>-<hash8>``, (3) a docs corpus's curator, (4) the knowledge curator with a loud warning that
the import summary counts. The bulk import used to put every collection with no usable row under the
knowledge curator, so catalog-044 derived the curator as a repo's owner (gate-xr789, nexus-wbfpw.33).

Real engine substrate (``t2_service_env``): owner resolution reads and writes the real catalog.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import msgpack
import numpy as np
import pytest

import nexus.catalog.http_catalog_client as hcc
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.db.http_vector_client import HttpVectorClient
from nexus.repo_identity import _repo_identity_with_main
from nexus.exporter import _resolve_import_owner_tumbler, export_collection, import_collection
from tests._chunk_seed import seed_chunks_direct

_MODEL = "bge-base-en-v15-768"
_DIM = 768


def _name(content_type: str, stem: str) -> str:
    return f"{content_type}__{stem}__{_MODEL}__v1"


def _fallbacks() -> tuple[list[str], object]:
    seen: list[str] = []
    return seen, seen.append


def _owner_row(reader, tumbler) -> dict:
    row = reader.get_owner_by_prefix(str(tumbler))
    assert row is not None
    return row


# ── (1) the bundle's owner ────────────────────────────────────────────────────


def test_a_bundle_owner_names_the_repo_owner_existing_or_registered(t2_service_env):
    reader, writer = make_catalog_reader(), make_catalog_writer()
    existing = writer.register_owner("r229-bundle-existing", "repo", repo_hash="b1b1b1b1", repo_root="/src/a")
    seen, hook = _fallbacks()

    got = _resolve_import_owner_tumbler(
        _name("code", "r229bundle-x"), reader, writer, content_type="code", on_fallback=hook,
        bundle_owner={"name": "ignored", "repo_hash": "b1b1b1b1", "repo_root": "/src/other"},
    )
    assert got == existing                                  # an existing owner is found by repo_hash, not re-registered

    got = _resolve_import_owner_tumbler(
        _name("code", "r229bundle-y"), reader, writer, content_type="code", on_fallback=hook,
        bundle_owner={"name": "r229-bundle-new", "repo_hash": "b2b2b2b2", "repo_root": "/src/b"},
    )
    row = _owner_row(reader, got)
    assert (row["owner_type"], row["repo_hash"], row["repo_root"], row["name"]) == (
        "repo", "b2b2b2b2", "/src/b", "r229-bundle-new")
    assert seen == []                                       # a source was found: no fallback


# ── (2) the repo-synth stem ───────────────────────────────────────────────────


def test_a_repo_stem_names_the_repo_owner_for_a_code_collection(t2_service_env):
    reader, writer = make_catalog_reader(), make_catalog_writer()
    existing = writer.register_owner("r229-stem-existing", "repo", repo_hash="2ad2825c", repo_root="/src/arc")
    seen, hook = _fallbacks()

    got = _resolve_import_owner_tumbler(
        _name("code", "arcaneum-2ad2825c"), reader, writer, content_type="code", on_fallback=hook)
    assert got == existing

    fresh = _resolve_import_owner_tumbler(
        _name("code", "r229stem-0badf00d"), reader, writer, content_type="code", on_fallback=hook)
    row = _owner_row(reader, fresh)
    assert (row["owner_type"], row["repo_hash"], row["repo_root"], row["name"]) == (
        "repo", "0badf00d", "", "r229stem")                 # no root: the source machine's path is not this one's
    assert seen == []


def test_a_docs_collection_with_a_code_sibling_takes_the_repo_owner(t2_service_env):
    reader, writer = make_catalog_reader(), make_catalog_writer()
    seen, hook = _fallbacks()
    writer.register_collection(
        _name("code", "r229sib-5eed5eed"), content_type="code", owner_id="1-1", embedding_model=_MODEL)

    got = _resolve_import_owner_tumbler(
        _name("docs", "r229sib-5eed5eed"), reader, writer, content_type="docs", on_fallback=hook)
    row = _owner_row(reader, got)
    assert (row["owner_type"], row["repo_hash"]) == ("repo", "5eed5eed")
    assert seen == []


# ── (3) a docs corpus's curator ───────────────────────────────────────────────


def test_a_docs_only_corpus_takes_its_corpus_curator(t2_service_env):
    reader, writer = make_catalog_reader(), make_catalog_writer()
    seen, hook = _fallbacks()

    got = _resolve_import_owner_tumbler(
        _name("docs", "r229-wow-addon-dev"), reader, writer, content_type="docs", on_fallback=hook)
    row = _owner_row(reader, got)
    assert (row["owner_type"], row["name"]) == ("curator", "r229-wow-addon-dev")
    # the way the doc indexer names it: the second call finds the same curator
    assert _resolve_import_owner_tumbler(
        _name("docs", "r229-wow-addon-dev"), reader, writer, content_type="docs", on_fallback=hook) == got
    # a repo-shaped stem with NO code collection of it is a corpus too
    lone = _resolve_import_owner_tumbler(
        _name("docs", "r229lone-cafe1234"), reader, writer, content_type="docs", on_fallback=hook)
    assert _owner_row(reader, lone)["owner_type"] == "curator"
    assert seen == []


# ── (4) the knowledge curator, loudly ─────────────────────────────────────────


def test_no_source_falls_back_to_the_knowledge_curator_and_says_so(t2_service_env):
    reader, writer = make_catalog_reader(), make_catalog_writer()
    curator = writer.register_owner("knowledge", "curator")
    seen, hook = _fallbacks()

    got = _resolve_import_owner_tumbler(
        _name("code", "r229nohash"), reader, writer, content_type="code", on_fallback=hook)
    assert got == curator
    assert len(seen) == 1 and "code" in seen[0]

    # a knowledge collection keeps its curator and is not a fallback
    seen.clear()
    assert _resolve_import_owner_tumbler(
        _name("knowledge", "r229-subject"), reader, writer, content_type="knowledge", on_fallback=hook,
    ) == curator
    assert seen == []


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


def test_the_import_summary_counts_a_fallback_and_the_cli_warns(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer()
    writer.register_owner("knowledge", "curator")
    nohash = _name("code", "r229summary")
    f = tmp_path / "nohash.nxexp"
    _write_bundle(f, nohash, ["r229 summary chunk"])
    result = import_collection(db=client, input_path=f, target_collection=nohash)
    assert result["owner_fallback_count"] == 1 and "code" in result["owner_fallback_reason"]

    stemmed = _name("code", "r229summary-1a2b3c4d")
    g = tmp_path / "stem.nxexp"
    _write_bundle(g, stemmed, ["r229 summary stem chunk"])
    result = import_collection(db=client, input_path=g, target_collection=stemmed)
    assert result["owner_fallback_count"] == 0 and result["owner_fallback_reason"] == ""
    doc = make_catalog_reader().by_source_uri(f"nxexp://{stemmed}/{g.name}")
    assert doc is not None
    assert _owner_row(make_catalog_reader(), doc.tumbler.owner_address())["repo_hash"] == "1a2b3c4d"


def test_an_export_carries_the_repo_owner_and_an_import_reads_it(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    reader, writer = make_catalog_reader(), make_catalog_writer()
    owner = writer.register_owner("r229-roundtrip", "repo", repo_hash="c3c3c3c3", repo_root="/src/rt")
    src = _name("code", "r229roundtrip-c3c3c3c3")
    writer.register_collection(src, content_type="code", owner_id=str(owner), embedding_model=_MODEL)
    text = "r229 roundtrip chunk"
    chash = hashlib.sha256(text.encode()).hexdigest()
    seed_chunks_direct(
        src, ids=[chash], documents=[text], embed=True,
        metadatas=[{"chunk_text_hash": chash, "indexed_at": datetime.now(UTC).isoformat()}],
    )
    writer.write_manifest(
        str(writer.register(
            owner=owner, title="rt doc", content_type="code", physical_collection=src,
            source_uri="file:///r229/rt.py")),
        [{"chash": chash, "position": 0}], collection=src,
    )
    out = tmp_path / "rt.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)
    with open(out, "rb") as fh:
        header = json.loads(fh.readline().decode())
    assert header["owner"] == {"name": "r229-roundtrip", "repo_hash": "c3c3c3c3", "repo_root": "/src/rt"}

    # a rename onto a name with no stem still lands under the bundle's repo owner, with no fallback
    dst = _name("code", "r229roundtrip-renamed")
    result = import_collection(db=client, input_path=out, target_collection=dst)
    assert result["owner_fallback_count"] == 0
    doc = reader.by_source_uri(f"nxexp://{dst}/file:///r229/rt.py")
    assert doc is not None and doc.tumbler.owner_address() == owner


# ── TASK 2: the root backfill ─────────────────────────────────────────────────


def test_ensure_owner_for_repo_backfills_an_empty_root_and_keeps_the_rest(t2_service_env, tmp_path):
    reader, writer = make_catalog_reader(), make_catalog_writer()
    name, repo_hash, main_repo = _repo_identity_with_main(tmp_path)
    owner = writer.register_owner(name, "repo", repo_hash=repo_hash, description="kept", head_hash="abc123")
    assert _owner_row(reader, owner)["repo_root"] == ""

    assert writer.ensure_owner_for_repo(tmp_path) == owner
    row = _owner_row(reader, owner)
    assert row["repo_root"] == str(main_repo)
    assert (row["description"], row["head_hash"], row["name"]) == ("kept", "abc123", name)


def test_ensure_owner_for_repo_leaves_an_existing_root_alone(t2_service_env, tmp_path, monkeypatch):
    writer = make_catalog_writer()
    name, repo_hash, _main = _repo_identity_with_main(tmp_path)
    owner = writer.register_owner(name, "repo", repo_hash=repo_hash, repo_root="/already/set")
    posts: list[str] = []
    real_post = hcc.HttpCatalogClient._post

    def _spy(self, path, body=None, **kw):
        posts.append(path)
        return real_post(self, path, body, **kw)

    monkeypatch.setattr(hcc.HttpCatalogClient, "_post", _spy)
    assert writer.ensure_owner_for_repo(tmp_path) == owner
    assert "/owners/upsert" not in posts
    assert make_catalog_reader().get_owner_by_prefix(str(owner))["repo_root"] == "/already/set"
