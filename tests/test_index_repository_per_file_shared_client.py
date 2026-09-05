# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-m20mf P3 fold-in (critic findings 1/1b/C2): the design's own
named target population -- "the indexer's per-file facades" -- is the
per-document hook-failure chain (``hook_registry.py``'s ``fire_document``
-> ``_record_document_hook_failure`` -> ``_persist_hook_failure`` ->
``mcp_infra.t2_ctx()``), not just the once-or-twice-per-run
taxonomy-postprocessing step ``tests/test_index_cmd_shared_client_fanout.py``
covers. This file drives a REAL ``index_repository()`` call over N real
files and forces a hook failure on EVERY one, proving the per-file
facade sharing the earlier fanout test could not reach (that test mocks
``nexus.indexer.index_repository`` entirely, per the established
CLI-test convention).

Uses the in-memory ``T3Database`` fixture shape from
``tests/test_indexer_e2e.py`` (no real embeddings, no network for T3) so
this stays fast; T2 still routes through the real per-session engine
substrate (autouse ``_pin_t2_substrate``), which is what the counts below
actually measure.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry
from tests.conftest import fake_credentials, make_vector_test_client


def _git_init(repo: Path) -> None:
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test"],
        ["git", "config", "user.name", "test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "tiny-repo"
    repo.mkdir()
    for i in range(3):
        (repo / f"module_{i}.py").write_text(
            f"def function_{i}(x):\n    '''Docstring for function {i}.'''\n    return x + {i}\n"
        )
    _git_init(repo)
    return repo


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=make_vector_test_client(), _ef_override=DefaultEmbeddingFunction()
    )


@pytest.fixture
def registry(tmp_path: Path, tiny_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(tiny_repo)
    return reg


def _instrument_httpx_clients(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    tally: list[int] = []
    orig_init = httpx.Client.__init__

    def _counting_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        tally.append(1)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _counting_init)
    return tally


def _instrument_t2database(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from nexus.db.t2 import T2Database

    tally: list[int] = []
    orig_init = T2Database.__init__

    def _counting_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        tally.append(id(self.telemetry._client))

    monkeypatch.setattr(T2Database, "__init__", _counting_init)
    return tally


def test_index_repository_shares_one_t2_client_across_n_per_file_hook_failures(
    tiny_repo: Path,
    registry: RepoRegistry,
    local_t3: T3Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL ``index_repository()`` run over 3 files, with an ALWAYS-
    FAILING document hook registered (forcing exactly one
    ``_record_document_hook_failure`` -> ``t2_ctx()`` call per file):
    BEFORE this wiring, N files -> N independent T2Database constructions
    -> N x 8 unshared httpx.Client()s. AFTER, N files still construct N
    T2Database facades (one per hook-failure record -- that part is
    unaffected; ``t2_ctx()`` is fresh-per-call by design), but they all
    share ONE T2 httpx.Client via ``index_repository``'s new ``client=``
    parameter + ``use_shared_t2_client_for_index_run``.

    CAN FAIL: reverting the ``with use_shared_t2_client_for_index_run
    (client):`` wrap in ``index_repository`` (indexer.py), or ``t2_ctx()``'s
    fallback to ``current_index_run_t2_client()``, makes the shared-client
    identity assertion fail and the httpx.Client count regress toward
    N x 8.
    """
    from nexus.db.t2._refreshable_client import build_shared_t2_client
    from nexus.hook_registry import HookRegistry, install_default_hooks
    from nexus.indexer import index_repository

    failure_count = {"n": 0}

    def _always_failing_hook(source_path: str, collection: str, content: str, *, doc_id: str = "") -> None:
        failure_count["n"] += 1
        raise RuntimeError("nexus-m20mf-p3-forced-hook-failure")

    hooks = HookRegistry()
    install_default_hooks(hooks)
    hooks.register_document(_always_failing_hook)

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "local")

    shared = build_shared_t2_client()
    client_tally = _instrument_httpx_clients(monkeypatch)
    t2_tally = _instrument_t2database(monkeypatch)

    with (
        patch("nexus.db.make_t3", return_value=local_t3),
        patch("nexus.config.get_credential", side_effect=fake_credentials()),
    ):
        index_repository(tiny_repo, registry, hooks=hooks, client=shared)

    assert failure_count["n"] == 3, (
        f"expected the always-failing document hook to fire once per "
        f"file (3 files in tiny_repo); fired {failure_count['n']} times "
        f"-- the harness itself is broken if this isn't 3, not the "
        f"property under test"
    )
    assert t2_tally and len(t2_tally) >= 3, (
        f"expected at least 3 T2Database constructions (one per per-file "
        f"hook-failure record via t2_ctx()); got {len(t2_tally)}"
    )
    assert set(t2_tally) == {id(shared)}, (
        f"expected EVERY per-file hook-failure T2Database's telemetry "
        f"store to share the identical injected client; found "
        f"{len(set(t2_tally))} distinct client id(s) across "
        f"{len(t2_tally)} T2Database constructions"
    )
    # Exactly 1, not 0: nexus.catalog.factory's process-lifetime shared
    # HttpCatalogClient singleton (a DIFFERENT tier entirely, also built on
    # RefreshableHttpStoreMixin but never one of T2Database's 8 domain
    # stores) lazily constructs on its first use during this run
    # (_migrate_legacy_collections) -- traced via a full stack capture
    # during authoring, not assumed. It is a per-PROCESS singleton, so
    # this count does not grow with N files or N hook failures either;
    # the property under test (per-file T2 sharing) is fully proven by
    # the identity assertion below, not by this incidental total.
    assert len(client_tally) == 1, (
        f"expected exactly 1 additional httpx.Client() construction -- "
        f"the catalog tier's own process-lifetime singleton client, "
        f"unrelated to T2Database and not affected by this fix -- and "
        f"ZERO from the {failure_count['n']} per-file T2 hook-failure "
        f"records (build_shared_t2_client() ran BEFORE instrumentation "
        f"started, so it is correctly excluded from this count). Got "
        f"{len(client_tally)}; more than 1 means some per-file T2Database "
        f"still built its own client instead of reusing the shared one"
    )

    assert not shared.is_closed
    shared.close()
