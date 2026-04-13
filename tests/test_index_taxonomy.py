# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for auto-taxonomy discover after nx index repo (RDR-070, nexus-0bg)."""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

from click.testing import CliRunner

from nexus.commands.index import index, _discover_taxonomy


def test_index_repo_triggers_taxonomy_discover(tmp_path) -> None:
    """index_repo_cmd calls _discover_taxonomy after indexing."""
    calls: list[str] = []

    def fake_discover(collection_name, taxonomy, chroma_client, *, force=False):
        calls.append(collection_name)
        return 2

    with ExitStack() as stack:
        stack.enter_context(patch("nexus.commands.index._discover_taxonomy", side_effect=fake_discover))
        stack.enter_context(patch("nexus.indexer.index_repository", return_value={"code_indexed": 5}))
        stack.enter_context(patch("nexus.commands.index.tqdm", side_effect=lambda **kw: None))
        stack.enter_context(patch("nexus.db.make_t3"))
        stack.enter_context(patch("nexus.db.t2.T2Database"))
        # Disable auto-label to prevent taxonomy_cmd import from poisoning T2Database
        stack.enter_context(patch("nexus.config.load_config", return_value={"taxonomy": {"auto_label": False, "local_exclude_collections": []}}))
        mock_reg = stack.enter_context(patch("nexus.commands.index._registry"))
        mock_reg.return_value.get.return_value = {"collection": "code__test", "docs_collection": "docs__test"}
        runner = CliRunner()
        result = runner.invoke(index, ["repo", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert len(calls) >= 1


def test_index_repo_no_taxonomy_flag(tmp_path) -> None:
    """--no-taxonomy skips taxonomy discover."""
    calls: list[str] = []

    def fake_discover(collection_name, taxonomy, chroma_client, *, force=False):
        calls.append(collection_name)
        return 0

    with (
        patch("nexus.commands.index._discover_taxonomy", side_effect=fake_discover),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.index._registry") as mock_reg,
    ):
        mock_reg.return_value.get.return_value = {"collection": "code__test"}
        runner = CliRunner()
        result = runner.invoke(index, ["repo", str(tmp_path), "--no-taxonomy"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 0


def test_index_repo_frecency_only_skips_taxonomy(tmp_path) -> None:
    """--frecency-only skips taxonomy discover."""
    calls: list[str] = []

    def fake_discover(collection_name, taxonomy, chroma_client, *, force=False):
        calls.append(collection_name)
        return 0

    with (
        patch("nexus.commands.index._discover_taxonomy", side_effect=fake_discover),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.index._registry") as mock_reg,
    ):
        mock_reg.return_value.get.return_value = {"collection": "code__test"}
        runner = CliRunner()
        result = runner.invoke(index, ["repo", str(tmp_path), "--frecency-only"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 0


def test_index_repo_taxonomy_failure_nonfatal(tmp_path) -> None:
    """Taxonomy discover failure does not fail indexing."""
    def bad_discover(*a, **kw):
        raise RuntimeError("taxonomy broke")

    with ExitStack() as stack:
        stack.enter_context(patch("nexus.commands.index._discover_taxonomy", side_effect=bad_discover))
        stack.enter_context(patch("nexus.indexer.index_repository", return_value={"code_indexed": 1}))
        stack.enter_context(patch("nexus.commands.index.tqdm", side_effect=lambda **kw: None))
        stack.enter_context(patch("nexus.db.make_t3"))
        stack.enter_context(patch("nexus.db.t2.T2Database"))
        stack.enter_context(patch("nexus.config.load_config", return_value={"taxonomy": {"auto_label": False, "local_exclude_collections": []}}))
        mock_reg = stack.enter_context(patch("nexus.commands.index._registry"))
        mock_reg.return_value.get.return_value = {"collection": "code__test"}
        runner = CliRunner()
        result = runner.invoke(index, ["repo", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Done" in result.output
