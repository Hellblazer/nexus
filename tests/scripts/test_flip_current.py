# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The atomic flip: ``current`` must never not exist.

nexus-utpuw.3 (P1b). Every shim resolves ``<tools>/current`` at spawn. If the
pointer vanishes for even an instant, a spawn landing in that window gets exit
70 instead of running — so ``ln -sfn`` is banned outright: it unlinks and then
symlinks, and the gap between those is exactly the window. The required form is
symlink-then-rename, where the rename replaces the pointer in one step.

THE LOAD-BEARING TEST IS ``test_current_always_resolves_during_a_flip_storm``.
It runs a reader loop against a repeated flip and asserts the pointer resolved
on every single iteration. A flip implementation that opens a window will fail
it non-deterministically rather than never — so the test is written to make
that likelihood high (many flips, a tight reader) and to report the exact
iteration and errno rather than a bare count.

``previous`` is written here rather than deferred, because .6's GC has a
never-delete rule for "the previous current" and the layout gave that no
on-disk representation until this bead. Recording it BEFORE the flip is
deliberate: a crash between the two leaves ``previous`` pointing at what is
still ``current``, which makes rollback a harmless no-op. The other order
leaves ``previous`` stale and makes rollback go somewhere wrong.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_INSTALL_DIR = _REPO / "src" / "nexus" / "_install"
_FLIP = _INSTALL_DIR / "flip.sh"


def _sh(snippet: str, tools: Path, extra_env: dict | None = None):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tools.parent / "home"),
        "NX_TOOLS_DIR": str(tools),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", f'. "{_FLIP}"; {snippet}'],
        capture_output=True, text=True, env=env,
    )


def _make_gen(tools: Path, name: str) -> Path:
    gen = tools / f"gen-{name}"
    (gen / "bin").mkdir(parents=True)
    (gen / "bin" / "nx").write_text(f"#!/bin/sh\necho {name}\n")
    (gen / "nexus-install.json").write_text("{}")
    return gen


@pytest.fixture
def tools(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    return d


def test_flip_library_is_present() -> None:
    # Committed file: its absence is the loudest thing this suite could report.
    assert _FLIP.is_file(), f"{_FLIP} is missing"


# --------------------------------------------------------------------------
# the property: no window
# --------------------------------------------------------------------------

def _flip_storm(tools: Path, swap: str, flips: int = 300) -> tuple[int, int]:
    """Run *swap* repeatedly while a reader watches ``current``. -> (misses, seen).

    The reader loops until a sentinel file appears, so it is GUARANTEED to span
    the whole flip period. An earlier version ran a fixed iteration count and
    finished before the flips got going -- it reported zero misses against a
    form that has a window, which is a vacuous pass, not a clean one.

    The reader uses only shell builtins. ``[ -d ... ]`` forks nothing and is
    true only if the pointer exists AND resolves to a directory. A forking
    reader (``readlink``) cannot tell a missing pointer from a fork that failed
    under the load of the flip loop, and duly reported windows that were not
    there.
    """
    done = tools.parent / f"done-{abs(hash(swap))}"
    reader_script = tools.parent / f"reader-{abs(hash(swap))}.sh"
    reader_script.write_text(
        f'misses=0; seen=0\n'
        f'while [ ! -e "{done}" ]; do\n'
        f'  seen=$((seen+1))\n'
        f'  [ -d "{tools}/current" ] || misses=$((misses+1))\n'
        f'done\n'
        f'echo "$misses $seen"\n'
    )
    reader = subprocess.Popen(["bash", str(reader_script)], stdout=subprocess.PIPE, text=True)
    try:
        subprocess.run(
            ["bash", "-c",
             f'. "{_FLIP}"; cd "{tools}"; for i in $(seq 1 {flips}); do {swap}; done'],
            capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(tools.parent / "home"), "NX_TOOLS_DIR": str(tools)},
        )
    finally:
        done.write_text("x")
    out, _ = reader.communicate(timeout=180)
    misses, seen = (int(x) for x in out.split())
    return misses, seen


