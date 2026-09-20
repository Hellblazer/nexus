# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The Python and shell statements of holder attribution must agree.

nexus-utpuw.10. The shell half already says this is coming -- census.sh's header
reads "src/nexus/upgrade_finish.py:50 hardcodes _PROC_MARKERS ... .10 does the
same for the Python side."

Two implementations exist for the same reason ``layout.sh`` and
``install_layout.py`` both exist (see tests/test_install_layout_twins_agree.py):
the callers have incompatible import constraints. ``_install/census.sh`` is
sourced by GC and the installer, which run with NOTHING installed and cannot
import nexus. ``install_census.py`` is imported by ``upgrade_finish.py`` and
``health.py``, which run after the install and can.

WHY THIS PINS RATHER THAN TRUSTS. The bug this bead fixes IS a drifted marker:
upgrade_finish's ``_PROC_MARKERS`` and ``running_from_tool_install()`` both
hardcode ``uv/tools/conexus``, which stopped matching the moment the layout
moved -- and nothing failed, it just silently stopped finding anything. A second
copy of holder attribution that drifts the same way would restore exactly the
defect being removed, so the two halves are compared against the SAME snapshot
rather than each being tested against its own idea of one.

The snapshots below are the real argv shapes, carried over from the census
suite: a shell-script holder, the four daemon classes, a shebang-wrapped MCP
server, a stamp-collision sibling, and a process that merely mentions a path
inside the tree.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.install_census import generation_holder_pids

_REPO = Path(__file__).resolve().parent.parent
_CENSUS_SH = _REPO / "src" / "nexus" / "_install" / "census.sh"


def _shell_pids(generation: str, snapshot: str) -> list[int]:
    """What the shell half says, given the same snapshot."""
    r = subprocess.run(
        ["bash", "-c", f'. "{_CENSUS_SH}"; nx_generation_holder_pids "$1" "$2"',
         "_", generation, snapshot],
        capture_output=True, text=True, timeout=60,
    )
    return [int(t) for t in r.stdout.split()]


def _snapshot(*lines: str) -> str:
    return "\n".join(lines)


@pytest.fixture
def gen(tmp_path):
    g = tmp_path / "tools" / "gen-A"
    (g / "bin").mkdir(parents=True)
    (g / "nexus-install.json").write_text("{}")
    return g


_CASES = {
    "python-hosted-mcp": ["  101 {gen}/bin/python {gen}/bin/nx-mcp"],
    "storage-service": ["  102 {gen}/bin/nx daemon service start --foreground"],
    "aspect-worker": ["  103 {gen}/bin/nx daemon aspect-worker start --tenant default"],
    # The only canonical holder whose argv does not begin <gen>/bin/nx.
    "mineru": ["  104 {gen}/bin/mineru-api --port 8899"],
    "in-flight-nx": ["  105 {gen}/bin/nx search grep"],
    "merely-mentions": ["  106 /usr/bin/vim {gen}/README.txt"],
    "no-holders": ["  107 /usr/bin/vim unrelated.txt"],
    "several": [
        "  108 {gen}/bin/python {gen}/bin/nx-mcp",
        "  109 {gen}/bin/nx daemon service start --foreground",
        "  110 /usr/bin/vim unrelated.txt",
    ],
}


@pytest.mark.parametrize("case", sorted(_CASES), ids=sorted(_CASES))
def test_both_halves_attribute_the_same_pids(gen, case) -> None:
    snapshot = _snapshot(*(line.format(gen=gen) for line in _CASES[case]))

    shell = _shell_pids(str(gen), snapshot)
    python = generation_holder_pids(gen, snapshot=snapshot)

    assert python == shell, (
        f"the halves disagree on '{case}': shell={shell} python={python}. "
        "A drifted second copy of holder attribution is the defect nexus-utpuw.10 "
        "exists to remove, not a thing to add."
    )


