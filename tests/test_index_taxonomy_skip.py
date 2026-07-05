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
    mock.get.return_value = {"code_collection": "code__myrepo__voyage-code-3__v1"}
    return mock


def _run(runner: CliRunner, repo: Path, stats: dict, *, taxonomy_incomplete: bool = False):
    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value=stats),
        patch("nexus.commands.index._taxonomy_incomplete", return_value=taxonomy_incomplete),
        patch("nexus.commands.index.run_collection_postprocessing") as post,
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])
    return result, post


def test_all_skip_run_skips_postprocessing(runner: CliRunner, index_home: Path) -> None:
    # 0 files changed AND taxonomy already built (topics exist) → skip.
    repo = index_home / "myrepo"
    repo.mkdir()

    result, post = _run(runner, repo, {"files_changed": 0}, taxonomy_incomplete=False)

    assert result.exit_code == 0, result.output
    post.assert_not_called()
    assert "skipping discovery" in result.output


def test_all_skip_but_taxonomy_incomplete_still_runs(runner: CliRunner, index_home: Path) -> None:
    # Self-heal guard: 0 files changed but a collection has no topics yet
    # (discover never succeeded) → discovery must still run.
    repo = index_home / "myrepo"
    repo.mkdir()

    result, post = _run(runner, repo, {"files_changed": 0}, taxonomy_incomplete=True)

    assert result.exit_code == 0, result.output
    post.assert_called_once()
    assert "skipping discovery" not in result.output


def test_changed_run_runs_postprocessing(runner: CliRunner, index_home: Path) -> None:
    repo = index_home / "myrepo"
    repo.mkdir()

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

    result, post = _run(runner, repo, {"files_changed": 2, "rdr_indexed": 2}, taxonomy_incomplete=False)

    assert result.exit_code == 0, result.output
    post.assert_called_once()
