# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-186 P2 (nexus-146xx.11): the in-process completion holder.

RF-186-2's explicit P2 obligation: pre-engine ladder completion state is
held in-process, and the holder MUST serve ``verified_rungs()`` for later
rungs within the SAME walk while the durable backend (engine) is down.
``_converge_preconditions()`` normally brings the engine up before
``_run_ladder()``, so the holder covers only the engine-defer window; a
crash inside it costs an idempotent re-derivation (RDR-142 contract),
never correctness.

Non-vacuity discipline: every engine-down test asserts the backend was
genuinely consulted-and-unavailable (call counters on the scripted
backend), so a test that passes because the backend silently served the
read cannot masquerade as holder coverage. The mutation the suite must
kill: bypass the holder's in-memory overlay → a later walk pays a
redundant ``verify()`` (tolerable, the walk still converges) — asserted
via exact ``verify_calls`` counts and ``ALREADY_RECORDED`` outcomes.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import pytest

from nexus.upgrade_ladder.completion import (
    CompletionRecord,
    CompletionStore,
    derive_ladder_position,
)
from nexus.upgrade_ladder.holder import InProcessCompletionHolder
from nexus.upgrade_ladder.protocol import CompletionLedger
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome

from tests.upgrade.test_ladder_runner import ScriptedRung, _outcomes


class BackendDown(RuntimeError):
    """Scripted stand-in for the engine-unreachable failure class."""


@dataclass
class ScriptedBackend:
    """Durable-backend stub with a switchable availability flag.

    Mirrors the ``CompletionStore`` read/write surface the holder fronts.
    Counters make engine-down tests non-vacuous: a test claiming the
    backend was down must show the backend was actually consulted.
    """

    down: bool = False
    records: dict[str, CompletionRecord] = field(default_factory=dict)
    record_calls: int = 0
    read_calls: int = 0

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        self.record_calls += 1
        if self.down:
            raise BackendDown("engine down")
        self.records[rung_name] = CompletionRecord(
            rung_name=rung_name,
            verified_at="backend-t",
            package_version=package_version,
            detail=detail,
        )

    def verified_rungs(self) -> frozenset[str]:
        self.read_calls += 1
        if self.down:
            raise BackendDown("engine down")
        return frozenset(self.records)

    def completions(self) -> dict[str, CompletionRecord]:
        self.read_calls += 1
        if self.down:
            raise BackendDown("engine down")
        return dict(self.records)


def _holder(backend: ScriptedBackend) -> InProcessCompletionHolder:
    return InProcessCompletionHolder(backend, now_fn=lambda: "held-t")


# ── Write-through when the backend is up ─────────────────────────────────────


def test_record_writes_through_to_backend_when_up() -> None:
    backend = ScriptedBackend()
    holder = _holder(backend)
    holder.record_verified("a", package_version="6.13.0")
    assert "a" in backend.records
    assert backend.records["a"].package_version == "6.13.0"
    assert holder.unflushed() == {}


def test_verified_rungs_unions_backend_records_from_prior_processes() -> None:
    backend = ScriptedBackend(
        records={
            "prior": CompletionRecord(
                rung_name="prior", verified_at="t", package_version="6.12.0"
            )
        }
    )
    holder = _holder(backend)
    holder.record_verified("mine", package_version="6.13.0")
    assert holder.verified_rungs() == frozenset({"prior", "mine"})


def test_completions_overlays_memory_over_backend() -> None:
    """A re-verification recorded this process is the newest fact — the
    in-memory record wins over a stale backend row for the same rung."""
    backend = ScriptedBackend(
        records={
            "x": CompletionRecord(rung_name="x", verified_at="old", package_version="6.12.0")
        }
    )
    holder = _holder(backend)
    holder.record_verified("x", package_version="6.13.0", detail="re-verified")
    record = holder.completions()["x"]
    assert record.package_version == "6.13.0"
    assert record.verified_at == "held-t"
    assert record.detail == "re-verified"


# ── Engine down: the RF-186-2 obligation ─────────────────────────────────────


