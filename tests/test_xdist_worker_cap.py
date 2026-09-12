"""The xdist worker cap: keep a parallel run inside the box's SysV shared-memory budget.

nexus-6qp25. Every pytest process boots its own engine substrate
(``tests/_engine_substrate.ensure_engine`` is "once per process"), and every
substrate is one Postgres cluster holding exactly one SysV shared-memory
segment. So concurrent xdist workers consume segments one-for-one against
``kern.sysv.shmmni``, and so does every OTHER cluster on the box: a running
gate, or a cluster orphaned by a killed battery.

When the budget runs out ``initdb`` fails with ``shmget: No space left on
device`` and EVERY substrate-backed test errors at SETUP. The run does not
report a test failure; it reports thousands of setup errors, which reads as
catastrophic breakage rather than as resource contention. One measured
instance cost 8988 errors and a diagnosis (2026-09-12).

MEASURED on the 16-core box this was written for, rather than assumed:

===========================  =========================================
one cluster                  1 segment, 56 bytes (PG keeps only a tiny
                             SysV interlock; real buffers are mmap)
``-n 4``, substrate tests    4 postmasters, 4 segments
``-n 16`` over ``tests/db/`` PEAK 21 segments, sampled every 1s through
                             boot; 1657 passed, zero shmget errors
``-n 16`` FULL SUITE         PEAK 20 segments; 18792 passed in 12m46s,
                             zero shmget errors
``kern.sysv.shmmni``         32
``kern.sysv.shmall``         16 MB total -- not binding at 56 B/segment
``kern.sysv.shmseg``         8 per process -- not binding, 1 each
===========================  =========================================

WHAT THIS CAP DOES AND DOES NOT DO — stated plainly, because the measurements
do not support the stronger claim.

The full-suite run settles the question left open when this was written: the
suite spawns NO extra clusters beyond one per worker, so 16 workers costs
about 20 of 32 and passes cleanly. Parallelism alone therefore never was the
cause, and no cap keyed on worker count would have prevented the original
failure without throttling everyone to roughly half speed for a problem they
did not cause.

That failure is NOT fully explained by anything measured here. It needed the
budget consumed by something else, and four orphaned clusters were live when
it ran (a battery killed mid-flight leaves its clusters behind; one had been
orphaned nearly three days). 16 + 4 + baseline is still under 32 by this
arithmetic, so something further was present that was not captured. Saying so
is better than picking a reserve that makes the story close.

So this cap is a HEADROOM GUARD, not a reproduction of the incident. It binds
only when the budget genuinely is low — which is correct behaviour whatever
consumed it — and on a quiet box it computes to the core count and changes
nothing. The primary fix for the measured contributor is not here: it is
making a killed battery reap its own clusters.
"""

from __future__ import annotations

import pytest

from tests._xdist_cap import (
    SEGMENTS_PER_WORKER,
    cap_from_headroom,
    clamp_numprocesses,
)


class TestCapFromHeadroom:
    """The derivation, over explicit inputs — no live kernel, no live box."""

    def test_headroom_is_ceiling_minus_in_use_minus_reserve(self) -> None:
        # 32 total, 4 already taken, reserve 8 => 20 free, 1 segment each.
        assert cap_from_headroom(shmmni=32, in_use=4, reserve=8, cpu_count=64) == 20

    def test_cpu_count_caps_it_when_the_budget_is_generous(self) -> None:
        # No point running more workers than cores even with segments to spare.
        assert cap_from_headroom(shmmni=4096, in_use=0, reserve=8, cpu_count=16) == 16

    def test_orphaned_clusters_shrink_the_cap(self) -> None:
        """The measured cause of the 2026-09-12 failure: a shared budget.

        Four orphaned clusters were live when the failing run started. A cap
        that ignored them would hand out the same number of workers as on a
        quiet box, which is exactly the bug.
        """
        quiet = cap_from_headroom(shmmni=32, in_use=4, reserve=8, cpu_count=16)
        with_orphans = cap_from_headroom(shmmni=32, in_use=12, reserve=8, cpu_count=16)
        assert with_orphans < quiet

    def test_never_returns_less_than_one(self) -> None:
        """A fully-consumed budget still has to run the suite, serially."""
        assert cap_from_headroom(shmmni=32, in_use=40, reserve=8, cpu_count=16) == 1

    def test_a_raised_shmmni_raises_the_cap(self) -> None:
        """Someone who deliberately raised their limits is not clamped.

        This is what makes the clamp defensible rather than paternalistic:
        it is computed from the box's real capacity, so it only binds a box
        that genuinely cannot take the parallelism asked for.
        """
        assert cap_from_headroom(shmmni=256, in_use=4, reserve=8, cpu_count=64) > 20

    def test_segments_per_worker_is_one_as_measured(self) -> None:
        """Pins the measurement the whole derivation rests on.

        If a future Postgres allocates more than one SysV segment per
        cluster this constant is wrong and every cap above is too generous,
        so the number is pinned here rather than left implicit in the
        arithmetic.
        """
        assert SEGMENTS_PER_WORKER == 1


