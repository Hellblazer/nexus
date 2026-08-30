# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-kkumv: ``nx enrich aspects-without-catalog --sweep`` deletes
orphaned aspect rows (worktree/tmp-path or missing-on-disk file:// rows,
and NULL-source_uri rows), excluding chroma:// store_put-origin markers,
and re-enqueues a basename-matched live canonical document that has no
aspect row of its own."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.enrich import enrich
from nexus.db.t2 import http_document_aspects_store as aspects_mod


class _FakeCatalogEntry:
    def __init__(self, tumbler: str, physical_collection: str, source_uri: str, file_path: str):
        self.tumbler = tumbler
        self.physical_collection = physical_collection
        self.source_uri = source_uri
        self.file_path = file_path


@pytest.fixture
def _sweep_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Canned census rows + a canned live catalog + capture-only writes."""
    rows = [
        # class A: worktree-marker path, file exists nowhere real.
        {"collection": "rdr__nexus", "source_path": "/repo/.claude/worktrees/agent-z/docs/rdr/rdr-999.md",
         "source_uri": "file:///repo/.claude/worktrees/agent-z/docs/rdr/rdr-999.md",
         "extracted_at": "2026-08-01T00:00:00Z", "model_version": "v1",
         "extractor_name": "rdr-frontmatter-v1", "confidence": 1.0, "tombstoned_match": False},
        # class A: missing on disk, no worktree marker, no live basename twin.
        {"collection": "rdr__nexus", "source_path": "/repo/docs/rdr/rdr-gone.md",
         "source_uri": "file:///repo/docs/rdr/rdr-gone.md",
         "extracted_at": "2026-08-01T00:00:00Z", "model_version": "v1",
         "extractor_name": "rdr-frontmatter-v1", "confidence": 1.0, "tombstoned_match": False},
        # class B: NULL source_uri (nexus-r0kum's writer-defect class).
        {"collection": "rdr__nexus", "source_path": "7e00d7d567d326ec6a6b595a4fbf12fc",
         "source_uri": None,
         "extracted_at": "2026-08-01T00:00:00Z", "model_version": "rdr-frontmatter-v1",
         "extractor_name": "rdr-frontmatter-v1", "confidence": 1.0, "tombstoned_match": False},
        # excluded: chroma:// store_put-origin marker, untouched by the sweep.
        {"collection": "knowledge__k", "source_path": "note",
         "source_uri": "chroma://knowledge__k/note",
         "extracted_at": "2026-08-01T00:00:00Z", "model_version": "v1",
         "extractor_name": "general-prose-v1", "confidence": 0.8, "tombstoned_match": False},
    ]

    def _fake_list(self, limit=None, offset=0):
        return [dict(r) for r in rows] if offset == 0 else []

    monkeypatch.setattr(
        aspects_mod.HttpDocumentAspectsStore, "list_without_catalog_document", _fake_list,
    )
    monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "t2.db")

    # The worktree-marker row's basename (rdr-999.md) IS registered under
    # a live, canonical (non-worktree) URI with no aspect row of its own —
    # the recovery target.
    canonical = _FakeCatalogEntry(
        "1.1.500", "rdr__nexus", "file:///repo/docs/rdr/rdr-999.md",
        "docs/rdr/rdr-999.md",
    )

    class _FakeReader:
        def all_documents(self):
            return [canonical]

    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _FakeReader(),
    )
    monkeypatch.setattr(
        aspects_mod.HttpDocumentAspectsStore, "get_by_doc_id", lambda self, doc_id: None,
    )

    deletes: list[tuple[str, str]] = []
    enqueues: list[dict] = []

    def _fake_t2_index_write(write_fn, **kw):
        class _FakeAspectQueue:
            def enqueue(self, collection, source_path, content_hash="", content="", *, doc_id=""):
                enqueues.append({
                    "collection": collection, "source_path": source_path, "doc_id": doc_id,
                })

        class _FakeDocumentAspects:
            def delete(self, collection, source_path):
                deletes.append((collection, source_path))
                return 1

        class _FakeDb:
            document_aspects = _FakeDocumentAspects()
            aspect_queue = _FakeAspectQueue()

        return write_fn(_FakeDb())

    monkeypatch.setattr("nexus.mcp_infra.t2_index_write", _fake_t2_index_write)

    return {"rows": rows, "deletes": deletes, "enqueues": enqueues}


def test_dry_run_reports_classes_without_writing(_sweep_env):
    result = CliRunner().invoke(enrich, ["aspects-without-catalog", "--sweep", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["apply"] is False
    assert report["class_a"] == 2
    assert report["class_b"] == 1
    assert report["excluded_store_put_origin_marker"] == 1
    assert report["deleted"] == 0
    assert not _sweep_env["deletes"]
    assert not _sweep_env["enqueues"]
    # the worktree-marker row has a recovery target named in the dry-run report
    worktree_action = next(
        a for a in report["actions"] if "worktrees" in a["source_path"]
    )
    assert worktree_action["recovery_doc_id"] == "1.1.500"


def test_apply_deletes_orphans_and_recovers_the_basename_match(_sweep_env):
    result = CliRunner().invoke(enrich, ["aspects-without-catalog", "--sweep", "--apply", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["apply"] is True
    assert report["deleted"] == 3
    assert report["delete_failures"] == 0
    assert report["enqueued"] == 1

    deletes = _sweep_env["deletes"]
    assert len(deletes) == 3
    assert ("knowledge__k", "note") not in deletes  # chroma:// marker untouched

    enqueues = _sweep_env["enqueues"]
    assert len(enqueues) == 1
    assert enqueues[0]["doc_id"] == "1.1.500"
    assert enqueues[0]["collection"] == "rdr__nexus"
    assert enqueues[0]["source_path"] == "docs/rdr/rdr-999.md"


def test_apply_without_sweep_is_refused(_sweep_env):
    result = CliRunner().invoke(enrich, ["aspects-without-catalog", "--apply"])
    assert result.exit_code != 0
    assert "--apply only applies alongside --sweep" in result.output
    assert not _sweep_env["deletes"]


def test_delete_failure_is_reported_and_exits_nonzero(_sweep_env, monkeypatch):
    def _failing_t2_index_write(write_fn, **kw):
        class _FakeDocumentAspects:
            def delete(self, collection, source_path):
                raise RuntimeError("engine unreachable")

        class _FakeAspectQueue:
            def enqueue(self, *a, **kw):
                raise AssertionError("must not enqueue after a delete failure")

        class _FakeDb:
            document_aspects = _FakeDocumentAspects()
            aspect_queue = _FakeAspectQueue()

        return write_fn(_FakeDb())

    monkeypatch.setattr("nexus.mcp_infra.t2_index_write", _failing_t2_index_write)
    result = CliRunner().invoke(enrich, ["aspects-without-catalog", "--sweep", "--apply", "--json"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["delete_failures"] == 3
    assert report["deleted"] == 0


def test_present_on_disk_orphan_is_still_deleted(_sweep_env, tmp_path, monkeypatch):
    """Review round (2026-08-29): a file:// row whose file still exists on
    THIS box but has no catalog document is an orphan by construction (the
    population is doc_id-NULL rows with no document by doc_id or byte-equal
    URI). It must be deleted like the rest; file presence is reported as
    a sub-bucket, never used as the delete trigger — a box-local exists()
    must not decide deletion on a multi-box tenant."""
    present = tmp_path / "present.md"
    present.write_text("# present\n")
    _sweep_env["rows"].append({
        "collection": "rdr__nexus", "source_path": str(present),
        "source_uri": present.as_uri(),
        "extracted_at": "2026-08-01T00:00:00Z", "model_version": "rdr-frontmatter-v1",
        "extractor_name": "rdr-frontmatter-v1", "confidence": 1.0, "tombstoned_match": False,
    })
    result = CliRunner().invoke(enrich, ["aspects-without-catalog", "--sweep", "--apply", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["class_a"] == 3
    assert report["class_a_by_reason"]["present_no_document"] == 1
    assert report["class_a_by_reason"]["ephemeral_path"] + report["class_a_by_reason"]["missing_on_disk"] == 2
    assert ("rdr__nexus", str(present)) in _sweep_env["deletes"]
    action = next(a for a in report["actions"] if a["source_path"] == str(present))
    assert action["reason"] == "present_no_document"
