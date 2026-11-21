# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-229 P2.5 (nexus-p2m1x.10), client half: the ``quarantine-`` half of every
``_BYPASS_SCHEMA_PREFIXES`` test reads the collection row's ``lifecycle_state``.

One test per site (twelve in all, db/t3.py and exporter.py through the doctor
checks). Each feeds the site four collections and asserts which survive:

* ``ROW_QUARANTINE``: no ``quarantine-`` prefix, row says ``quarantine`` -> the site skips it;
* ``PREFIXED_LIVE``: a ``quarantine-`` name whose row says ``live`` -> the site keeps it;
* ``PREFIXED_NOROW``: a ``quarantine-`` name with no ``lifecycle_state`` (no row) -> today's
  prefix outcome, the site skips it;
* ``PLAIN``: an ordinary live collection -> the site keeps it.

No engine: the listing seams are fakes, the way each site's caller reaches them.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus import mcp_infra
from nexus.cli import main
from nexus.commands.catalog_cmds.doctor import (
    _run_chunk_size_distribution,
    _run_chunk_text_dedup,
    _run_collections_drift,
    _run_name_vs_embed_dim,
    _run_t3_vs_catalog,
)
from nexus.commands.catalog_cmds.integrity import _class_a_vanished_collections
from nexus.commands.catalog_cmds.t3_orphans import classify_t3_orphan_collections
from nexus.commands.collection import _find_dimension_mismatched_collections
from nexus.db.t3 import _bypass_canonical_schema
from nexus.errors import NexusError
from nexus.exporter import FormatVersionError, import_collection

_TOK = "bge-base-en-v15-768"
ROW_QUARANTINE = f"code__9-1__{_TOK}__v1"
PREFIXED_LIVE = f"quarantine-code__9-2__{_TOK}__v1"
PREFIXED_NOROW = f"quarantine-code__9-3__{_TOK}__v1"
PLAIN = f"code__9-4__{_TOK}__v1"


def _rows(*, count: int = 5, tok: str = _TOK) -> list[dict]:
    """The four-collection listing, in the shape ``HttpVectorClient.list_collections`` returns."""
    def row(name: str, state: str | None) -> dict:
        r: dict = {"name": name, "count": count, "stored_count": count}
        if state is not None:
            r.update(content_type="code", owner_id="9-1", embedding_model=tok, lifecycle_state=state)
        return r

    return [
        row(ROW_QUARANTINE.replace(_TOK, tok), "quarantine"),
        row(PREFIXED_LIVE.replace(_TOK, tok), "live"),
        row(PREFIXED_NOROW.replace(_TOK, tok), None),
        row(PLAIN.replace(_TOK, tok), "live"),
    ]


class _ListingT3:
    """A T3 handle that lists a fixed set of rows and cannot open any of them."""

    _tenant = "default"

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_collections(self, *_a, **_k) -> list[dict]:
        return [dict(r) for r in self._rows]

    def get_or_create_collection(self, name: str, **_k):
        raise RuntimeError("no engine in this test")

    def embedding_mode(self) -> str:
        return "onnx-local"


class _FakeCatalog:
    """The slice of the catalog reader the checks touch: nothing registered, nothing documented."""

    def __init__(self, *, doc_counts: dict[str, int] | None = None, registry: list[dict] | None = None) -> None:
        self._doc_counts = doc_counts or {}
        self._registry = registry or []

    def list_collections(self, *_a, **_k) -> list[dict]:
        return list(self._registry)

    def distinct_doc_collections(self) -> list[str]:
        return sorted(self._doc_counts)

    def collection_doc_counts(self, include_deleted: bool = False) -> dict[str, int]:
        return dict(self._doc_counts)


def _patch_env(t3, cat):
    return (
        patch("nexus.db.make_t3", return_value=t3),
        patch("nexus.commands.catalog._get_catalog", return_value=cat),
        patch("nexus.commands.catalog._get_catalog_writer", return_value=MagicMock()),
    )


# ── db/t3.py:91, the write path's schema bypass ─────────────────────────────


@pytest.fixture()
def row_cache():
    mcp_infra.invalidate_collections_cache()
    yield mcp_infra.prime_collections_cache
    mcp_infra.invalidate_collections_cache()


def test_t3_schema_bypass_reads_the_cached_row(row_cache):
    row_cache(_rows())
    assert _bypass_canonical_schema(ROW_QUARANTINE) is True
    assert _bypass_canonical_schema(PREFIXED_LIVE) is False
    # No lifecycle_state: no row, so the name decides.
    assert _bypass_canonical_schema(PREFIXED_NOROW) is True
    assert _bypass_canonical_schema(PLAIN) is False
    # Never listed at all: the name decides, both halves.
    assert _bypass_canonical_schema("quarantine-never-listed") is True
    assert _bypass_canonical_schema("taxonomy__centroids") is True


