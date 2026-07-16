# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-qgc4b: `nx index repo` skips the expensive post-index taxonomy passes
on all-skip runs (0 files changed), and still runs them when something changed.

The 2026-07-04 incident: an all-skip re-index spent 655s re-clustering 1,461
unchanged chunks. The gate keys on the ``files_changed`` count that
``index_repository`` now returns.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def index_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _mock_reg() -> MagicMock:
    mock = MagicMock()
    mock.get.return_value = {"code_collection": "code__myrepo__emb__v1"}
    return mock


def _run(runner: CliRunner, repo: Path, stats: dict, *, taxonomy_incomplete: bool = False):
    # nexus-tevzq: the gate now probes per collection via
    # _collections_without_topics (one T2 open serves gate + subset);
    # taxonomy_incomplete=True is modeled as "every collection lacks topics".
    no_topics = {"code__myrepo__emb__v1"} if taxonomy_incomplete else set()
    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value=stats),
        patch("nexus.commands.index._collections_without_topics", return_value=no_topics),
        patch("nexus.commands.index.run_collection_postprocessing") as post,
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])
    return result, post


def test_all_skip_run_skips_postprocessing(runner: CliRunner, index_home: Path) -> None:
    # 0 files changed AND taxonomy already built (topics exist) → skip.
    repo = index_home / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()

    result, post = _run(runner, repo, {"files_changed": 0}, taxonomy_incomplete=False)

    assert result.exit_code == 0, result.output
    post.assert_not_called()
    assert "skipping discovery" in result.output


def test_all_skip_but_taxonomy_incomplete_still_runs(runner: CliRunner, index_home: Path) -> None:
    # Self-heal guard: 0 files changed but a collection has no topics yet
    # (discover never succeeded) → discovery must still run.
    repo = index_home / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()

    result, post = _run(runner, repo, {"files_changed": 0}, taxonomy_incomplete=True)

    assert result.exit_code == 0, result.output
    post.assert_called_once()
    assert "skipping discovery" not in result.output


def test_changed_run_runs_postprocessing(runner: CliRunner, index_home: Path) -> None:
    repo = index_home / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()

    # files_changed > 0 short-circuits — runs even when taxonomy looks complete.
    result, post = _run(runner, repo, {"files_changed": 3}, taxonomy_incomplete=False)

    assert result.exit_code == 0, result.output
    post.assert_called_once()
    assert "skipping discovery" not in result.output


class _FakeTaxonomy:
    def __init__(self, topics_by_col: dict, raise_for: str | None = None):
        self._topics = topics_by_col
        self._raise_for = raise_for

    def get_topics_for_collection(self, col: str):
        if col == self._raise_for:
            raise RuntimeError("taxonomy probe boom")
        return self._topics.get(col, [])


class _FakeDB:
    def __init__(self, taxonomy):
        self.taxonomy = taxonomy

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def _patch_t2(monkeypatch, taxonomy):
    import nexus.db.t2 as t2mod

    monkeypatch.setattr(t2mod, "T2Database", lambda *_a, **_k: _FakeDB(taxonomy))


def test_taxonomy_incomplete_true_when_a_collection_has_no_topics(monkeypatch) -> None:
    from nexus.commands.index import _taxonomy_incomplete

    _patch_t2(monkeypatch, _FakeTaxonomy({"code__a": [{"id": 1}], "docs__b": []}))
    assert _taxonomy_incomplete(["code__a", "docs__b"]) is True


def test_taxonomy_incomplete_false_when_all_have_topics(monkeypatch) -> None:
    from nexus.commands.index import _taxonomy_incomplete

    _patch_t2(monkeypatch, _FakeTaxonomy({"code__a": [{"id": 1}], "docs__b": [{"id": 2}]}))
    assert _taxonomy_incomplete(["code__a", "docs__b"]) is False


def test_taxonomy_incomplete_true_on_probe_error_failsafe(monkeypatch) -> None:
    from nexus.commands.index import _taxonomy_incomplete

    _patch_t2(monkeypatch, _FakeTaxonomy({"code__a": [{"id": 1}]}, raise_for="code__a"))
    # A probe error must err toward running discovery, never toward skipping it.
    assert _taxonomy_incomplete(["code__a"]) is True


