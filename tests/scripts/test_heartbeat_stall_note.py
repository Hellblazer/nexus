# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wo6sc: the package-upgrade gate must name a heartbeat stall when one
is in the window, instead of pointing the reader at supervisor setup.

WHAT THIS EXERCISES. ``rehearse_package_upgrade.sh`` needs a container, a
native binary and a live service, so it cannot be driven end to end from
pytest. Following the ``store_put_census.sh`` precedent (nexus-xm0cp), the
attribution logic lives in
``tests/e2e/migration-rehearsal/lib/heartbeat_stall_note.sh`` and the harness
sources it; every test below sources that SAME file and calls that SAME
function against a fixture supervisor log.
``test_harness_sources_and_calls_the_library`` pins the wiring so the
extraction cannot drift back into an inline copy.

RED and GREEN are both proven, because the failure mode this closes is a
confident message naming the wrong cause -- a note that fires unconditionally
would swap one misattribution for another.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "tests/e2e/migration-rehearsal/lib/heartbeat_stall_note.sh"
HARNESS = REPO_ROOT / "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh"

_MISSED = (
    "2026-09-12 20:35:47,755 nexus.daemon.storage_service_daemon ERROR "
    "event='storage_service_heartbeat_missed_ttl' elapsed_s=31.622 ttl_s=15.0 "
    "phases_s={'poll': 0.0, 'health': 0.001, 'pg': 0.0, 'stamp': 31.621%s}"
)
_HEALTHY = (
    "2026-09-12 20:34:38,008 nexus.daemon.storage_service_daemon INFO "
    "event='storage_service_lease_published' scope='1000' generation=1"
)


def _note(log_path: Path | str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; heartbeat_stall_note "$1"', "_", str(log_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"the note must never fail the gate: {proc.stderr}"
    return proc.stdout


def test_a_stalled_heartbeat_is_named_and_the_wrong_remedy_disowned(tmp_path) -> None:
    """RED input: the real 2026-09-12 shape."""
    log = tmp_path / "storage_service.log"
    log.write_text(_HEALTHY + "\n" + (_MISSED % "") + "\n")

    out = _note(log)
    assert "heartbeat stall" in out.lower()
    assert "NOT supervisor setup" in out
    assert "1 missed-TTL tick" in out
    assert "31.62" in out, "the note quotes the tick it found"


def test_a_clean_log_says_nothing(tmp_path) -> None:
    """GREEN input: a genuine supervisor-setup failure must keep its own
    message. A note that always fires is the same defect wearing new text."""
    log = tmp_path / "storage_service.log"
    log.write_text(_HEALTHY + "\n")
    assert _note(log) == ""


def test_a_missing_log_says_nothing_and_does_not_fail(tmp_path) -> None:
    assert _note(tmp_path / "absent.log") == ""


def test_every_stalled_tick_is_counted(tmp_path) -> None:
    log = tmp_path / "storage_service.log"
    log.write_text("\n".join([_HEALTHY, _MISSED % "", _MISSED % "", _HEALTHY]) + "\n")
    assert "2 missed-TTL tick" in _note(log)


def test_a_subphased_tick_tells_the_reader_how_to_read_it(tmp_path) -> None:
    """With nexus-wo6sc sub-phasing present the note points at the two terms
    that separate a filesystem stall from descheduling."""
    log = tmp_path / "storage_service.log"
    log.write_text((_MISSED % ", 'stamp.write_replace': 31.6, 'stamp.unaccounted': 0.01") + "\n")
    out = _note(log)
    assert "stamp.unaccounted is descheduling" in out


def test_a_pre_subphase_tick_admits_it_cannot_name_the_cause(tmp_path) -> None:
    """The honest branch. An old engine logs a bare ``stamp``; saying which
    syscall stalled from that line would be the same overreach twice."""
    log = tmp_path / "storage_service.log"
    log.write_text((_MISSED % "") + "\n")
    assert "not determinable from this line alone" in _note(log)


def test_harness_sources_and_calls_the_library() -> None:
    """The wiring pin: extraction must not drift back to an inline copy."""
    text = HARNESS.read_text(encoding="utf-8")
    assert "heartbeat_stall_note.sh" in text, "harness no longer sources the library"
    assert "heartbeat_stall_note" in text.replace("heartbeat_stall_note.sh", ""), \
        "harness sources the library but never calls it"


@pytest.mark.parametrize("branch", ["put", "get"])
def test_both_skew_window_failure_branches_call_it(branch: str) -> None:
    """Sibling sweep: the put and get branches share the failure shape, so a
    fix applied to only the one that happened to fire leaves the other
    misdirecting the next reader."""
    text = HARNESS.read_text(encoding="utf-8")
    marker = f"skew-window T1 {branch} failed (rc="
    idx = text.index(marker)
    window = text[max(0, idx - 400):idx + 200]
    assert "_stall_note" in window, (
        f"the {branch} branch reports the opaque error without the note"
    )
    # ...and the wrapper is a wrapper, not a second implementation.
    assert "_stall_note() { heartbeat_stall_note" in text, (
        "the gate's indenting wrapper must delegate to the tested library "
        "function, or these fixtures pin logic the harness does not run"
    )
