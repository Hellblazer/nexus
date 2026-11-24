# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rule (e) of generation gc: a live interpreter's lease keeps its generation.

The lease exists for what the process census cannot see: a process started
through ``tools/current/...`` (its argv never names ``gen-<stamp>``), one that
rewrote its title, one whose argv was cut. EVERY rule test below is therefore
census-blind: it either hands ``plan`` a snapshot that omits the child, or starts
the child through ``current/bin/python`` and flips the pointer away. A test whose
child the census could see passes with rule (e) deleted, which is the vacuous-gate
shape (nexus-moht0); the mutation run recorded on the change deletes rule (e) and
every census-blind test here goes red.
"""
from __future__ import annotations

import errno
import json
import os
import sys
from pathlib import Path

import pytest

from nexus._install import gc_core, lease_core
from tests._lease_support import NT, blind_snapshot, holder, make_generation, make_tools, venv_python
from tests._module_seam import setattr_in

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="patches fcntl.flock")


def _actions(plan) -> dict[str, str]:
    return {entry.name[len("gen-"):]: action for action, entry, _d in plan}


def _details(plan) -> dict[str, str]:
    return {entry.name[len("gen-"):]: detail for _a, entry, detail in plan}


def _plan(tools: Path, **kw):
    kw.setdefault("snapshot", blind_snapshot())
    kw.setdefault("keep", 1)
    # grace 0: nothing here is a build in progress.
    kw.setdefault("grace_minutes", 0)
    return gc_core.plan(tools, **kw)


def _records(config: Path) -> list[dict]:
    path = config / "logs" / gc_core.GC_LOG_NAME
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    return cfg


# ── the rule, against a census that cannot see the child ─────────────────────

def test_a_leased_generation_the_census_cannot_see_is_kept(tmp_path: Path) -> None:
    tools, (a, b, c, d) = make_tools(tmp_path)
    with holder(venv_python(a)):
        plan = _plan(tools)  # the injected table lists gc only: the census sees nobody
        assert _actions(plan) == {
            "20261101T000000Z": "keep", "20261102T000000Z": "reap",
            "20261103T000000Z": "reap", "20261104T000000Z": "skip",
        }, plan
        assert "lease" in _details(plan)["20261101T000000Z"]
        # Non-vacuity: an idle leased sibling IS reaped, so "keep" came from the live lease.
        assert gc_core.census_state(blind_snapshot())[0]


def test_a_killed_holder_frees_its_generation(tmp_path: Path) -> None:
    tools, (a, *_rest) = make_tools(tmp_path)
    with holder(venv_python(a)):
        assert _actions(_plan(tools))["20261101T000000Z"] == "keep"
    assert _actions(_plan(tools))["20261101T000000Z"] == "reap"


@pytest.mark.skipif(NT, reason="current is a symlink here; the junction reading is TestRealWindows")
def test_a_process_started_through_current_keeps_its_generation_after_a_flip(tmp_path: Path) -> None:
    """The real census, not an injected one: its argv names ``current``, which the
    census never matches, and after the flip ``current`` points elsewhere."""
    tools, (a, b, c, d) = make_tools(tmp_path)
    current = tools / "current"
    current.symlink_to(a, target_is_directory=True)
    with holder(current / "bin" / "python"):
        current.unlink()
        current.symlink_to(d, target_is_directory=True)
        plan = gc_core.plan(tools, keep=1, grace_minutes=0)  # real ps
        assert gc_core._census().generation_holder_pids(a, snapshot=gc_core._census().ps_snapshot()) == [], (
            "the census sees the child, so this test would pass without rule (e)"
        )
        assert _actions(plan)["20261101T000000Z"] == "keep", plan
        assert _actions(plan)["20261102T000000Z"] == "reap", plan


def test_the_lease_and_the_census_are_ored(tmp_path: Path) -> None:
    """A free lease does not override a census holder (an unleased process: python -S)."""
    tools, (a, *_rest) = make_tools(tmp_path)
    with holder(venv_python(a), extra=("-S",)) as child:
        snapshot = blind_snapshot() + f"{child.pid} {venv_python(a).as_posix()} -S -c x\n"  # rows are slash-spelt (census windows reading)
        assert lease_core.probe(a) == "free"
        plan = _plan(tools, snapshot=snapshot)
        assert _actions(plan)["20261101T000000Z"] == "keep"
        assert "held by" in _details(plan)["20261101T000000Z"]


def test_a_generation_with_no_lease_file_is_judged_by_the_census_alone(tmp_path: Path) -> None:
    tools, (a, b, c, d) = make_tools(tmp_path, leased=False)
    assert lease_core.probe(a) == "absent"
    assert _actions(_plan(tools))["20261101T000000Z"] == "reap"
    assert _actions(_plan(tools, snapshot=blind_snapshot() + f"4242 {venv_python(a).as_posix()} -c x\n"))[
        "20261101T000000Z"] == "keep"


def test_a_failed_census_still_keeps_every_complete_generation(tmp_path: Path) -> None:
    tools, _gens = make_tools(tmp_path)
    plan = _plan(tools, snapshot="", census_ok=False)
    assert {a for a in _actions(plan).values()} <= {"keep", "skip"}, plan


@pytest.mark.skipif(NT, reason="current is a symlink here")
def test_only_would_be_reaped_entries_are_probed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Probing takes the exclusive lock for a moment; it stays off current,
    previous, the keep window and the running installer's generation."""
    tools, (a, b, c, d) = make_tools(tmp_path)
    make_generation(tools, "20261105T000000Z")  # the keep window (keep=1)
    (tools / "current").symlink_to(a, target_is_directory=True)  # protects a
    probed: list[str] = []

    def spy(gen, platform=None):
        probed.append(Path(gen).name)
        return "free"

    monkeypatch.setattr(lease_core, "probe", spy)
    _plan(tools, keep=1, self_generation=str(b))  # protects b
    assert sorted(probed) == ["gen-20261103T000000Z", "gen-20261104T000000Z"], probed