def test_both_halves_ignore_a_stamp_collision_sibling(tmp_path) -> None:
    """install_generation.sh suffixes a same-second collision, so gen-<stamp>
    and gen-<stamp>a coexist by design. Neither half may borrow the other's
    holders -- the shell half enforces it with a '/' path boundary."""
    tools = tmp_path / "tools"
    base = tools / "gen-20260826T0100Z"
    sibling = tools / "gen-20260826T0100Za"
    for d in (base, sibling):
        (d / "bin").mkdir(parents=True)
        (d / "nexus-install.json").write_text("{}")
    snapshot = _snapshot(f"  201 {sibling}/bin/python {sibling}/bin/nx-mcp")

    assert _shell_pids(str(base), snapshot) == []
    assert generation_holder_pids(base, snapshot=snapshot) == []


def test_both_halves_resolve_a_pseudo_generation_symlink(tmp_path) -> None:
    """.7 registers the legacy uv tree as a gen-* SYMLINK pointing outside
    tools/. A live holder's argv names the REAL path it exec'd from, never the
    ledger pointer, so both halves must resolve one level before matching."""
    tools = tmp_path / "tools"
    tools.mkdir()
    real = tmp_path / "uvtools" / "conexus"
    (real / "bin").mkdir(parents=True)
    ledger = tools / "gen-legacy-uv-tool"
    ledger.symlink_to(real)
    snapshot = _snapshot(f"  301 {real}/bin/python {real}/bin/nx-mcp")

    shell = _shell_pids(str(ledger), snapshot)
    python = generation_holder_pids(ledger, snapshot=snapshot)

    assert shell == [301], f"shell half failed to resolve the ledger: {shell}"
    assert python == shell


def test_both_halves_refuse_the_filesystem_root(tmp_path) -> None:
    """"/" normalises to the empty string, and an empty match would make the
    boundary pattern "/" -- every process on the machine a holder of
    everything. The shell half refuses with a usage exit (nexus-qzawu)."""
    snapshot = _snapshot("  401 /usr/bin/vim notes.txt")

    assert _shell_pids("/", snapshot) == []
    with pytest.raises(ValueError):
        generation_holder_pids("/", snapshot=snapshot)


# ===========================================================================
# THE COLLAPSE: census.sh dispatches, it does not reimplement
#
# Everything above compared two implementations. census.sh now dispatches to
# census_core.py, which is the module the Python side imports, so those
# comparisons are the core against itself. They are kept as WIRING tests --
# a verb under the wrong name, a snapshot that does not cross the boundary, a
# refusal that leaks to stdout all fail them -- and what replaces their
# drift-catching role is below.
#
# Two REAL divergences were found by differencing the halves before the wiring
# went in, neither of which the tests above could see. The Python half had no
# absolute-path guard, so generation_holder_pids("rel/path") matched against a
# bare relative string and returned pids where the shell refused with exit 64,
# and the empty string returned "no holders" -- the under-reporting answer.
# And dispatching newly exposed the core to its OWN argv, since the generation
# path has to reach the subprocess somehow. Both are pinned here.
# ===========================================================================

import os
import re

_CORE = _REPO / "src" / "nexus" / "_install" / "census_core.py"

_DISPATCHERS = {
    "nx_generation_holder_pids": "holder_pids",
    "nx_census_report": "report",
}


def _shell_function_bodies() -> dict[str, str]:
    """Every ``nx_*`` function census.sh defines, name -> body text.

    Brace-counting rather than a regex per function: the bodies contain braces
    and a regex stopping at the first ``}`` would report a truncated body as a
    complete one, which here would mean reporting an implementation as a clean
    dispatch.
    """
    text = _CENSUS_SH.read_text()
    bodies: dict[str, str] = {}
    for match in re.finditer(r"^(nx_[a-z_]+)\(\)\s*\{", text, re.MULTILINE):
        name = match.group(1)
        depth, i = 0, match.end() - 1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        bodies[name] = text[match.end():i]
    return bodies


