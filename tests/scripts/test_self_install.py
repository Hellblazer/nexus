# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx self install`` — the packaged installer. nexus-utpuw.14 (P6a).

THE ENABLING INSIGHT: under side-by-side generations a SELF-install is safe by
construction. The running nx builds a NEW tree and never mutates its own, so
the thing that was impossible before -- upgrading yourself while you are
running -- becomes ordinary. That is also why the machinery ships INSIDE the
package: scripts/reinstall-tool.sh (.8) is a thin repo wrapper around the same
scripts, and this command execs the packaged copy from its own untouched
generation. One implementation, no parity test.

THE HAZARD THIS MUST HONOUR: the installer is exec'd FROM generation N while it
builds N+1 and then reaps. GC rule (d) -- never delete the generation hosting
the running installer -- is the only thing standing between that and deleting
the tree the running process is executing from. .6 tests the rule; this tests
that this caller actually passes it.

SCOPE FENCE (nexus-utpuw.14, do not silently widen): this replaces the
MECHANISM of `uv tool upgrade conexus`. It does NOT merge that with
`nx upgrade`. RDR-143 CA-2 keeps them two commands deliberately -- binary
upgrade versus migration ladder -- and merging them is a separate RDR.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click
import pytest

from _generation_harness import SAFE_BASE_PATH, stub_uv

_REPO = Path(__file__).resolve().parents[2]


def _receipt(gen: Path, *, extras: list[str], source: str, source_kind: str) -> None:
    # spec and extras are ONE FACT: install_layout refuses a receipt whose spec
    # does not carry the extras it lists ("They are one fact, not two"). A
    # fixture that writes them inconsistently is a receipt no installer emits.
    spec = f"conexus[{','.join(extras)}]" if extras else "conexus"
    (gen / "nexus-install.json").write_text(json.dumps({
        "schema": 1, "version": "7.18.0",
        "spec": spec, "source_kind": source_kind, "source": source,
        "extras": extras, "python": "3.12", "base_interpreter": "/usr/bin",
        "created_at": "2026-08-26T00:00:00Z", "installer_schema": 1,
    }))


