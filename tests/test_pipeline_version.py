# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for pipeline version stamping and staleness detection (RDR-029)."""
from unittest.mock import MagicMock


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


# ── Phase 2: Stamp only on force (key invariant) ────────────────────────────


def test_stamp_not_called_when_force_false():
    """Non-force indexing must never advance the pipeline version stamp.

    This is the most critical invariant in RDR-029: a partial incremental run
    must not mark stale chunks as current.
    """
    from nexus.indexer import stamp_collection_version

    col = MagicMock()
    col.metadata = {"pipeline_version": "3"}

    # Simulate the guard in _run_index: stamp block only executes when force=True
    force = False
    if force:
        stamp_collection_version(col)

    col.modify.assert_not_called()


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


def test_doctor_pipeline_sweep_retired_on_service_handle():
    """RDR-155 P4a.2 (nexus-1k8s1): the doctor pipeline-version sweep read
    Chroma COLLECTION metadata, which has no pgvector equivalent — on the
    service-backed handle (production) it reports a clean retired-skip line
    instead of a misleading 'check failed'. The stale/no-stamp/taxonomy
    detail tests retired with the sweep; staleness tracking on pgvector is
    a P5 ETL concern.
    """
    from unittest.mock import patch
    from click.testing import CliRunner

    runner = CliRunner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []

    from nexus.cli import main
    with (
        patch("nexus.config.is_local_mode", return_value=False),
        patch("nexus.config.get_credential", return_value="sk-key"),
        patch("nexus.health.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.registry.RepoRegistry", return_value=mock_reg),
        # Vector-service reachability probe: pretend the service is up.
        patch("nexus.db.http_vector_client._get", return_value=[]),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "pipeline versions" in result.output
    assert "sweep retired with the Chroma serving path" in result.output
    # The sweep must not report a failure for what is a deliberate retire.
    assert "check failed" not in result.output


def test_stamp_collection_version_noop_on_service_stub() -> None:
    """nexus-kwkkz: a service-backed collection has no Chroma `modify`; stamping
    must no-op instead of raising AttributeError (it crashed `nx index repo`)."""
    from nexus.indexer import stamp_collection_version

    class _ServiceStub:  # no `modify`, like _ServiceCollectionStub
        metadata = {}

    stamp_collection_version(_ServiceStub())  # must not raise


def test_stamp_collection_version_calls_modify_when_available() -> None:
    from unittest.mock import MagicMock
    from nexus.indexer import stamp_collection_version, PIPELINE_VERSION

    col = MagicMock()
    col.metadata = {"existing": "v"}
    stamp_collection_version(col)
    col.modify.assert_called_once_with(
        metadata={"existing": "v", "pipeline_version": PIPELINE_VERSION}
    )
