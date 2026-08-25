# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Generation GC: the only code in this arc that deletes anything.

nexus-utpuw.6 (P2b). Everything else here builds beside, points at, or reports
on. This reaps — so the tests are the deliverable, and they are written as
refusals rather than as coverage.

FOUR NEVER-DELETE RULES, each tested alone AND in combination:
  (a) the generation `current` points at
  (b) the PREVIOUS current — rollback for free (.3 records it)
  (c) any generation with a live holder (.5's census)
  (d) the generation hosting the RUNNING INSTALLER. Under `nx self install`
      (.14) the installer is exec'd from its own generation; keep-last-N
      usually covers this implicitly, and the plan is explicit that implicit
      is not good enough.

THE DATA-LOSS HAZARD IS THE PARENT DIRECTORY. ``~/.local/share/nexus/`` also
holds ``chroma/`` and ``fastembed_cache/`` — documented user data that
``nx uninstall`` deliberately does not remove. A GC that walks the parent
instead of ``tools/gen-*`` deletes a user's vector store. That is asserted
explicitly, with both directories present before and after, because "we only
look at gen-*" is exactly the kind of claim that stays true until someone
changes a glob.

THE BASE INTERPRETER IS NEVER OURS TO TOUCH. Old generations' ``pyvenv.cfg``
``home=`` points at a uv-managed CPython outside the tools tree. Deleting or
pruning it silently breaks every old generation (the pipx#146 / uv#8028 class).
GC never reaches outside ``tools/``, which is what makes that true here; .11
adds the doctor check for when uv prunes it out from under us.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GC = _REPO / "src" / "nexus" / "_install" / "gc.sh"


def _stub_ps(bin_dir: Path, lines: list[str]) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    ps = bin_dir / "ps"
    ps.write_text("#!/bin/sh\ncat <<'PSEOF'\n" + "\n".join(lines) + "\nPSEOF\n")
    ps.chmod(ps.stat().st_mode | stat.S_IXUSR)


def _sh(snippet: str, tools: Path, stub_bin: Path, extra_env: dict | None = None):
    env = {
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tools.parent / "home"),
        "NX_TOOLS_DIR": str(tools),
        "NX_BIN_DIR": str(tools.parent / "bin"),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", f'. "{_GC}"; {snippet}'],
        capture_output=True, text=True, env=env,
    )


def _gen(tools: Path, name: str) -> Path:
    g = tools / f"gen-{name}"
    (g / "bin").mkdir(parents=True)
    (g / "nexus-install.json").write_text("{}")
    (g / "bin" / "nx").write_text("#!/bin/sh\nexit 0\n")
    return g


def _point(tools: Path, link: str, target: Path) -> None:
    p = tools / link
    if p.is_symlink() or p.exists():
        p.unlink()
    p.symlink_to(target)


@pytest.fixture
def env(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub_bin = tmp_path / "stubbin"
    _stub_ps(stub_bin, ["  999 /usr/bin/vim unrelated.txt"])
    return tools, stub_bin


def _names(tools: Path) -> set[str]:
    return {p.name for p in tools.iterdir() if p.name.startswith("gen-")}


def test_gc_is_present() -> None:
    assert _GC.is_file(), f"{_GC} is missing"


# --------------------------------------------------------------------------
# the four never-delete rules, each on its own
# --------------------------------------------------------------------------

def test_rule_a_never_deletes_what_current_points_at(env) -> None:
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(8)]
    _point(tools, "current", gens[0])  # deliberately the OLDEST

    result = _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert result.returncode == 0, result.stderr
    assert gens[0].is_dir(), (
        "the live generation was reaped because it fell outside keep-last-N -- "
        "current is a hard rule, not a tiebreak"
    )


def test_rule_b_never_deletes_the_previous_current(env) -> None:
    """Rollback is free only while the tree it rolls back to still exists."""
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(8)]
    _point(tools, "current", gens[7])
    _point(tools, "previous", gens[0])  # oldest, far outside keep-last-N

    _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert gens[0].is_dir(), "previous was reaped; rollback would point at a hole"


def test_rule_c_never_deletes_a_generation_with_a_live_holder(env) -> None:
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(8)]
    _point(tools, "current", gens[7])
    _stub_ps(stub_bin, [f"  101 {gens[0]}/bin/python {gens[0]}/bin/nx-mcp"])

    _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert gens[0].is_dir(), (
        "a generation a live process is running from was deleted -- that is the "
        "nexus-q3xrx failure the whole epic exists to make impossible"
    )


def test_rule_d_never_deletes_the_generation_running_the_installer(env) -> None:
    """Under `nx self install` (.14) the installer is exec'd from its own
    generation. keep-last-N usually covers it; the plan is explicit that
    implicit is not good enough, because 'usually' is not a rule."""
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(8)]
    _point(tools, "current", gens[7])

    _sh(f'nx_gc_generations --keep 2 --self "{gens[0]}"', tools, stub_bin)

    assert gens[0].is_dir(), "GC reaped the tree it was running from"


# --------------------------------------------------------------------------
# in combination, and the boundary
# --------------------------------------------------------------------------

def test_all_four_rules_together(env) -> None:
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(10)]
    _point(tools, "current", gens[0])
    _point(tools, "previous", gens[1])
    _stub_ps(stub_bin, [f"  101 {gens[2]}/bin/python {gens[2]}/bin/nx"])

    result = _sh(f'nx_gc_generations --keep 2 --self "{gens[3]}"', tools, stub_bin)

    assert result.returncode == 0, result.stderr
    for protected in gens[:4]:
        assert protected.is_dir(), f"{protected.name} was protected and got reaped"
    # keep-last-N protects the newest two independently of the rules above.
    assert gens[9].is_dir() and gens[8].is_dir()


