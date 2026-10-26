# SPDX-License-Identifier: AGPL-3.0-or-later
"""release-preflight.sh names the suite lease when a pytest leg is refused by it (nexus-tvt6u).

A leg that exits through the suite lease prints one pytest.exit banner and no test output, so the
script's detail patterns all missed it and the leg read "(no verdict line matched -- re-run this leg
alone)", which looks like a broken test. The script is sourced with ``RELEASE_PREFLIGHT_SOURCE_ONLY=1``
(it defines its helpers and stops), and ``check`` runs against a stub leg that prints what pytest prints.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _suite_lease

SCRIPT = Path(__file__).resolve().parent / "e2e" / "release-preflight.sh"
BANNER = "! _pytest.outcomes.Exit: {msg} !"


def _check(output: str, rc: int) -> str:
    """The line ``check`` prints for a leg that prints *output* and exits *rc*."""
    env = {**os.environ, "RELEASE_PREFLIGHT_SOURCE_ONLY": "1", "LEG_OUT": output, "LEG_RC": str(rc)}
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; check "the-leg" bash -c \'printf "%s\\n" "$LEG_OUT"; exit "$LEG_RC"\'', "bash", str(SCRIPT)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[FAIL] the-leg" in proc.stdout, proc.stdout + proc.stderr
    return proc.stdout.strip()


def _refusal_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wait: str | None) -> str:
    """The conftest's own pytest.exit message for a held lease, so the script is pinned to the real wording."""
    gate = sys.modules["tests.conftest"]
    root = tmp_path / "leases"
    lease = root / _suite_lease.RESOURCE
    lease.mkdir(parents=True)
    (lease / "pid").write_text(f"{os.getpid()}\n")
    (lease / "label").write_text("peer-run: pytest tests/db\n")
    monkeypatch.setattr(gate, "_selected_t2_substrate_boots_engine", lambda: True)
    monkeypatch.setattr(_suite_lease, "inside_a_holder", lambda: False)
    monkeypatch.setattr(_suite_lease, "_lease_root", lambda: root)
    for name in ("LAB_CPUS", "LAB_MEM_MB", gate._SUITE_LEASE_UNGUARDED_ENV, "NX_SUITE_LEASE_WAIT"):
        monkeypatch.delenv(name, raising=False)
    if wait is None:  # the give-up path: a wait that runs out, in virtual time
        from tests._module_seam import module_time  # noqa: PLC0415

        clock = {"t": 0.0}
        fake = module_time(monkeypatch, _suite_lease)
        fake.sleep = lambda s: clock.__setitem__("t", clock["t"] + s)
        fake.monotonic = lambda: clock["t"]
    else:
        monkeypatch.setenv("NX_SUITE_LEASE_WAIT", wait)
    monkeypatch.setenv(_suite_lease.EVENTS_LOG_ENV, str(tmp_path / "events.log"))
    with pytest.raises(pytest.exit.Exception) as err:
        gate._take_suite_lease()
    return err.value.msg


@pytest.mark.parametrize("wait", ["0", None], ids=["immediate-refusal", "gave-up-after-the-wait"])
def test_a_leg_refused_by_the_suite_lease_names_the_lease_and_its_holder(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wait: str | None) -> None:
    msg = _refusal_message(monkeypatch, tmp_path, wait)
    line = _check("Exit: " + msg + "\n" + BANNER.format(msg=msg), rc=75)
    assert f"suite lease held by pid {os.getpid()} (peer-run: pytest tests/db)" in line, line
    assert "no verdict line matched" not in line


def test_the_lint_leg_shape_is_recognised_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """lint_leg_check returns 1 with the tail of the output, not pytest's 75."""
    msg = _refusal_message(monkeypatch, tmp_path, "0")
    line = _check("Exit: " + msg + "\n" + BANNER.format(msg=msg), rc=1)  # the tail the leg prints: pytest's line, then the banner
    assert "suite lease held by pid" in line, line