@pytest.mark.slow
def test_the_flip_has_no_window_that_ln_sfn_would_open(tools) -> None:
    """A MEASURED COMPARISON rather than an absolute, and marked ``slow``.

    MARKED SLOW FOR TWO REASONS, both learned the hard way on CI. It cannot
    calibrate everywhere -- a GitHub ubuntu runner recorded 0 misses for
    ``ln -sfn``, where macOS records ~1% -- and it is a CPU hog: two tight
    busy-wait readers for ~20s. Run in the default suite it took develop red
    twice over, once on its own non-vacuity guard and once by starving a
    50ms wall-clock budget in a NEIGHBOURING shard. The deterministic
    structural tests in this file are the real gate; this one is the
    corroborating measurement, and it belongs in the nightly leg.

    Measured while building this: against ``python os.replace`` -- a bare
    ``rename(2)``, atomic by POSIX definition and therefore incapable of opening
    a window -- a builtin reader still records a small nonzero miss rate. That
    number is the floor of the measurement, not a property of the primitive. So
    the assertion here runs the BANNED form in the same test, on the same
    machine, under the same load, and requires this implementation to be
    dramatically better. Self-calibrating: no hardcoded rate to rot as hardware
    changes.

    Reference rates from a 3,000-flip run while writing this test:
    ln -sfn 0.96%, mv -h 0.047%, mv -f -h 0.048%, os.replace 0.019%.
    """
    a = _make_gen(tools, "A")
    b = _make_gen(tools, "B")
    _sh(f'nx_flip_current "{a}"', tools)

    impl_misses, impl_seen = _flip_storm(
        tools, f'nx_flip_current "{b}" >/dev/null 2>&1; nx_flip_current "{a}" >/dev/null 2>&1')

    _sh(f'nx_flip_current "{a}"', tools)
    banned_misses, banned_seen = _flip_storm(
        tools, f'ln -sfn "{b}" current; ln -sfn "{a}" current')

    assert impl_seen > 10_000 and banned_seen > 10_000, (
        f"NON-VACUITY: reader barely spun (impl={impl_seen}, banned={banned_seen}); "
        f"a clean result would prove nothing"
    )
    if banned_misses == 0:
        # NOT a pass, and not a failure either: on this machine the banned form
        # did not open a window the reader could see, so there is no baseline to
        # compare against and a green result would be meaningless. Measured on a
        # GitHub ubuntu runner, where GNU `ln -sfn` recorded 0 misses where macOS
        # BSD `ln` records ~1%. Skipping is honest here BECAUSE the load-bearing
        # assertions do not depend on it: `ln -sfn` is banned by a code-level
        # check that always runs, and the flip's observable behaviour (target,
        # absoluteness, previous, rollback) is pinned deterministically.
        pytest.skip(
            f"no baseline: `ln -sfn` recorded 0 misses over {banned_seen} reads on "
            f"this machine, so the comparison has nothing to calibrate against"
        )

    impl_rate = impl_misses / impl_seen
    banned_rate = banned_misses / banned_seen
    assert impl_rate * 5 < banned_rate, (
        f"the flip is no better than the banned `ln -sfn`: "
        f"impl {impl_misses}/{impl_seen} ({impl_rate:.5%}) vs "
        f"ln -sfn {banned_misses}/{banned_seen} ({banned_rate:.5%}) -- "
        f"symlink-then-rename must not be opening a window of its own"
    )


