# SPDX-License-Identifier: AGPL-3.0-or-later
"""A rung rides out an engine restart (nexus-f3yqd.5, marlowe's rehearsal from 7.76.0).

An engine stopped and started from another shell while ``nx upgrade`` was inside the manifest-section-labels
rung made the converge raise "nexus-service endpoint is not resolvable", and the upgrade exited 1 though the
engine answered again 4 s later. The runner now waits, bounded, for the engine and resumes the rung; a
rung whose engine does not come back is DEFERRED (a rerun resumes it), never FAILED. Any other converge
error still fails the walk.
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus.db.http_vector_client import VectorServiceError
from nexus.db.service_endpoint import ServiceEndpointUnresolvableError
from nexus.upgrade_ladder.protocol import ConvergeResult, ProgressReporter
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SECTION_LABELS, RUNG_MANIFEST_SPAN_FILL, LadderRegistry
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from tests.upgrade.test_ladder_runner import InMemoryLedger, Recorder, ScriptedRung


@dataclass
class _EngineStopsOnce(ScriptedRung):
    """The engine goes away during the first converge call, then serves again."""

    outages: int = 1
    error: Exception | None = None

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        if self.outages:
            self.outages -= 1
            self.converge_calls += 1
            raise self.error or ServiceEndpointUnresolvableError(
                "nexus-service endpoint is not resolvable (NX_STORAGE_BACKEND=service): start the supervisor"
            )
        return super().converge(report)


def _walk(*rungs, wait=lambda _budget: True, may_return=True):
    store = InMemoryLedger()
    recorder = Recorder()
    waits: list[float] = []

    def engine_wait(budget: float) -> bool:
        waits.append(budget)
        return wait(budget)

    runner = LadderRunner(
        LadderRegistry(rungs), store, reporter=recorder, package_version_fn=lambda: "8.1.2",
        engine_wait=engine_wait, engine_may_return=lambda: may_return,
    )
    return runner.run(), store, recorder, waits


def test_an_engine_restart_mid_rung_is_waited_out_and_the_rung_resumes() -> None:
    rung = _EngineStopsOnce(RUNG_MANIFEST_SECTION_LABELS)
    report, store, recorder, waits = _walk(rung)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED], [(r.outcome, r.detail) for r in report.runs]
    assert len(waits) == 1 and rung.converge_calls == 2
    assert RUNG_MANIFEST_SECTION_LABELS in store.verified_rungs()
    assert any(name == "ladder_engine_wait" for name, _f in recorder.events)


def test_a_transport_failure_from_the_vector_client_counts_as_the_engine_away() -> None:
    rung = _EngineStopsOnce(RUNG_MANIFEST_SPAN_FILL, error=VectorServiceError("Connection refused"))
    report, _store, _rec, waits = _walk(rung)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED] and len(waits) == 1


def test_an_engine_that_does_not_come_back_defers_the_rung_instead_of_failing_the_upgrade() -> None:
    rung = _EngineStopsOnce(RUNG_MANIFEST_SECTION_LABELS, outages=10)
    report, store, _rec, waits = _walk(rung, wait=lambda _budget: False)
    (run,) = report.runs
    assert run.outcome is RungOutcome.DEFERRED, (run.outcome, run.detail)
    assert "nx upgrade" in run.detail and not store.verified_rungs() and len(waits) == 1


def test_an_engine_that_keeps_dropping_is_retried_a_bounded_number_of_times() -> None:
    rung = _EngineStopsOnce(RUNG_MANIFEST_SECTION_LABELS, outages=10)
    report, _store, _rec, waits = _walk(rung)
    (run,) = report.runs
    assert run.outcome is RungOutcome.DEFERRED and len(waits) == LadderRunner.ENGINE_RESTART_RETRIES


def test_any_other_converge_error_still_fails_the_walk() -> None:
    rung = _EngineStopsOnce(RUNG_MANIFEST_SECTION_LABELS, error=ValueError("bad row"))
    report, _store, _rec, waits = _walk(rung)
    (run,) = report.runs
    assert run.outcome is RungOutcome.FAILED and waits == []
    assert isinstance(VectorServiceError("x", code=422), VectorServiceError)  # an HTTP refusal is not an outage
    rung2 = _EngineStopsOnce(RUNG_MANIFEST_SECTION_LABELS, error=VectorServiceError("model unavailable", code=422))
    report2, _s, _r, waits2 = _walk(rung2)
    assert report2.runs[0].outcome is RungOutcome.FAILED and waits2 == []


def test_nx_upgrade_says_it_is_waiting_for_the_engine(capsys) -> None:
    from nexus.commands.upgrade import _BatchProgress  # noqa: PLC0415 — the CLI's reporter

    _BatchProgress(clock=lambda: 0.0).emit("ladder_engine_wait", rung="manifest-section-labels", wait_s=120.0, error="x")
    assert capsys.readouterr().out == (
        "  the engine stopped answering (a restart?); waiting up to 120 s for it, then this step resumes\n"
    )


def test_the_real_section_label_rung_resumes_after_the_engine_drops_mid_batch() -> None:
    """The rehearsal's shape on the real rung: its first owner read finds the engine gone (the catalog client
    raises httpx.ConnectError), the runner waits, and the rung's next converge fills and records."""
    import httpx  # noqa: PLC0415 — the catalog client's transport

    from tests.upgrade.test_manifest_section_labels_rung import World, _mixed  # noqa: PLC0415 — that file's fixture

    world = World()
    _mixed(world)
    real = world.chash_positions_with_tombstoned
    dropped = [False]

    def flaky(chashes):
        if not dropped[0]:
            dropped[0] = True
            raise httpx.ConnectError("[Errno 61] Connection refused")
        return real(chashes)

    world.chash_positions_with_tombstoned = flaky  # type: ignore[method-assign]
    report, store, recorder, waits = _walk(world.rung())
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED], [(r.outcome, r.detail) for r in report.runs]
    assert dropped[0] and len(waits) == 1 and RUNG_MANIFEST_SECTION_LABELS in store.verified_rungs()


def test_an_engine_that_was_never_there_is_not_waited_for() -> None:
    """No supervisor, never started: nx upgrade must not sit through a restart wait per step."""
    rung = _EngineStopsOnce(RUNG_MANIFEST_SECTION_LABELS, outages=10)
    report, _store, _rec, waits = _walk(rung, may_return=False)
    assert report.runs[0].outcome is RungOutcome.DEFERRED and waits == []
