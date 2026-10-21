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

import sys

import pytest

from tests._xdist_cap import (
    SEGMENTS_PER_WORKER,
    cap_from_headroom,
    clamp_numprocesses,
)
from tests._module_seam import setattr_in


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

        setattr_in(monkeypatch, mod, "subprocess.run", boom)
        assert mod._sysctl_int("kern.sysv.shmmni") is None
        assert mod.segments_in_use() is None
        assert mod.effective_cap() is None

    def test_garbage_sysctl_output_yields_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests._xdist_cap as mod

        class R:
            returncode = 0
            stdout = "not-a-number\n"

        setattr_in(monkeypatch, mod, "subprocess.run", lambda *a, **k: R())
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


class TestMemoryCap:
    """nexus-vo9qn. The run's tmpfs decision is final, so the worker count must
    keep the grant from filling: grant memory / a measured per-worker budget."""

    GIB = 1024**3

    def test_the_budget_covers_the_measured_per_worker_peak(self) -> None:
        from tests._xdist_cap import BYTES_PER_WORKER

        # lab-l, 48 GB, 16 workers peaked 39.4 GB anon incl. shmem (labrun sampler)
        assert BYTES_PER_WORKER >= 39.4e9 / 16
        # lab-m, 16 GB, -n 8 peaked 14.5 GB: the budget must not exceed what fits
        assert BYTES_PER_WORKER >= 14.5e9 / 8

    @pytest.mark.parametrize(
        "cls,grant_gib,workers",
        [("lab-s", 4, 1), ("lab-m", 16, 6), ("lab-6c18g", 18, 7), ("lab-l", 48, 19), ("lab-xl", 96, 38)],
    )
    def test_cap_per_lab_class(self, cls: str, grant_gib: int, workers: int) -> None:
        from tests._xdist_cap import cap_from_memory

        assert cap_from_memory(grant_bytes=grant_gib * self.GIB) == workers

    def test_lab_l_keeps_its_sixteen_workers_and_lab_m_loses_two(self) -> None:
        from tests._xdist_cap import cap_from_memory

        assert clamp_numprocesses(requested=16, cap=cap_from_memory(grant_bytes=48 * self.GIB))[1] is None
        assert clamp_numprocesses(requested=8, cap=cap_from_memory(grant_bytes=16 * self.GIB))[0] == 6

    @pytest.mark.parametrize(("cls", "grant_gib", "workers"), [("lab-mac-s", 4, 1), ("lab-mac-m", 10, 4)])
    def test_cap_per_mac_lab_class(self, cls: str, grant_gib: int, workers: int) -> None:
        from tests._xdist_cap import cap_from_memory

        assert cap_from_memory(grant_bytes=grant_gib * self.GIB) == workers

    def test_the_clamp_note_names_tmpfs_only_on_linux(self) -> None:
        # nexus-pnyfq: macOS lab grants carry LAB_MEM_MB, but their clusters are not on tmpfs.
        from tests._xdist_cap import memory_clamp_note

        linux = memory_clamp_note(requested=8, cap=6, platform="linux")
        mac = memory_clamp_note(requested=8, cap=4, platform="darwin")
        assert "tmpfs" in linux and "8 -> 6" in linux
        assert "tmpfs" not in mac and "no cgroups" in mac and "8 -> 4" in mac

    def test_never_below_one(self) -> None:
        from tests._xdist_cap import cap_from_memory

        assert cap_from_memory(grant_bytes=1) == 1

    def test_grant_is_the_smaller_of_lab_mem_and_the_cgroup(self) -> None:
        from tests._xdist_cap import grant_memory_bytes

        env = {"LAB_MEM_MB": "16384"}
        assert grant_memory_bytes(env=env, cgroup_limit=None) == 16 * self.GIB
        assert grant_memory_bytes(env=env, cgroup_limit=8 * self.GIB) == 8 * self.GIB
        assert grant_memory_bytes(env={}, cgroup_limit=12 * self.GIB) == 12 * self.GIB
        assert grant_memory_bytes(env={}, cgroup_limit=None) is None

    @pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
    def test_a_garbled_lab_mem_is_ignored(self, raw: str) -> None:
        from tests._xdist_cap import grant_memory_bytes

        assert grant_memory_bytes(env={"LAB_MEM_MB": raw}, cgroup_limit=None) is None

    def test_no_limit_visible_means_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests._engine_substrate as sub
        import tests._xdist_cap as mod

        monkeypatch.delenv("LAB_MEM_MB", raising=False)
        monkeypatch.setattr(sub, "cgroup_memory_limit", lambda: None)
        assert mod.effective_memory_cap() is None

    def test_a_visible_grant_caps_and_the_opt_out_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests._engine_substrate as sub
        import tests._xdist_cap as mod

        monkeypatch.setenv("LAB_MEM_MB", str(16 * 1024))
        monkeypatch.setattr(sub, "cgroup_memory_limit", lambda: None)
        assert mod.effective_memory_cap() == 6
        monkeypatch.setenv(mod.NO_CAP_ENV, "1")
        assert mod.effective_memory_cap() is None

    def test_a_broken_probe_yields_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests._engine_substrate as sub
        import tests._xdist_cap as mod

        def boom():
            raise OSError("no cgroup")

        monkeypatch.setattr(sub, "cgroup_memory_limit", boom)
        monkeypatch.delenv(mod.NO_CAP_ENV, raising=False)
        assert mod.effective_memory_cap() is None

    def test_conftest_clamps_to_the_memory_cap_and_says_why(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        import tests._xdist_cap as mod
        from tests.conftest import _clamp_xdist_workers

        monkeypatch.setattr(mod, "effective_cap", lambda: None)
        monkeypatch.setattr(mod, "effective_memory_cap", lambda: 6)

        class Opt:
            numprocesses = 8

        class Cfg:
            option = Opt()

        cfg = Cfg()
        _clamp_xdist_workers(cfg)
        assert cfg.option.numprocesses == 6
        out = capsys.readouterr().out
        assert "8 -> 6" in out and "NX_XDIST_NO_CAP" in out
        # nexus-pnyfq: the tmpfs explanation is Linux's; a macOS grant has no tmpfs clusters.
        assert ("tmpfs" in out) == sys.platform.startswith("linux"), out

    def test_conftest_leaves_a_request_within_the_memory_cap(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import tests._xdist_cap as mod
        from tests.conftest import _clamp_xdist_workers

        monkeypatch.setattr(mod, "effective_cap", lambda: None)
        monkeypatch.setattr(mod, "effective_memory_cap", lambda: 16)

        class Opt:
            numprocesses = 16

        class Cfg:
            option = Opt()

        cfg = Cfg()
        _clamp_xdist_workers(cfg)
        assert cfg.option.numprocesses == 16


class TestSetWorkerCount:
    """nexus-vo9qn. xdist 3.8.0 turns ``-n N`` into ``option.tx = ["popen"] * N``
    in ``pytest_cmdline_main``, before conftest's ``pytest_configure``, and spawns
    ``len(tx)`` workers. A clamp that sets ``numprocesses`` only is inert."""

    def test_the_generated_tx_list_follows_the_count(self) -> None:
        from tests._xdist_cap import set_worker_count

        class Opt:
            numprocesses = 4
            tx = ["popen"] * 4

        opt = Opt()
        set_worker_count(opt, 2)
        assert opt.numprocesses == 2 and opt.tx == ["popen", "popen"]

    def test_a_hand_written_tx_is_left_alone(self) -> None:
        from tests._xdist_cap import set_worker_count

        class Opt:
            numprocesses = 4
            tx = ["popen", "ssh=box"]

        opt = Opt()
        set_worker_count(opt, 2)
        assert opt.numprocesses == 2 and opt.tx == ["popen", "ssh=box"]


# A tiny rootdir whose conftest runs the REAL ``_clamp_xdist_workers`` at the real
# ``pytest_configure`` time, against real xdist; ``setup`` fakes the box.
_PROBE_CONFTEST = '''
{setup}
from tests.conftest import _clamp_xdist_workers


def pytest_configure(config):
    _clamp_xdist_workers(config)
'''
_PROBE_TEST = '''
import pytest


@pytest.mark.parametrize("i", range(8))
def test_p(i):
    pass
'''


def _run_real_xdist(tmp_path, *, setup: str, env_extra: dict[str, str], n: int):
    """Run a real ``pytest -n N`` (real xdist workers) in a scratch rootdir and
    return ``(created, total, output)`` from xdist's "created: A/B workers"."""
    import os
    import re
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    (tmp_path / "conftest.py").write_text(_PROBE_CONFTEST.format(setup=setup))
    (tmp_path / "test_probe.py").write_text(_PROBE_TEST)
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("LAB_MEM_MB", "NX_XDIST_NO_CAP", "PYTEST_ADDOPTS")
    }
    env["PYTHONPATH"] = str(repo)
    env["NX_TEST_T2_SUBSTRATE"] = "none"  # the child needs no engine (nexus-fam6l)
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=",
         "-n", str(n), "-v", f"--basetemp={tmp_path / 'bt'}", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=240,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    m = re.search(r"created: (\d+)/(\d+) workers", out)
    assert m, out
    return int(m.group(1)), int(m.group(2)), out


class TestClampsReallyReduceWorkers:
    """The fake-Cfg tests above cannot see that the clamp is inert; only a real
    xdist run can. Each asserts the worker count xdist actually created."""

    def test_the_memory_cap_creates_fewer_workers(self, tmp_path) -> None:
        # 5200 MiB grant / 2560 MiB per worker = 2 workers (a tighter cgroup can
        # only lower it).
        created, total, out = _run_real_xdist(
            tmp_path, setup="", env_extra={"LAB_MEM_MB": "5200"}, n=4,
        )
        assert "clamped 4 ->" in out
        assert 1 <= created <= 2 and created == total

    def test_the_sysv_cap_creates_fewer_workers(self, tmp_path) -> None:
        # 32 segments, 22 in use, reserve 8 -> room for 2 workers.
        setup = (
            "import tests._xdist_cap as c\n"
            "c._sysctl_int = lambda name: 32\n"
            "c.segments_in_use = lambda: 22\n"
        )
        created, total, out = _run_real_xdist(tmp_path, setup=setup, env_extra={}, n=4)
        assert "SysV shared memory" in out
        assert created == total == 2
