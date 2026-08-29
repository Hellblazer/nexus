# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""`nx doctor` under the generation layout. nexus-utpuw.11.

TWO FAILURES, RELATED BY THE SAME CAUSE.

1. "Process freshness" rendered SILENT GREEN -- the exact failure it exists to
   prevent. It was born from three live incidents (6.7.0/6.7.1) where doctor
   said "latest" and the whole machine was stale. Under generations
   ``report.stale`` was empty BY CONSTRUCTION (nexus-utpuw.10: the markers were
   pinned to the current generation, while a stale process by definition runs
   from a different one), so the green branch fired unconditionally and said
   "all running conexus processes match the installed version" having examined
   NOTHING.

   .10 fixed the enumeration. This fixes the sentence: a green must state what
   it examined, because "no stale processes" and "no processes found at all"
   are different answers that rendered identically. A green over an empty
   examined-set is vacuously true, and a vacuous truth in a health check is
   indistinguishable from a real one at the point it matters.

   Note what was ALREADY fixed and is not this: nexus-bawvu closed the
   VANISHING-ROW mode, where a probe failure returned [] and the row disappeared
   entirely. That remains closed; the tests below do not re-litigate it.

2. Doctor knew nothing about the layout at all. health.py carried ZERO
   references to install_layout, so every way the generation layout can be
   broken -- a dangling `current`, a receipt-less build, uv taking the shims
   back -- was invisible to the one command whose job is noticing.

FAILURE DIRECTION, stated once and applied throughout: uncertain means SAY SO.
A check that cannot determine its answer warns; it never returns ok. This arc
has paid for the other direction repeatedly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus import health, install_layout


def _receipt(version: str = "7.18.0", base_interpreter: str = "/usr/bin") -> str:
    # A base_interpreter that EXISTS by default: the check below is about a
    # pruned one, and a fixture whose interpreter never exists would make every
    # other test in this module trip that row instead of the one it targets.
    return json.dumps({
        "schema": 1, "version": version, "spec": "conexus",
        # A LIST, which is what the shell half actually writes
        # (`"extras": ["local"]`) and what read_receipt requires. The first
        # version of this fixture used "" -- a shape no installer produces --
        # and read_receipt raised on every generation, which the check's
        # `except: continue` swallowed into "all interpreters present".
        "source_kind": "directory", "source": "/src/nexus", "extras": [],
        "python": "3.12", "base_interpreter": base_interpreter,
        "created_at": "2026-08-26T00:00:00Z", "installer_schema": 1,
    })


def _generation(
    tools: Path, stamp: str, *, receipt: bool = True,
    base_interpreter: str = "/usr/bin",
) -> Path:
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    # Real entry points, and a python that ANSWERS for them: the shim check
    # derives the names it owns by asking the generation's interpreter which
    # console scripts the distribution declares (the installer's own query,
    # GH #1487 / nexus-50hm9), never from a listing of <current>/bin -- so a
    # generation that cannot answer makes that check say so, not pass.
    for ep in ("nx", "nx-mcp"):
        (gen / "bin" / ep).write_text("#!/bin/sh\n")
    (gen / "bin" / "python").write_text("#!/bin/sh\nprintf 'nx\\nnx-mcp\\n'\n")
    (gen / "bin" / "python").chmod(0o755)
    if receipt:
        (gen / "nexus-install.json").write_text(
            _receipt(base_interpreter=base_interpreter)
        )
    return gen


