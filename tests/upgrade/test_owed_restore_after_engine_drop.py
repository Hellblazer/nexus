# SPDX-License-Identifier: AGPL-3.0-or-later
"""An engine that dies right after a manifest write does not lose the document's indexed_at restore
(nexus-f3yqd.5, review of the ladder engine-restart fix).

The engine restamps a document's indexed_at on every manifest write, and the rung writes the scan's stamp
back after. When the engine dies between the two, the restore fails; when the next call then raises, the
converge propagates, and the runner reruns the rung once the engine is back. The rerun's scan used to
read the run-time stamp as the document's own, which switched off the chunk-newer guard for its other
rows. The rung now keeps the restores it owes and pays them before its next scan.
"""

from __future__ import annotations

import pytest

from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from tests.upgrade.test_ladder_runner import InMemoryLedger, Recorder
from tests.upgrade.test_manifest_span_fill_rung import DOC_STAMP, SPAN, World, h

RESTAMP = "2026-11-10T00:00:00+00:00"


class _Dies:
    """The world's engine, which goes away right after the first manifest write commits."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.dead = False
        self.died = False
        inner_append, inner_update, inner_many, inner_get = (
            world.append_manifest_many, world.update, world.update_many, world.get_manifests,
        )

        def append(docs, **kw):
            self._alive()
            out = inner_append(docs, **kw)
            if not self.died:
                self.died = self.dead = True  # the first write commits, then the engine is gone before the restore
            return out

        world.append_manifest_many = append  # type: ignore[method-assign]
        world.update = lambda *a, **k: (self._alive(), inner_update(*a, **k))[1]  # type: ignore[method-assign]
        world.update_many = lambda *a, **k: (self._alive(), inner_many(*a, **k))[1]  # type: ignore[method-assign]
        world.get_manifests = lambda *a, **k: (self._alive(), inner_get(*a, **k))[1]  # type: ignore[method-assign]

    def _alive(self) -> None:
        if self.dead:
            raise ConnectionRefusedError(61, "Connection refused")

    def back(self, _budget: float) -> bool:
        self.dead = False
        return True


def test_a_restore_the_dead_engine_refused_is_paid_before_the_rerun_scans() -> None:
    world = World()
    world.restamp_on_append = RESTAMP
    a, b = world.doc("1.1.1"), world.doc("1.1.2")
    for i, d in enumerate((a, b)):
        world.row(d, 0, h(i + 1))
        world.chunk(h(i + 1), **SPAN)
    engine = _Dies(world)
    runner = LadderRunner(
        LadderRegistry((world.rung(doc_batch=1),)), InMemoryLedger(), reporter=Recorder(),
        package_version_fn=lambda: "8.1.2", engine_wait=engine.back, engine_may_return=lambda: True,
        engine_wait_budget=lambda: 1.0,
    )
    report = runner.run()
    assert world.docs[a].indexed_at == DOC_STAMP, "the first document kept the dead engine's restamp"
    assert world.docs[b].indexed_at == DOC_STAMP
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED], [(r.outcome, r.detail) for r in report.runs]


class _Note:
    """An in-memory NoteStore: the T2 note a second nx upgrade would read."""

    def __init__(self) -> None:
        self.note: dict | None = None

    def load(self, *, strict: bool = False):
        return self.note

    def save(self, note):
        self.note = __import__("json").loads(__import__("json").dumps(note))

    def clear(self):
        self.note = None


def test_the_owed_write_back_survives_the_run_that_could_not_make_it() -> None:
    """The critic's case: the engine dies right after the last batch's write. The step defers and the process
    ends; the next nx upgrade builds a new rung, reads the note, and pays before it scans."""
    world = World()
    world.restamp_on_append = RESTAMP
    a = world.doc("1.1.1")
    world.row(a, 0, h(1))
    world.chunk(h(1), **SPAN)
    note = _Note()
    engine = _Dies(world)
    first = world.rung(doc_batch=1, owed_store=note)
    assert first.converge(Recorder()).deferred  # the write-back was refused; the step defers
    assert world.docs[a].indexed_at == RESTAMP and a in (note.note or {}).get("docs", {})
    engine.dead = False  # the engine is back for the next run
    second = world.rung(doc_batch=1, owed_store=note)  # a new process: a new rung, the same note
    second.converge(Recorder())
    assert world.docs[a].indexed_at == DOC_STAMP and note.note is None


def test_an_owed_write_back_that_keeps_failing_is_dropped_after_three_payments() -> None:
    world = World()
    note = _Note()
    note.note = {"docs": {"9.9.9": {"stamp": DOC_STAMP, "tries": 0}}}  # a document deleted since
    world.update = lambda *a, **k: (_ for _ in ()).throw(KeyError("9.9.9"))  # type: ignore[method-assign]
    outcomes = [world.rung(owed_store=note).converge(Recorder()).deferred for _ in range(3)]
    assert outcomes == [True, True, False] and note.note is None


def test_a_payment_that_finds_the_engine_away_raises_for_the_runner_to_wait() -> None:
    world = World()
    note = _Note()
    note.note = {"docs": {"1.1.1": {"stamp": DOC_STAMP, "tries": 0}}}
    world.doc("1.1.1")
    world.update = lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError(61, "refused"))  # type: ignore[method-assign]
    with pytest.raises(ConnectionRefusedError):
        world.rung(owed_store=note).converge(Recorder())
    assert note.note["docs"]["1.1.1"]["tries"] == 0  # kept whole for the next payment

def test_the_section_label_rung_pays_its_owed_restore_too() -> None:
    from tests.upgrade.test_manifest_section_labels_rung import World as LabelWorld  # noqa: PLC0415 — that rung's world
    from tests.upgrade.test_manifest_section_labels_rung import _labelled  # noqa: PLC0415

    world = LabelWorld()
    world.restamp_on_append = RESTAMP
    for n in (1, 2):
        world.doc(f"1.1.{n}")
        world.row(f"1.1.{n}", 0, h(n))
        _labelled(world, h(n))
    stamp = world.docs["1.1.1"].indexed_at
    engine = _Dies(world)
    runner = LadderRunner(
        LadderRegistry((world.rung(doc_batch=1),)), InMemoryLedger(), reporter=Recorder(),
        package_version_fn=lambda: "8.1.2", engine_wait=engine.back, engine_may_return=lambda: True,
        engine_wait_budget=lambda: 1.0,
    )
    report = runner.run()
    assert world.docs["1.1.1"].indexed_at == stamp != RESTAMP
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED], [(r.outcome, r.detail) for r in report.runs]
