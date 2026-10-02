# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 decision D2 on the four note writers (nexus-z0o2p.34): the completion stamp is LAST.

A note used to be written with its completion stamp riding the one ``write_manifest_many`` request,
and the post-store chains (chash index, taxonomy assignment, aspect enqueue) fired after it. A
process killed in a chain left a note that read complete: its next put was a staleness-style skip
for nothing, and nothing would fire the chains for it again. ``put_note`` now writes the pieces and
the owner rows in ONE request with no stamp, the producer fires the chains, and
:func:`nexus.catalog.note_write.stamp_note` sends the stamp.

Each journey drives a real producer against the real engine substrate and kills the process inside
a post-store chain (a BaseException, which neither the hook registry's per-hook containment nor an
``except Exception`` can swallow):

* the note's chunk and its owner row have landed, in the one request that carried no stamp;
* the document is ``indexing`` (the fence ``put_note`` began and nothing completed);
* the rerun fires the chains again, and the stamp is the last request it makes;
* ``nx memory promote --remove`` keeps its T2 source when the kill stops it before the stamp.

The four producers are MCP ``store_put``, ``nx store put``, ``nx memory promote`` and the recovery
bundle import.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from tests._catalog_fixture_ops import active_reader, documents_by_title
from tests.conftest import make_vector_test_client

pytestmark = [pytest.mark.integration]

_SUBJECT = "z0o2p34-note"
_WRITE_MANY = "/manifest/write_many"
_STAMP = "/index-run/complete"


class HookKilled(BaseException):
    """The simulated death of the client process inside a post-store chain."""


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    from nexus.rate_brake import reset_brake

    monkeypatch.setattr("nexus.retry.time.sleep", lambda seconds: None)
    reset_brake()
    yield
    reset_brake()


@pytest.fixture
def vec(t2_service_env):
    import nexus.db.http_vector_client as hvc

    return hvc.HttpVectorClient(tenant=t2_service_env)


