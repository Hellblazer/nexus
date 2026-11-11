# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor`` names each check as it starts and lists the slow ones, on
stderr so ``--json`` stays one document (nexus-mjmjl)."""

from __future__ import annotations

import io

from click.testing import CliRunner

from nexus import health
from nexus.progress.step_report import StepReporter, no_ticker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_each_core_check_runs_as_a_named_step() -> None:
    clock = Clock()
    lines: list[str] = []
    rep = StepReporter(
        0, echo=lines.append, stream=io.StringIO(), tty=False, clock=clock, ticker=no_ticker,
        compact=True, slow_s=3.0, heartbeat_s=3.0, announce="Checking {what}{detail}",
    )
    step = health._row_stepper(rep)

    def slow_check() -> list:
        clock.now += 4
        return ["row"]

    assert step("_check_python", lambda: ["ok"]) == ["ok"]
    assert step("_check_t3_local", slow_check) == ["row"]
    assert lines == ["Checking python", "Checking t3 local", "  t3 local: done in 4.0 s"]
    assert health._row_stepper(None)("_check_python", lambda: ["plain"]) == ["plain"]


def test_nx_doctor_writes_progress_to_stderr_only(monkeypatch) -> None:
    from nexus.commands.doctor import doctor_cmd

    def fake_run(git_hooks_scope=None, engine_status=None, *, verbose=False, progress=None):
        with progress.step("python"):
            pass
        return [], True

    monkeypatch.setattr("nexus.health.run_health_checks", fake_run)
    monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", lambda: None)
    monkeypatch.setattr("nexus.commands.doctor._run_supplementary_checks", lambda _s, progress=None: False)
    result = CliRunner().invoke(doctor_cmd, [])
    assert "Checking python" in result.stderr
    assert "Summary: 2 step(s) in " in result.stderr and "Checking the engine's status" in result.stderr
    assert "Checking python" not in result.stdout


def test_json_stdout_stays_one_document(monkeypatch) -> None:
    import json

    from nexus.commands.doctor import doctor_cmd

    def fake_run(git_hooks_scope=None, engine_status=None, *, verbose=False, progress=None):
        with progress.step("python"):
            pass
        return [], True

    monkeypatch.setattr("nexus.health.run_health_checks", fake_run)
    monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", lambda: None)
    result = CliRunner().invoke(doctor_cmd, ["--json"])
    json.loads(result.stdout)


def test_the_supplementary_checks_run_as_steps(monkeypatch) -> None:
    from nexus.commands import doctor

    names: list[str] = []

    class Rep:
        def step(self, what, prints=False):
            names.append(what)
            import contextlib

            return contextlib.nullcontext()

    monkeypatch.setattr(doctor, "_SUPPLEMENTARY_CHECK_NAMES", ("t1",))
    monkeypatch.setattr(doctor, "_run_check_t1", lambda: None)
    doctor._run_supplementary_checks(None, progress=Rep())
    assert names == ["t1"]


def test_the_default_sweep_has_no_catalog_document_walk() -> None:
    """F30 (nexus-f3yqd.6): the walk was the default sweep's longest named step (22 s on a laptop tenant); it now
    runs only under `nx doctor --check-index-runs`."""
    import inspect

    assert "_walk_documents_for_doctor" not in inspect.getsource(health.run_health_checks)
    assert "_walk_documents_for_doctor()" in inspect.getsource(health.run_index_run_checks)
