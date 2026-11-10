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


@pytest.mark.parametrize("still_dead", [True])
def test_a_restore_that_still_fails_defers_the_rung_and_names_the_document(still_dead) -> None:
    world = World()
    world.restamp_on_append = RESTAMP
    a = world.doc("1.1.1")
    world.row(a, 0, h(1))
    world.chunk(h(1), **SPAN)
    engine = _Dies(world)
    rung = world.rung(doc_batch=1)
    rung.converge(Recorder())  # the write commits, the restore is refused and owed
    assert world.docs[a].indexed_at == RESTAMP
    world.update = lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("refused"))  # type: ignore[method-assign]
    world.update_many = lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("refused"))  # type: ignore[method-assign]
    engine.dead = False
    result = rung.converge(Recorder())
    assert result.deferred and a in result.detail, result.detail


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
