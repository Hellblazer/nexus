# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-m20mf P3 (fold-in): a real `nx index repo` CLI invocation actually
uses the shared-client mechanism.

Coordinator directive (2026-09-05): P3 delivers nothing to any command
until taxonomy_cmd and index.py are wired to build one shared client per
command/run and pass it through. This file proves the index half: the
``index`` Click group callback (src/nexus/commands/index.py) builds ONE
``httpx.Client`` via ``build_shared_t2_client()`` per process invocation,
stashes it on ``ctx.obj``, and both of index.py's ``T2Database`` call
sites (``_collections_without_topics`` and ``run_collection_postprocessing``,
both inside ``index_repo_cmd``'s taxonomy-discovery step) pick it up.

Mirrors ``tests/test_index_cmd.py``'s established convention for testing
``nx index repo`` without the heavy MinerU/embedding pipeline: the
registry and ``nexus.indexer.index_repository`` are mocked (exactly as
every other CLI-level index test in this suite does), but the taxonomy
discovery step this file is actually about runs FOR REAL against the
engine substrate -- ``_collections_without_topics`` executes
unconditionally whenever ``index_repository`` reports any ``stats`` at
all (see ``index_repo_cmd``'s call site), regardless of files_changed, so
a real `nx index repo` CLI invocation genuinely reaches both T2Database
construction sites without needing a real repository, real files, or a
real embedder.

One incidental, correctly-excluded httpx.Client: ``run_collection_postprocessing``
calls ``make_t3()`` (T3's ``HttpVectorClient``, an entirely different
client class from T2's ``RefreshableHttpStoreMixin``, itself calling a
one-shot ``httpx.get()`` probe internally) -- out of scope for this T2-only
mechanism. This file counts total httpx.Client constructions and
separately proves the T2-specific count via ``build_shared_t2_client``'s
own call count, so that unrelated T3 probe cannot mask a T2 regression.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.storage_mode import T2_FACADE_STORES

pytestmark = pytest.mark.usefixtures("cloud_mode")


def _instrument_httpx_clients(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    tally: list[int] = []
    orig_init = httpx.Client.__init__

    def _counting_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        tally.append(1)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _counting_init)
    return tally


def _instrument_build_shared_t2_client(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count calls to the T2-specific shared-client factory directly --
    robust against any OTHER, unrelated httpx.Client built during the
    same command run (e.g. T3's probe inside ``make_t3()``)."""
    import nexus.commands.index as index_mod
    from nexus.db.t2 import _refreshable_client

    tally: list[int] = []
    orig = _refreshable_client.build_shared_t2_client

    def _counting(*args: Any, **kwargs: Any) -> httpx.Client:
        tally.append(1)
        return orig(*args, **kwargs)

    # index.py imports build_shared_t2_client via a deferred, function-local
    # import inside the `index()` group callback itself (`from
    # nexus.db.t2._refreshable_client import build_shared_t2_client`), so
    # patching the SOURCE module's attribute is what that import resolves
    # against at call time -- patching index_mod would miss it.
    monkeypatch.setattr(_refreshable_client, "build_shared_t2_client", _counting)
    del index_mod  # imported only to document the deferred-import relationship above
    return tally


def _instrument_t2database(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, int]]:
    """Snapshot each T2Database construction's domain-store client ids
    IMMEDIATELY (the command closes its own T2Database instances inside
    ``with`` blocks before this test can inspect them post-hoc). Store
    names come from ``nexus.db.storage_mode.T2_FACADE_STORES``, not
    hand-typed here -- a hand-typed list is exactly how ``tuples``
    (HttpTupleStore, RDR-205) went missing from this fan-out check for as
    long as it did."""
    from nexus.db.t2 import T2Database

    store_names = T2_FACADE_STORES
    snapshots: list[dict[str, int]] = []
    orig_init = T2Database.__init__

    def _capturing_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        snapshots.append({name: id(getattr(self, name)._client) for name in store_names})

    monkeypatch.setattr(T2Database, "__init__", _capturing_init)
    return snapshots


def test_real_index_repo_command_shares_one_t2_httpx_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ``nx index repo`` invocation (the actual ``index`` Click
    group + ``repo`` subcommand, not a bare function call), with only the
    expensive indexer internals mocked (the established convention in
    tests/test_index_cmd.py): BEFORE this wiring, index.py's two
    T2Database call sites (``_collections_without_topics`` +
    ``run_collection_postprocessing``) would each have built one
    httpx.Client() per domain store (independently, so the two sites'
    counts would have added up); AFTER, the index group's ONE shared
    client backs both. Table: T2Database constructions -> 2, T2
    shared-client builds -> 1 (not one-per-domain-store per site)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo"}

    shared_client_tally = _instrument_build_shared_t2_client(monkeypatch)
    t2_snapshots = _instrument_t2database(monkeypatch)
    client_tally = _instrument_httpx_clients(monkeypatch)

    with patch("nexus.commands.index._registry", return_value=reg):
        with patch("nexus.indexer.index_repository", return_value={"files_changed": 0}):
            runner = CliRunner()
            result = runner.invoke(main, ["index", "repo", str(repo_dir)])

    assert result.exit_code == 0, result.output

    # _collections_without_topics always constructs one; run_collection_
    # postprocessing constructs a second ONLY when the substrate collection has
    # no topics yet, which depends on what earlier tests in the session did.
    # Pin the range and the sharing invariant, not a state-dependent exact
    # count (it read 1 on a warm substrate and 2 on a cold one).
    assert 1 <= len(t2_snapshots) <= 2, (
        f"expected 1 or 2 T2Database constructions for `nx index repo` "
        f"(_collections_without_topics, plus run_collection_postprocessing "
        f"when the collection still needs topics); got {len(t2_snapshots)}"
    )
    assert len(shared_client_tally) == 1, (
        f"expected build_shared_t2_client() to be called exactly ONCE per "
        f"`nx index repo` invocation (in the `index` group callback); got "
        f"{len(shared_client_tally)} calls"
    )

    store_count = len(t2_snapshots[0]) if t2_snapshots else 0
    all_client_ids = set().union(*(set(snap.values()) for snap in t2_snapshots))
    assert len(all_client_ids) == 1, (
        f"expected BOTH T2Database constructions' {store_count} domain "
        f"stores each to share the SAME single client across the whole "
        f"`nx index repo` run; found {len(all_client_ids)} distinct "
        f"client objects: {t2_snapshots}"
    )

    # Total httpx.Client count includes AT MOST one unrelated T3 probe
    # (make_t3() -> HttpVectorClient -> a one-shot httpx.get() managed-
    # service probe) alongside the ONE T2 shared client -- 1 or 2 total
    # (never 0, never >2), not the 2 * store_count two independent
    # T2Database facades would have built before this wiring. Exactly 2
    # vs 1 depends on
    # whether an EARLIER test in the same pytest session already warmed
    # get_http_vector_client()'s process-lifetime cache (nexus-m20mf P3
    # fold-in fix: the original ==2 pin was order-dependent and failed
    # when run after tests/test_index_cmd.py in the same session) -- the
    # property this assertion actually needs to prove (T2 collapsed from
    # 16 to 1, T3 unaffected either way) does not depend on which.
    assert 1 <= len(client_tally) <= 2, (
        f"expected 1 or 2 total httpx.Client constructions (1 T2 shared "
        f"client, +1 more only if T3's process-lifetime probe cache was "
        f"cold going into this test); got {len(client_tally)}"
    )


def test_default_index_t2database_construction_outside_a_command_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_collections_without_topics`` called OUTSIDE a live ``index``
    Click context (e.g. a helper imported and called directly) must still
    build one independent httpx.Client() per domain store -- the
    additive contract."""
    from nexus.commands.index import _collections_without_topics

    monkeypatch.setattr(
        "nexus.commands._helpers.default_db_path", lambda: tmp_path / "memory.db"
    )
    client_tally = _instrument_httpx_clients(monkeypatch)
    expected = len(T2_FACADE_STORES)

    result = _collections_without_topics(["nonexistent-collection"])

    # Real round trip against the engine substrate: an unknown collection
    # genuinely has zero topics, so it lands in the "without topics" set
    # via the normal (not fail-safe/exception) path.
    assert result == {"nonexistent-collection"}
    assert len(client_tally) == expected, (
        f"expected _collections_without_topics() called with no active "
        f"Click context to build {expected} independent httpx.Client()s, "
        f"one per domain store (default, unwired behavior); got "
        f"{len(client_tally)}"
    )
