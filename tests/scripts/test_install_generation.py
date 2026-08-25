# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation builder: build a new tree, never touch an existing one.

nexus-utpuw.2 (P1a). ``src/nexus/_install/install_generation.sh`` builds one
generation at ``$NX_TOOLS_DIR/gen-<stamp>`` and writes its receipt. It is the
half of nexus-utpuw that makes an install safe under live holders, so the
load-bearing test here is ``test_an_existing_generation_is_byte_identical_after``
— if that ever goes red, side-by-side has stopped being side-by-side and the
whole epic is void.

WHAT A STUBBED uv DOES AND DOES NOT PROVE. These tests put a fake ``uv`` on
PATH that fabricates a venv shape and records its argv. That proves the
ORCHESTRATION: which directory is built, what spec uv is handed, when the
receipt appears, what happens on failure. It proves nothing about uv actually
producing a working venv — that belongs to the live-holder E2E tiers (.16/.17)
and is not claimed here.

THE COMPLETION MARKER. A venv cannot be built elsewhere and moved into place:
console-script shebangs bake absolute paths at install time, which is why the
design bans ``uv venv --relocatable`` and requires building at the final path.
So visibility cannot come from an atomic directory rename. It comes from the
receipt instead — a generation is a ``gen-*`` directory CONTAINING a valid
``nexus-install.json``, written last. A crash before that leaves a directory
that no enumerator counts.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_INSTALLER = _REPO / "src" / "nexus" / "_install" / "install_generation.sh"


def _make_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _stub_uv(bin_dir: Path, *, argv_log: Path, fail_install: bool = False) -> None:
    """A fake ``uv`` that fabricates a venv and records every invocation.

    ``uv venv <dir>`` creates the shape the builder inspects: ``bin/python``
    plus a ``pyvenv.cfg`` carrying a ``home =`` line, which is the field the
    receipt's ``base_interpreter`` is read from and the field CPython itself
    consults. ``uv pip install`` appends its argv to *argv_log* so the tests
    can assert the SPEC uv actually received, rather than asserting the
    builder's string construction against itself.
    """
    fail = "exit 1" if fail_install else "exit 0"
    _make_executable(
        bin_dir / "uv",
        f"""#!/bin/bash
echo "$@" >> "{argv_log}"
if [[ "$1" == "venv" ]]; then
    target=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --python) shift 2 ;;
            -*) shift ;;
            venv) shift ;;
            *) target="$1"; shift ;;
        esac
    done
    mkdir -p "$target/bin"
    printf '#!/bin/sh\\necho stub-python\\n' > "$target/bin/python"
    chmod +x "$target/bin/python"
    printf 'home = /opt/uv/pythons/cpython-3.12.8/bin\\nversion = 3.12.8\\n' > "$target/pyvenv.cfg"
    exit 0
fi
if [[ "$1" == "pip" && "$2" == "install" ]]; then
    {fail}
fi
exit 0
""",
    )


def _run(tools: Path, *args: str, uv_bin: Path, extra_env: dict | None = None):
    env = {
        "PATH": f"{uv_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tools.parent / "home"),
        "NX_TOOLS_DIR": str(tools),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(_INSTALLER), *args],
        capture_output=True, text=True, env=env,
    )


def _build(tools: Path, uv_bin: Path, argv_log: Path, *extra: str):
    """Build one generation and return (CompletedProcess, generation Path)."""
    result = _run(tools, "--source", "conexus", "--version", "7.18.0", *extra, uv_bin=uv_bin)
    assert result.returncode == 0, f"builder failed: {result.stderr}"
    gen = Path(result.stdout.strip().splitlines()[-1])
    return result, gen