def test_the_shell_half_only_dispatches() -> None:
    """No attribution logic left in shell. A half-collapsed census is worse
    than the two halves were: it voids the comparison above while leaving a
    second implementation to drift."""
    bodies = _shell_function_bodies()
    assert set(bodies) == set(_DISPATCHERS), (
        f"census.sh defines {sorted(bodies)}, the table says {sorted(_DISPATCHERS)}"
    )
    for name, body in bodies.items():
        assert "_nx_census_core " in body, f"{name} does not reach the core"
        for forbidden in ("awk", "grep", "readlink", "ENVIRON", "ps "):
            assert forbidden not in body, (
                f"{name} still does its own {forbidden!r}; the attribution rules "
                f"live in census_core.py now"
            )
        verb = body.split("_nx_census_core ")[1].split()[0].rstrip(";")
        assert verb == _DISPATCHERS[name], f"{name} dispatches {verb!r}"


def test_the_dispatch_table_matches_the_cores_verbs() -> None:
    from nexus._install.census_core import _VERBS

    reached = set(_DISPATCHERS.values()) | {"ps_snapshot"}  # via _nx_ps_snapshot
    assert reached <= set(_VERBS), f"shell dispatches verbs the core lacks: {reached - set(_VERBS)}"


def test_exactly_one_ps_runs_per_census(tmp_path: Path) -> None:
    """THE safety property, and the one the dispatch could most easily break.

    Every generation must be attributed from ONE view of the process table.
    Per-generation snapshots let a process exit between them, so a tree can
    appear held by two generations or by none, and GC would then reap against
    a state that never existed at any instant.

    The shell half already lost this once and got it back: it tested the
    snapshot's VALUE, and a census with no holders has an empty and therefore
    falsy snapshot, so ps ran N+1 times while the comment above it said
    otherwise. A dispatch per generation would reintroduce it by a different
    route, which is why `report` runs the whole loop inside one process.

    Counted rather than reasoned about: a fake `ps` earlier on PATH appends a
    line per invocation.
    """
    tools = tmp_path / "tools"
    for name in ("gen-A", "gen-B", "gen-C", "gen-D"):
        (tools / name / "bin").mkdir(parents=True)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    counter = tmp_path / "ps-calls"
    fake_ps = bin_dir / "ps"
    fake_ps.write_text(f'#!/bin/sh\necho call >> "{counter}"\nexit 0\n')
    fake_ps.chmod(0o755)

    r = subprocess.run(
        ["bash", "-c", f'. "{_CENSUS_SH}"; nx_census_report "$1"', "_", str(tools)],
        capture_output=True, text=True,
        env={
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path),
        },
    )
    assert r.returncode == 0, r.stderr

    # Non-vacuity: a census that never ran ps at all would also satisfy "not
    # more than one", and would mean the fake was never reached.
    calls = counter.read_text().count("call") if counter.exists() else 0
    assert calls == 1, (
        f"ps ran {calls} times for a census over 4 generations; exactly one "
        f"snapshot must serve the whole loop"
    )
    assert r.stdout.count("holders=") == 4, (
        f"the census did not report all four generations:\n{r.stdout}"
    )


