# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-m20mf P3 fold-in (critic finding 2): ``nx collection reindex`` is
a DIFFERENT Click group from ``index`` (which already builds its own
shared client per ``nx index repo`` invocation) -- ``run_collection_
postprocessing`` is called from BOTH, and before this fix silently got NO
shared client on the ``collection`` path (the ambient Click-context lookup
that used to live inside ``run_collection_postprocessing`` naturally found
nothing, since a different group was active). This is the concrete
regression case the critic named; this file proves the fix directly by
running a real ``nx collection reindex`` invocation end to end.

House pattern reused from ``tests/test_collection_reindex_e2e.py``: real
``nx`` CLI verbs in-process via ``CliRunner`` against the session's real
PG-backed engine substrate, real indexing path (server-side bge-768
embeddings under ``NX_LOCAL=1`` -- no API keys).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.storage_mode import T2_FACADE_STORES
from tests._catalog_fixture_ops import documents_by_file_path


def _instrument_httpx_clients(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    tally: list[int] = []
    orig_init = httpx.Client.__init__

    def _counting_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        tally.append(1)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _counting_init)
    return tally


def _instrument_t2database(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, int]]:
    """Snapshot every domain store's ``id(_client)``, one snapshot per
    ``T2Database`` construction. Store names come from
    ``nexus.db.storage_mode.T2_FACADE_STORES`` -- the same set
    ``T2Database.__init__`` validates at construction (and
    ``tests/db/test_storage_mode.py`` pins as exactly what ``__init__``
    constructs) -- rather than hand-typed here: a hand-typed list is
    exactly how ``tuples`` (HttpTupleStore, RDR-205) went missing from
    this fan-out check for as long as it did: the store existed, the
    shared-client wiring covered it, and this instrumentation simply
    never looked."""
    from nexus.db.t2 import T2Database

    store_names = T2_FACADE_STORES
    snapshots: list[dict[str, int]] = []
    orig_init = T2Database.__init__

    def _capturing_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        snapshots.append({name: id(getattr(self, name)._client) for name in store_names})

    monkeypatch.setattr(T2Database, "__init__", _capturing_init)
    return snapshots


@pytest.mark.scenario
def test_real_collection_reindex_shares_one_t2_httpx_client(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ``nx collection reindex`` invocation must build and share
    ONE T2 client across ``run_collection_postprocessing``'s T2Database,
    exactly like ``nx index repo`` already does -- not silently fall back
    to N unshared clients (one per domain store) because a different
    Click group is active.

    CAN FAIL: reverting collection.py's local ``build_shared_t2_client()``
    + ``client=`` wiring (or reintroducing the ambient Click-context
    lookup inside ``run_collection_postprocessing`` itself) makes the
    httpx.Client count regress to one-per-domain-store and/or the
    per-store client identity assertion fail."""
    corpus = "m20mfreindexfanout"

    md = tmp_path / "reindex-fanout.md"
    md.write_text(
        "# Shared Client Reindex Fanout\n\n"
        "Consistent hashing distributes keys across a ring of nodes to "
        "minimize rebalancing when nodes join or leave a cluster.\n"
    )

    runner = CliRunner()
    idx = runner.invoke(main, ["index", "md", str(md), "--corpus", corpus])
    assert idx.exit_code == 0, idx.output

    docs = documents_by_file_path(str(md.resolve()))
    assert len(docs) == 1, f"expected exactly one catalog document, got {docs}"
    collection = docs[0].physical_collection
    assert collection, f"expected a physical_collection on the freshly indexed doc: {docs[0]}"

    client_tally = _instrument_httpx_clients(monkeypatch)
    t2_snapshots = _instrument_t2database(monkeypatch)

    reindex = runner.invoke(main, ["collection", "reindex", collection])
    assert reindex.exit_code == 0, (
        f"reindex must complete (exception={reindex.exception!r}): {reindex.output}"
    )

    assert len(t2_snapshots) == 1, (
        f"expected exactly 1 T2Database construction for `nx collection "
        f"reindex` (run_collection_postprocessing's single T2Database "
        f"context); got {len(t2_snapshots)}"
    )
    store_count = len(t2_snapshots[0])
    stores_clients = set(t2_snapshots[0].values())
    assert len(stores_clients) == 1, (
        f"expected all {store_count} domain stores to share the identical "
        f"shared client under `nx collection reindex`; found "
        f"{len(stores_clients)} distinct client objects: {t2_snapshots[0]}"
    )
    assert len(client_tally) == 2, (
        f"expected exactly 2 httpx.Client() constructions: 1 for the T2 "
        f"shared client, 1 for an unrelated T3 managed-service probe "
        f"inside run_collection_postprocessing's make_t3() call (the "
        f"identical incidental construction the index.py fanout test "
        f"observes for `nx index repo` -- not process-cached across the "
        f"`nx index md` call earlier in this test). BEFORE this fix, this "
        f"count would have been {store_count + 1} ({store_count} unshared "
        f"T2 domain-store clients + the same T3 probe), since "
        f"run_collection_postprocessing had no way to receive a shared "
        f"client from `nx collection reindex`'s Click group. Got "
        f"{len(client_tally)}"
    )