def _hosting_generation(tools: Path, stamp: str, **kw) -> Path:
    """A generation complete enough to be the one we are 'running from'."""
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    for ep in ("nx", "nx-mcp"):
        (gen / "bin" / ep).write_text("#!/bin/sh\n")
    (gen / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.8\n")
    _receipt(gen, extras=kw.get("extras", []),
             source=kw.get("source", "/src/nexus"),
             source_kind=kw.get("source_kind", "directory"))
    return gen


@pytest.fixture
def bed(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    # A REAL directory source: install_generation.sh verifies that a
    # directory-kind source exists, so a receipt naming a nonexistent path
    # fails the build for a reason unrelated to what is under test.
    src = tmp_path / "src-nexus"
    src.mkdir()
    (src / "pyproject.toml").write_text('[project]\nversion = "7.19.0"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    stub_uv(stub_bin)
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("PATH", f"{stub_bin}:{SAFE_BASE_PATH}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tools, bin_dir, src


def _run_self_install(**kw):
    from nexus.commands.self_cmd import perform_self_install

    return perform_self_install(**kw)


def test_self_install_builds_a_new_generation_and_flips(bed, monkeypatch) -> None:
    """The core: the running nx produces a NEW tree and repoints current."""
    tools, _, src = bed
    host = _hosting_generation(tools, "20260101T000000Z", source=str(src))
    (tools / "current").symlink_to(host)
    monkeypatch.setattr(sys, "prefix", str(host))

    _run_self_install()

    current = (tools / "current").resolve()
    assert current != host.resolve(), "current still points at the hosting generation"
    assert (current / "nexus-install.json").is_file(), "the new tree has no receipt"


def test_the_hosting_generation_survives_its_own_installer(bed, monkeypatch) -> None:
    """GC RULE (d), from the caller's side. The installer is exec'd FROM the
    tree it is about to consider reaping. .6 proves the rule holds; this proves
    THIS caller passes --self, which is the half that can be forgotten without
    any test in .6 noticing."""
    tools, _, src = bed
    # THE HOST MUST BE PROTECTED BY RULE (d) ALONE, or this proves nothing.
    # The first version of this test ran self-install from CURRENT, so after
    # the flip the host became `previous` and rule (b) protected it -- dropping
    # --self entirely left the test green. Found by mutation.
    #
    # So: run from the OLDEST generation while a newer one is current. After
    # the flip, previous is the generation that WAS current, and the host is
    # outside keep-1 and is neither current nor previous. Only rule (d) is
    # left between it and the reaper.
    host = _hosting_generation(tools, "20260101T000000Z", source=str(src))
    middle = _hosting_generation(tools, "20260601T000000Z", source=str(src))
    (tools / "current").symlink_to(middle)
    monkeypatch.setattr(sys, "prefix", str(host))

    _run_self_install(keep=1)

    assert (tools / "current").resolve() not in (host.resolve(), middle.resolve()), (
        "current did not advance, so the reap never had a reason to consider the host"
    )
    assert host.is_dir(), (
        "the installer reaped the generation it was running from -- rule (d) "
        "was not passed by this caller"
    )
    assert (host / "bin" / "nx").is_file(), "the hosting tree was gutted"


def test_extras_are_carried_forward(bed, monkeypatch) -> None:
    """The load-bearing reason `uv tool upgrade` was chosen over
    `uv tool install`: a raw install strips [local] and reintroduces the 5.6.2
    local-search P0. There is no uv receipt to re-derive them from now, so they
    must be threaded explicitly out of nexus-install.json."""
    tools, _, src = bed
    host = _hosting_generation(tools, "20260101T000000Z", extras=["local"], source=str(src))
    (tools / "current").symlink_to(host)
    monkeypatch.setattr(sys, "prefix", str(host))

    _run_self_install()

    new = (tools / "current").resolve()
    receipt = json.loads((new / "nexus-install.json").read_text())
    assert "local" in receipt["extras"], (
        f"extras were dropped by the self-install: {receipt['extras']!r} -- "
        "this is the 768->384 embedder downgrade vector"
    )


def test_extras_flag_adds_to_an_existing_install(bed, monkeypatch) -> None:
    """nexus-pffc4 THE FIX: `nx self install --extras dt` on a generation
    whose receipt already carries [local] produces a build with BOTH —
    merge, never replace (replacing would drop [local] and reintroduce the
    5.6.2 768->384 embedder downgrade)."""
    tools, _, src = bed
    host = _hosting_generation(tools, "20260101T000000Z", extras=["local"], source=str(src))
    (tools / "current").symlink_to(host)
    monkeypatch.setattr(sys, "prefix", str(host))

    _run_self_install(add_extras=("dt",))

    new = (tools / "current").resolve()
    receipt = json.loads((new / "nexus-install.json").read_text())
    # The builder normalizes receipt order; BOTH surviving is the contract.
    assert sorted(receipt["extras"]) == ["dt", "local"], receipt["extras"]


def test_extras_flag_dry_run_shows_the_merged_set(bed, monkeypatch, capsys) -> None:
    """The dry-run argv is the contract: receipt extras first, requested
    appended, comma-joined for install_generation.sh --extras. Repeatable
    and comma-separated forms both flatten; duplicates collapse."""
    tools, _, src = bed
    host = _hosting_generation(tools, "20260101T000000Z", extras=["local"], source=str(src))
    (tools / "current").symlink_to(host)
    monkeypatch.setattr(sys, "prefix", str(host))

    assert _run_self_install(dry_run=True, add_extras=("dt,local", "dt")) is None

    out = capsys.readouterr().out
    assert "--extras local,dt" in out, out


def test_extras_flag_refuses_junk_names(bed, monkeypatch) -> None:
    """A bad name must fail HERE with its own message, not as an opaque uv
    resolution error deep inside the generation build."""
    tools, _, src = bed
    host = _hosting_generation(tools, "20260101T000000Z", source=str(src))
    (tools / "current").symlink_to(host)
    monkeypatch.setattr(sys, "prefix", str(host))

    with pytest.raises(click.ClickException) as exc:
        _run_self_install(add_extras=("loc al",))
    assert "not a valid extra name" in str(exc.value)


def test_extras_flag_refuses_off_a_generation(bed, monkeypatch, tmp_path) -> None:
    """--extras is a generation-install surface: the legacy convergence
    branches bridge extras by their own rules, and silently dropping a
    requested one there is the green-then-degraded shape [local] exists to
    prevent. The refusal must say so, before any converge/refuse branching."""
    from nexus.commands.self_cmd import perform_self_install

    checkout_venv = tmp_path / "checkout" / ".venv"
    checkout_venv.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(checkout_venv))

    with pytest.raises(click.ClickException) as exc:
        perform_self_install(add_extras=("local",))
    message = str(exc.value)
    assert "--extras applies to a generation install" in message, message


@pytest.mark.parametrize("source_kind,source", [
    ("directory", None),   # None -> the bed's real source dir
    ("registry", "conexus"),
])
def test_both_source_kinds_round_trip(bed, monkeypatch, source_kind, source) -> None:
    """A self-install must reproduce the KIND it came from. Re-deriving it
    wrongly is how a dev install silently becomes a registry one -- nexus-q3xrx
    incident #2, arrived at from the other direction."""
    tools, _, src = bed
    host = _hosting_generation(tools, "20260101T000000Z",
                               source_kind=source_kind,
                               source=source if source else str(src))
    (tools / "current").symlink_to(host)
    monkeypatch.setattr(sys, "prefix", str(host))

    _run_self_install()

    new = (tools / "current").resolve()
    receipt = json.loads((new / "nexus-install.json").read_text())
    assert receipt["source_kind"] == source_kind, receipt


def test_it_does_not_run_the_migration_ladder(bed, monkeypatch) -> None:
    """SCOPE FENCE. RDR-143 CA-2 keeps binary upgrade and migration ladder as
    two commands on purpose. `nx upgrade` today never invokes uv or pip at all
    -- it is ladder-only -- so there is no existing home for flip/GC there and
    none for migrations here. A self-install that quietly ran migrations would
    merge them by accident."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def _record(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)):
            calls.append([str(c) for c in cmd])
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _record)

    tools, _, src = bed
    host = _hosting_generation(tools, "20260101T000000Z", source=str(src))
    (tools / "current").symlink_to(host)
    monkeypatch.setattr(sys, "prefix", str(host))

    _run_self_install()

    joined = [" ".join(c) for c in calls]
    assert not any("upgrade" in j and "nx" in j for j in joined), (
        f"the self-install invoked the migration ladder: {joined}"
    )


def test_the_packaged_machinery_is_resolvable(bed) -> None:
    """`nx self install` execs the PACKAGED scripts, not the repo's. If they do
    not ship, this command cannot work from a real install and the failure
    appears only after release."""
    from nexus.commands.self_cmd import packaged_install_dir

    d = packaged_install_dir()
    assert (d / "install_generation.sh").is_file(), f"not shipped: {d}"
    assert (d / "layout.sh").is_file()


def test_it_refuses_clearly_from_a_dev_checkout(bed, monkeypatch, tmp_path) -> None:
    """Found by running the command rather than testing it: from a dev
    checkout's .venv there is no receipt, and the first version died with a
    raw InstallLayoutError traceback naming a missing JSON file.

    A dev venv is not a generation and self-installing from one is not
    meaningful -- the repo has scripts/reinstall-tool.sh for exactly that. What
    matters is that it SAYS so. A traceback tells the reader that nexus is
    broken; the truth is that they are standing somewhere this command does not
    apply."""
    from nexus.commands.self_cmd import perform_self_install

    checkout_venv = tmp_path / "checkout" / ".venv"
    checkout_venv.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(checkout_venv))
    # nexus-gu9zo made "not a generation" a two-way branch: a PACKAGED
    # non-generation now converges instead of refusing. So this test has to say
    # which of the two it is, rather than inheriting the answer from whatever
    # shape the test runner itself was installed in — otherwise it silently
    # becomes a test of the developer's own install.
    import nexus.upgrade_finish as _uf
    monkeypatch.setattr(_uf, "running_from_tool_install", lambda: False)

    with pytest.raises(click.ClickException) as exc:
        perform_self_install()

    message = str(exc.value)
    assert "generation" in message.lower(), message
    assert "reinstall-tool.sh" in message, (
        f"refused without naming what to run instead: {message!r}"
    )


# ── nexus-gu9zo: the three-site classification ──────────────────────────────
#
# The original guard asked ONE path-shape question and treated every No as a
# dev checkout, which is wrong for the commonest install in existence. These
# pin all three arms, and — critically — pin them WITHOUT depending on what
# shape of install the test runner itself happens to be, which would make the
# whole family ambient.


def _not_a_generation(tmp_path, monkeypatch) -> Path:
    """A venv root that is real but is not under <tools> and is not gen-*."""
    site = tmp_path / "uv-tools" / "conexus"
    (site / "bin").mkdir(parents=True)
    (site / "pyvenv.cfg").write_text("home = /opt/py/bin\n")
    monkeypatch.setattr(sys, "prefix", str(site))
    return site


def _set_site_kind(monkeypatch, *, packaged: bool) -> None:
    """Pin the packaged-vs-dev-checkout answer instead of inheriting it.

    perform_self_install consults upgrade_finish.running_from_tool_install.
    Left unpatched these tests would pass or fail according to how THIS
    checkout was installed, which is precisely the ambient-state dependency
    that makes a gate stop meaning anything.
    """
    import nexus.upgrade_finish as uf
    monkeypatch.setattr(uf, "running_from_tool_install", lambda: packaged)


def test_a_packaged_uv_tool_install_converges_instead_of_refusing(
    bed, monkeypatch, tmp_path,
) -> None:
    """THE BEAD. A fresh `uv tool install conexus` lands on the legacy layout;
    before this it got "nothing to do from a dev checkout" and a pointer to a
    repo script it has no copy of."""
    tools, _, _ = bed
    _not_a_generation(tmp_path, monkeypatch)
    _set_site_kind(monkeypatch, packaged=True)

    seen = {}

    def _fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"{tools}/gen-20260827T000000Z\n", stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = _run_self_install()

    assert result is not None, "converge returned nothing"
    assert seen["argv"][0] == "bash"
    assert seen["argv"][1].endswith("migrate_legacy.sh"), seen["argv"]
    assert "--source" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--source") + 1] == "conexus"


def test_converge_does_not_thread_extras_itself(bed, monkeypatch, tmp_path) -> None:
    """A legacy box has no receipt — that is what makes it legacy. Extras must
    come from migrate_legacy.sh reading the legacy uv-receipt.toml one last
    time; passing --extras from here would mean inventing them."""
    _not_a_generation(tmp_path, monkeypatch)
    _set_site_kind(monkeypatch, packaged=True)
    seen = {}

    def _fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="/t/gen-x\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _run_self_install()
    assert "--extras" not in seen["argv"], seen["argv"]


def test_converge_never_reaps_in_the_same_pass(bed, monkeypatch, tmp_path) -> None:
    """migrate_legacy.sh never sources gc.sh so a reap cannot fire while the
    legacy tree still has live holders. This caller must not add one back."""
    _not_a_generation(tmp_path, monkeypatch)
    _set_site_kind(monkeypatch, packaged=True)
    calls = []

    def _fake_run(argv, **kw):
        calls.append(" ".join(str(a) for a in argv))
        return subprocess.CompletedProcess(argv, 0, stdout="/t/gen-x\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _run_self_install()
    assert not any("nx_gc_generations" in c for c in calls), calls


def test_a_real_dev_checkout_still_refuses(bed, monkeypatch, tmp_path) -> None:
    """The arm that must NOT change. A checkout has no receipt and this
    command does not apply there."""
    _not_a_generation(tmp_path, monkeypatch)
    _set_site_kind(monkeypatch, packaged=False)

    with pytest.raises(click.ClickException) as exc:
        _run_self_install()
    assert "reinstall-tool.sh" in str(exc.value)


def test_a_migration_that_migrated_nothing_is_not_reported_as_success(
    bed, monkeypatch, tmp_path,
) -> None:
    """migrate_legacy.sh's clean no-op is empty stdout. Reaching it HERE is a
    contradiction — we only got here because this looked like a legacy tool
    install — so it must be surfaced, not returned as a successful converge
    that moved nothing."""
    _not_a_generation(tmp_path, monkeypatch)
    _set_site_kind(monkeypatch, packaged=True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )

    with pytest.raises(click.ClickException) as exc:
        _run_self_install()
    assert "no legacy tree" in str(exc.value).lower(), str(exc.value)


def test_converge_failure_surfaces_stderr(bed, monkeypatch, tmp_path) -> None:
    _not_a_generation(tmp_path, monkeypatch)
    _set_site_kind(monkeypatch, packaged=True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 64, stdout="", stderr="migrate_legacy: uv not resolvable"),
    )

    with pytest.raises(click.ClickException) as exc:
        _run_self_install()
    assert "uv not resolvable" in str(exc.value)


def test_converge_dry_run_builds_no_generation(bed, monkeypatch, tmp_path) -> None:
    _not_a_generation(tmp_path, monkeypatch)
    _set_site_kind(monkeypatch, packaged=True)
    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    assert _run_self_install(dry_run=True) is None
    assert not calls, "dry run executed something"


def test_the_migration_machinery_also_ships_in_the_wheel() -> None:
    """The converge path execs migrate_legacy.sh from the PACKAGE. Nothing
    asserted it ships — only install_generation.sh and layout.sh were pinned —
    and per that test's own docstring the failure would appear only after
    release. legacy.sh rides along because migrate_legacy.sh sources it."""
    from nexus.commands.self_cmd import packaged_install_dir

    d = packaged_install_dir()
    assert (d / "migrate_legacy.sh").is_file(), f"not shipped: {d}"
    assert (d / "legacy.sh").is_file(), f"not shipped: {d}"
