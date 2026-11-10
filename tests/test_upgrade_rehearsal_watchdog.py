# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline self-test for the upgrade rehearsal driver's watchdog (nexus-f3yqd.5).

No network, no engine, no uv: every step here is a fake command (this interpreter running a
one-line program). The driver must judge each correctly: a chatty step passes, a step silent
past the limit fails as "silent" and is killed, a chatty step that runs past its ceiling fails
as "ceiling" and is killed, a failing step fails as "failed". The non-vacuity and version
selection helpers are pure and are pinned here too.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parent.parent / "scripts" / "upgrade_rehearsal.py"


def _load():
    spec = importlib.util.spec_from_file_location("_upgrade_rehearsal", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal"] = module
    spec.loader.exec_module(module)
    return module


ur = _load()


def _py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def _run(
    tmp_path: Path,
    code: str,
    *,
    silence: float = 1.0,
    ceiling: float = 5.0,
    name: str = "fake",
):
    return ur.run_step(
        name,
        _py(code),
        env=dict(os.environ),
        cwd=tmp_path,
        silence_limit_s=silence,
        ceiling_s=ceiling,
        log_dir=tmp_path / "logs",
    )


CHATTY = "import time\nfor i in range(6):\n    print('tick', i)\n    time.sleep(0.3)\n"


def test_chatty_step_passes(tmp_path: Path) -> None:
    r = _run(tmp_path, CHATTY)
    assert r.status == "ok", r
    assert r.exit_code == 0
    assert r.output_bytes > 0
    assert r.longest_gap_s < 1.0
    assert r.wall_s >= 1.5
    assert "tick 5" in (tmp_path / "logs" / "fake.log").read_text()


def test_silent_step_fails_and_is_killed_early(tmp_path: Path) -> None:
    t0 = time.monotonic()
    r = _run(
        tmp_path,
        "import time\nprint('hello', flush=True)\ntime.sleep(30)\n",
        silence=1.0,
        ceiling=20.0,
    )
    took = time.monotonic() - t0
    assert r.status == "silent", r
    assert took < 10, "the watchdog must kill the silent step, not wait for it"
    assert r.longest_gap_s >= 1.0
    assert "silent" in r.reason


def test_step_silent_from_the_start_fails(tmp_path: Path) -> None:
    r = _run(tmp_path, "import time\ntime.sleep(30)\n", silence=1.0, ceiling=20.0)
    assert r.status == "silent", r
    assert r.output_bytes == 0


def test_slow_chatty_step_fails_on_ceiling(tmp_path: Path) -> None:
    code = "import time\nwhile True:\n    print('still going', flush=True)\n    time.sleep(0.2)\n"
    t0 = time.monotonic()
    r = _run(tmp_path, code, silence=1.0, ceiling=2.0)
    took = time.monotonic() - t0
    assert r.status == "ceiling", r
    assert took < 10
    assert r.wall_s >= 2.0
    assert "ceiling" in r.reason


def test_a_step_silent_to_its_ceiling_reports_the_gap_it_sat_through(
    tmp_path: Path,
) -> None:
    # silence limit above the ceiling: the ceiling breach must still count the unbroken silence
    r = _run(tmp_path, "import time\ntime.sleep(30)\n", silence=60.0, ceiling=2.0)
    assert r.status == "ceiling", r
    assert r.longest_gap_s >= 2.0


def test_failing_step_fails_with_its_exit_code(tmp_path: Path) -> None:
    r = _run(tmp_path, "import sys\nprint('boom')\nsys.exit(3)\n")
    assert r.status == "failed", r
    assert r.exit_code == 3


def test_missing_binary_is_a_failed_step_not_a_crash(tmp_path: Path) -> None:
    r = ur.run_step(
        "nope",
        ["/nonexistent/binary-xyz"],
        env=dict(os.environ),
        cwd=tmp_path,
        silence_limit_s=1.0,
        ceiling_s=5.0,
        log_dir=tmp_path / "logs",
    )
    assert r.status == "failed"
    assert r.exit_code != 0


def test_killed_step_takes_its_children_with_it(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "print('spawned', flush=True)\n"
        "time.sleep(60)\n"
    )
    r = _run(tmp_path, code, silence=1.0, ceiling=20.0)
    assert r.status == "silent"
    pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    assert not alive, "the step's child survived the kill of the step's process group"


def test_step_sees_a_tty(tmp_path: Path) -> None:
    r = _run(tmp_path, "import sys\nprint('tty' if sys.stdout.isatty() else 'notty')\n")
    assert r.status == "ok"
    assert "tty" in (tmp_path / "logs" / "fake.log").read_text().replace("notty", "")


def test_unlimited_silence_for_setup_steps(tmp_path: Path) -> None:
    r = ur.run_step(
        "setup",
        _py("import time\ntime.sleep(1.5)\nprint('done')\n"),
        env=dict(os.environ),
        cwd=tmp_path,
        silence_limit_s=None,
        ceiling_s=10.0,
        log_dir=tmp_path / "logs",
    )
    assert r.status == "ok"


# --- non-vacuity ---------------------------------------------------------------------------


def _steps(**over):
    base = {
        n: ur.StepResult(
            name=n,
            status="ok",
            exit_code=0,
            wall_s=1.0,
            longest_gap_s=0.5,
            output_bytes=10,
            reason="",
        )
        for n in ur.JUDGED_STEPS
    }
    base.update(over)
    return list(base.values())


PREPARED = {"verdict": "prepared", "collections": {"a": 30_000, "b": 30_000}}


def test_nonvacuity_clean_run_has_no_findings() -> None:
    assert (
        ur.nonvacuity_findings(
            PREPARED,
            _steps(),
            floor=50_000,
            doctor_text="✓ all good\n",
            search_text="[0.1] bootw-a-first-note\n",
            sentinel_expect="bootw-a-first-note",
        )
        == []
    )


def test_nonvacuity_row_floor() -> None:
    f = ur.nonvacuity_findings(
        {"verdict": "prepared", "collections": {"a": 10}},
        _steps(),
        floor=50_000,
        doctor_text="",
        search_text="bootw-a-first-note",
        sentinel_expect="bootw-a-first-note",
    )
    assert any("rows" in x and "floor" in x for x in f)


def test_nonvacuity_prepare_must_have_said_prepared() -> None:
    f = ur.nonvacuity_findings(
        {"verdict": "error", "collections": {"a": 99_999}},
        _steps(),
        floor=1,
        doctor_text="",
        search_text="bootw-a-first-note",
        sentinel_expect="bootw-a-first-note",
    )
    assert any("prepared" in x for x in f)


def test_nonvacuity_missing_step_is_a_finding() -> None:
    steps = [s for s in _steps() if s.name != "doctor"]
    f = ur.nonvacuity_findings(
        PREPARED,
        steps,
        floor=1,
        doctor_text="",
        search_text="x",
        sentinel_expect="x",
    )
    assert any("doctor" in x and "did not run" in x for x in f)


def test_nonvacuity_doctor_cross_is_a_finding() -> None:
    f = ur.nonvacuity_findings(
        PREPARED,
        _steps(),
        floor=1,
        doctor_text="✓ ok\n✗ catalog: broken\n",
        search_text="sentinel",
        sentinel_expect="sentinel",
    )
    assert any("doctor" in x and "✗" in x for x in f)


def test_nonvacuity_doctor_with_no_output_is_a_finding() -> None:
    f = ur.nonvacuity_findings(
        PREPARED,
        _steps(),
        floor=1,
        doctor_text="  \n",
        search_text="s",
        sentinel_expect="s",
    )
    assert any("doctor" in x and "no output" in x for x in f)


def test_nonvacuity_sentinel_absent_is_a_finding() -> None:
    f = ur.nonvacuity_findings(
        PREPARED,
        _steps(),
        floor=1,
        doctor_text="✓",
        search_text="No results.",
        sentinel_expect="bootw-a-first-note",
    )
    assert any("sentinel" in x for x in f)


def test_nonvacuity_ignores_ansi_in_doctor_and_search() -> None:
    f = ur.nonvacuity_findings(
        PREPARED,
        _steps(),
        floor=1,
        doctor_text="\x1b[32m✓\x1b[0m fine",
        search_text="\x1b[1mbootw-a-\x1b[0mfirst-note",
        sentinel_expect="bootw-a-first-note",
    )
    assert f == []


def test_total_rows_accepts_the_real_seeder_shape() -> None:
    assert ur.total_rows({"collections": {"x": 3, "y": 4}}) == 7
    assert ur.total_rows({}) == 0


# --- version selection ---------------------------------------------------------------------

PUBLISHED = ["7.76.0", "7.98.0", "7.99.0", "8.0.0", "8.1.0rc1", "8.0.0.post1"]


def test_both_is_newest_below_candidate_last_7x_and_the_pre_pci_release() -> None:
    assert ur.resolve_from_versions("both", PUBLISHED, candidate="8.1.0") == [
        "8.0.0.post1",
        "7.99.0",
        "7.76.0",
    ]


def test_both_dedupes_when_the_previous_is_the_last_7x() -> None:
    assert ur.resolve_from_versions(
        "both", ["7.76.0", "7.99.0"], candidate="8.0.0"
    ) == [
        "7.99.0",
        "7.76.0",
    ]


def test_explicit_version_is_used_as_given() -> None:
    assert ur.resolve_from_versions("8.0.0", PUBLISHED, candidate="8.1.0") == ["8.0.0"]


def test_explicit_version_must_be_published() -> None:
    with pytest.raises(ur.RehearsalError):
        ur.resolve_from_versions("9.9.9", PUBLISHED, candidate="8.1.0")


def test_both_with_nothing_published_below_fails_loud() -> None:
    with pytest.raises(ur.RehearsalError):
        ur.resolve_from_versions("both", ["9.0.0"], candidate="8.1.0")


# --- verdict output ------------------------------------------------------------------------


def test_verdict_file_and_table(tmp_path: Path) -> None:
    steps = _steps(
        upgrade=ur.StepResult(
            name="upgrade",
            status="silent",
            exit_code=-9,
            wall_s=61.0,
            longest_gap_s=10.4,
            output_bytes=123,
            reason="silent for 10.4s (limit 10.0s)",
        )
    )
    verdict = ur.build_verdict(
        from_version="8.0.0",
        target="wheel x.whl",
        steps=steps,
        findings=["x"],
        prepare=PREPARED,
        floor=50_000,
    )
    assert verdict["passed"] is False
    assert verdict["from_version"] == "8.0.0"
    table = ur.format_table(verdict)
    assert "upgrade" in table and "silent" in table and "10.4" in table
    out = tmp_path / "v.json"
    ur.write_verdict(verdict, out)
    assert json.loads(out.read_text())["steps"][0]["name"]
    line = ur.verdict_line(verdict)
    assert line.startswith("UPGRADE REHEARSAL FAILED") and "8.0.0" in line


def test_a_passing_verdict_says_passed() -> None:
    verdict = ur.build_verdict(
        from_version="7.99.0",
        target="wheel",
        steps=_steps(),
        findings=[],
        prepare=PREPARED,
        floor=1,
    )
    assert verdict["passed"] is True
    assert ur.verdict_line(verdict).startswith("UPGRADE REHEARSAL PASSED")


def test_combined_line_names_each_run_and_where_it_failed() -> None:
    ok = ur.build_verdict(
        from_version="8.0.0",
        target="wheel",
        steps=_steps(),
        findings=[],
        prepare=PREPARED,
        floor=1,
    )
    bad = ur.build_verdict(
        from_version="7.99.0",
        target="wheel",
        findings=[],
        prepare=PREPARED,
        floor=1,
        steps=_steps(
            upgrade=ur.StepResult(name="upgrade", status="ceiling", reason="x")
        ),
    )
    assert ur.combined_line([ok]) == "UPGRADE REHEARSAL PASSED: from 8.0.0 to wheel"
    line = ur.combined_line([ok, bad])
    assert line.startswith("UPGRADE REHEARSAL FAILED") and "7.99.0 (at upgrade)" in line
    assert "8.0.0 (" not in line
    assert ur.combined_line([]).startswith("UPGRADE REHEARSAL FAILED")


def test_nonvacuity_deferred_upgrade_is_a_finding() -> None:
    f = ur.nonvacuity_findings(
        PREPARED,
        _steps(),
        floor=1,
        doctor_text="✓",
        search_text="s",
        sentinel_expect="s",
        upgrade_text="[upgrade-finish] upgraded 7.99.0 -> 8.0.0; DEFERRED: engine convergence held back",
    )
    assert any("DEFERRED" in x for x in f)
    assert (
        ur.nonvacuity_findings(
            PREPARED,
            _steps(),
            floor=1,
            doctor_text="✓",
            search_text="s",
            sentinel_expect="s",
            upgrade_text="upgraded 7.99.0 -> 8.0.0",
        )
        == []
    )


def test_a_step_sees_the_login_name_a_users_shell_carries(monkeypatch, tmp_path):
    """getpass and the engine's PG provisioning both read the login name; a step without it saw
    the seeder and the old client disagree on the cluster superuser (2026-11-10 measuring run)."""
    build_env = ur.build_env

    monkeypatch.setenv("USER", "someone")
    monkeypatch.setenv("LOGNAME", "someone")
    monkeypatch.setenv("NX_VOYAGE_API_KEY", "must-not-pass")
    env = build_env(tmp_path / "home", tmp_path / "config")
    assert env["USER"] == "someone" and env["LOGNAME"] == "someone"
    assert "NX_VOYAGE_API_KEY" not in env