@pytest.fixture
def layout(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    return tools, bin_dir


def _result(results, label_fragment):
    for r in results:
        if label_fragment.lower() in r.label.lower():
            return r
    raise AssertionError(f"no result labelled like {label_fragment!r}: {[r.label for r in results]}")


# --------------------------------------------------------------------------
# 1. a green must say what it examined
# --------------------------------------------------------------------------

def test_freshness_green_states_how_many_processes_it_examined(monkeypatch) -> None:
    """The silent green, killed at the sentence rather than the probe. With
    processes examined and none stale, the detail must say so with a COUNT --
    otherwise this row reads identically whether it checked 12 processes or
    zero."""
    from nexus import upgrade_finish as uf

    report = uf.SkewReport(installed_version="7.18.0", install_mtime=0.0)
    monkeypatch.setattr(uf, "detect_stale_processes", lambda: report)
    monkeypatch.setattr(uf, "install_source", lambda: "directory — /src/nexus")
    monkeypatch.setattr(uf, "enumerate_processes", lambda *a, **k: [
        (1, 10, "/g/bin/python /g/bin/nx-mcp"),
        (2, 10, "/g/bin/nx daemon service start --foreground"),
    ])

    row = _result(health._check_process_skew(), "Process freshness")

    assert row.ok is True
    assert "2" in row.detail, (
        f"a green that does not say what it examined is the silent green: {row.detail!r}"
    )


def test_freshness_green_says_plainly_when_it_examined_nothing(monkeypatch) -> None:
    """Zero conexus processes running is a legitimate state -- a fresh box with
    nothing started. What is NOT legitimate is rendering it as 'all running
    conexus processes match the installed version', which is a vacuous truth
    dressed as a positive finding. It must say it found none."""
    from nexus import upgrade_finish as uf

    report = uf.SkewReport(installed_version="7.18.0", install_mtime=0.0)
    monkeypatch.setattr(uf, "detect_stale_processes", lambda: report)
    monkeypatch.setattr(uf, "install_source", lambda: "directory — /src/nexus")
    monkeypatch.setattr(uf, "enumerate_processes", lambda *a, **k: [])

    row = _result(health._check_process_skew(), "Process freshness")

    detail = row.detail.lower()
    assert "no running conexus processes" in detail or "none" in detail, (
        f"an empty examined-set rendered as a positive finding: {row.detail!r}"
    )
    assert "all running conexus processes match" not in detail, (
        "claimed every process matches, having examined none"
    )


# --------------------------------------------------------------------------
# 2. the layout itself
# --------------------------------------------------------------------------

def test_a_healthy_layout_passes(layout) -> None:
    """Non-vacuity for everything below: a correct layout must be quiet, or the
    checks below prove nothing by failing."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\nexec \"$(readlink /t/current)/bin/nx\" \"$@\"\n")

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is True, row.detail


def test_a_dangling_current_pointer_is_a_hard_failure(layout) -> None:
    """`current` is what every shim resolves at spawn. Dangling means nothing
    starts -- not a warning."""
    tools, _ = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    import shutil
    shutil.rmtree(gen)

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False and row.warn is False, (
        f"a dangling current pointer did not fail: ok={row.ok} warn={row.warn}"
    )
    assert "current" in row.detail.lower()


def test_a_uv_owned_shim_symlink_is_reported(layout) -> None:
    """nexus-utpuw.7's ACCEPTED RISK, made visible. Between migration and reap
    uv still holds a valid receipt, so a stray `uv tool upgrade conexus`
    re-symlinks ~/.local/bin/nx over the nexus-owned shim and live sessions
    start resolving through uv's tree again. The mitigation is that re-running
    the installer repairs it -- which only helps if somebody knows to."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    uv_tree = tools.parent / "uvtools" / "conexus" / "bin"
    uv_tree.mkdir(parents=True)
    (uv_tree / "nx").write_text("#!/bin/sh\necho uv\n")
    (bin_dir / "nx").symlink_to(uv_tree / "nx")

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False, "uv taking the shim back was reported as healthy"
    assert "nx" in row.detail, row.detail


def test_a_receiptless_generation_directory_is_not_a_failure(layout) -> None:
    """Build wreckage, not breakage. A gen-* directory without a receipt is a
    build that died before finishing; nothing ever pointed `current` at it and
    GC reaps it. Reporting it as a fault would train the operator to ignore this
    row, which is how a real fault gets missed."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")
    _generation(tools, "20260826T020000Z", receipt=False)

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is True, (
        f"receipt-less build wreckage was reported as a fault: {row.detail!r}"
    )


def test_an_unreadable_layout_warns_rather_than_passing(layout, monkeypatch) -> None:
    """The failure direction, asserted rather than assumed. A check that cannot
    determine its answer must say so; returning ok would be the silent green
    this bead exists to remove, relocated one function over."""
    from nexus import install_layout

    # The layout must be INSTALLED for this path to be reachable: an empty
    # tools root now short-circuits to "nothing installed" before
    # current_generation is consulted at all, so patching it on a bare fixture
    # would test nothing.
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")

    def _boom(*a, **k):
        raise OSError("layout unreadable")

    monkeypatch.setattr(install_layout, "current_generation", _boom)

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False, "an unreadable layout passed"
    assert "could not" in row.detail.lower() or "unreadable" in row.detail.lower(), row.detail


# --------------------------------------------------------------------------
# RG-C findings against this check (nexus-utpuw.11 follow-up)
# --------------------------------------------------------------------------

# Every name in NEVER_SHIM, derived from the set itself rather than retyped:
# 29bac46f3's message claimed "one per excluded name" while covering five of
# nine (RG-C reviewer 2). Deriving it means the coverage cannot drift from the
# set again, and adding a name to NEVER_SHIM adds its regression case for free.
@pytest.mark.parametrize("intruder", sorted(install_layout.NEVER_SHIM))
def test_an_unrelated_tools_symlink_is_not_a_reclaimed_shim(layout, intruder) -> None:
    """FALSE POSITIVE found by RG-C, reproduced before fixing. The shim set is
    derived from <current>/bin -- but a venv's bin holds python, pip and
    activate, which nx_write_shims explicitly NEVER shims. ~/.local/bin is a
    SHARED directory: pyenv, asdf and homebrew all leave a `python` symlink
    there. Treating one as evidence that uv reclaimed our shims hard-fails a
    perfectly healthy install.

    That is this check's own docstring warning, inverted -- a row that cries
    wolf on common machine configurations trains the operator to ignore it,
    which is how the real reclaim gets missed."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    # The intruder name also lives in the generation's bin/ (a venv ships its
    # python, pip and activate). `python` is the answering stub the fixture
    # already wrote and must keep answering; the others are plain files.
    if not (gen / "bin" / intruder).exists():
        (gen / "bin" / intruder).write_text("#!/bin/sh\n")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")
    (bin_dir / intruder).symlink_to("/usr/bin/true")

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is True, (
        f"an unrelated {intruder!r} symlink was reported as uv reclaiming the "
        f"shims: {row.detail!r}"
    )


def test_a_uv_managed_interpreter_link_is_not_a_reclaimed_shim(layout) -> None:
    """GH #1487 (nexus-50hm9), reproduced before fixing. A generation's bin/
    holds the interpreter it was built from (python3.12); uv leaves a link of
    the SAME name in ~/.local/bin (`uv python install`). Deriving the owned
    set from the bin listing reported that link as uv reclaiming a shim and
    offered `nx self install` as the fix -- which rewrites every "taken" name
    into a nexus shim, i.e. would hijack the user's default python3.12. The
    owned set is what the distribution DECLARES; python3.12 is never in it."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (gen / "bin" / "python3.12").write_text("#!/bin/sh\n")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text(install_layout.render_shim("nx", tools=tools))
    (bin_dir / "nx-mcp").write_text(install_layout.render_shim("nx-mcp", tools=tools))
    uv_python = tools.parent / "uv-python" / "cpython-3.12" / "bin" / "python3.12"
    uv_python.parent.mkdir(parents=True)
    uv_python.write_text("#!/bin/sh\n")
    (bin_dir / "python3.12").symlink_to(uv_python)
    (bin_dir / "python3").symlink_to(uv_python)

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is True, (
        f"a uv-managed interpreter link was reported as a reclaimed shim: {row.detail!r}"
    )
    assert "python3.12" not in row.detail


def test_a_generation_that_cannot_answer_warns_rather_than_passing(layout) -> None:
    """Uncertain means say so: with no interpreter to ask, the owned set is
    unknown, and the check must not fall back to a directory listing (the
    GH #1487 shape) or return green over a set it never derived."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (gen / "bin" / "python").unlink()
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False and row.warn is True, row
    assert "console scripts" in row.detail, row.detail


def test_a_reclaimed_dependency_shim_is_caught_when_the_generation_ships_it(layout) -> None:
    """nx_write_shims also shims the dependency scripts (mineru, mineru-api)
    the distribution does not declare, when the generation built them. A
    symlink at that name is a reclaim exactly like `nx`."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (gen / "bin" / "mineru").write_text("#!/bin/sh\n")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")
    (bin_dir / "mineru").symlink_to("/usr/bin/true")

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False and "mineru" in row.detail, row.detail


def test_a_foreign_dependency_script_link_is_not_ours_when_the_generation_lacks_it(layout) -> None:
    """The same name on a generation WITHOUT mineru built (no extra): nexus
    wrote no shim there, so a separately installed mineru link is not a
    reclaim -- the writer's own present-in-bin rule, mirrored."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")
    (bin_dir / "mineru").symlink_to("/usr/bin/true")

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is True, row.detail


def test_a_real_reclaimed_shim_is_still_caught(layout) -> None:
    """Non-vacuity for the exclusions above: narrowing the set must not blind
    the check to the thing it exists for (nexus-utpuw.7's accepted risk)."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").symlink_to("/usr/bin/true")

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False and "nx" in row.detail


def test_a_broken_layout_is_fatal_so_doctors_exit_code_says_so(layout) -> None:
    """RG-C: none of the hard branches set fatal=True, so
    format_health_for_cli reported success while the row showed a dirty glyph.
    27 other checks of comparable or lesser severity set it. Automation that
    gates on doctor's EXIT STATUS -- rather than grepping for a glyph -- saw a
    pass on a layout where nothing will start."""
    tools, _ = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    import shutil
    shutil.rmtree(gen)

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False and row.fatal is True, (
        f"a broken layout is not fatal: ok={row.ok} fatal={row.fatal} -- "
        "doctor exits 0 while reporting that nothing will start"
    )


def test_generations_present_but_no_current_is_a_hard_failure(layout) -> None:
    """RG-C minor: a fully ABSENT current routed to WARN while only the
    DANGLING sub-case hard-failed, though the docstring claimed both.

    The distinction that actually matters is not present-vs-absent, it is
    whether anything was ever installed. Generations on disk with no pointer is
    a BROKEN install; an empty tools root is simply a box that has not
    installed yet, and hard-failing that would fail every fresh machine."""
    tools, _ = layout
    _generation(tools, "20260826T010000Z")  # built, but current never written

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False and row.warn is False, (
        f"generations exist with no current pointer, reported as: {row.detail!r}"
    )


def test_an_empty_tools_root_is_not_a_failure(layout) -> None:
    """The other half of the distinction above: nothing installed yet is not
    breakage, and a doctor that fails a fresh box is a doctor people stop
    running."""
    row = _result(health._check_generation_layout(), "Generation layout")

    # EXACTLY ok, not "ok or warn". The first version of this assertion read
    # `row.ok is not False or row.warn is True`, which accepts either rendering
    # and therefore pins neither -- mutating the branch to ok=False,warn=True
    # left it green (RG-C reviewer 2, proved by mutation). An assertion that
    # accepts both answers to the question it asks is not an assertion.
    assert row.ok is True and row.warn is False and row.fatal is False, (
        f"nothing-installed rendered as ok={row.ok} warn={row.warn} "
        f"fatal={row.fatal}: {row.detail!r}"
    )


# --------------------------------------------------------------------------
# the checks this bead enumerated that did not ship first time round
# --------------------------------------------------------------------------

def test_a_missing_base_interpreter_on_current_is_fatal(layout) -> None:
    """The bead calls this "the one failure we can only detect, never prevent"
    (research amendment 4, the uv-python-pruning class).

    A generation's venv does not contain its interpreter -- pyvenv.cfg records
    a ``home =`` pointing at one uv manages elsewhere. uv prunes those. When it
    prunes the one a generation points at, that tree stops working, and nothing
    nexus does can stop it happening: the only defence is noticing.

    On CURRENT that is fatal, because nothing will start."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z", base_interpreter="/opt/pruned/bin")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")

    row = _result(health._check_generation_layout(), "Base interpreter")

    assert row.ok is False and row.fatal is True, (
        f"a pruned base interpreter under current is not fatal: {row.detail!r}"
    )


def test_a_missing_base_interpreter_on_an_old_generation_warns(layout) -> None:
    """On a NON-current generation it is a warning, not a failure: that tree is
    a rollback target rather than the running install, and reporting it as
    breakage on a box that works fine is how this row gets ignored."""
    tools, bin_dir = layout
    good = _generation(tools, "20260826T020000Z")
    _generation(tools, "20260101T000000Z", base_interpreter="/opt/pruned/bin")
    (tools / "current").symlink_to(good)
    (bin_dir / "nx").write_text("#!/bin/sh\n")

    row = _result(health._check_generation_layout(), "Base interpreter")

    assert row.ok is False and row.warn is True and row.fatal is False, (
        f"a pruned interpreter on an old generation was not a warning: {row.detail!r}"
    )


def test_a_live_base_interpreter_passes(layout) -> None:
    """Non-vacuity for both above."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z", base_interpreter=str(Path("/usr/bin")))
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")

    row = _result(health._check_generation_layout(), "Base interpreter")
    assert row.ok is True, row.detail


def test_an_orphan_uv_install_is_reported_with_the_fix_named(layout, tmp_path, monkeypatch) -> None:
    """A uv-managed conexus alive ALONGSIDE the generation layout. nexus-utpuw.7
    leaves the legacy tree in place until it has zero holders, so its presence
    is expected during the migration window -- what makes it worth naming is
    that uv still holds a valid receipt for it, so `uv tool upgrade conexus`
    will rebuild it and re-symlink over the shims (.7's accepted risk)."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")
    home = tmp_path / "home"
    uv_conexus = home / ".local" / "share" / "uv" / "tools" / "conexus" / "bin"
    uv_conexus.mkdir(parents=True)
    (uv_conexus / "nx").write_text("#!/bin/sh\n")
    monkeypatch.setenv("HOME", str(home))
    # This test models uv's root UNDER $HOME; the suite fence pins UV_TOOL_DIR
    # to an empty fenced root (tests/_fence_home.py), which would hide it.
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)

    row = _result(health._check_generation_layout(), "uv install")

    assert row.ok is False, "an orphan uv install was not reported"
    assert "reinstall-tool" in " ".join(row.fix_suggestions) or "reinstall-tool" in row.detail, (
        f"reported without naming the fix: {row.detail!r} {row.fix_suggestions}"
    )


def test_no_orphan_uv_install_passes(layout, tmp_path, monkeypatch) -> None:
    """Non-vacuity: a migrated box with no legacy tree must be quiet."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

    row = _result(health._check_generation_layout(), "uv install")
    assert row.ok is True, row.detail


def test_a_shim_with_the_right_shape_and_wrong_content_is_caught(layout) -> None:
    """The bead asks that shims "match the template", not merely that they are
    regular files. A shim that is a regular file but resolves the WRONG pointer
    -- a stale one baked before NX_TOOLS_DIR moved, or a hand-edit -- has the
    right shape and the wrong behaviour, and the not-a-symlink check passes it."""
    from nexus import install_layout

    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\nexec /somewhere/else/bin/nx \"$@\"\n")

    row = _result(health._check_generation_layout(), "Shim")

    assert row.ok is False, "a shim that does not match the template passed"
    assert "nx" in row.detail
    # and the real template must pass
    (bin_dir / "nx").write_text(install_layout.render_shim("nx", tools=tools))
    assert _result(health._check_generation_layout(), "Shim").ok is True


def test_holders_of_older_generations_are_rendered_informationally(layout, monkeypatch) -> None:
    """The bead asks for the holder census "rendered informationally". Holders
    are a FACT, not a fault: they converge at their next spawn. A row that
    failed on them would contradict the acceptance criterion, which exists
    precisely so live holders stop being an obstacle."""
    from nexus import install_census

    tools, bin_dir = layout
    old = _generation(tools, "20260101T000000Z")
    new = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(new)
    (bin_dir / "nx").write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        install_census, "generation_holder_pids",
        lambda gen, snapshot=None: [4242] if Path(gen).name == old.name else [],
    )

    row = _result(health._check_generation_layout(), "Holders")

    assert row.ok is True, f"a live holder was rendered as a fault: {row.detail!r}"
    assert "4242" in row.detail or "1" in row.detail, row.detail


# --------------------------------------------------------------------------
# the silent-green regression guard the bead asked for BY NAME
# --------------------------------------------------------------------------

def test_a_stale_process_renders_red_not_green(layout, monkeypatch) -> None:
    """The bead's own words: "Add a test that a stale process under the
    generation layout renders RED, not green (the silent-green regression
    guard -- assert the failing colour, not merely that a string appears)."

    Every other test in this module asserts what a HEALTHY box reports. This
    one asserts the colour of an UNHEALTHY one, which is the direction the
    original defect failed in: the row was green while the machine was stale,
    and a test that only checks green-when-healthy would have passed
    throughout."""
    from nexus import upgrade_finish as uf

    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)

    report = uf.SkewReport(installed_version="7.18.0", install_mtime=0.0)
    report.stale.append(uf.StaleProcess(
        pid=4242, kind="mcp-host",
        command=f"{gen}/bin/python {gen}/bin/nx-mcp", age_s=99,
    ))
    monkeypatch.setattr(uf, "detect_stale_processes", lambda: report)
    monkeypatch.setattr(uf, "install_source", lambda: "directory — /src/nexus")

    row = _result(health._check_process_skew(), "Process freshness")

    assert row.ok is False, (
        f"a stale process rendered GREEN — the exact defect this row exists to "
        f"prevent: ok={row.ok} detail={row.detail!r}"
    )
    assert "4242" in row.detail, f"the stale pid is not named: {row.detail!r}"


# --------------------------------------------------------------------------
# remediation advice follows the layout the box actually has
# --------------------------------------------------------------------------

def test_upgrade_advice_names_the_generation_installer_on_a_generation_box(layout) -> None:
    """Three remediation strings named `uv tool upgrade conexus`,
    `uv tool install conexus==<pin>` and `uv tool install --reinstall conexus`.
    None of them touches a generation install, and the third actively triggers
    .7's accepted risk by rebuilding the uv tree over the shims."""
    tools, _ = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)

    advice = health._upgrade_advice("uv tool upgrade conexus")

    # CHANGED at nexus-utpuw.13, deliberately: this asserted
    # "reinstall-tool.sh". .11 chose the repo script because .14 had not
    # landed; `nx self install` is the packaged installer, is what .15's hook
    # actually runs, and needs no checkout — a generation box has `nx` on PATH
    # via the shims. A reader whose `nx` IS a checkout gets a refusal naming
    # scripts/reinstall-tool.sh, so that case corrects itself.
    assert "nx self install" in advice, advice
    assert "reinstall-tool.sh" not in advice, advice
    assert "uv tool" not in advice, f"uv advice survived on a generation box: {advice!r}"


def test_upgrade_advice_keeps_the_uv_form_on_an_unmigrated_box(layout) -> None:
    """NOT a blanket replacement, and this is the half a find-and-replace would
    have got wrong. A box that has not migrated still upgrades through uv --
    .7 leaves that state in place until the legacy tree has zero holders --
    and sending such a user to a script in a checkout they may not have is a
    different wrong answer."""
    advice = health._upgrade_advice("uv tool upgrade conexus")

    assert advice == "uv tool upgrade conexus", (
        f"an un-migrated box was told to run the generation installer: {advice!r}"
    )
