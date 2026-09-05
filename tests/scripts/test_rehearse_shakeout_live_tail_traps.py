"""rehearse_shakeout.sh's live `tail -f` (nexus-s71lr pass 3) must die with
the script: every EXIT trap and the INT/TERM traps call ``_kill_live_tail``.
Sibling of ``test_release_sandbox_live_tail_traps.py`` — same shape, a
DIFFERENT script (no pre-existing trap to chain here: confirmed no `trap `
line existed in this file before nexus-s71lr; these are the first ones,
not a replacement)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "rehearse_shakeout.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


def _code_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def test_every_exit_trap_kills_the_live_tail(script_text: str) -> None:
    exit_traps = [ln for ln in _code_lines(script_text) if re.search(r"\btrap\s+'.*'\s+EXIT\b", ln)]
    assert len(exit_traps) >= 1, exit_traps  # non-vacuity: this script installs at least one
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
    # the substitution never sees EOF and the caller hangs (release-sandbox.sh
    # pass-2 critique; same fix applied here from the start).
    assert "$(_start_live_log_tail" not in script_text


def test_start_helper_accepts_multiple_log_files(script_text: str) -> None:
    """Phase D runs two `nx index repo` calls concurrently (IDXA/IDXB) --
    the helper must tail both from one backgrounded process/PID, not force
    two separate trackers."""
    start = script_text[script_text.index("_start_live_log_tail() {"):]
    start = start[: start.index("}") + 1]
    assert re.search(r"tail\s+.*-f\s+\"\$@\"", start), start


def test_phase_c_and_d_index_calls_are_wrapped(script_text: str) -> None:
    """The three `nx index repo` log-redirected calls (IDX1, IDX2, and the
    Phase D concurrent pair IDXA/IDXB) must each capture _LIVE_TAIL_PID into
    their own named variable right after starting the tail, and stop it by
    that name -- proves each call site is individually wrapped, not just
    that the helper exists somewhere in the file."""
    for tail_var in ("IDX1_TAIL_PID", "IDX2_TAIL_PID", "LOAD_TAIL_PID"):
        assert f'{tail_var}="$_LIVE_TAIL_PID"' in script_text, f"no capture for {tail_var}"
        assert f'_stop_live_log_tail "${tail_var}"' in script_text, f"no stop for {tail_var}"


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


def test_start_helper_returns_promptly_with_two_live_tails(tmp_path: Path) -> None:
    """The Phase-D two-file call shape: one backgrounded `tail -f a b`
    process, one PID, killable the same way as the single-file case."""
    import subprocess
    log_a = tmp_path / "a.log"
    log_b = tmp_path / "b.log"
    probe = tmp_path / "probe2.sh"
    src = SCRIPT.read_text()
    fn = src[src.index("_start_live_log_tail() {"):]
    fn = fn[: fn.index("\n}\n") + 3]
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n_LIVE_TAIL_PID=\"\"\n"
        + fn
        + f'\n_start_live_log_tail "{log_a}" "{log_b}"\nkill "$_LIVE_TAIL_PID"\necho done\n'
    )
    res = subprocess.run(["bash", str(probe)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stderr
    assert "done" in res.stdout
    assert log_a.exists() and log_b.exists()