class _Run:
    """What one producer run did: the catalog requests in order, with the chain fires among them."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.write_bodies: list[dict] = []
        self.armed = True
        self.fires = 0


@pytest.fixture
def run(monkeypatch) -> Iterator[_Run]:
    """Record the catalog requests (``write``/``stamp``), and make the batch chain the kill point."""
    from nexus.hook_registry import HookRegistry

    state = _Run()
    orig_post = HttpCatalogClient._post
    orig_batch = HookRegistry.fire_batch

    def _post(self, path, body=None, **kw):
        if path == _WRITE_MANY:
            state.events.append("write")
            state.write_bodies.append(body or {})
        elif path == _STAMP:
            state.events.append("stamp")
        return orig_post(self, path, body, **kw)

    def _fire_batch(self, *a, **kw):
        state.events.append("chains")
        state.fires += 1
        if state.armed:
            raise HookKilled("killed in a post-store chain")
        return orig_batch(self, *a, **kw)

    monkeypatch.setattr(HttpCatalogClient, "_post", _post)
    monkeypatch.setattr(HookRegistry, "fire_batch", _fire_batch)
    yield state


def _doc(title: str):
    (doc,) = documents_by_title(title)
    return doc


def _manifest(title: str) -> list[str]:
    return list(active_reader().get_chunk_chashes(str(_doc(title).tumbler)))


def _present(vec, collection: str, chashes: list[str]) -> set[str]:
    return set(vec.existing_ids(collection, chashes))


def _assert_killed_before_the_stamp(run: _Run, title: str, content: str, vec) -> str:
    """The shared half: the kill landed in the chains, after the one stamp-free write and before
    any stamp, with the note whole and owned and the fence still ``indexing``."""
    from nexus.corpus import t3_collection_name

    assert run.events == ["write", "chains"], "non-vacuity: the kill landed in the chain, after the write"
    assert "complete" not in run.write_bodies[0], "the one write request carried no completion stamp"
    assert run.write_bodies[0]["docs"][0]["rows"], "and it carried the note's owner row"
    assert _doc(title).index_state == "indexing", "nothing has stamped the note"
    assert _manifest(title) == [_chash(content)], "the owner row landed"
    collection = t3_collection_name(_SUBJECT, for_write=True)
    assert _present(vec, collection, [_chash(content)]) == {_chash(content)}, "and so did the chunk"
    return collection


def _assert_rerun_completed(run: _Run, title: str, content: str) -> None:
    assert run.events == ["write", "chains", "write", "chains", "stamp"], (
        "the rerun fired the chains again and the stamp is the last request it made")
    assert _doc(title).index_state == "complete"
    assert _manifest(title) == [_chash(content)]


# ── MCP store_put ────────────────────────────────────────────────────────────


def test_a_kill_in_a_chain_after_mcp_store_put_leaves_the_note_indexing_and_the_rerun_fires_the_chains_again(
    t2_service_env, vec, run,
) -> None:
    from nexus.mcp.core import store_put

    title, content = "z0o2p34-mcp", "z0o2p34 mcp store_put body, long enough to be one piece"
    with pytest.raises(HookKilled):
        store_put(content=content, collection=_SUBJECT, title=title)
    _assert_killed_before_the_stamp(run, title, content, vec)

    run.armed = False
    result = store_put(content=content, collection=_SUBJECT, title=title)
    assert result.startswith("Stored: "), result
    _assert_rerun_completed(run, title, content)


# ── nx store put ─────────────────────────────────────────────────────────────


def test_a_kill_in_a_chain_after_nx_store_put_leaves_the_note_indexing_and_the_rerun_fires_the_chains_again(
    t2_service_env, vec, run,
) -> None:
    title, content = "z0o2p34-cli", "z0o2p34 nx store put body, long enough to be one piece"

    def _put():
        return CliRunner().invoke(
            main, ["store", "put", "-", "--title", title, "-c", _SUBJECT], input=content)

    with pytest.raises(HookKilled):
        _put()
    _assert_killed_before_the_stamp(run, title, content, vec)

    run.armed = False
    result = _put()
    assert result.exit_code == 0, result.output
    _assert_rerun_completed(run, title, content)


# ── nx memory promote ────────────────────────────────────────────────────────


class _T2:
    def __init__(self, path: Path) -> None:
        self._path = path

    def open(self) -> T2Database:
        return T2Database(self._path)


def test_a_kill_in_a_chain_after_nx_memory_promote_leaves_the_note_indexing_and_keeps_the_t2_entry(
    t2_service_env, vec, run, tmp_path,
) -> None:
    from nexus.db import make_t3  # noqa: F401 — patched below
    from nexus.mcp_infra import inject_t3

    title, content = "z0o2p34-promote", "z0o2p34 promote body, long enough to be one piece"
    t2 = _T2(tmp_path / "promote-t2.db")
    with t2.open() as db:
        row_id = db.put(project="proj", title=title, content=content, ttl=None)
    t3 = T3Database(_client=make_vector_test_client(), _ef_override=DefaultEmbeddingFunction())
    inject_t3(t3)
    try:
        def _promote():
            with patch("nexus.commands.memory.t2_handle", side_effect=t2.open), \
                 patch("nexus.db.make_t3", return_value=t3):
                return CliRunner().invoke(main, [
                    "memory", "promote", str(row_id), "--collection", _SUBJECT, "--remove"])

        with pytest.raises(HookKilled):
            _promote()
        _assert_killed_before_the_stamp(run, title, content, vec)
        with t2.open() as db:
            assert db.get(project="proj", title=title) is not None, "the kill stopped --remove: the source stays"

        run.armed = False
        result = _promote()
        assert result.exit_code == 0, result.output
        _assert_rerun_completed(run, title, content)
        with t2.open() as db:
            assert db.get(project="proj", title=title) is None, "the completed promote removed its source"
    finally:
        inject_t3(None)


# ── the recovery bundle import ───────────────────────────────────────────────


def test_a_kill_in_a_chain_after_the_recovery_import_leaves_the_note_indexing_and_the_rerun_fires_the_chains_again(
    t2_service_env, vec, run,
) -> None:
    from nexus.catalog.recovery_bundle import _default_import_doc
    from nexus.corpus import t3_collection_name
    from nexus.db import make_t3

    title, content = "z0o2p34-import", "z0o2p34 recovery import body, long enough to be one piece"
    collection = t3_collection_name(_SUBJECT, for_write=True)
    rec = {"record": "knowledge_doc", "source_uri": "", "collection": collection, "title": title,
           "tags": "", "category": "", "content": content}

    with pytest.raises(HookKilled):
        _default_import_doc(make_t3(), rec)
    _assert_killed_before_the_stamp(run, title, content, vec)

    run.armed = False
    _default_import_doc(make_t3(), rec)
    _assert_rerun_completed(run, title, content)