def test_keep_last_n_boundary(env) -> None:
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(5)]
    _point(tools, "current", gens[4])

    _sh("nx_gc_generations --keep 3", tools, stub_bin)

    survivors = _names(tools)
    assert {g.name for g in gens[2:]} <= survivors, "the newest 3 must survive"
    assert gens[0].name not in survivors and gens[1].name not in survivors


def test_a_held_generation_outside_keep_last_n_is_still_retained(env) -> None:
    """The bead names this case specifically: the rules are not a tiebreak
    against keep-last-N, they are absolute."""
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(6)]
    _point(tools, "current", gens[5])
    _stub_ps(stub_bin, [f"  101 {gens[0]}/bin/python {gens[0]}/bin/nx-mcp"])

    _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert gens[0].is_dir()
    assert gens[1].name not in _names(tools), (
        "NON-VACUITY: nothing was reaped at all, so retention proves nothing"
    )


def test_gc_is_a_no_op_on_a_fresh_single_generation_install(env) -> None:
    tools, stub_bin = env
    only = _gen(tools, "00")
    _point(tools, "current", only)

    result = _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert result.returncode == 0, result.stderr
    assert only.is_dir()


# --------------------------------------------------------------------------
# the data-loss hazard
# --------------------------------------------------------------------------

def test_sibling_user_data_is_never_touched(env) -> None:
    """~/.local/share/nexus/ holds chroma/ and fastembed_cache/ — user data
    that uninstall deliberately leaves alone. A GC that walks the parent
    instead of tools/gen-* deletes someone's vector store."""
    tools, stub_bin = env
    parent = tools.parent
    chroma = parent / "chroma"
    (chroma).mkdir()
    (chroma / "chroma.sqlite3").write_text("PRECIOUS")
    cache = parent / "fastembed_cache"
    cache.mkdir()
    (cache / "model.onnx").write_text("PRECIOUS")

    gens = [_gen(tools, f"{i:02d}") for i in range(5)]
    _point(tools, "current", gens[4])

    _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert (chroma / "chroma.sqlite3").read_text() == "PRECIOUS"
    assert (cache / "model.onnx").read_text() == "PRECIOUS"
    assert gens[0].name not in _names(tools), (
        "NON-VACUITY: nothing was reaped, so 'the siblings survived' proves nothing"
    )


def test_non_generation_entries_inside_tools_are_left_alone(env) -> None:
    """Only gen-* is ours. Anything else in tools/ belongs to somebody else,
    including the pointers themselves."""
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(4)]
    _point(tools, "current", gens[3])
    (tools / "notes.txt").write_text("keep me")
    (tools / "some-other-dir").mkdir()

    _sh("nx_gc_generations --keep 1", tools, stub_bin)

    assert (tools / "notes.txt").read_text() == "keep me"
    assert (tools / "some-other-dir").is_dir()
    assert (tools / "current").is_symlink(), "GC removed the pointer it must never touch"


def test_a_receipt_less_directory_is_not_treated_as_a_generation(env) -> None:
    """RG-A handed this to .6: .2 leaves a gen-* directory with no receipt when
    a build dies before writing one. It is wreckage, and it is safe to reap
    precisely because nothing ever pointed `current` at it — but it must not be
    counted toward keep-last-N, or one crashed install shields a real
    generation from retention it is entitled to."""
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(3)]
    _point(tools, "current", gens[2])
    wreckage = tools / "gen-99-crashed"
    (wreckage / "bin").mkdir(parents=True)  # no nexus-install.json

    _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert not wreckage.exists(), "receipt-less wreckage should be reaped"
    assert gens[1].is_dir() and gens[2].is_dir(), (
        "the wreckage was counted toward keep-last-N and shielded nothing real"
    )


# --------------------------------------------------------------------------
# reporting and refusals
# --------------------------------------------------------------------------

def test_gc_reports_what_it_deleted(env) -> None:
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(5)]
    _point(tools, "current", gens[4])

    out = _sh("nx_gc_generations --keep 2", tools, stub_bin).stdout

    assert gens[0].name in out and gens[1].name in out, (
        "a destructive pass that says nothing leaves an operator no way to know "
        "what went, which is the wrong default for the only reaping code here"
    )


def test_dry_run_deletes_nothing_but_reports_the_same_set(env) -> None:
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(5)]
    _point(tools, "current", gens[4])

    dry = _sh("nx_gc_generations --keep 2 --dry-run", tools, stub_bin).stdout
    assert _names(tools) == {g.name for g in gens}, "--dry-run deleted something"

    wet = _sh("nx_gc_generations --keep 2", tools, stub_bin).stdout
    assert sorted(w for w in dry.split() if w.startswith("gen-") or "/gen-" in w) == \
           sorted(w for w in wet.split() if w.startswith("gen-") or "/gen-" in w)


def test_a_keep_of_zero_is_refused(env) -> None:
    """--keep 0 would mean 'retain nothing', and the four rules are the only
    thing between that and an unusable install. It is almost certainly a
    mistake, and refusing costs one message."""
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(3)]
    _point(tools, "current", gens[2])

    result = _sh("nx_gc_generations --keep 0", tools, stub_bin)

    assert result.returncode != 0
    assert _names(tools) == {g.name for g in gens}, "a refused GC deleted something"


def test_gc_never_follows_the_current_symlink_when_deleting(env) -> None:
    """rm -rf through a symlink would delete the TARGET's contents. The
    pointers live in the same directory being swept."""
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(4)]
    _point(tools, "current", gens[3])
    _point(tools, "previous", gens[2])

    _sh("nx_gc_generations --keep 1", tools, stub_bin)

    assert (gens[3] / "bin" / "nx").exists(), "current's target lost its contents"
    assert (gens[2] / "bin" / "nx").exists(), "previous's target lost its contents"
