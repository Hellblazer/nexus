# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Per-generation holder census: who is still running from which tree.

nexus-utpuw.5 (P2a). Replaces ``scripts/reinstall-tool.sh``'s
``live_venv_processes()``, which answered one question — "is ANYTHING running
from the tool venv" — and used the answer to REFUSE.

THE ROLE CHANGES, AND THAT IS THE POINT OF THE WHOLE ARC. Under side-by-side
generations nothing is ever refused (nexus-utpuw comment 1: zero flags, zero
steps). A holder is no longer an obstacle, it is a fact about one generation:
an input to GC (.6, which must never reap a held tree) and one informational
line telling the operator which generations are still spoken for. Those
sessions converge on their next spawn.

MARKERS ARE DERIVED, NOT HARDCODED. ``upgrade_finish.py:50`` today hardcodes
``_PROC_MARKERS = ('uv/tools/conexus', '.local/bin/nx')`` — a substring that
silently stops matching when the layout moves, which is the failure class this
arc keeps removing. Under generations the marker set is enumerable: the
``gen-*`` directories that exist plus the shim directory. Nothing to keep in
sync, so nothing to rot.

``ps`` is stubbed here. That is the seam: the census is a query over a process
snapshot, and a test that cannot fabricate the snapshot can only assert that
the code runs.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CENSUS = _REPO / "src" / "nexus" / "_install" / "census.sh"