def test_receipt_less_wreckage_with_a_free_lease_is_reaped(tmp_path: Path) -> None:
    """A build killed after the lease was written and before the receipt."""
    tools, gens = make_tools(tmp_path)
    wreck = make_generation(tools, "20261100T000000Z", receipt=False)
    assert lease_core.lease_path(wreck).is_file()
    assert _actions(_plan(tools))["20261100T000000Z"] == "reap"


# ── errors ───────────────────────────────────────────────────────────────────

def _flock_raises(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    def boom(fd, op):
        raise OSError(code, os.strerror(code))

    setattr_in(monkeypatch, lease_core, "fcntl.flock", boom)


@POSIX
def test_a_filesystem_without_flock_falls_back_to_the_census(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, (a, *_rest) = make_tools(tmp_path)
    _flock_raises(monkeypatch, errno.ENOLCK)
    assert lease_core.probe(a) == "unsupported"
    assert _actions(_plan(tools))["20261101T000000Z"] == "reap"
    assert _actions(_plan(tools, snapshot=blind_snapshot() + f"4242 {venv_python(a).as_posix()} -c x\n"))[
        "20261101T000000Z"] == "keep"


@POSIX
@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP])
def test_every_lock_unsupported_errno_is_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    tools, (a, *_rest) = make_tools(tmp_path)
    _flock_raises(monkeypatch, code)
    assert lease_core.probe(a) == "unsupported"


@POSIX
def test_any_other_lock_error_keeps_the_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, (a, *_rest) = make_tools(tmp_path)
    _flock_raises(monkeypatch, errno.EACCES)
    assert lease_core.probe(a) == "error:EACCES"
    plan = _plan(tools)
    assert _actions(plan)["20261101T000000Z"] == "keep"
    assert "EACCES" in _details(plan)["20261101T000000Z"]


def test_an_unknown_lease_version_keeps_the_generation(tmp_path: Path) -> None:
    tools, (a, *_rest) = make_tools(tmp_path)
    lease_core.lease_path(a).write_text("nx-lease 9\n")
    assert lease_core.probe(a).startswith("error:")
    assert _actions(_plan(tools))["20261101T000000Z"] == "keep"


def test_an_unopenable_lease_path_keeps_the_generation(tmp_path: Path) -> None:
    tools, (a, *_rest) = make_tools(tmp_path)
    lease_core.lease_path(a).unlink()
    lease_core.lease_path(a).mkdir()
    assert lease_core.probe(a).startswith("error:")
    assert _actions(_plan(tools))["20261101T000000Z"] == "keep"