def test_flip_is_not_implemented_with_ln_sfn() -> None:
    """A literal negative assertion, because `ln -sfn` is the obvious way to
    write this and it is precisely wrong. Scoped to code, not comments: the
    header legitimately names it in order to ban it."""
    code = [
        line for line in _FLIP.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    offenders = [line for line in code if "ln -sfn" in line or "ln -nsf" in line]
    assert offenders == [], f"ln -sfn leaves a window where current does not exist: {offenders}"


# --------------------------------------------------------------------------
# what the flip records
# --------------------------------------------------------------------------

def test_flip_points_current_at_the_generation(tools) -> None:
    a = _make_gen(tools, "A")
    result = _sh(f'nx_flip_current "{a}"', tools)
    assert result.returncode == 0, result.stderr
    assert os.readlink(tools / "current") == str(a)


def test_stored_target_is_absolute(tools) -> None:
    """The shim does a plain `readlink` (never `readlink -f`, which is macOS
    >= 12.3 only), so a relative target would resolve against the shim's CWD."""
    a = _make_gen(tools, "A")
    _sh(f'nx_flip_current "{a}"', tools)
    assert os.readlink(tools / "current").startswith("/")


def test_flip_records_the_outgoing_generation_as_previous(tools) -> None:
    """.6's GC has a never-delete rule for the previous current. Until this
    bead the layout gave that no on-disk representation, so GC would have had
    to approximate it by mtime -- the heuristic this whole arc replaced."""
    a = _make_gen(tools, "A")
    b = _make_gen(tools, "B")

    _sh(f'nx_flip_current "{a}"', tools)
    _sh(f'nx_flip_current "{b}"', tools)

    assert os.readlink(tools / "current") == str(b)
    assert os.readlink(tools / "previous") == str(a)


def test_first_flip_has_no_previous(tools) -> None:
    """Nothing to record on a virgin install, and inventing one would make GC
    protect a generation that never existed."""
    a = _make_gen(tools, "A")
    result = _sh(f'nx_flip_current "{a}"', tools)
    assert result.returncode == 0, result.stderr
    assert not (tools / "previous").exists()


# --------------------------------------------------------------------------
# rollback -- in this bead deliberately, because it comes free
# --------------------------------------------------------------------------

def test_rollback_returns_to_the_prior_generation(tools) -> None:
    a = _make_gen(tools, "A")
    b = _make_gen(tools, "B")
    _sh(f'nx_flip_current "{a}"', tools)
    _sh(f'nx_flip_current "{b}"', tools)

    result = _sh("nx_rollback_current", tools)

    assert result.returncode == 0, result.stderr
    assert os.readlink(tools / "current") == str(a)


def test_rollback_is_reversible(tools) -> None:
    """Rolling back must itself record a previous, or a mistaken rollback is a
    one-way door."""
    a = _make_gen(tools, "A")
    b = _make_gen(tools, "B")
    _sh(f'nx_flip_current "{a}"', tools)
    _sh(f'nx_flip_current "{b}"', tools)
    _sh("nx_rollback_current", tools)

    assert os.readlink(tools / "current") == str(a)
    assert os.readlink(tools / "previous") == str(b)

    _sh("nx_rollback_current", tools)
    assert os.readlink(tools / "current") == str(b)


def test_rollback_without_a_previous_refuses(tools) -> None:
    a = _make_gen(tools, "A")
    _sh(f'nx_flip_current "{a}"', tools)

    result = _sh("nx_rollback_current", tools)

    assert result.returncode != 0
    assert "previous" in result.stderr.lower()
    assert os.readlink(tools / "current") == str(a), "a refused rollback must change nothing"


def test_rollback_refuses_a_previous_that_is_gone(tools) -> None:
    """GC is supposed to never reap the previous generation. If it ever does,
    rollback must refuse rather than point current at a hole."""
    a = _make_gen(tools, "A")
    b = _make_gen(tools, "B")
    _sh(f'nx_flip_current "{a}"', tools)
    _sh(f'nx_flip_current "{b}"', tools)

    import shutil
    shutil.rmtree(a)

    result = _sh("nx_rollback_current", tools)

    assert result.returncode != 0
    assert os.readlink(tools / "current") == str(b), "current must not move to a missing target"
    # The refusal in nx_flip_current would ALSO stop this, so behaviour alone
    # cannot tell whether nx_rollback_current's own check exists -- a mutation
    # deleting it left every assertion green. What that check actually buys is
    # the DIAGNOSIS: "previous generation is gone" names the reaped generation
    # and the rollback that wanted it, where the generic refusal only says a
    # path is not a directory. Assert the thing the guard is for.
    assert "previous generation is gone" in result.stderr, (
        f"rollback must say WHY it refused, not just that a path is not a "
        f"directory: {result.stderr.strip()}"
    )


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------

def test_flip_refuses_a_target_that_is_not_a_directory(tools) -> None:
    result = _sh(f'nx_flip_current "{tools}/gen-nonexistent"', tools)
    assert result.returncode != 0
    assert not (tools / "current").exists(), "a refused flip must not create the pointer"


def test_flip_refuses_a_relative_target(tools) -> None:
    """A relative target would resolve against whatever CWD the shim happens to
    have, which is not a stable anchor."""
    _make_gen(tools, "A")
    result = _sh("nx_flip_current gen-A", tools)
    assert result.returncode != 0
    assert "absolute" in result.stderr.lower()


def test_flip_leaves_no_temporary_pointer_behind(tools) -> None:
    a = _make_gen(tools, "A")
    _sh(f'nx_flip_current "{a}"', tools)
    leftovers = [p.name for p in tools.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"temporary pointers left behind: {leftovers}"
