# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7), fix round r1: ``nx index repo`` exits non-zero over a failed reconcile commit.

The run's stats carry ``catalog_hook_failed`` (``tests/test_rdr193_failed_commit_run.py`` pins that the run counts it);
the command must turn the count into a failure. The indexer is faked, but a CLI run that reaches it ends in storage
touches (the taxonomy failure marker, the engine's uptime, the T2 client), the same reason the ``--sensitive-glob``
CLI tests mint a tenant, so this one lives in ``tests/integration`` where the python_unit tenant ceiling does not count it.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]


def test_nx_index_repo_exits_non_zero_when_the_catalog_commit_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "myrepo"
    d.mkdir()
    (d / ".git").mkdir()
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo"}
    with patch("nexus.commands.index._registry", return_value=reg), patch(
        "nexus.indexer.index_repository", return_value={"files_changed": 0, "catalog_hook_failed": 1},
    ):
        result = CliRunner().invoke(main, ["index", "repo", str(d)])
    assert result.exit_code != 0, result.output
    assert "catalog" in result.output.lower() and "commit" in result.output.lower(), result.output