def test_t3_schema_bypass_never_fetches_on_the_write_path(row_cache, monkeypatch):
    """The write path reads whatever the cache holds: a cold cache costs no round trip."""
    mcp_infra.invalidate_collections_cache()
    monkeypatch.setattr(
        mcp_infra, "_refresh_collections_cache_if_stale",
        lambda **_k: pytest.fail("the write path fetched the collection listing"),
    )
    assert _bypass_canonical_schema(PREFIXED_NOROW) is True
    assert _bypass_canonical_schema(PLAIN) is False


# ── exporter.py:1847, the import's refusal of a no-chunk-hash target ────────


def _export_header(tmp_path, **extra) -> object:
    path = tmp_path / "x.nxexp"
    header = {"format_version": 1, "collection_name": "src", **extra}
    path.write_text(json.dumps(header) + "\n")
    return path


@pytest.mark.parametrize(
    ("target", "state", "refused"),
    [
        ("odd-label", "quarantine", True),
        (PREFIXED_LIVE, "live", False),
        (PREFIXED_NOROW, None, True),
        ("taxonomy__centroids", "live", True),
    ],
    ids=["row-quarantine", "prefixed-live", "prefixed-no-row", "taxonomy"],
)
def test_import_refuses_a_quarantine_row_not_a_quarantine_name(tmp_path, target, state, refused):
    row = None if state is None else {"content_type": "code", "lifecycle_state": state}
    db = SimpleNamespace(_resolve_collection_row=lambda name, **_k: row)
    path = _export_header(tmp_path)
    with patch("nexus.exporter._owners_apply", return_value=True), \
            patch("nexus.db.serving_engine_version.require_note_uri_engine"):
        if refused:
            with pytest.raises(NexusError, match="not supported"):
                import_collection(db, path, target_collection=target)
        else:
            # Past the gate: the header names no embedding_model, which is the next refusal.
            with pytest.raises(FormatVersionError, match="embedding_model"):
                import_collection(db, path, target_collection=target)


@pytest.mark.parametrize(
    ("target", "refused"),
    [(PREFIXED_NOROW, True), ("odd-label", False)],
    ids=["prefixed", "plain"],
)
def test_import_falls_back_to_the_label_when_the_row_lookup_fails(tmp_path, target, refused):
    # Review round 2, M1: a resolver that raises must not abort the import; with no row the label decides.
    def _boom(name, **_k):
        raise RuntimeError("listing unavailable")

    db = SimpleNamespace(_resolve_collection_row=_boom)
    path = _export_header(tmp_path)
    with patch("nexus.exporter._owners_apply", return_value=True), \
            patch("nexus.db.serving_engine_version.require_note_uri_engine"):
        if refused:
            with pytest.raises(NexusError, match="not supported"):
                import_collection(db, path, target_collection=target)
        else:
            with pytest.raises(FormatVersionError, match="embedding_model"):
                import_collection(db, path, target_collection=target)


# ── commands/collection.py:783 ──────────────────────────────────────────────


def test_dimension_mismatch_finder_skips_the_quarantine_row(monkeypatch):
    monkeypatch.setattr(
        "nexus.db.http_vector_client._get",
        lambda path, *, tenant="default": {"embedding_mode": "onnx-local", "embedding_models": ["bge-base-en-v15-768"]},
    )
    tok = "minilm-l6-v2-384"
    mismatches, skipped, _label = _find_dimension_mismatched_collections(_ListingT3(_rows(tok=tok)))
    assert [m["name"] for m in mismatches] == [PLAIN.replace(_TOK, tok)]
    # PREFIXED_LIVE is not skipped as a bypass name: it reaches the conformance gate, which counts it.
    assert skipped == 1


# ── commands/catalog_cmds/t3_orphans.py:89 ──────────────────────────────────


def test_orphan_classifier_skips_the_quarantine_row():
    rows = classify_t3_orphan_collections(_FakeCatalog(), _ListingT3(_rows()))
    assert sorted(r["name"] for r in rows) == sorted([PREFIXED_LIVE, PLAIN])


# ── commands/catalog_cmds/integrity.py:550 ──────────────────────────────────


def _registry() -> list[dict]:
    """The catalog's own registry rows: PREFIXED_NOROW has none."""
    return [r for r in _rows() if r["name"] != PREFIXED_NOROW]


def test_class_a_vanished_skips_the_registry_quarantine_row():
    cat = _FakeCatalog(
        doc_counts={n: 2 for n in (ROW_QUARANTINE, PREFIXED_LIVE, PREFIXED_NOROW, PLAIN)},
        registry=_registry(),
    )
    present = _ListingT3([{"name": "knowledge__here__bge-base-en-v15-768__v1", "count": 1}])
    unreadable: list[str] = []
    vanished, names = _class_a_vanished_collections(cat, present, unreadable)
    assert names == {PREFIXED_LIVE, PLAIN}
    assert sorted(v["physical_collection"] for v in vanished) == sorted([PREFIXED_LIVE, PLAIN])
    assert unreadable == []