def test_an_ordinary_red_still_gets_the_old_fallback() -> None:
    assert "no verdict line matched" in _check("something broke, nothing recognisable", rc=1)


def test_a_failing_test_that_merely_quotes_the_refusal_is_not_rewritten() -> None:
    """pytest's assertion-rewrite output indents a quoted string behind ``E   ``; the ``Exit: ``
    text is mid-line there, which only the line-start anchor tells from pytest's own line."""
    quoted = (
        "    def test_x():\n"
        ">       assert 'Exit: suite lease: refusing to start. The lease is held by pid 1 (x)' == ''\n"
        "E       AssertionError: assert 'Exit: suite lease: refusing to start. The lease is held by pid 1 (x)' == ''\n"
        "E         Exit: suite lease: refusing to start. The lease is held by pid 1 (x)\n"
        "FAILED tests/x.py::test_x"
    )
    line = _check(quoted, rc=1)
    assert "suite lease held by" not in line, line


def test_the_banner_alone_is_not_the_exit_line() -> None:
    """The ``! _pytest.outcomes.Exit: ... !`` banner is mid-line; pytest's own line is what is matched."""
    line = _check(BANNER.format(msg="suite lease: refusing to start. The lease is held by pid 1 (x)"), rc=1)
    assert "suite lease held by" not in line, line


# -- no pytest leg of the preflight may queue on the suite lease (nexus-tvt6u) ----------------


def test_no_pytest_leg_of_the_preflight_can_queue_behind_another_run(tmp_path: Path) -> None:
    """The preflight's contract is seconds-scale, and a pytest leg that takes the suite lease
    waits up to 30 minutes behind another session's full suite with no output (``check``
    captures it). Every pytest leg must therefore run with ``NX_TEST_T2_SUBSTRATE=none`` (no
    lease) or ``NX_SUITE_LEASE_WAIT=0`` (refuse at once). The real script runs from a copy in a
    scratch tree with a stub ``uv`` that records each call's environment."""
    root = tmp_path / "tree"
    (root / "tests" / "e2e" / "lib").mkdir(parents=True)
    (root / "tests" / "e2e" / "release-preflight.sh").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "tests" / "e2e" / "lib" / "mandatory_pins.sh").write_text(
        "MANDATORY_PIN_MARK_EXPR=pin\nMANDATORY_PIN_EXPECTED=2\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv-calls"
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s|%s\\n" "${{NX_TEST_T2_SUBSTRATE-UNSET}}" "${{NX_SUITE_LEASE_WAIT-UNSET}}" "$*" >> {log}\n'
        'case "$*" in\n'
        '  *--collect-only*) printf "a::b\\nc::d\\n" ;;\n'
        '  *"-m lint"*) echo "500 passed in 1.00s" ;;\n'
        "esac\n"
        "exit 0\n", encoding="utf-8")
    uv.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in ("NX_TEST_T2_SUBSTRATE", "NX_SUITE_LEASE_WAIT", "RELEASE_PREFLIGHT_SOURCE_ONLY")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", GITHUB_TOKEN="stub")
    proc = subprocess.run(["bash", str(root / "tests" / "e2e" / "release-preflight.sh")],
                          capture_output=True, text=True, env=env, timeout=120, cwd=str(root))
    assert "PREFLIGHT PASSED" in proc.stdout, proc.stdout + proc.stderr
    calls = [line.split("|", 2) for line in log.read_text(encoding="utf-8").splitlines()]
    pytest_calls = [(sub, wait, args) for sub, wait, args in calls if "pytest" in args.split()]
    # ci-evidence-contexts, mandatory-pins (collect + run), the lint leg, pin-tests: five calls, counted so
    # a leg that stops reaching pytest cannot turn this into a pass over nothing.
    assert len(pytest_calls) == 5, calls
    for sub, wait, args in pytest_calls:
        assert sub == "none" or wait == "0", f"a preflight pytest leg can queue on the suite lease: {args}"