def _tree_hashes(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.fixture
def env(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    uv_bin = tmp_path / "stubbin"
    argv_log = tmp_path / "uv-argv.log"
    _stub_uv(uv_bin, argv_log=argv_log)
    return tools, uv_bin, argv_log


def test_installer_is_present_and_executable() -> None:
    # Not skipif-gated: this file is committed, so its absence is the loudest
    # thing this suite could report, not a reason to report nothing.
    assert _INSTALLER.is_file(), f"{_INSTALLER} is missing"


# --------------------------------------------------------------------------
# THE property: an existing generation is never touched
# --------------------------------------------------------------------------

def test_an_existing_generation_is_byte_identical_after(env) -> None:
    """THE test of this bead. nexus-utpuw exists because in-place
    ``uv tool install --reinstall`` rebuilds the tree live processes are
    running from. If a build ever mutates a byte of an existing generation,
    side-by-side has stopped being side-by-side."""
    tools, uv_bin, argv_log = env

    _, first = _build(tools, uv_bin, argv_log)
    (first / "bin" / "nx").write_text("#!/bin/sh\necho first-generation\n")
    before = _tree_hashes(first)
    assert before, "fixture is vacuous: the first generation has no files to compare"

    _, second = _build(tools, uv_bin, argv_log)

    assert second != first, "the second build must land in its own directory"
    assert _tree_hashes(first) == before, (
        "a pre-existing generation was mutated by a subsequent build -- this is "
        "the exact failure nexus-utpuw exists to make impossible"
    )
    assert (first / "bin" / "nx").read_text() == "#!/bin/sh\necho first-generation\n"


def test_a_second_build_does_not_reuse_an_existing_stamp(env) -> None:
    """Two builds inside the same clock second must not collide. Second-
    resolution timestamps do not prevent that; refusing to build into an
    existing directory does."""
    tools, uv_bin, argv_log = env
    _, first = _build(tools, uv_bin, argv_log)
    _, second = _build(tools, uv_bin, argv_log)
    _, third = _build(tools, uv_bin, argv_log)
    assert len({first, second, third}) == 3, "generation directories collided"


# --------------------------------------------------------------------------
# shape, receipt, and the completion marker
# --------------------------------------------------------------------------

def test_generation_has_the_expected_shape(env) -> None:
    tools, uv_bin, argv_log = env
    _, gen = _build(tools, uv_bin, argv_log)

    assert gen.parent == tools
    assert gen.name.startswith("gen-")
    assert (gen / "bin" / "python").exists()
    assert (gen / "nexus-install.json").is_file()


def test_stamps_sort_chronologically(env) -> None:
    """GC's keep-last-N (.6) is only well-defined if lexical order is creation
    order. Pinned here because .6 will rely on it."""
    tools, uv_bin, argv_log = env
    names = [_build(tools, uv_bin, argv_log)[1].name for _ in range(3)]
    assert names == sorted(names)


def test_receipt_round_trips_through_the_python_contract(env) -> None:
    """The builder writes it in shell; nexus-utpuw.9 reads it in Python. The
    receipt must satisfy the contract .1 locked, not merely be JSON."""
    from nexus.install_layout import Receipt

    tools, uv_bin, argv_log = env
    _, gen = _build(tools, uv_bin, argv_log, "--extras", "local")

    receipt = Receipt.from_json((gen / "nexus-install.json").read_text())
    assert receipt.version == "7.18.0"
    assert receipt.extras == ["local"], "extras are the 768->384 embedder P0"
    assert receipt.source_kind == "registry"
    assert receipt.spec == "conexus[local]==7.18.0"


def test_receipt_base_interpreter_comes_from_pyvenv_cfg(env) -> None:
    """.11's doctor check tests whether that path still exists, and the thing
    that goes missing is what pyvenv.cfg's ``home`` names -- so record that,
    not something re-derived."""
    tools, uv_bin, argv_log = env
    _, gen = _build(tools, uv_bin, argv_log)

    payload = json.loads((gen / "nexus-install.json").read_text())
    assert payload["base_interpreter"] == "/opt/uv/pythons/cpython-3.12.8/bin"
    assert payload["python"] == "3.12.8"


def test_a_failed_install_leaves_no_generation(env, tmp_path) -> None:
    """TRIPWIRE for the completion marker. A venv must be built AT its final
    path (shebangs bake absolute paths), so visibility cannot come from an
    atomic rename -- it comes from the receipt being written last. A crash
    before that must leave a directory no enumerator counts."""
    tools, _, argv_log = env
    failing_uv = tmp_path / "failbin"
    _stub_uv(failing_uv, argv_log=argv_log, fail_install=True)

    result = _run(tools, "--source", "conexus", "--version", "7.18.0", uv_bin=failing_uv)

    assert result.returncode != 0, "a failed install must fail the builder"
    generations = [d for d in tools.glob("gen-*") if (d / "nexus-install.json").exists()]
    assert generations == [], (
        "a half-built tree carries no receipt, so it is not a generation -- "
        "otherwise GC could keep it and reap a working one"
    )


# --------------------------------------------------------------------------
# spec construction -- asserted against what uv actually received
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("args", "expected_spec", "expected_kind"),
    [
        (("--source", "conexus", "--version", "7.18.0"), "conexus==7.18.0", "registry"),
        (("--source", "conexus", "--version", "7.18.0", "--extras", "local"),
         "conexus[local]==7.18.0", "registry"),
        (("--source", "conexus", "--version", "7.18.0", "--extras", "local,dev"),
         "conexus[dev,local]==7.18.0", "registry"),
        (("--source", "."), ".", "directory"),
        (("--source", ".", "--extras", "local"), ".[local]", "directory"),
    ],
)
def test_spec_reaches_uv_with_extras_before_the_pin(
    env, args, expected_spec, expected_kind,
) -> None:
    """``conexus==7.18.0[local]`` is not a valid requirement. Asserted on uv's
    RECORDED ARGV rather than on the receipt, so this tests what uv was handed
    rather than the builder agreeing with itself."""
    tools, uv_bin, argv_log = env
    result = _run(tools, *args, uv_bin=uv_bin)
    assert result.returncode == 0, result.stderr

    invocations = argv_log.read_text().splitlines()
    installs = [line for line in invocations if line.startswith("pip install")]
    assert len(installs) == 1, f"expected exactly one pip install, got {installs}"
    assert expected_spec in installs[0], f"uv received {installs[0]!r}"

    gen = Path(result.stdout.strip().splitlines()[-1])
    payload = json.loads((gen / "nexus-install.json").read_text())
    assert payload["spec"] == expected_spec
    assert payload["source_kind"] == expected_kind


def test_the_venv_is_built_at_its_final_path(env) -> None:
    """Not built elsewhere and moved: console-script shebangs bake absolute
    paths at install time, so a relocation would leave every entry point
    pointing at a directory that no longer exists."""
    tools, uv_bin, argv_log = env
    _, gen = _build(tools, uv_bin, argv_log)

    invocations = argv_log.read_text().splitlines()
    venvs = [line for line in invocations if line.startswith("venv")]
    assert any(str(gen) in line for line in venvs), (
        f"uv venv was not pointed at the final generation path: {venvs}"
    )
    assert not any("--relocatable" in line for line in venvs), (
        "--relocatable rewrites shebangs to an exec trick; absolute baked paths "
        "are exactly what this design requires"
    )


def test_pip_install_targets_the_new_generations_interpreter(env) -> None:
    tools, uv_bin, argv_log = env
    _, gen = _build(tools, uv_bin, argv_log)
    installs = [l for l in argv_log.read_text().splitlines() if l.startswith("pip install")]
    assert str(gen / "bin" / "python") in installs[0]


# --------------------------------------------------------------------------
# interface
# --------------------------------------------------------------------------

def test_builder_prints_the_generation_path(env) -> None:
    """.3 flips ``current`` to whatever this built. If the caller had to
    re-derive the stamp it would race with collision resolution."""
    tools, uv_bin, argv_log = env
    result, gen = _build(tools, uv_bin, argv_log)
    assert gen.is_dir()
    assert gen.parent == tools


def test_builder_honours_NX_TOOLS_DIR(env, tmp_path) -> None:
    tools, uv_bin, argv_log = env
    elsewhere = tmp_path / "other-tools"
    elsewhere.mkdir()
    result = _run(elsewhere, "--source", "conexus", "--version", "7.18.0", uv_bin=uv_bin)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip().splitlines()[-1]).parent == elsewhere
    assert list(tools.glob("gen-*")) == []


