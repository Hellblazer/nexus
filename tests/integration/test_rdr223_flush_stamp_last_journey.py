# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 decision D2 on the ChunkBatcher flush of ``nx index repo`` (nexus-z0o2p.34).

``_batch_flush`` writes every file of a flush in ONE combined ``write_manifest_many`` (chunks and owner
rows together). The completion stamp used to ride that request, and the flush-grain hooks (taxonomy
assignment, aspect enqueue) and the per-file hooks fired after it. A process killed in a hook left
documents that read complete whose hooks nothing would fire again, and the next run skipped them as
fresh. The write now carries no stamp; ``ChunkBatcher`` calls ``on_batch_stamp`` after the flush-grain
and per-file hooks of the flush have fired, and the indexer sends ONE stamp-only ``append_many`` for
the flush's documents (an empty row list per document, ``complete`` per document).

The journey runs ``_run_index`` against the real engine on a repo of three small files (two prose
files in one ``docs__`` flush, one code file) and kills the process in the flush-grain hook:

* the documents of the flush hold their chunks and owner rows, written by a request with no stamp;
* every one of them is ``indexing``;
* the rerun (no force) redoes them, fires the hooks again and ends with one stamp request for the
  flush, after the hooks.
"""
from __future__ import annotations

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.hook_registry import HookRegistry
from tests.integration.test_rdr223_index_repo_oversize_journey import (
    _docs_by_file,
    _index_state,
    _traffic,
    flush_repo,  # noqa: F401 — the fixture
)

pytestmark = [pytest.mark.integration]


class HookKilled(BaseException):
    """The simulated death of the client process inside a flush-grain hook."""


def test_a_kill_in_a_flush_hook_leaves_the_flushs_documents_indexing_and_the_rerun_stamps_them_last(
    flush_repo, monkeypatch,
) -> None:
    from nexus.indexer import _run_index

    repo, reg = flush_repo
    _run_index(repo, reg, force=False)                # registers the repo's owner
    with _traffic() as base:
        _run_index(repo, reg, force=True)
    written = {b["collection"] for p, b, _ in base if p == "/manifest/write_many"}
    (docs_col,) = [c for c in written if c.startswith("docs__")]
    docs = _docs_by_file(docs_col)
    assert set(docs) == {"a.md", "b.md"}, docs
    wanted = {name: chashes for name, (_doc, chashes) in docs.items()}
    assert all(wanted.values())

    # Make the next run redo both files: a changed file hashes differently, so the run is not a skip.
    (repo / "a.md").write_text((repo / "a.md").read_text() + "\nAppended after the first run.\n")
    (repo / "b.md").write_text((repo / "b.md").read_text() + "\nAppended after the first run too.\n")

    state = {"armed": True, "fires": 0}
    real_fire = HookRegistry.fire_batch

    def _fire_batch(self, *a, grain="file", **kw):
        if grain == "flush":
            state["fires"] += 1
            if state["armed"]:
                raise HookKilled("killed in a flush-grain hook")
        return real_fire(self, *a, grain=grain, **kw)

    monkeypatch.setattr(HookRegistry, "fire_batch", _fire_batch)
    with _traffic() as killed, pytest.raises(HookKilled):
        _run_index(repo, reg, force=False)

    assert state["fires"] >= 1, "non-vacuity: the kill landed in a flush-grain hook"
    data = [(p, b) for p, b, _ in killed if p == "/manifest/write_many" and b["collection"] == docs_col]
    assert len(data) == 1 and len(data[0][1]["docs"]) == 2, "the two files went out in ONE write"
    assert not data[0][1].get("complete"), "and that write carried no completion stamp"
    now = _docs_by_file(docs_col)
    for name in ("a.md", "b.md"):
        doc, chashes = now[name]
        assert chashes and chashes != wanted[name], f"{name}: the new version landed with its owner rows"
        assert _index_state(doc) == "indexing", f"{name}: the stamp had not been sent"

    state["armed"] = False
    with _traffic() as rerun:
        _run_index(repo, reg, force=False)
    assert state["fires"] >= 2, "the rerun fired the flush-grain hooks again"
    stamps = [(p, b) for p, b, _ in rerun if p == "/manifest/append_many"]
    assert stamps, "the rerun sent its stamps"
    stamped = {e["doc_id"] for _p, b in stamps for e in b["docs"] if "complete" in e}
    for name in ("a.md", "b.md"):
        doc, _ = _docs_by_file(docs_col)[name]
        assert _index_state(doc) == "complete"
        assert doc in stamped, f"{name} was stamped by a stamp-only request"
    for _p, b in stamps:
        for e in b["docs"]:
            if "complete" in e:
                assert e["rows"] == [], "a stamp request carries no rows"


def test_a_stamp_the_flush_cannot_send_leaves_the_documents_indexing_and_is_recorded_not_fatal(
    flush_repo, monkeypatch,
) -> None:
    """The chunks and owner rows landed, so a stamp that fails (the engine is down for that one
    request) must not fail the flush or the run: the documents stay ``indexing`` and are named in the
    run summary's refusal collector, and the next run redoes them."""
    import httpx

    from nexus import mcp_infra
    from nexus.indexer import _run_index

    repo, reg = flush_repo
    _run_index(repo, reg, force=False)                # registers the repo's owner
    with _traffic() as base:
        _run_index(repo, reg, force=True)
    written = {b["collection"] for p, b, _ in base if p == "/manifest/write_many"}
    (docs_col,) = [c for c in written if c.startswith("docs__")]
    (repo / "a.md").write_text((repo / "a.md").read_text() + "\nChanged so the next run redoes it.\n")
    (repo / "b.md").write_text((repo / "b.md").read_text() + "\nChanged too.\n")

    real_append_many = HttpCatalogClient.append_manifest_many

    def _append_many(self, docs, *a, complete=None, **kw):
        if complete:
            raise httpx.ConnectError("the engine dropped the stamp request")
        return real_append_many(self, docs, *a, complete=complete, **kw)

    monkeypatch.setattr(HttpCatalogClient, "append_manifest_many", _append_many)
    monkeypatch.setattr("nexus.retry.time.sleep", lambda seconds: None)
    mcp_infra.reset_complete_refusals()

    _run_index(repo, reg, force=False)                # does not raise

    now = _docs_by_file(docs_col)
    for name in ("a.md", "b.md"):
        doc, chashes = now[name]
        assert chashes, f"{name}: its chunks and owner rows landed"
        assert _index_state(doc) == "indexing", f"{name}: nothing stamped it"
    assert set(mcp_infra.get_complete_refusals()) >= {now["a.md"][0], now["b.md"][0]}

    monkeypatch.setattr(HttpCatalogClient, "append_manifest_many", real_append_many)
    mcp_infra.reset_complete_refusals()
    _run_index(repo, reg, force=False)
    for name in ("a.md", "b.md"):
        assert _index_state(_docs_by_file(docs_col)[name][0]) == "complete"
    assert mcp_infra.get_complete_refusals() == []