def test_record_while_backend_down_is_held_in_memory_not_raised() -> None:
    backend = ScriptedBackend(down=True)
    holder = _holder(backend)
    holder.record_verified("a", package_version="6.13.0")
    assert backend.record_calls == 1  # write-through was attempted (non-vacuous)
    assert backend.records == {}
    assert holder.verified_rungs() == frozenset({"a"})
    assert holder.unflushed().keys() == {"a"}


def test_reads_while_backend_down_serve_memory_without_raising() -> None:
    backend = ScriptedBackend(down=True)
    holder = _holder(backend)
    holder.record_verified("a", package_version="6.13.0")
    assert holder.verified_rungs() == frozenset({"a"})
    assert holder.completions()["a"].rung_name == "a"
    assert backend.read_calls >= 1  # the backend was genuinely consulted


def test_unflushed_tracks_only_records_that_missed_the_backend() -> None:
    """The .12 flush seam: a record that failed write-through stays owed;
    one that reached the backend does not."""
    backend = ScriptedBackend(down=True)
    holder = _holder(backend)
    holder.record_verified("missed", package_version="6.13.0")
    backend.down = False
    holder.record_verified("landed", package_version="6.13.0")
    assert holder.unflushed().keys() == {"missed"}
    assert "landed" in backend.records


def test_position_derives_from_the_union_through_the_single_algorithm() -> None:
    """The holder grows NO position surface of its own (Gap-4 mechanism 1:
    ``ladder_position`` is defined only in completion.py); position over
    the union view goes through ``derive_ladder_position``."""
    backend = ScriptedBackend(
        records={
            "a": CompletionRecord(rung_name="a", verified_at="t", package_version="6.12.0")
        }
    )
    holder = _holder(backend)
    holder.record_verified("c", package_version="6.13.0")  # hole at "b"
    assert derive_ladder_position(holder.verified_rungs(), ("a", "b", "c")) == 1
    holder.record_verified("b", package_version="6.13.0")
    assert derive_ladder_position(holder.verified_rungs(), ("a", "b", "c")) == 3
    # No position surface, no setter — the RDR-185 Gap-4 mechanism-1 shape.
    assert not hasattr(holder, "ladder_position")
    assert not hasattr(holder, "set_ladder_position")


# ── Protocol/implementation agreement (reviewer High + critic NO-GO fix) ─────


def test_minimal_protocol_backend_is_fully_served() -> None:
    """The holder's real backend dependency must be exactly the
    ``CompletionLedger`` Protocol surface. A backend implementing ONLY the
    declared methods (the reasonable .12 reading) must have its records
    served, not silently discarded as 'backend down'."""

    class MinimalLedger:
        """Exactly the CompletionLedger surface — nothing else."""

        def __init__(self) -> None:
            self._records: dict[str, CompletionRecord] = {
                "existing": CompletionRecord(
                    rung_name="existing", verified_at="t", package_version="6.12.0"
                )
            }

        def record_verified(
            self, rung_name: str, *, package_version: str, detail: str = ""
        ) -> None:
            self._records[rung_name] = CompletionRecord(
                rung_name=rung_name,
                verified_at="t",
                package_version=package_version,
                detail=detail,
            )

        def verified_rungs(self) -> frozenset[str]:
            return frozenset(self._records)

        def completions(self) -> dict[str, CompletionRecord]:
            return dict(self._records)

    backend = MinimalLedger()
    assert isinstance(backend, CompletionLedger)
    holder = InProcessCompletionHolder(backend, now_fn=lambda: "held-t")
    assert holder.verified_rungs() == frozenset({"existing"})
    assert holder.completions()["existing"].package_version == "6.12.0"


def test_backend_missing_a_protocol_method_fails_loud() -> None:
    """A backend that does NOT conform to the Protocol is a programming
    error, not an outage: the read must raise, never be swallowed into the
    'backend down' warning path (the silent split-brain the 2026-07-18
    review flagged)."""

    class NonConformant:
        def record_verified(
            self, rung_name: str, *, package_version: str, detail: str = ""
        ) -> None:
            pass

        def verified_rungs(self) -> frozenset[str]:
            return frozenset()

        # completions() deliberately missing

    holder = InProcessCompletionHolder(NonConformant(), now_fn=lambda: "held-t")  # type: ignore[arg-type]
    assert holder.verified_rungs() == frozenset()  # declared surface: fine
    with pytest.raises(AttributeError):
        holder.completions()  # missing Protocol method: LOUD, not "engine down"


