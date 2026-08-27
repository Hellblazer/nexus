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


# --------------------------------------------------------------------------
# symlinked entries: .7's legacy pseudo-generation
# --------------------------------------------------------------------------

def test_a_symlinked_pseudo_generation_reaps_its_real_target(env) -> None:
    """.7 registers the legacy uv-tool tree as a pseudo-generation: a symlink
    named gen-legacy-uv-tool pointing OUTSIDE tools/ at the real uv-managed
    tree. It is always receipt-less (nothing ever writes nexus-install.json
    into a tree this project does not own), so it is never protected by
    keep-last-N -- but a REAP of it must delete the REAL tree, not just
    unlink the pointer. Plain `rm -rf` on a symlink only removes the link
    and leaves its target on disk untouched, which would silently defeat
    the bead's 'plain rm -rf of the legacy dir' requirement."""
    tools, stub_bin = env
    legacy_real = tools.parent / "uv-tool-dir" / "conexus"
    (legacy_real / "bin").mkdir(parents=True)
    # A uv-managed venv always carries pyvenv.cfg (PEP 405), and the reap
    # requires it as proof the ledger target really is a venv and not an
    # arbitrary path someone pointed the pointer at.
    (legacy_real / "pyvenv.cfg").write_text("home = /opt/py/bin\n")
    (legacy_real / "bin" / "nx").write_text("legacy nx")
    # A uv-managed venv always carries this, and the reap requires it as proof
    # that the ledger target really is a venv rather than an arbitrary path.
    (legacy_real / "pyvenv.cfg").write_text("home = /opt/py/bin\n")
    pseudo = tools / "gen-legacy-uv-tool"
    pseudo.symlink_to(legacy_real)
    current = _gen(tools, "00")
    _point(tools, "current", current)

    result = _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert result.returncode == 0, result.stderr
    assert not legacy_real.exists(), "the real legacy tree survived its own reap"
    assert not pseudo.exists() and not pseudo.is_symlink(), (
        "the now-dangling pseudo-generation pointer was left behind"
    )


def test_a_symlinked_pseudo_generation_with_a_live_holder_is_not_reaped(env) -> None:
    tools, stub_bin = env
    legacy_real = tools.parent / "uv-tool-dir" / "conexus"
    (legacy_real / "bin").mkdir(parents=True)
    # A uv-managed venv always carries pyvenv.cfg (PEP 405), and the reap
    # requires it as proof the ledger target really is a venv and not an
    # arbitrary path someone pointed the pointer at.
    (legacy_real / "pyvenv.cfg").write_text("home = /opt/py/bin\n")
    (legacy_real / "bin" / "nx").write_text("legacy nx")
    # A uv-managed venv always carries this, and the reap requires it as proof
    # that the ledger target really is a venv rather than an arbitrary path.
    (legacy_real / "pyvenv.cfg").write_text("home = /opt/py/bin\n")
    pseudo = tools / "gen-legacy-uv-tool"
    pseudo.symlink_to(legacy_real)
    current = _gen(tools, "00")
    _point(tools, "current", current)
    _stub_ps(stub_bin, [f"  909 {legacy_real}/bin/python {legacy_real}/bin/nx-mcp"])

    result = _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert result.returncode == 0, result.stderr
    assert legacy_real.exists(), (
        "a legacy tree a live process is running from was reaped -- the exact "
        "nexus-q3xrx failure the pseudo-generation registration exists to prevent"
    )
    assert pseudo.is_symlink()


def test_a_symlinked_pseudo_generation_never_counts_toward_keep_last_n(env) -> None:
    """Permanently receipt-less by construction, so it must never shield a
    real generation from retention -- the same non-vacuity concern the
    receipt-less-wreckage test above pins for a crashed build."""
    tools, stub_bin = env
    legacy_real = tools.parent / "uv-tool-dir" / "conexus"
    (legacy_real / "bin").mkdir(parents=True)
    # A uv-managed venv always carries pyvenv.cfg (PEP 405), and the reap
    # requires it as proof the ledger target really is a venv and not an
    # arbitrary path someone pointed the pointer at.
    (legacy_real / "pyvenv.cfg").write_text("home = /opt/py/bin\n")
    pseudo = tools / "gen-legacy-uv-tool"
    pseudo.symlink_to(legacy_real)
    gens = [_gen(tools, f"{i:02d}") for i in range(3)]
    _point(tools, "current", gens[2])

    _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert not legacy_real.exists(), "the pseudo-generation was wrongly protected"
    assert gens[1].is_dir() and gens[2].is_dir()


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


