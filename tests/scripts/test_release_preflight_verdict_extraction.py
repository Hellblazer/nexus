# SPDX-License-Identifier: AGPL-3.0-or-later
"""``tests/e2e/release-preflight.sh``'s ``check()`` detail-extraction (nexus-jzyt3).

Root cause this closes: cutting conexus 7.44.0, the `engine-release-floor`
leg reported `[FAIL] engine-release-floor -- (no verdict line matched)`.
`scripts/check_engine_release_floor.py`'s bare form, on a dev-checkout box
with a globally-exported `NX_GATE_REPORT_DIR`, attempts to write the
`deployed-engine-version` tracker (an HTTP T2 write) and hit the
nexus-a2qhz production-write guard -- an exception the script did not
catch, so it propagated as a bare Python traceback whose last line
(`nexus.db.service_endpoint.ProductionWriteGuardError: STOP: ...`) does not
start with an ALL-CAPS verdict token, and `check()`'s detail-extraction
(neither the first-tier `FAILED|E  +|FATAL|GATE ...` grep nor the
second-tier `^[A-Z][A-Z0-9 ]{2,}[(:]` fallback) matched anything.

The real fix is on the Python side (`check_engine_release_floor.py`'s
`record_deploy_from_gate_report_leg` now catches `ProductionWriteGuardError`
and emits a `TRACKER NOT RECORDED (exit 3): ...` verdict through the same
choreography path every other tracker refusal uses -- see
`tests/scripts/test_check_engine_release_floor.py::
test_choreography_record_deploy_names_production_write_guard`). This file
proves the OTHER half: that `check()`'s own shell-side extraction, given
that shaped message from ANY command (never invoking the real Python
script, no network, no engine substrate -- a stubbed command standing in
for it), surfaces the actual refusal instead of "(no verdict line matched)".

Extraction method: `check()`/`record()`/`first_line()` (`release-preflight.sh`
lines 39-87) are self-contained -- they read no global state the harness
does not itself set (`PASS`/`FAIL`/`SKIP`/`RESULTS` are declared inside this
same span) and depend on nothing from the script's `cd`/`set -uo pipefail`
preamble above them. Sourcing exactly that line range (never the whole
script, which would also fetch tags, probe the floor for real, etc.) is
therefore both sufficient and side-effect-free.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import release_messages

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "release-preflight.sh"
_FUNC_LINES = (39, 87)  # 1-based, inclusive: first_line/record/check only


def _extract_functions() -> str:
    lines = SCRIPT.read_text().splitlines()
    start, end = _FUNC_LINES
    body = "\n".join(lines[start - 1 : end])
    assert "check ()" in body and "record ()" in body and "first_line ()" in body, (
        "release-preflight.sh's check()/record()/first_line() moved out of "
        f"lines {_FUNC_LINES} -- update _FUNC_LINES to match"
    )
    return body


def _run_check(name: str, message: str, exit_code: int, tmp_path: Path) -> subprocess.CompletedProcess:
    """Source the real check()/record()/first_line(), run one `check` call
    against a stub command that echoes *message* to stderr and exits
    *exit_code*, print the recorded RESULTS row so the test can inspect it.

    The message is written to a file and `cat`-ed rather than interpolated
    into the generated shell source directly -- this text contains quotes,
    parens, and an embedded URL, and round-tripping it through ANOTHER
    layer of shell quoting (the stub command itself) is exactly the kind of
    thing worth not hand-rolling.
    """
    msg_file = tmp_path / "message.txt"
    msg_file.write_text(message)
    stub_cmd = f"bash -c {shlex.quote(f'cat {shlex.quote(str(msg_file))} >&2; exit {exit_code}')}"
    script = (
        _extract_functions()
        + f'\ncheck {shlex.quote(name)} {stub_cmd}'
        + '\nprintf \'%s\\n\' "${RESULTS[@]}"\n'
    )
    return subprocess.run(
        ["bash", "-c", script], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )


class TestProductionWriteGuardRefusalIsExtracted:
    """The exact shape check_engine_release_floor.py now emits for
    ProductionWriteGuardError (post-fix) is recognized, not swallowed."""

    def test_tracker_not_recorded_production_write_guard_message_is_extracted(
        self, tmp_path: Path
    ) -> None:
        # Drawn from the real catalog entry (release_messages.py), never a
        # hand-typed copy -- a hand-typed string here would drift silently
        # the next time that message changes and prove nothing real.
        template = release_messages.get(
            "record_deploy_from_gate_report_leg::tracker_production_write_guard"
        )
        message = template.replace(
            "[exc]", "STOP: refusing a WRITE to 'https://api.conexus-nexus.com'"
        )
        proc = _run_check("engine-release-floor", message, 3, tmp_path)
        assert proc.returncode == 0, proc.stderr
        # record() prints its own human-readable "[FAIL] ..." line to
        # stdout as a side effect; the RESULTS row this test actually
        # parses is the LAST line (this script's own trailing printf).
        result_line = proc.stdout.strip().splitlines()[-1]
        status, name, detail = result_line.split("|", 2)
        assert status == "FAIL"
        assert name == "engine-release-floor"
        assert detail != "(no verdict line matched -- re-run this leg alone to see its output)"
        assert "TRACKER NOT RECORDED" in detail
        assert "NX_GATE_REPORT_DIR" in detail
        assert "NX_ALLOW_PROD_WRITE" in detail


class TestUncaughtTracebackStillFallsThroughLoud:
    """Pins the ORIGINAL failure mode this bead reported: an uncaught
    exception's traceback (the pre-fix shape -- kept here so a FUTURE
    regression that reintroduces an uncaught exception in some OTHER
    leg is at least visible as "(no verdict line matched)" rather than
    silently mis-attributed, never as a false PASS)."""

    def test_bare_traceback_with_no_allcaps_line_is_named_unmatched(self, tmp_path: Path) -> None:
        traceback_tail = (
            "nexus.db.service_endpoint.ProductionWriteGuardError: STOP: "
            "refusing a WRITE to 'https://api.conexus-nexus.com'"
        )
        proc = _run_check("engine-release-floor", traceback_tail, 1, tmp_path)
        assert proc.returncode == 0, proc.stderr
        result_line = proc.stdout.strip().splitlines()[-1]
        status, name, detail = result_line.split("|", 2)
        assert status == "FAIL"
        assert name == "engine-release-floor"
        assert detail == "(no verdict line matched -- re-run this leg alone to see its output)"