def test_a_caller_supplied_snapshot_crosses_on_stdin_not_argv(tmp_path: Path) -> None:
    """gc.sh and reinstall-tool.sh take a snapshot and pass it down their reap
    loops. A real `ps axww` on a busy box runs to hundreds of kilobytes, and
    argv and the environment both have a hard size limit, so the snapshot goes
    on stdin. A 600 KB one would fail with E2BIG if it ever went through argv.
    """
    gen = tmp_path / "tools" / "gen-A"
    gen.mkdir(parents=True)
    filler = "\n".join(f"{i} /some/unrelated/process --flag" for i in range(20000))
    snapshot = f"4242 {gen}/bin/nx serve\n{filler}\n"
    assert len(snapshot) > 600_000, f"filler too small to prove the point: {len(snapshot)}"

    r = subprocess.run(
        ["bash", "-c", f'. "{_CENSUS_SH}"; nx_generation_holder_pids "$1" "$2"',
         "_", str(gen), snapshot],
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["4242"], r.stdout


def test_the_census_does_not_count_itself(tmp_path: Path) -> None:
    """Found by differencing the halves, and introduced by the dispatch itself.

    When census.sh dispatches, the core runs as
    ``python3 census_core.py holder_pids <generation>`` and its own argv names
    the tree, so it counted itself: a generation nobody runs from reported one
    holder and would never be reaped. The shell half never had this, because
    its pattern travelled in the environment and its pipeline could not appear
    in its own snapshot.

    A trailing slash is what made it bite, since the match appends one, so that
    is what this passes.
    """
    gen = tmp_path / "tools" / "gen-A"
    gen.mkdir(parents=True)

    # THE PATH MUST NOT REACH ANY ANCESTOR'S ARGV, or this measures the wrong
    # thing. Written to a file and read back inside the shell, so bash's own
    # command line carries the FILENAME and the core's carries the path. Passed
    # in argv directly, bash matches too and the test fails on the ancestor
    # rather than on the behaviour it names -- which is how it failed first.
    argfile = tmp_path / "arg"
    argfile.write_text(f"{gen}/")
    r = subprocess.run(
        ["bash", "-c",
         f'. "{_CENSUS_SH}"; nx_generation_holder_pids "$(cat "$1")"', "_", str(argfile)],
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", (
        f"the census counted itself as a holder of an empty generation: {r.stdout!r}"
    )


def test_an_ancestor_naming_the_tree_is_still_counted(tmp_path: Path) -> None:
    """The limit of the fix above, stated so nobody mistakes it for a bug later.

    Only the census's OWN pid is excluded. A parent process whose argv happens
    to name the generation is still reported as a holder, and that is
    deliberate: excluding an ancestry would be a denylist, and a denylist here
    can drop a REAL holder. This file already paid for one -- a `grep -v grep`
    inherited from live_venv_processes() censused `nx search grep` as zero
    holders and GC reaped the tree that process was running from (nexus-qzawu).

    Over-reporting keeps a tree nobody holds, which costs disk until the next
    pass. Under-reporting deletes a tree somebody is running from. The choice
    of direction is the same one the module makes everywhere else.

    Behaviour unchanged by the collapse, and measured: the pre-dispatch shell
    half reported THREE self-matches for this invocation (the shell plus its
    pipeline children) where the dispatching one reports one.
    """
    gen = tmp_path / "tools" / "gen-A"
    gen.mkdir(parents=True)
    r = subprocess.run(
        ["bash", "-c", f'. "{_CENSUS_SH}"; nx_generation_holder_pids "$1"', "_", f"{gen}/"],
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() != "", (
        "the ancestor shell was not counted -- if this became an exclusion, "
        "check it is not a denylist that can drop a real holder"
    )


@pytest.mark.parametrize("bad", ["rel/path", "", "relative", "./x"])
def test_both_halves_refuse_a_relative_generation(bad: str, tmp_path: Path) -> None:
    """The divergence the 138-line pin above did not catch.

    The shell half always refused a non-absolute generation. The Python half
    never did: it matched against the bare string and returned pids, and for
    the empty string returned "no holders" -- the under-reporting answer, which
    is the one that invites a reap. Both refuse now.
    """
    from nexus.install_census import generation_holder_pids as py_pids

    snapshot = "123 /somewhere/rel/path/bin/nx\n"
    with pytest.raises(ValueError, match="absolute"):
        py_pids(bad, snapshot=snapshot)

    r = subprocess.run(
        ["bash", "-c", f'. "{_CENSUS_SH}"; nx_generation_holder_pids "$1" "$2"',
         "_", bad, snapshot],
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    assert r.returncode != 0, f"the shell half accepted {bad!r}"
    assert r.stdout.strip() == "", "a refusal must not also print pids"
