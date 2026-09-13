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

import pathlib
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


class TestTheLibraryReachesTheContainer:
    """nexus-wo6sc, 2026-09-13. The note was correct, unit-tested and
    mutation-checked, and had never once run where it was written to run.

    rehearse_package_upgrade.sh sources lib/heartbeat_stall_note.sh, but
    run.sh's --package-upgrade branch staged only the Dockerfile and the
    script, and Dockerfile.package-upgrade COPYed only the wheel and the
    script. Inside the container the source failed with "No such file or
    directory"; the script runs under `set -uo pipefail` with no `-e`, so it
    carried on and _stall_note called an undefined function. The attribution
    was absent from exactly the failure report it exists to annotate, and no
    test could see it: test_harness_sources_and_calls_the_library reads the
    script's TEXT on the host, which was correct the whole time.

    Found by nexus-dd reading a battery log, not by the suite. These checks
    are the suite catching up: they assert the library REACHES the image,
    which is the property that was actually missing.
    """

    DOCKERFILE = REPO_ROOT / "tests/e2e/migration-rehearsal/Dockerfile.package-upgrade"
    RUNNER = REPO_ROOT / "tests/e2e/migration-rehearsal/run.sh"

    def test_the_package_upgrade_image_copies_the_lib_directory(self) -> None:
        text = self.DOCKERFILE.read_text(encoding="utf-8")
        assert "COPY lib/" in text, (
            "Dockerfile.package-upgrade does not COPY lib/, so the script's "
            "`source lib/heartbeat_stall_note.sh` fails inside the container "
            "and the stall attribution never prints"
        )

    def test_the_runner_stages_lib_for_the_package_upgrade_branch(self) -> None:
        text = self.RUNNER.read_text(encoding="utf-8")
        idx = text.index('cp "$HERE/rehearse_package_upgrade.sh" "$STAGE/"')
        window = text[max(0, idx - 600):idx + 600]
        assert '"$HERE/lib"' in window, (
            "run.sh's --package-upgrade branch does not stage lib/ next to "
            "the script, so the Dockerfile's COPY has nothing to copy"
        )

    def test_the_script_refuses_to_run_without_the_library(self) -> None:
        """The backstop for the next time staging drifts: an absent library
        must STOP the gate, not silently remove one of its outputs. Driven
        by actually running the script with and without lib/ present, since
        the whole defect was that the on-host text looked right."""
        import os
        import shutil
        import subprocess
        import tempfile

        script = REPO_ROOT / "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh"
        env = {
            **os.environ,
            "PREV_RELEASE": "6.9.0",
            "PREV_ENGINE_TAG": "engine-service-v0.1.42",
            "NEW_ENGINE_TAG": "engine-service-v0.1.43",
        }
        with tempfile.TemporaryDirectory() as tmp:
            staged = pathlib.Path(tmp) / "rehearse_package_upgrade.sh"
            shutil.copy(script, staged)

            without = subprocess.run(
                ["bash", str(staged)], capture_output=True, text=True,
                env=env, timeout=60, check=False,
            )
            assert without.returncode == 1, (
                "a missing library must stop the gate; got rc="
                f"{without.returncode}"
            )
            assert "heartbeat_stall_note.sh is missing" in without.stderr, (
                f"the refusal must name the file: {without.stderr[:400]}"
            )

            (pathlib.Path(tmp) / "lib").mkdir()
            shutil.copy(LIB, pathlib.Path(tmp) / "lib" / LIB.name)
            with_lib = subprocess.run(
                ["bash", str(staged)], capture_output=True, text=True,
                env=env, timeout=60, check=False,
            )
            assert "heartbeat_stall_note.sh is missing" not in with_lib.stderr, (
                "the guard fired even though the library was present -- it "
                f"would refuse every real run: {with_lib.stderr[:400]}"
            )


class TestHeartbeatCensus:
    """nexus-wo6sc, 2026-09-13. The census exists because a zero from a
    passing run was worth nothing.

    The 4-wide battery at ef6d9c466 produced no missed-TTL lines, and that
    could not be read as "no stalls": the supervisor log lives inside the
    container and is dumped only on the FAILURE path, so a green run
    discards it. The distribution that would distinguish a threshold effect
    from a structural one was unobservable by construction.

    Its load-bearing rule is that an unreadable log is NOT zero ticks.
    Those are different findings, and conflating them is how a run reports
    clean when it measured nothing.
    """

    SLOW = (
        "2026-09-12 20:34:54,016 nexus.daemon.storage_service_daemon WARNING "
        "event='storage_service_heartbeat_slow' elapsed_s=6.806 ttl_s=15.0 "
        "phases_s={'stamp': 6.775, 'stamp.unaccounted': 0.002}"
    )

    @staticmethod
    def _census(log_path) -> str:
        proc = subprocess.run(
            ["bash", "-c", f'source "{LIB}"; heartbeat_census "$1"', "_", str(log_path)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, f"the census must never fail a run: {proc.stderr}"
        return proc.stdout

    def test_an_unreadable_log_is_not_reported_as_zero(self, tmp_path) -> None:
        """The rule the whole thing turns on. If this ever regresses to
        printing missed_ttl=0, a run that measured nothing reads as clean —
        which is exactly the reading that wasted a 43-minute battery."""
        out = self._census(tmp_path / "absent.log")
        assert "UNREADABLE" in out
        assert "NOT a report of zero stalls" in out
        assert "missed_ttl=0" not in out, (
            "an absent log must not be rendered as a zero count"
        )

    def test_a_clean_readable_log_reports_a_real_zero(self, tmp_path) -> None:
        log = tmp_path / "storage_service.log"
        log.write_text(_HEALTHY + "\n")
        out = self._census(log)
        assert "missed_ttl=0 slow=0" in out
        assert "a real zero, read from a readable log" in out
        assert "UNREADABLE" not in out

    def test_slow_ticks_are_counted_and_printed_verbatim(self, tmp_path) -> None:
        """A count discards the stamp.* breakdown, which is the entire
        reason the sub-phasing was built, so the lines come out whole."""
        log = tmp_path / "storage_service.log"
        log.write_text("\n".join([_HEALTHY, self.SLOW, self.SLOW]) + "\n")
        out = self._census(log)
        assert "missed_ttl=0 slow=2" in out
        assert out.count("stamp.unaccounted") == 2, (
            "each slow line must appear whole, with its phase breakdown"
        )

    def test_missed_and_slow_are_counted_separately(self, tmp_path) -> None:
        log = tmp_path / "storage_service.log"
        log.write_text("\n".join([self.SLOW, _MISSED % "", _HEALTHY]) + "\n")
        out = self._census(log)
        assert "missed_ttl=1 slow=1" in out

    def test_the_harness_runs_the_census_on_every_exit_path(self) -> None:
        """Wiring pin. The census must sit ABOVE the pass/fail branch, or it
        only reports when something else already failed — which is the
        defect it was written to remove."""
        text = HARNESS.read_text(encoding="utf-8")
        census_at = text.index("heartbeat_census | sed")
        result_at = text.index('say "RESULT"')
        assert census_at < result_at, (
            "the census must run before the pass/fail branch, not inside it"
        )
        assert "heartbeat_census" in text.split("_STALL_NOTE_LIB")[-1], (
            "the startup guard must require heartbeat_census to exist too"
        )