class TestClampNumprocesses:
    """The clamp itself, over a fake option object."""

    def test_a_request_within_the_cap_is_untouched(self) -> None:
        assert clamp_numprocesses(requested=4, cap=20) == (4, None)

    def test_a_request_over_the_cap_is_clamped_and_explains_itself(self) -> None:
        got, note = clamp_numprocesses(requested=16, cap=6)
        assert got == 6
        assert note is not None
        # The note has to name the cause, because the failure it prevents is
        # a wall of setup errors that reads as a code regression.
        assert "shared memory" in note.lower()
        assert "NX_XDIST_NO_CAP" in note

    def test_none_means_no_xdist_and_is_left_alone(self) -> None:
        """No -n at all: a single process, one cluster, nothing to clamp."""
        assert clamp_numprocesses(requested=None, cap=6) == (None, None)

    def test_zero_is_left_alone(self) -> None:
        """``-n 0`` disables xdist; clamping it to 1 would silently enable it."""
        assert clamp_numprocesses(requested=0, cap=6) == (0, None)

    @pytest.mark.parametrize("requested", [1, 2, 6])
    def test_at_or_below_the_cap_is_never_raised(self, requested: int) -> None:
        """The cap is a ceiling, never a floor — it never ADDS parallelism."""
        got, note = clamp_numprocesses(requested=requested, cap=6)
        assert got == requested
        assert note is None


class TestDegradesRatherThanCrashes:
    """This code runs in EVERY pytest invocation in this repo, including CI.

    So its failure mode has to be "no cap applied", never "the run dies".
    A cap that can raise takes down the runs it was meant to protect, plus
    every run it was irrelevant to (nexus-19's review point, 2026-09-12).
    """

    def test_unreadable_sysctl_yields_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests._xdist_cap as mod

        monkeypatch.setattr(mod, "_sysctl_int", lambda name: None)
        assert mod.effective_cap() is None

    def test_unreadable_ipcs_yields_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Crucially NOT "assume zero in use" — assuming the budget is free is
        the one wrong direction, since a busy box is exactly when it binds."""
        import tests._xdist_cap as mod

        monkeypatch.setattr(mod, "_sysctl_int", lambda name: 32)
        monkeypatch.setattr(mod, "segments_in_use", lambda: None)
        assert mod.effective_cap() is None

    def test_sysctl_raising_yields_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests._xdist_cap as mod

        def boom(*a, **k):
            raise OSError("no sysctl on this box")

        monkeypatch.setattr(mod.subprocess, "run", boom)
        assert mod._sysctl_int("kern.sysv.shmmni") is None
        assert mod.segments_in_use() is None
        assert mod.effective_cap() is None

    def test_garbage_sysctl_output_yields_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests._xdist_cap as mod

        class R:
            returncode = 0
            stdout = "not-a-number\n"

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
        assert mod._sysctl_int("kern.sysv.shmmni") is None

    def test_env_opt_out_yields_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests._xdist_cap as mod

        monkeypatch.setenv(mod.NO_CAP_ENV, "1")
        assert mod.effective_cap() is None

    @pytest.mark.parametrize(
        "shmmni,in_use,reserve,cpu",
        [(0, 0, 8, 16), (32, 999, 8, 16), (-5, 0, 8, 16), (32, 0, 999, 16), (32, 0, 8, 0)],
    )
    def test_absurd_inputs_never_yield_a_nonpositive_cap(
        self, shmmni: int, in_use: int, reserve: int, cpu: int
    ) -> None:
        """A cap of 0 or below would disable the suite or crash xdist."""
        assert cap_from_headroom(
            shmmni=shmmni, in_use=in_use, reserve=reserve, cpu_count=cpu
        ) >= 1

    def test_conftest_hook_swallows_a_broken_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The wiring itself must not propagate an exception into collection."""
        import tests._xdist_cap as mod
        from tests.conftest import _clamp_xdist_workers

        def boom():
            raise RuntimeError("derivation exploded")

        monkeypatch.setattr(mod, "effective_cap", boom)

        class Opt:
            numprocesses = 16

        class Cfg:
            option = Opt()

        cfg = Cfg()
        _clamp_xdist_workers(cfg)  # must not raise
        assert cfg.option.numprocesses == 16  # and must not have changed anything