def test_taxonomy_incomplete_false_for_empty_collection_list() -> None:
    from nexus.commands.index import _taxonomy_incomplete

    assert _taxonomy_incomplete([]) is False


def test_rdr_only_run_still_runs_postprocessing(runner: CliRunner, index_home: Path) -> None:
    # An RDR-only re-index (no code/prose files, but rdr_indexed > 0) is a
    # content change: files_changed folds in rdr_indexed, so discovery runs.
    repo = index_home / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()

    result, post = _run(runner, repo, {"files_changed": 2, "rdr_indexed": 2}, taxonomy_incomplete=False)

    assert result.exit_code == 0, result.output
    post.assert_called_once()


# ── nexus-tevzq: per-collection zero-change discover gate ─────────────────────
#
# qgc4b's gate is repo-grain: any changed file re-discovers EVERY collection.
# 2026-07-15 evidence: 300.3s re-clustering three collections when only some
# kinds changed. _discover_subset narrows the DISCOVER loop (only) to
# collections whose own kind wrote files, preserving the qgc4b self-heal
# (zero-topic collections always discover).


def test_discover_subset_no_by_kind_keeps_all() -> None:
    # Older stats shape (no files_changed_by_kind) → full discovery, back-compat.
    from nexus.commands.index import _discover_subset

    cols = ["code__a__m__v1", "docs__a__m__v1"]
    assert _discover_subset(cols, None) == cols


def test_discover_subset_drops_unchanged_kind_with_topics(monkeypatch) -> None:
    from nexus.commands.index import _discover_subset

    _patch_t2(monkeypatch, _FakeTaxonomy({"docs__a__m__v1": [{"id": 1}]}))
    cols = ["code__a__m__v1", "docs__a__m__v1"]
    by_kind = {"code": 3, "docs": 0, "rdr": 0}
    assert _discover_subset(cols, by_kind) == ["code__a__m__v1"]


def test_discover_subset_keeps_unchanged_kind_without_topics(monkeypatch) -> None:
    # Self-heal (qgc4b guard, per collection): zero topics → discover anyway.
    from nexus.commands.index import _discover_subset

    _patch_t2(monkeypatch, _FakeTaxonomy({"docs__a__m__v1": []}))
    cols = ["code__a__m__v1", "docs__a__m__v1"]
    by_kind = {"code": 3, "docs": 0, "rdr": 0}
    assert _discover_subset(cols, by_kind) == cols


def test_discover_subset_unknown_kind_kept_failsafe(monkeypatch) -> None:
    # A collection whose prefix isn't in by_kind must err toward discovering.
    from nexus.commands.index import _discover_subset

    _patch_t2(monkeypatch, _FakeTaxonomy({}))
    cols = ["knowledge__x__m__v1"]
    assert _discover_subset(cols, {"code": 0, "docs": 0, "rdr": 0}) == cols


def test_discover_subset_probe_error_keeps_collection(monkeypatch) -> None:
    # Probe failure on an unchanged collection must keep it (fail toward
    # discovery), mirroring _taxonomy_incomplete's fail-safe.
    from nexus.commands.index import _discover_subset

    _patch_t2(
        monkeypatch,
        _FakeTaxonomy({"docs__a__m__v1": [{"id": 1}]}, raise_for="docs__a__m__v1"),
    )
    cols = ["docs__a__m__v1"]
    assert _discover_subset(cols, {"code": 1, "docs": 0, "rdr": 0}) == cols


