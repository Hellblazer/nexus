# SPDX-License-Identifier: AGPL-3.0-or-later
"""``compact_checkpoint.py``: the running-work section (RDR-253 "Background work").

Split from ``test_compact_checkpoint.py`` (file-size ceiling, nexus-sby49.4), whose ``mod`` and ``env`` fixtures and
helpers it imports.
"""
from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.hooks.test_compact_checkpoint import (  # noqa: F401 (env and mod are fixtures)
    SID,
    env,
    mod,
    nx_calls,
    pre_payload,
    run,
)


class TestRunningWork:
    """RDR-253 "Background work": the checkpoint lists the session's own identity's running work rows, so they survive a
    ``/clear`` as text. ``nx identity work list --running --json`` with no argument is the caller's own identity; the hook
    reads no token. Any failure leaves the section out and never delays or vetoes the checkpoint."""

    ASK = "identity work list --running --json"

    def _rows(self, monkeypatch, tmp_path: Path, mod, body: str) -> None:
        """Replace the stand-in for the work read only: it answers *body* (python source for the child) to the work verb."""
        real = mod._nx_command()
        shim = tmp_path / "work_shim.py"
        shim.write_text(textwrap.dedent(f"""
            import subprocess, sys
            if sys.argv[1:4] == ["identity", "work", "list"]:
                {body}
            sys.exit(subprocess.call([sys.executable, {real[1]!r}, *sys.argv[1:]]))
            """), encoding="utf-8")
        monkeypatch.setattr(mod, "_nx_command", lambda: [sys.executable, str(shim)])

    def test_running_rows_are_listed_after_the_board_and_the_checks(self, mod, env, monkeypatch, tmp_path) -> None:
        live_wt = tmp_path / "wt-live"
        live_wt.mkdir()
        gone_wt = tmp_path / "wt-gone"  # an agent's isolation worktree the harness removed: never listed
        self._rows(monkeypatch, tmp_path, mod,
                   f"import json; print(json.dumps({{'identity': 'i1', 'alive': True, 'work': [{{'kind': 'worktree', 'ref': {str(live_wt)!r}, "
                   f"'state': 'running', 'stale': False}}, {{'kind': 'worktree', 'ref': {str(gone_wt)!r}, 'state': 'running', 'stale': False}}, "
                   "{'kind': 'labrun', 'ref': 'full', 'state': 'running', 'stale': False}]})); "
                   "sys.exit(0)")
        code, out = run(mod, monkeypatch, "pre", pre_payload())
        assert code == 0 and out == ""
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert "## Background work this identity still has running" in text
        assert f"- worktree {live_wt}" in text and "- labrun full" in text
        assert str(gone_wt) not in text
        assert text.index("## Threads board entry") < text.index("## Background work") < text.index("## Working state")

    def test_no_rows_adds_no_section(self, mod, env, monkeypatch) -> None:
        run(mod, monkeypatch, "pre", pre_payload())                      # the stand-in answers the work verb with nothing
        assert "Background work" not in (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert self.ASK in nx_calls(env, work=True), "it did ask, for the caller's own identity (no argument)"

    @pytest.mark.parametrize("body", [
        "sys.stderr.write('no identity'); sys.exit(1)",
        "print('not json'); sys.exit(0)",
        "print('{\"work\": 7}'); sys.exit(0)",
        "print('{}'); sys.exit(0)",
        "import time; time.sleep(30)",
    ], ids=["exit-1", "not-json", "wrong-shape", "no-work-key", "hang"])
    def test_any_failure_leaves_the_section_out_and_the_checkpoint_written(self, mod, env, monkeypatch, tmp_path, body) -> None:
        monkeypatch.setattr(mod, "_WORK_TIMEOUT_S", 0.8)
        self._rows(monkeypatch, tmp_path, mod, body)
        started = time.monotonic()
        code, out = run(mod, monkeypatch, "pre", pre_payload())
        assert code == 0 and out == "" and time.monotonic() - started < 20
        text = (env.checkpoints / f"{SID}.md").read_text(encoding="utf-8")
        assert "Background work" not in text and "## Working state" in text

    def test_a_missing_nx_leaves_it_out(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_nx_command", lambda: None)
        assert mod._running_work_section(None) == []

    def test_the_read_is_bounded_to_a_few_seconds(self, mod) -> None:
        assert mod._WORK_TIMEOUT_S <= 3.0