def test_class_a_vanished_reads_no_registry_when_nothing_is_absent():
    """The clean path pays no extra catalog request."""
    cat = _FakeCatalog(doc_counts={PLAIN: 2})
    cat.list_collections = lambda *_a, **_k: pytest.fail("read the registry with nothing absent")
    _class_a_vanished_collections(cat, _ListingT3([{"name": PLAIN, "count": 1}]), [])


def test_class_a_vanished_falls_back_to_the_prefix_when_the_registry_is_unreadable():
    cat = _FakeCatalog(doc_counts={n: 2 for n in (ROW_QUARANTINE, PREFIXED_NOROW, PLAIN)})

    def boom(*_a, **_k):
        raise RuntimeError("registry unreachable")

    cat.list_collections = boom
    present = _ListingT3([{"name": "knowledge__here__bge-base-en-v15-768__v1", "count": 1}])
    _vanished, names = _class_a_vanished_collections(cat, present, [])
    # No row to read, so the name decides, as it always did.
    assert names == {ROW_QUARANTINE, PLAIN}


# ── commands/catalog_cmds/collections.py:70 and :591 ────────────────────────


def test_backfill_skips_the_quarantine_row():
    t3, cat, writer = _patch_env(_ListingT3(_rows()), _FakeCatalog())
    with t3, cat, writer:
        result = CliRunner().invoke(main, ["catalog", "backfill-collections"])
    assert result.exit_code == 0, result.output
    assert ROW_QUARANTINE not in result.output
    assert PREFIXED_NOROW not in result.output
    assert PLAIN in result.output
    # A prefixed name whose row is live is listed (the quarantine-sibling skip that follows is its own rule).
    assert PREFIXED_LIVE in result.output


def test_collection_gc_excludes_the_quarantine_row_as_bypass():
    t3, cat, writer = _patch_env(_ListingT3(_rows(count=0)), _FakeCatalog())
    with t3, cat, writer:
        result = CliRunner().invoke(main, ["catalog", "collection-gc"])
    assert result.exit_code == 0, result.output
    assert "bypass-schema (excluded): 2" in result.output
    assert "zombie candidates): 2" in result.output
    assert f"would delete {PLAIN}" in result.output
    assert f"would delete {PREFIXED_LIVE}" in result.output
    assert ROW_QUARANTINE not in result.output
    assert PREFIXED_NOROW not in result.output


# ── commands/catalog_cmds/doctor.py: 438, 651, 768, 911, 1096 ───────────────


def test_doctor_collections_drift_skips_the_quarantine_row():
    t3, cat, writer = _patch_env(_ListingT3(_rows()), _FakeCatalog())
    with t3, cat, writer:
        report = _run_collections_drift()
    assert report["t3_not_in_projection"] == sorted([PREFIXED_LIVE, PLAIN])


def test_doctor_chunk_size_distribution_skips_the_quarantine_row():
    with patch("nexus.db.make_t3", return_value=_ListingT3(_rows())):
        report = _run_chunk_size_distribution()
    assert sorted(report["tables"]) == sorted([PREFIXED_LIVE, PLAIN])


def test_doctor_chunk_text_dedup_skips_the_quarantine_row():
    with patch("nexus.db.make_t3", return_value=_ListingT3(_rows())):
        report = _run_chunk_text_dedup()
    assert sorted(report["within"]) == sorted([PREFIXED_LIVE, PLAIN])


def test_doctor_t3_vs_catalog_skips_the_quarantine_row():
    names = (ROW_QUARANTINE, PREFIXED_LIVE, PREFIXED_NOROW, PLAIN)
    registry = [{"name": n} for n in names]
    t3, cat, writer = _patch_env(_ListingT3(_rows(count=0)), _FakeCatalog(registry=registry))
    with t3, cat, writer:
        report = _run_t3_vs_catalog()
    assert sorted(report["zombies"]) == sorted([PREFIXED_LIVE, PLAIN])


def test_doctor_name_vs_embed_dim_skips_the_quarantine_row():
    with patch("nexus.db.make_t3", return_value=_ListingT3(_rows())):
        report = _run_name_vs_embed_dim()
    # ROW_QUARANTINE and PREFIXED_NOROW are bypassed; PLAIN is probed (and unreadable here);
    # PREFIXED_LIVE reaches the conformance gate, which counts it.
    assert [e["collection"] for e in report["read_errors"]] == [PLAIN]
    assert report["skipped_non_conformant"] == 1
