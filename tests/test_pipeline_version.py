# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for pipeline version stamping and staleness detection (RDR-029)."""
from unittest.mock import MagicMock

import pytest


# ── Phase 1: Constant + helpers ──────────────────────────────────────────────


def test_pipeline_version_constant():
    from nexus.indexer import PIPELINE_VERSION
    assert PIPELINE_VERSION == "4"
    assert isinstance(PIPELINE_VERSION, str)


def test_stamp_merges_metadata():
    from nexus.indexer import stamp_collection_version, PIPELINE_VERSION

    col = MagicMock()
    col.metadata = {"existing_key": "existing_value"}

    stamp_collection_version(col)

    col.modify.assert_called_once_with(
        metadata={"existing_key": "existing_value", "pipeline_version": PIPELINE_VERSION}
    )


def test_stamp_handles_none_metadata():
    from nexus.indexer import stamp_collection_version, PIPELINE_VERSION

    col = MagicMock()
    col.metadata = None

    stamp_collection_version(col)

    col.modify.assert_called_once_with(
        metadata={"pipeline_version": PIPELINE_VERSION}
    )


def test_get_pipeline_version_returns_value():
    from nexus.indexer import get_collection_pipeline_version

    col = MagicMock()
    col.metadata = {"pipeline_version": "3"}
    assert get_collection_pipeline_version(col) == "3"


def test_get_pipeline_version_returns_none_for_new():
    from nexus.indexer import get_collection_pipeline_version

    col = MagicMock()
    col.metadata = None
    assert get_collection_pipeline_version(col) is None

    col.metadata = {}
    assert get_collection_pipeline_version(col) is None


# ── Phase 3: Staleness detection ─────────────────────────────────────────────


def test_staleness_warning_on_mismatch():
    from nexus.indexer import check_pipeline_staleness

    col = MagicMock()
    col.metadata = {"pipeline_version": "2"}

    assert check_pipeline_staleness(col, "code__test") is True


def test_no_warning_for_new_collection():
    from nexus.indexer import check_pipeline_staleness

    col = MagicMock()
    col.metadata = None

    assert check_pipeline_staleness(col, "code__test") is False


def test_no_warning_for_matching_version():
    from nexus.indexer import check_pipeline_staleness, PIPELINE_VERSION

    col = MagicMock()
    col.metadata = {"pipeline_version": PIPELINE_VERSION}

    assert check_pipeline_staleness(col, "code__test") is False


# ── Phase 4: --force-stale CLI mutual exclusion ─────────────────────────────


def test_force_stale_force_mutual_exclusion():
    from click.testing import CliRunner
    from nexus.commands.index import index

    runner = CliRunner()
    result = runner.invoke(index, ["repo", "/tmp", "--force", "--force-stale"])
    assert result.exit_code != 0


def test_force_stale_frecency_mutual_exclusion():
    from click.testing import CliRunner
    from nexus.commands.index import index

    runner = CliRunner()
    result = runner.invoke(index, ["repo", "/tmp", "--frecency-only", "--force-stale"])
    assert result.exit_code != 0


# ── Phase 5: nx doctor pipeline version check ───────────────────────────────


def test_doctor_reports_stale_collections():
    """nx doctor flags collections with outdated pipeline_version."""
    from unittest.mock import patch
    from click.testing import CliRunner
    from nexus.indexer import PIPELINE_VERSION

    runner = CliRunner()

    # Create a mock collection with old pipeline version
    stale_col = MagicMock()
    stale_col.name = "code__myrepo"
    stale_col.metadata = {"pipeline_version": "2"}

    current_col = MagicMock()
    current_col.name = "docs__myrepo"
    current_col.metadata = {"pipeline_version": PIPELINE_VERSION}

    mock_client = MagicMock()
    mock_client.list_collections.return_value = [stale_col, current_col]

    mock_reg = MagicMock()
    mock_reg.all.return_value = []

    from nexus.cli import main
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=mock_client),
    ):
        result = runner.invoke(main, ["doctor"])

    # Stale collection should be flagged
    assert "v2" in result.output
    assert f"v{PIPELINE_VERSION}" in result.output
    assert "force-stale" in result.output


def test_doctor_handles_no_version_stamp():
    """Collections without pipeline_version are reported but not flagged as errors."""
    from unittest.mock import patch
    from click.testing import CliRunner

    runner = CliRunner()

    col = MagicMock()
    col.name = "code__newrepo"
    col.metadata = {}

    mock_client = MagicMock()
    mock_client.list_collections.return_value = [col]

    mock_reg = MagicMock()
    mock_reg.all.return_value = []

    from nexus.cli import main
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=mock_client),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "no version stamp" in result.output