# --------------------------------------------------------------------------
# the ONLY route out of tools/ -- fenced, and tested in the dangerous direction
# --------------------------------------------------------------------------

def test_a_gen_symlink_that_is_not_the_reserved_ledger_is_never_followed(env) -> None:
    """TRIPWIRE, and it is here because the capability was added without it.

    .7 taught GC to follow a gen-* symlink so the legacy uv-tool tree could be
    reaped through a ledger pointer. That is the only way this sweep can delete
    anything outside the tools root, and it originally arrived guarded only by
    "the target is not literally /". Measured against that version: a
    `gen-rogue` symlink pointing at an unrelated directory caused rm -rf of
    that directory. One value out of infinitely many dangerous ones is not a
    guard.
    """
    tools, stub_bin = env
    precious = tools.parent / "PRECIOUS_USER_DATA"
    precious.mkdir()
    (precious / "thesis.txt").write_text("irreplaceable")

    gens = [_gen(tools, f"{i:02d}") for i in range(3)]
    _point(tools, "current", gens[2])
    (tools / "gen-rogue").symlink_to(precious)

    result = _sh("nx_gc_generations --keep 1", tools, stub_bin)

    assert (precious / "thesis.txt").read_text() == "irreplaceable", (
        "GC followed an unrecognised gen-* symlink out of the tools root and "
        "deleted what it pointed at"
    )
    assert (tools / "gen-rogue").is_symlink(), "the rogue pointer was silently removed"
    assert "refusing to reap through an unrecognised generation symlink" in result.stderr
    assert gens[0].name not in _names(tools), (
        "NON-VACUITY: nothing was reaped at all, so the survival above proves nothing"
    )


def test_the_reserved_ledger_pointing_at_a_non_venv_unlinks_but_does_not_delete(env) -> None:
    """Right name, wrong target. A home directory or a checkout has no
    pyvenv.cfg, so the pointer goes and the tree stays. Failing this way leaves
    litter; failing the other way deletes data."""
    tools, stub_bin = env
    not_a_venv = tools.parent / "not-a-venv"
    not_a_venv.mkdir()
    (not_a_venv / "important.txt").write_text("keep me")

    gens = [_gen(tools, f"{i:02d}") for i in range(2)]
    _point(tools, "current", gens[1])
    (tools / "gen-legacy-uv-tool").symlink_to(not_a_venv)

    result = _sh("nx_gc_generations --keep 1", tools, stub_bin)

    assert (not_a_venv / "important.txt").read_text() == "keep me"
    assert not (tools / "gen-legacy-uv-tool").exists()
    assert "not a venv" in result.stderr


def test_the_reserved_ledger_pointing_at_a_real_venv_is_reaped(env) -> None:
    """The intended case still works: a genuine uv-managed venv, carrying
    pyvenv.cfg, is deleted through the ledger pointer."""
    tools, stub_bin = env
    legacy = tools.parent / "uv-tools" / "conexus"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "pyvenv.cfg").write_text("home = /opt/py/bin\n")
    (legacy / "bin" / "nx").write_text("#!/bin/sh\nexit 0\n")

    gens = [_gen(tools, f"{i:02d}") for i in range(2)]
    _point(tools, "current", gens[1])
    (tools / "gen-legacy-uv-tool").symlink_to(legacy)

    _sh("nx_gc_generations --keep 1", tools, stub_bin)

    assert not legacy.exists(), "the legacy tree was not reaped through its ledger"
    assert not (tools / "gen-legacy-uv-tool").exists()


def test_rule_c_holds_when_the_holders_argv_contains_the_word_grep(env) -> None:
    """RG-B Critical (nexus-qzawu), at the layer where it does the damage. The
    census dropped holders whose argv contained the word 'grep', so rule (c) saw
    an unheld generation and reaped it out from under a live process. Rule (c) is
    absolute; it cannot depend on what a holder happens to be searching for."""
    tools, stub_bin = env
    gens = [_gen(tools, f"{i:02d}") for i in range(8)]
    _point(tools, "current", gens[7])
    _stub_ps(stub_bin, [f"  101 {gens[0]}/bin/python {gens[0]}/bin/nx search grep"])

    _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert gens[0].is_dir(), (
        "a generation a live process is running from was deleted because that "
        "process's argv contained the word 'grep' -- nexus-q3xrx via the census"
    )