def _stub_ps(bin_dir: Path, lines: list[str]) -> None:
    """A fake ``ps ax -o pid=,command=`` emitting *lines* verbatim."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    ps = bin_dir / "ps"
    body = "#!/bin/sh\ncat <<'PSEOF'\n" + "\n".join(lines) + "\nPSEOF\n"
    ps.write_text(body)
    ps.chmod(ps.stat().st_mode | stat.S_IXUSR)


def _sh(snippet: str, tools: Path, stub_bin: Path, bin_dir: Path | None = None):
    env = {
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tools.parent / "home"),
        "NX_TOOLS_DIR": str(tools),
        "NX_BIN_DIR": str(bin_dir or (tools.parent / "bin")),
    }
    return subprocess.run(
        ["bash", "-c", f'. "{_CENSUS}"; {snippet}'],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def env(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tmp_path / "bin").mkdir()
    return tools, tmp_path / "stubbin", tmp_path / "bin"


def _make_gens(tools: Path, *names: str) -> list[Path]:
    out = []
    for name in names:
        gen = tools / f"gen-{name}"
        (gen / "bin").mkdir(parents=True)
        (gen / "nexus-install.json").write_text("{}")
        out.append(gen)
    return out


def test_census_is_present() -> None:
    assert _CENSUS.is_file(), f"{_CENSUS} is missing"


# --------------------------------------------------------------------------
# attribution: which generation is each process holding
# --------------------------------------------------------------------------

def test_processes_are_attributed_to_the_generation_they_run_from(env) -> None:
    tools, stub_bin, bin_dir = env
    a, b = _make_gens(tools, "A", "B")
    _stub_ps(stub_bin, [
        f"  101 {a}/bin/python {a}/bin/nx-mcp",
        f"  102 {a}/bin/python {a}/bin/nx doctor",
        f"  201 {b}/bin/python {b}/bin/nx-mcp-catalog",
        "  999 /usr/bin/vim notes.txt",
    ])

    result = _sh(f'nx_generation_holder_pids "{a}"', tools, stub_bin, bin_dir)
    assert result.returncode == 0, result.stderr
    assert sorted(result.stdout.split()) == ["101", "102"]

    result = _sh(f'nx_generation_holder_pids "{b}"', tools, stub_bin, bin_dir)
    assert result.stdout.split() == ["201"]


def test_a_generation_with_no_holders_reports_none(env) -> None:
    """The case GC cares about most: nothing is running from this tree, so it
    is a candidate for reaping."""
    tools, stub_bin, bin_dir = env
    a, b = _make_gens(tools, "A", "B")
    _stub_ps(stub_bin, [f"  101 {a}/bin/python {a}/bin/nx-mcp"])

    result = _sh(f'nx_generation_holder_pids "{b}"', tools, stub_bin, bin_dir)

    assert result.returncode == 0, "no holders is an ANSWER, not an error"
    assert result.stdout.strip() == ""


def test_a_process_holding_only_the_shim_path_holds_no_generation(env) -> None:
    """A shim resolves `current` and execs the real path, so a live holder's
    argv names its GENERATION. Something naming only the shim has not resolved
    one -- and must not pin a tree it is not running from, or GC would keep a
    generation alive on the strength of a wrapper."""
    tools, stub_bin, bin_dir = env
    (a,) = _make_gens(tools, "A")
    _stub_ps(stub_bin, [f"  301 /bin/sh {bin_dir}/nx doctor"])

    result = _sh(f'nx_generation_holder_pids "{a}"', tools, stub_bin, bin_dir)

    assert result.stdout.strip() == "", (
        "a shim-only process was attributed to a generation it is not running from"
    )


def test_the_census_grep_does_not_count_itself(env) -> None:
    """Preserved from live_venv_processes(): a transient grep whose own argv
    contains the search string would otherwise count as a holder, and every
    generation would look permanently occupied."""
    tools, stub_bin, bin_dir = env
    (a,) = _make_gens(tools, "A")
    _stub_ps(stub_bin, [
        f"  101 {a}/bin/python {a}/bin/nx-mcp",
        f"  555 grep -F {a}",
        f"  556 /bin/sh -c ps ax | grep -F {a}",
    ])

    result = _sh(f'nx_generation_holder_pids "{a}"', tools, stub_bin, bin_dir)

    assert result.stdout.split() == ["101"], (
        f"transient greps were counted as holders: {result.stdout.split()}"
    )


# --------------------------------------------------------------------------
# derived, not hardcoded
# --------------------------------------------------------------------------

def test_the_marker_set_is_whatever_generations_exist(env) -> None:
    """upgrade_finish.py:50 hardcodes _PROC_MARKERS and stops matching when the
    layout moves. Here a brand-new generation is censused with no code change,
    because the marker set IS the directory listing."""
    tools, stub_bin, bin_dir = env
    _make_gens(tools, "A")
    brand_new = tools / "gen-ZZZ-invented-by-this-test"
    (brand_new / "bin").mkdir(parents=True)
    (brand_new / "nexus-install.json").write_text("{}")
    _stub_ps(stub_bin, [f"  777 {brand_new}/bin/python {brand_new}/bin/nx-mcp"])

    result = _sh("nx_census_report", tools, stub_bin, bin_dir)

    assert result.returncode == 0, result.stderr
    assert "gen-ZZZ-invented-by-this-test" in result.stdout
    assert "777" in result.stdout


def test_a_directory_that_is_not_a_generation_is_not_censused(env) -> None:
    """~/.local/share/nexus/ also holds chroma/ and fastembed_cache/. The
    census enumerates gen-* and nothing else -- the same scoping .6's GC
    depends on, where walking the parent would be a data-loss bug."""
    tools, stub_bin, bin_dir = env
    _make_gens(tools, "A")
    (tools / "chroma").mkdir()
    (tools / "fastembed_cache").mkdir()
    _stub_ps(stub_bin, ["  101 /usr/bin/vim x"])

    result = _sh("nx_census_report", tools, stub_bin, bin_dir)

    assert "chroma" not in result.stdout
    assert "fastembed_cache" not in result.stdout


# --------------------------------------------------------------------------
# the role change: a report, never a refusal
# --------------------------------------------------------------------------

def test_the_census_never_fails_because_something_is_running(env) -> None:
    """THE role change. live_venv_processes() fed a refusal; nexus-utpuw
    comment 1 deletes refusal entirely. A census that exits non-zero when it
    finds holders would smuggle the old behaviour back in as an exit status."""
    tools, stub_bin, bin_dir = env
    (a,) = _make_gens(tools, "A")
    _stub_ps(stub_bin, [f"  101 {a}/bin/python {a}/bin/nx-mcp"])

    assert _sh("nx_census_report", tools, stub_bin, bin_dir).returncode == 0
    assert _sh(f'nx_generation_holder_pids "{a}"', tools, stub_bin, bin_dir).returncode == 0


def test_the_report_names_generations_and_their_holder_counts(env) -> None:
    tools, stub_bin, bin_dir = env
    a, b = _make_gens(tools, "A", "B")
    _stub_ps(stub_bin, [
        f"  101 {a}/bin/python {a}/bin/nx-mcp",
        f"  102 {a}/bin/python {a}/bin/nx doctor",
    ])

    out = _sh("nx_census_report", tools, stub_bin, bin_dir).stdout

    assert "gen-A" in out and "gen-B" in out
    lines = {ln.split()[0]: ln for ln in out.splitlines() if ln.strip()}
    assert "2" in lines[[k for k in lines if k.endswith("gen-A")][0]]
    assert "0" in lines[[k for k in lines if k.endswith("gen-B")][0]]


def test_census_on_a_tools_dir_with_no_generations_is_quiet_and_clean(env) -> None:
    tools, stub_bin, bin_dir = env
    _stub_ps(stub_bin, ["  101 /usr/bin/vim x"])
    result = _sh("nx_census_report", tools, stub_bin, bin_dir)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_holder_pids_refuses_a_relative_generation(env) -> None:
    tools, stub_bin, bin_dir = env
    _make_gens(tools, "A")
    _stub_ps(stub_bin, ["  101 /usr/bin/vim x"])
    result = _sh("nx_generation_holder_pids gen-A", tools, stub_bin, bin_dir)
    assert result.returncode != 0
    assert "absolute" in result.stderr.lower()


# --------------------------------------------------------------------------
# symlinked entries: .7's legacy pseudo-generation
# --------------------------------------------------------------------------

def test_a_symlinked_generation_entry_is_censused_by_its_real_target(env) -> None:
    """.7 registers the legacy uv-tool tree as a pseudo-generation: a symlink
    inside tools/ pointing OUTSIDE it, at $(uv tool dir)/conexus. A holder's
    ps argv names that real path, never our synthetic pointer -- so census
    must resolve one level of symlink before grepping, or a live legacy
    holder reads as zero holders and GC's rule (c) would not protect it."""
    tools, stub_bin, bin_dir = env
    legacy_real = tools.parent / "uv-tool-dir" / "conexus"
    legacy_real.mkdir(parents=True)
    pseudo = tools / "gen-legacy-uv-tool"
    pseudo.symlink_to(legacy_real)
    _stub_ps(stub_bin, [f"  909 {legacy_real}/bin/python {legacy_real}/bin/nx-mcp"])

    result = _sh(f'nx_generation_holder_pids "{pseudo}"', tools, stub_bin, bin_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["909"], (
        "a live holder of the legacy tree was not attributed to its pseudo-"
        "generation pointer"
    )


def test_a_symlinked_generation_entry_with_no_holders_reports_none(env) -> None:
    tools, stub_bin, bin_dir = env
    legacy_real = tools.parent / "uv-tool-dir" / "conexus"
    legacy_real.mkdir(parents=True)
    pseudo = tools / "gen-legacy-uv-tool"
    pseudo.symlink_to(legacy_real)
    _stub_ps(stub_bin, ["  101 /usr/bin/vim x"])

    result = _sh(f'nx_generation_holder_pids "{pseudo}"', tools, stub_bin, bin_dir)

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_one_ps_snapshot_serves_the_whole_census(env) -> None:
    """Attribution must come from a SINGLE snapshot. Calling ps per generation
    lets a process exit between calls and appear to hold two trees, or none --
    and GC would then reap on a view that never existed at any instant."""
    tools, stub_bin, bin_dir = env
    _make_gens(tools, "A", "B", "C")
    counter = tools.parent / "ps-calls"
    ps = stub_bin / "ps"
    stub_bin.mkdir(parents=True, exist_ok=True)
    ps.write_text(f'#!/bin/sh\necho x >> "{counter}"\nexit 0\n')
    ps.chmod(0o755)

    _sh("nx_census_report", tools, stub_bin, bin_dir)

    calls = counter.read_text().count("x") if counter.exists() else 0
    assert calls == 1, f"ps was invoked {calls} times for one census; expected 1"


# --------------------------------------------------------------------------
# attribution is structural, not a denylist on argv text (nexus-qzawu)
# --------------------------------------------------------------------------

def test_a_holder_whose_argv_contains_the_word_grep_is_still_a_holder(env) -> None:
    """RG-B Critical. The transient-grep exclusion was a DENYLIST on argv text
    (``grep -v -e '[[:space:]]grep[[:space:]]' -e '[[:space:]]grep$'``), so it
    dropped every process whose argv merely contained the word -- ``nx search
    grep`` is a live holder that reported as none. GC's rule (c) then reaped the
    tree that process was running from: nexus-q3xrx, reachable with no symlink
    trickery. Reproduced against the real scripts before this test existed."""
    tools, stub_bin, bin_dir = env
    (a,) = _make_gens(tools, "A")
    _stub_ps(stub_bin, [f"  101 {a}/bin/python {a}/bin/nx search grep"])

    result = _sh(f'nx_generation_holder_pids "{a}"', tools, stub_bin, bin_dir)

    assert result.stdout.split() == ["101"], (
        "a live holder was dropped because its argv contained the word 'grep'; "
        "GC will reap the tree it is running from"
    )


def test_a_stamp_collision_sibling_does_not_borrow_its_neighbours_holders(env) -> None:
    """install_generation.sh suffixes a same-second stamp collision, so
    ``gen-<stamp>`` and ``gen-<stamp>a`` coexist by design. Substring matching
    made every holder of the suffixed tree count as a holder of the bare one."""
    tools, stub_bin, bin_dir = env
    base, suffixed = _make_gens(tools, "20260825", "20260825a")
    _stub_ps(stub_bin, [f"  202 {suffixed}/bin/python {suffixed}/bin/nx-mcp"])

    result = _sh(f'nx_generation_holder_pids "{base}"', tools, stub_bin, bin_dir)

    assert result.stdout.strip() == "", (
        f"gen-20260825 borrowed a holder of gen-20260825a: {result.stdout.split()}"
    )


def test_a_process_merely_naming_a_path_inside_a_generation_counts_as_a_holder(env) -> None:
    """DELIBERATE, and pinned here so it cannot be 'fixed' by accident. An editor
    with a file inside the tree open is not running from it, yet it is counted.

    Narrowing attribution to argv[0] would end this over-attribution and buy
    under-reporting instead. The failure directions are not symmetric: this way
    GC retains a tree nobody holds, which wastes disk until the next pass; the
    other way GC deletes a tree somebody is running from. Retention is the
    direction to fail in."""
    tools, stub_bin, bin_dir = env
    (a,) = _make_gens(tools, "A")
    _stub_ps(stub_bin, [f"  303 /usr/bin/vim {a}/README.txt"])

    result = _sh(f'nx_generation_holder_pids "{a}"', tools, stub_bin, bin_dir)

    assert result.stdout.split() == ["303"], (
        "attribution narrowed to argv[0]; that trades over-retention for "
        "under-reporting, which is the direction that deletes data"
    )