def test_gate_passes_discover_subset_to_postprocessing(runner: CliRunner, index_home: Path) -> None:
    # End-to-end through the CLI gate: code changed, docs didn't (topics
    # exist) → postprocessing receives the FULL list plus the narrowed
    # discover_collections.
    repo = index_home / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()

    reg = MagicMock()
    reg.get.return_value = {
        "code_collection": "code__myrepo__emb__v1",
        "docs_collection": "docs__myrepo__emb__v1",
    }
    stats = {
        "files_changed": 3,
        "files_changed_by_kind": {"code": 3, "docs": 0, "rdr": 0},
    }
    with (
        patch("nexus.commands.index._registry", return_value=reg),
        patch("nexus.indexer.index_repository", return_value=stats),
        # Pin the collection list: _collections_from_registry_info filters by
        # ambient mode config (local mode excludes code__*) — CI py3.13 runs
        # local-mode and collapsed the list to docs-only (release-PR failure).
        patch(
            "nexus.commands.index._collections_from_registry_info",
            return_value=["code__myrepo__emb__v1", "docs__myrepo__emb__v1"],
        ),
        patch("nexus.commands.index._collections_without_topics", return_value=set()),
        patch("nexus.commands.index.run_collection_postprocessing") as post,
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    post.assert_called_once()
    _args, kwargs = post.call_args
    assert _args[0] == [
        "code__myrepo__emb__v1",
        "docs__myrepo__emb__v1",
    ]
    assert kwargs["discover_collections"] == ["code__myrepo__emb__v1"]


def test_postprocessing_discovers_only_subset(monkeypatch) -> None:
    # run_collection_postprocessing narrows ONLY the discover loop; the full
    # list stays available to the downstream projection/link steps.
    import nexus.db as dbmod
    from nexus.commands import index as index_mod

    monkeypatch.setattr(dbmod, "make_t3", lambda: MagicMock())
    _patch_t2(monkeypatch, _FakeTaxonomy({}))
    discovered: list[str] = []
    monkeypatch.setattr(
        index_mod, "_discover_taxonomy", lambda col, *_a, **_k: discovered.append(col) or 0
    )

    index_mod.run_collection_postprocessing(
        ["code__a__m__v1", "docs__a__m__v1"],
        quiet=True,
        discover_collections=["code__a__m__v1"],
    )
    assert discovered == ["code__a__m__v1"]


def test_discover_subset_precomputed_no_topics_skips_probe(monkeypatch) -> None:
    # Gate path passes the probe result in — _discover_subset must NOT
    # re-open T2 (review Medium-2).
    import nexus.db.t2 as t2mod

    from nexus.commands.index import _discover_subset

    def _boom(*_a, **_k):
        raise AssertionError("T2 opened despite precomputed no_topics")

    monkeypatch.setattr(t2mod, "T2Database", _boom)
    cols = ["code__a__m__v1", "docs__a__m__v1"]
    by_kind = {"code": 3, "docs": 0, "rdr": 0}
    assert _discover_subset(cols, by_kind, no_topics=set()) == ["code__a__m__v1"]
    assert _discover_subset(cols, by_kind, no_topics={"docs__a__m__v1"}) == cols


def test_self_heal_run_narrows_to_zero_topic_collections(runner: CliRunner, index_home: Path) -> None:
    # Critic coverage gap: files_changed==0 + one zero-topic collection →
    # postprocessing runs with discovery narrowed to the self-heal candidate.
    repo = index_home / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()

    reg = MagicMock()
    reg.get.return_value = {
        "code_collection": "code__myrepo__emb__v1",
        "docs_collection": "docs__myrepo__emb__v1",
    }
    stats = {
        "files_changed": 0,
        "files_changed_by_kind": {"code": 0, "docs": 0, "rdr": 0},
    }
    with (
        patch("nexus.commands.index._registry", return_value=reg),
        patch("nexus.indexer.index_repository", return_value=stats),
        # Pinned for the same mode-config reason as the test above.
        patch(
            "nexus.commands.index._collections_from_registry_info",
            return_value=["code__myrepo__emb__v1", "docs__myrepo__emb__v1"],
        ),
        patch(
            "nexus.commands.index._collections_without_topics",
            return_value={"docs__myrepo__emb__v1"},
        ),
        patch("nexus.commands.index.run_collection_postprocessing") as post,
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    post.assert_called_once()
    assert post.call_args.kwargs["discover_collections"] == [
        "docs__myrepo__emb__v1"
    ]


def test_postprocessing_default_discovers_all(monkeypatch) -> None:
    # discover_collections=None (reindex_cmd path) keeps full discovery.
    import nexus.db as dbmod
    from nexus.commands import index as index_mod

    monkeypatch.setattr(dbmod, "make_t3", lambda: MagicMock())
    _patch_t2(monkeypatch, _FakeTaxonomy({}))
    discovered: list[str] = []
    monkeypatch.setattr(
        index_mod, "_discover_taxonomy", lambda col, *_a, **_k: discovered.append(col) or 0
    )

    index_mod.run_collection_postprocessing(
        ["code__a__m__v1", "docs__a__m__v1"], quiet=True
    )
    assert discovered == ["code__a__m__v1", "docs__a__m__v1"]