def test_builder_refuses_a_missing_source(env) -> None:
    tools, uv_bin, _ = env
    result = _run(tools, "--version", "7.18.0", uv_bin=uv_bin)
    assert result.returncode != 0
    assert "source" in result.stderr.lower()


def test_builder_refuses_a_relative_tools_override(env) -> None:
    """The layout contract refuses a relative NX_TOOLS_DIR; the builder must
    inherit that refusal rather than resolving it against its own CWD."""
    tools, uv_bin, _ = env
    result = _run(tools, "--source", "conexus", "--version", "7.18.0",
                  uv_bin=uv_bin, extra_env={"NX_TOOLS_DIR": "relative/tools"})
    assert result.returncode != 0
    assert "absolute" in result.stderr.lower()


def test_source_kind_does_not_depend_on_the_working_directory(env, tmp_path) -> None:
    """TRIPWIRE. Classifying by ``[ -e "$SOURCE" ]`` makes the answer depend on
    where the caller stands: from the repo root, ``--source conexus`` finds the
    plugin directory ``conexus/`` and a registry install is silently recorded as
    a directory one. .7's legacy migration keys off source_kind, so a wrong
    answer here is not cosmetic."""
    tools, uv_bin, _ = env

    # A directory named exactly like the distribution, in the CWD the builder runs from.
    decoy_cwd = tmp_path / "decoy"
    (decoy_cwd / "conexus").mkdir(parents=True)

    result = subprocess.run(
        ["bash", str(_INSTALLER), "--source", "conexus", "--version", "7.18.0"],
        capture_output=True, text=True, cwd=decoy_cwd,
        env={
            "PATH": f"{uv_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path / "home"),
            "NX_TOOLS_DIR": str(tools),
        },
    )
    assert result.returncode == 0, result.stderr
    gen = Path(result.stdout.strip().splitlines()[-1])
    payload = json.loads((gen / "nexus-install.json").read_text())
    assert payload["source_kind"] == "registry", (
        "a bare distribution name is a registry source wherever the caller stands"
    )


