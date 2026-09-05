"""release-sandbox.sh's live `tail -f` (nexus-s71lr) must die with the
script: every EXIT trap and the INT/TERM traps call ``_kill_live_tail``.
The pass-2 review found the tail was only stopped when the indexed
command returned, so a Ctrl-C on a stall orphaned a ``tail -f`` holding
the log open."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "release-sandbox.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


def _code_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def test_every_exit_trap_kills_the_live_tail(script_text: str) -> None:
    exit_traps = [ln for ln in _code_lines(script_text) if re.search(r"\btrap\s+'.*'\s+EXIT\b", ln)]
    assert len(exit_traps) >= 3, exit_traps  # non-vacuity: the script installs several
    offenders = [ln for ln in exit_traps if "_kill_live_tail" not in ln]
    assert not offenders, offenders


def test_int_and_term_traps_kill_the_live_tail(script_text: str) -> None:
    lines = _code_lines(script_text)
    for sig in ("INT", "TERM"):
        matches = [ln for ln in lines if re.search(rf"\btrap\s+'[^']*_kill_live_tail[^']*'\s+{sig}\b", ln)]
        assert matches, f"no {sig} trap calling _kill_live_tail"


def test_start_records_pid_and_stop_clears_it(script_text: str) -> None:
    start = script_text[script_text.index("_start_live_log_tail() {"):]
    start = start[: start.index("}") + 1]
    assert "_LIVE_TAIL_PID=$!" in start
    stop = script_text[script_text.index("_stop_live_log_tail() {"):]
    stop = stop[: stop.index("}") + 1]
    assert "_LIVE_TAIL_PID=\"\"" in stop


def test_start_is_never_called_through_command_substitution(script_text: str) -> None:
    # A backgrounded tail inherits a $(...) substitution's stdout pipe, so
    # the substitution never sees EOF and the caller hangs (pass-2 critique).
    assert "$(_start_live_log_tail" not in script_text


def test_start_helper_returns_promptly_with_a_live_tail(tmp_path: Path) -> None:
    import subprocess
    log = tmp_path / "step.log"
    probe = tmp_path / "probe.sh"
    src = SCRIPT.read_text()
    fn = src[src.index("_start_live_log_tail() {"):]
    fn = fn[: fn.index("\n}\n") + 3]
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n_LIVE_TAIL_PID=\"\"\n"
        + fn
        + f'\n_start_live_log_tail "{log}"\nkill "$_LIVE_TAIL_PID"\necho done\n'
    )
    res = subprocess.run(["bash", str(probe)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stderr
    assert "done" in res.stdout