# ── Through the runner: the bead's TDD spec ──────────────────────────────────


def test_two_rung_walk_engine_down_later_rung_sees_earlier_via_holder() -> None:
    """Bead spec: two-rung walk, engine down for rung 1 → rung 2's
    ``verified_rungs()`` read (and the end-of-walk position derivation)
    sees rung 1 from the in-process holder. Position == 2 is load-bearing:
    it requires rung 1 in the contiguous verified prefix, which only the
    holder can serve while the backend is down."""
    backend = ScriptedBackend(down=True)
    holder = _holder(backend)
    one = ScriptedRung("one")
    two = ScriptedRung("two", done=1)  # already converged: exercises the :221 read mid-walk
    report = LadderRunner(
        LadderRegistry((one, two)), holder, package_version_fn=lambda: "6.13.0"
    ).run()
    assert _outcomes(report) == [("one", RungOutcome.RECORDED), ("two", RungOutcome.RECORDED)]
    assert report.converged
    assert report.position == 2
    assert backend.records == {}  # nothing reached the backend
    assert backend.record_calls == 2  # write-through attempted for both (non-vacuous)
    assert holder.unflushed().keys() == {"one", "two"}


def test_holder_is_consulted_second_walk_skips_redundant_verify() -> None:
    """The mutation-killing assert: bypass the holder's in-memory overlay
    and this fails — with the backend down, a second walk would find no
    record, pay a redundant ``verify()`` (verify_calls == 2), and report
    RECORDED instead of ALREADY_RECORDED. The walk still converges either
    way (tolerable degradation); the holder being consulted is what these
    exact counts pin."""
    backend = ScriptedBackend(down=True)
    holder = _holder(backend)
    rung = ScriptedRung("a")
    runner = LadderRunner(LadderRegistry((rung,)), holder, package_version_fn=lambda: "6.13.0")

    first = runner.run()
    assert _outcomes(first) == [("a", RungOutcome.RECORDED)]
    assert rung.verify_calls == 1

    second = runner.run()
    assert _outcomes(second) == [("a", RungOutcome.ALREADY_RECORDED)]
    assert rung.verify_calls == 1  # no redundant verify: the holder served the record
    assert second.position == 1


# ── Over the real CompletionStore: production wiring shape ───────────────────


def test_holder_over_real_completion_store_is_behaviorally_transparent(
    tmp_path: pathlib.Path,
) -> None:
    """The upgrade-command wiring wraps ``CompletionStore`` in the holder;
    with the backend up this must be a behavioral no-op: records land
    durably and a fresh store (new process) sees them."""
    db = tmp_path / "ladder.db"
    with CompletionStore(db, now_fn=lambda: "t0") as store:
        holder = InProcessCompletionHolder(store)
        rung = ScriptedRung("a")
        report = LadderRunner(
            LadderRegistry((rung,)), holder, package_version_fn=lambda: "6.13.0"
        ).run()
        assert _outcomes(report) == [("a", RungOutcome.RECORDED)]
        assert holder.unflushed() == {}
    with CompletionStore(db, now_fn=lambda: "t1") as fresh:
        assert fresh.verified_rungs() == frozenset({"a"})


def test_holder_never_swallows_memory_state_on_backend_recovery() -> None:
    """Backend comes back mid-process: reads union both sides; the
    memory-held record is still served even though the backend never
    received it (flushing is .12's job, not a read side effect)."""
    backend = ScriptedBackend(down=True)
    holder = _holder(backend)
    holder.record_verified("held", package_version="6.13.0")
    backend.down = False
    backend.records["durable"] = CompletionRecord(
        rung_name="durable", verified_at="t", package_version="6.12.0"
    )
    assert holder.verified_rungs() == frozenset({"held", "durable"})
    assert holder.unflushed().keys() == {"held"}
    assert "held" not in backend.records  # reads did not sneak a flush in


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
