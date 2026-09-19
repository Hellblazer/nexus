# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported Stop verification hook (RDR-215 bead nexus-q02nx.13).

``test_stop_verification_hook.py`` owns the decision surface and drives both
implementations. This file owns the two things that have no bash counterpart
to differential against:

* the contract that NO path can emit deny or block -- asserted over the
  whole decision space rather than spot-checked, since "warns only" is the
  script's own stated guarantee and the close gate is what enforces;
* the catalog sync's move to a daemon thread, which is the bead's one
  deliberate behaviour change and the only part of the port that can fail
  in a way the script could not.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest
import structlog
import structlog.testing

from nexus._hook_runtime._io import configure_hook_logging
from nexus.hooks import stop_verification as hook


def _fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    """A one-file executable on PATH, so the hook's shutil.which finds it."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(0o755)
    return bin_dir


def _catalog_repo(tmp_path: Path, *, dirty: bool = True) -> Path:
    """A git-backed catalog with a documents.jsonl, optionally dirty."""
    root = tmp_path / "catalog"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@t.com"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "T"],
        capture_output=True, check=True,
    )
    (root / "documents.jsonl").write_text('{"a": 1}\n')
    subprocess.run(["git", "-C", str(root), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "init"], capture_output=True, check=True
    )
    if dirty:
        (root / "documents.jsonl").write_text('{"a": 1}\n{"b": 2}\n')
    return root


class TestItCanOnlyEverApprove:
    """The contract is "warns only", so this is checked across the decision
    space rather than on one happy path. A deny or block from here would be
    a silent expansion of what a Stop hook can do to a session."""

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"session_id": "s1"},
            {"session_id": ""},
            {"session_id": "../../escape"},
            {"session_id": "s1", "stop_hook_active": True},
            "not a dict at all",
            [],
        ],
    )
    def test_every_payload_shape_approves(self, payload, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "nowhere"))
        result = hook.run(payload)
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
        assert result.exit_code == 0

    def test_the_only_key_the_envelope_can_carry_is_reason(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "nowhere"))
        parsed = json.loads(hook.run({"session_id": "s1"}).stdout)
        assert set(parsed) <= {"decision", "reason"}

    def test_the_bare_envelope_matches_the_scripts_bytes(self):
        assert hook._approve().stdout == '{"decision": "approve"}'

    def test_the_reason_envelope_matches_the_scripts_bytes(self):
        assert (
            hook._approve("WARNING: x\n").stdout
            == '{"decision": "approve", "reason": "WARNING: x\\n"}'
        )

    def test_a_reason_with_quotes_cannot_break_the_envelope(self):
        """The script escaped through json.dumps too, with a printf
        fallback that would NOT have escaped correctly. Here there is no
        fallback path to get wrong."""
        parsed = json.loads(hook._approve('has "quotes" and \\ backslash').stdout)
        assert parsed["reason"] == 'has "quotes" and \\ backslash'


class TestTheCatalogSyncThread:
    """The bead's one deliberate behaviour change.

    It must run to completion off the hook's own path AND record its
    outcome, because the synchronous version it replaces discarded both
    streams and its exit status -- an operator had no way to learn that a
    session-close push had failed.

    **Asserted through structlog's own capture, not by reading
    ``hook.log``.** A file assertion was written first and did not work:
    ``tests/conftest.py`` configures structlog for the whole session with
    the default ``PrintLoggerFactory``, so a structlog event never reaches
    the stdlib handler that owns that file, and the emitted line lands on
    stderr instead. Measured, after two wrong guesses -- the same call
    writes the file correctly from a script outside the repo, where no
    conftest applies. Capturing at the structlog level tests what this hook
    is responsible for (that the thread emits an outcome, with which event
    name and fields) and leaves where the line is written to
    ``logging_setup``, which owns it.
    """

    def _run_and_capture(self, catalog: str) -> list[dict]:
        """Start the sync thread inside a capture and wait for it.

        The join is INSIDE the capture block deliberately: the emit happens
        on the thread, and leaving the block first would end the capture
        while the thread was still running -- a test that passes or fails
        on scheduling, which is the vacuity shape this epic has already
        paid for once today.
        """
        # Take _emit's LAZY CONFIGURATION path before capturing, not
        # during it. _emit configures logging on its first call in a
        # process (when active_log_file() is None), and that
        # reconfiguration replaces structlog's processor chain -- including
        # the one capture_logs just injected -- so the very first event
        # goes to the freshly-made stderr/file sink and the capture list
        # stays empty. Measured in a bare interpreter, not inferred: the
        # first _emit printed to stderr and captured nothing, the second
        # captured fine. NOT a production defect; the line is emitted
        # either way, and nothing in production is trying to capture it.
        configure_hook_logging()

        # conftest's pytest_configure installs
        # make_filtering_bound_logger(WARNING) for the whole session, and a
        # filtering bound logger drops .info() BEFORE any processor runs --
        # so capture_logs alone sees the failure events and not the success
        # one. Nothing filters this way in production, where the hook log is
        # configured at INFO. Lower it for the duration; conftest's own
        # autouse _restore_structlog_after_test puts the session config back.
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG)
        )
        with structlog.testing.capture_logs() as logs:
            thread = hook._start_catalog_sync(catalog)
            thread.join(timeout=30)
            alive = thread.is_alive()
            daemon = thread.daemon
        assert not alive, "the sync thread did not run to completion"
        assert daemon is True, (
            "the sync thread must be a daemon: a session-close sync must "
            "never hold the interpreter open"
        )
        return logs

    def test_a_successful_sync_is_logged(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "PATH", f"{_fake_bin(tmp_path, 'nx', 'exit 0')}{os.pathsep}{os.environ['PATH']}"
        )
        logs = self._run_and_capture(str(tmp_path / "catalog"))
        assert "stop_verification_catalog_sync_ok" in [e["event"] for e in logs], logs

    def test_a_failed_sync_is_logged_with_its_returncode(self, tmp_path, monkeypatch):
        """The case the synchronous version could not report at all: it
        redirected both streams to /dev/null and swallowed the status with
        `|| true`, so a failed session-close push was indistinguishable
        from a successful one."""
        monkeypatch.setenv(
            "PATH",
            f"{_fake_bin(tmp_path, 'nx', 'echo boom >&2; exit 3')}"
            f"{os.pathsep}{os.environ['PATH']}",
        )
        logs = self._run_and_capture(str(tmp_path / "catalog"))
        failed = [e for e in logs if e["event"] == "stop_verification_catalog_sync_failed"]
        assert failed, logs
        assert failed[0]["returncode"] == 3
        assert "boom" in failed[0]["stderr"]

    def test_a_missing_nx_does_not_raise_into_the_server(self, tmp_path, monkeypatch):
        """A thread that raises is a thread that can take the server with
        it. FileNotFoundError is the realistic input: nx leaving PATH
        between the which() check and the spawn."""
        monkeypatch.setenv("PATH", "/nonexistent")
        logs = self._run_and_capture(str(tmp_path / "catalog"))
        failed = [e for e in logs if e["event"] == "stop_verification_catalog_sync_failed"]
        assert failed, logs
        assert "error" in failed[0]

    def test_the_outcome_names_the_catalog_it_synced(self, tmp_path, monkeypatch):
        """A session with several catalogs configured over time should not
        leave an operator guessing which one a line refers to."""
        monkeypatch.setenv(
            "PATH", f"{_fake_bin(tmp_path, 'nx', 'exit 0')}{os.pathsep}{os.environ['PATH']}"
        )
        catalog = str(tmp_path / "catalog")
        logs = self._run_and_capture(catalog)
        assert any(e.get("catalog") == catalog for e in logs), logs


class TestWhenTheSyncIsAttemptedAtAll:
    """Same three conditions as the script, in the same order. Each is a
    real filesystem state rather than a patched predicate."""

    def test_no_nx_on_path_means_no_sync(self, tmp_path, monkeypatch):
        # Build the repo BEFORE scrubbing PATH -- _catalog_repo shells out
        # to git, which is not on /nonexistent either.
        catalog = _catalog_repo(tmp_path)
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog))
        assert hook._catalog_sync_target() is None

    def test_a_non_git_catalog_means_no_sync(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "PATH", f"{_fake_bin(tmp_path, 'nx', 'exit 0')}{os.pathsep}{os.environ['PATH']}"
        )
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "documents.jsonl").write_text("{}\n")
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(plain))
        assert hook._catalog_sync_target() is None

    def test_a_clean_catalog_means_no_sync(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "PATH", f"{_fake_bin(tmp_path, 'nx', 'exit 0')}{os.pathsep}{os.environ['PATH']}"
        )
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(_catalog_repo(tmp_path, dirty=False)))
        assert hook._catalog_sync_target() is None

    def test_a_dirty_jsonl_is_the_one_case_that_syncs(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "PATH", f"{_fake_bin(tmp_path, 'nx', 'exit 0')}{os.pathsep}{os.environ['PATH']}"
        )
        catalog = _catalog_repo(tmp_path, dirty=True)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog))
        assert hook._catalog_sync_target() == str(catalog)

    def test_a_dirty_non_jsonl_file_does_not_trigger_a_sync(self, tmp_path, monkeypatch):
        """The script greps the porcelain output for '.jsonl' specifically."""
        monkeypatch.setenv(
            "PATH", f"{_fake_bin(tmp_path, 'nx', 'exit 0')}{os.pathsep}{os.environ['PATH']}"
        )
        catalog = _catalog_repo(tmp_path, dirty=False)
        (catalog / "notes.txt").write_text("unrelated\n")
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog))
        assert hook._catalog_sync_target() is None