def test_a_directory_source_that_does_not_exist_fails_loudly(env) -> None:
    tools, uv_bin, _ = env
    result = _run(tools, "--source", "/nonexistent/checkout", uv_bin=uv_bin)
    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_the_installer_is_executable_and_the_library_is_not() -> None:
    """hatchling propagates the source file's mode into the wheel (verified:
    0o755 for this script, 0o644 for layout.sh). A lost exec bit would not
    surface until `nx self install` (.14) tried to exec a packaged copy, with
    an error that names permissions rather than the chmod that caused it."""
    assert os.access(_INSTALLER, os.X_OK), (
        "install_generation.sh is EXECUTED (reinstall-tool.sh, and .14's "
        "`nx self install` exec the packaged copy)"
    )
    layout = _INSTALLER.parent / "layout.sh"
    assert not os.access(layout, os.X_OK), (
        "layout.sh is SOURCED, never executed -- an exec bit on it invites "
        "someone to run it, and its set-no-options contract assumes a caller"
    )


def test_a_pyvenv_cfg_without_home_refuses_rather_than_writing_an_empty_field(env, tmp_path) -> None:
    """RG-A finding. An EMPTY base_interpreter is worse than a missing one:
    .11's doctor check tests whether that path still exists, and Path("")
    resolves to False, so every healthy install would read as "the base
    interpreter was pruned" -- or, written the other way, as a silent pass.
    Either way the check stops meaning anything."""
    tools, _, argv_log = env
    broken_uv = tmp_path / "brokenbin"
    _stub_uv(broken_uv, argv_log=argv_log)
    # A uv that builds a venv whose pyvenv.cfg has no `home` line.
    uv = broken_uv / "uv"
    uv.write_text(uv.read_text().replace(
        "printf 'home = /opt/uv/pythons/cpython-3.12.8/bin\\nversion = 3.12.8\\n'",
        "printf 'version = 3.12.8\\n'"))

    result = _run(tools, "--source", "conexus", "--version", "7.18.0", uv_bin=broken_uv)

    assert result.returncode != 0
    assert "base_interpreter" in result.stderr
    assert [d for d in tools.glob("gen-*") if (d / "nexus-install.json").exists()] == [], (
        "a generation we cannot describe must not get a receipt"
    )


def test_stamp_exhaustion_fails_loudly(env) -> None:
    """Nine colliding suffixes is a bound, not a guarantee. .14's self-install
    and the SessionStart auto-upgrade can both fire near-simultaneously, so the
    exhaustion path is reachable -- and it must say so rather than overwrite."""
    tools, uv_bin, _ = env
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    # Claim every candidate for this second and the next two, so the run cannot
    # simply tick past the collision.
    for offset in range(3):
        stamp = (now + datetime.timedelta(seconds=offset)).strftime("%Y%m%dT%H%M%SZ")
        for suffix in ["", *"abcdefgh"]:
            (tools / f"gen-{stamp}{suffix}").mkdir(exist_ok=True)

    result = _run(tools, "--source", "conexus", "--version", "7.18.0", uv_bin=uv_bin)

    assert result.returncode != 0
    assert "collision" in result.stderr.lower()