# ── the reap re-takes the lock, and holds it through the delete ──────────────

def test_a_lease_taken_between_plan_and_reap_keeps_the_generation(tmp_path: Path, config: Path) -> None:
    tools, (a, *_rest) = make_tools(tmp_path)
    started: list = []

    def start_holder(entry: Path) -> None:
        if entry == a and not started:
            ctx = holder(venv_python(a))
            ctx.__enter__()
            started.append(ctx)

    try:
        lines = gc_core.gc_generations(
            tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0, before_reap=start_holder,
        )
        assert started, "the seam never ran"
        assert a.is_dir() and (a / "pyvenv.cfg").is_file(), "gc deleted a leased generation"
        assert not any(line.startswith(f"reaped {a}") for line in lines), lines
        events = [(r["event"], r.get("generation")) for r in _records(config)]
        assert ("gc_reap_kept", str(a)) in events, events
        assert ("gc_reaped", str(a)) not in events
    finally:
        started[0].__exit__(None, None, None)


def test_the_lock_is_held_while_the_tree_is_deleted(tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, (a, *_rest) = make_tools(tmp_path)
    during: list[str] = []
    real = gc_core.shutil.rmtree

    def watching(path, *args, **kw):
        if Path(path) == a:
            during.append(lease_core.probe(a))
        return real(path, *args, **kw)

    setattr_in(monkeypatch, gc_core, "shutil.rmtree", watching)
    if NT:
        # The Windows reap is the custom walk, not rmtree: probe from inside it.
        real_walk = gc_core._remove_all_but_lease

        def walking(root, lease_name, on_error):
            during.append(lease_core.probe(a))
            return real_walk(root, lease_name, on_error)

        monkeypatch.setattr(gc_core, "_remove_all_but_lease", walking)
    gc_core.gc_generations(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0)
    assert during and set(during) == {"held"}, during


# ── the record ───────────────────────────────────────────────────────────────

def test_the_run_record_counts_each_lease_state(tmp_path: Path, config: Path) -> None:
    tools, (a, b, c, d) = make_tools(tmp_path)
    lease_core.lease_path(c).unlink()  # absent
    with holder(venv_python(a)):
        gc_core.gc_generations(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0)
    run = [r for r in _records(config) if r["event"] == "gc_run"][-1]
    assert run["lease"] == {"absent": 1, "held": 1, "free": 1, "unsupported": 0, "error": 0}, run
    kept = {k["generation"]: k for k in run["kept"]}
    assert kept[str(a)]["lease"] == "held"
    reaped = [r for r in _records(config) if r["event"] == "gc_reaped"]
    assert {r["lease"] for r in reaped} == {"free", "absent"}, reaped
    assert "lease_unsupported" not in run or not run["lease_unsupported"]


@POSIX
def test_an_unsupported_root_is_recorded_once_per_run(tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, gens = make_tools(tmp_path)
    _flock_raises(monkeypatch, errno.ENOLCK)
    gc_core.gc_generations(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0)
    events = [r["event"] for r in _records(config)]
    assert events.count("lease_unsupported") == 1, events
    run = [r for r in _records(config) if r["event"] == "gc_run"][-1]
    assert run["lease"]["unsupported"] == 3 and run["lease_unsupported"] is True


def test_the_kept_line_names_the_lease(tmp_path: Path, config: Path) -> None:
    tools, (a, *_rest) = make_tools(tmp_path)
    with holder(venv_python(a)):
        lines = gc_core.gc_generations(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0)
    assert any(line.startswith(f"kept {a}: leased:") for line in lines), lines


def test_a_dry_run_probes_and_deletes_nothing(tmp_path: Path) -> None:
    tools, (a, b, *_rest) = make_tools(tmp_path)
    with holder(venv_python(a)):
        lines = gc_core.gc_generations(tools, keep=1, snapshot=blind_snapshot(), grace_minutes=0, dry_run=True)
    assert any(line.startswith(f"kept {a}") for line in lines)
    assert any(line == f"would reap {b}" for line in lines)
    assert a.is_dir() and b.is_dir()
