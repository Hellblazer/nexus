# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The shim writer: nexus-owned regular files, derived not hardcoded.

nexus-utpuw.4 (P1c). Writes ``<bin>/<command>`` for every console script the
installed distribution declares, plus an explicit dependency list, using the
body ``.1`` fixed. These are the files a PATH lookup finds, so they are what
binds a spawn to a generation.

WHY THE SET IS DERIVED. Audit finding F1: a hardcoded five-name allowlist
omitted ``nx-session-end-launcher``, the project's FOURTH console script, which
``conexus/hooks/hooks.json`` invokes by bare PATH name with no fallback — every
SessionEnd flush would have died silently after migration. It works today only
because uv links all of a project's own entry points, so the omission would not
have surfaced until the layout moved. The rule is therefore
``entry_points ∪ {mineru, mineru-api}``, and
``test_a_new_entry_point_is_shimmed_with_no_code_change`` is what keeps it a
rule rather than a longer list.

WHY THE DEPENDENCY LIST IS SEPARATE AND STILL EXPLICIT. uv does not link a
DEPENDENCY's entry points, which is the entire reason the old
``reinstall-tool.sh`` symlinked mineru by hand. But ``<gen>/bin`` also holds
``python``, ``pip`` and ``activate``, so this stays an exclusion discipline —
never a glob over that directory.

ENTRY POINTS ARE THIRD-PARTY DATA. They come from whatever wheels the
distribution depends on, so a name reaching the shim body is not ours. The
allowlist ``.1`` enforces is what stands between a malicious or careless
console-script name and a file written into the operator's PATH — see
nexus-xk7g2, where a denylist admitted ``nx$(touch${IFS}PWNED)``. A name that
fails it is SKIPPED WITH A WARNING, never silently dropped.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SHIMS = _REPO / "src" / "nexus" / "_install" / "shims.sh"


def _sh(snippet: str, tools: Path, bin_dir: Path, extra_env: dict | None = None):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tools.parent / "home"),
        "NX_TOOLS_DIR": str(tools),
        "NX_BIN_DIR": str(bin_dir),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", f'. "{_SHIMS}"; {snippet}'],
        capture_output=True, text=True, env=env,
    )


def _make_gen(tools: Path, name: str, *, entry_points: list[str], bin_extras: list[str] = ()):
    """A generation whose own python answers the entry-point query.

    Stubbing ``<gen>/bin/python`` is the seam: the writer asks the generation
    what it declares, so a test can change the answer without touching code —
    which is exactly what F1's regression case needs.
    """
    gen = tools / f"gen-{name}"
    (gen / "bin").mkdir(parents=True)
    (gen / "nexus-install.json").write_text("{}")

    # The names are DATA, read from a file -- never interpolated into the stub's
    # source. Interpolating them made the stub itself evaluate `$(touch ...)`
    # when it ran, so the payload fired in the harness and the writer only ever
    # received the harmless prefix: the hostile-name cases passed for the wrong
    # reason, and left a marker file in the repo root. Same defect as the
    # rendered-shim injection test, one layer up, and caught the same way --
    # by a marker appearing somewhere it should not.
    names_file = gen / "declared-entry-points.txt"
    names_file.write_text("".join(f"{ep}\n" for ep in entry_points))
    python = gen / "bin" / "python"
    python.write_text(f'#!/bin/sh\ncat "{names_file}"\n')
    python.chmod(python.stat().st_mode | stat.S_IXUSR)

    # Everything the writer might find in bin/, including things it must not shim.
    for name_ in [*entry_points, *bin_extras, "pip", "activate"]:
        target = gen / "bin" / name_
        if target.exists():
            continue
        # Echoes its arguments so the end-to-end test proves the shim forwards
        # "$@" rather than merely reaching the right binary.
        target.write_text(f'#!/bin/sh\necho "ran-{name_} $*"\n')
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return gen


@pytest.fixture
def env(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return tools, bin_dir


_PROJECT_SCRIPTS = ["nx", "nx-mcp", "nx-mcp-catalog", "nx-session-end-launcher"]


def test_shim_writer_is_present() -> None:
    assert _SHIMS.is_file(), f"{_SHIMS} is missing"


# --------------------------------------------------------------------------
# the derived set
# --------------------------------------------------------------------------

def test_every_declared_console_script_is_shimmed(env) -> None:
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=_PROJECT_SCRIPTS)

    result = _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    assert result.returncode == 0, result.stderr
    for name in _PROJECT_SCRIPTS:
        assert (bin_dir / name).is_file(), f"{name} was not shimmed"


def test_nx_session_end_launcher_is_not_forgotten(env) -> None:
    """The F1 case by name. hooks.json invokes it by bare PATH name with no
    fallback, so omitting it kills every SessionEnd flush -- silently."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=_PROJECT_SCRIPTS)
    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)
    assert (bin_dir / "nx-session-end-launcher").is_file()


def test_a_new_entry_point_is_shimmed_with_no_code_change(env) -> None:
    """THE test that keeps this a rule instead of a list. A fifth console
    script appears in the distribution and must be shimmed without anyone
    editing the writer."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=[*_PROJECT_SCRIPTS, "nx-brand-new-verb"])

    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    assert (bin_dir / "nx-brand-new-verb").is_file(), (
        "the shim set must be derived from the distribution's entry points, not "
        "from a hardcoded list that a fifth script would silently fall off"
    )


def test_dependency_scripts_are_shimmed_although_uv_would_not_link_them(env) -> None:
    """mineru is a default dependency, and uv links only the project's OWN
    entry points -- which is why reinstall-tool.sh symlinked these by hand.
    Dropping them regresses `nx mineru start` through its PATH fallback."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=_PROJECT_SCRIPTS,
                    bin_extras=["mineru", "mineru-api"])

    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    assert (bin_dir / "mineru").is_file()
    assert (bin_dir / "mineru-api").is_file()


@pytest.mark.parametrize("never", ["python", "pip", "activate"])
def test_the_venvs_own_machinery_is_never_shimmed(never: str, env) -> None:
    """An exclusion discipline, not a glob over <gen>/bin."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=_PROJECT_SCRIPTS)
    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)
    assert not (bin_dir / never).exists(), f"{never} must never be shimmed"


def test_a_declared_script_missing_from_bin_is_skipped_quietly(env) -> None:
    """Declared but not built (a partial install, an optional extra). No shim,
    and no error -- a shim pointing at nothing would fail at exec with a
    message about the target rather than about the install."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=[*_PROJECT_SCRIPTS, "nx-declared-not-built"])
    (gen / "bin" / "nx-declared-not-built").unlink()

    result = _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    assert result.returncode == 0, result.stderr
    assert not (bin_dir / "nx-declared-not-built").exists()
    assert (bin_dir / "nx").is_file(), "one missing target must not abort the rest"


# --------------------------------------------------------------------------
# entry points are third-party data
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "nx$(touch${IFS}PWNED)",
    "nx`id`",
    'nx";id;"',
    "nx;id",
    "-nx",
    "../escape",
])
def test_a_hostile_entry_point_name_is_refused_not_written(hostile: str, env) -> None:
    """TRIPWIRE. These names arrive from third-party wheels, and the shim body
    interpolates them into a shell script written onto the operator's PATH.
    nexus-xk7g2 is the reproduction: a denylist admitted the first of these."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=["nx", hostile])

    result = _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    assert (bin_dir / "nx").is_file(), "a bad sibling must not abort the good ones"
    written = sorted(p.name for p in bin_dir.iterdir())
    assert written == ["nx"], f"a refused name still produced a file: {written}"
    # The payload's own effect, checked where it would actually land: the shim
    # writer's working directory, not just the bin dir.
    assert not (bin_dir / "PWNED").exists()
    assert not (tools.parent / "PWNED").exists()
    assert not (Path.cwd() / "PWNED").exists()


def test_a_refused_name_is_warned_about_not_silently_dropped(env) -> None:
    """Silence here means an operator whose tool vanished has nothing to read."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=["nx", "bad name"])

    result = _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    assert "bad name" in result.stderr, (
        f"a skipped entry point must say so: {result.stderr.strip()}"
    )


# --------------------------------------------------------------------------
# what a shim IS
# --------------------------------------------------------------------------

def test_shim_body_is_exactly_the_contract_template(env) -> None:
    """The body is fixed by .1. If the writer composes its own, the twins test
    keeps passing while the files on disk say something else -- and
    readlink-before-exec is the whole design."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=["nx"])
    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    expected = _sh('nx_render_shim nx', tools, bin_dir).stdout
    assert (bin_dir / "nx").read_text().rstrip("\n") == expected.rstrip("\n")


def test_shims_are_regular_files_not_symlinks(env) -> None:
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=["nx"])
    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    shim = bin_dir / "nx"
    assert shim.is_file() and not shim.is_symlink()
    assert os.access(shim, os.X_OK), "a shim that is not executable is not on PATH in any useful sense"


def test_an_existing_uv_owned_symlink_becomes_a_regular_file(env) -> None:
    """Migration (.7) replaces uv's symlinks at these exact names. The old
    entry must be gone, not followed."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=["nx"])
    legacy_target = tools.parent / "legacy-nx"
    legacy_target.write_text("#!/bin/sh\necho legacy\n")
    (bin_dir / "nx").symlink_to(legacy_target)

    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    assert not (bin_dir / "nx").is_symlink()
    assert (bin_dir / "nx").is_file()
    assert legacy_target.read_text() == "#!/bin/sh\necho legacy\n", (
        "the write must replace the link, never follow it into the old target"
    )


def test_writing_twice_is_idempotent(env) -> None:
    """Re-running the installer must repair a reverted shim without churning
    the ones that were already right."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=_PROJECT_SCRIPTS)

    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)
    first = {p.name: p.read_text() for p in bin_dir.iterdir()}
    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)
    second = {p.name: p.read_text() for p in bin_dir.iterdir()}

    assert first == second


def test_no_temporary_files_are_left_behind(env) -> None:
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=_PROJECT_SCRIPTS)
    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)
    leftovers = [p.name for p in bin_dir.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"temporary files left behind: {leftovers}"


def test_a_written_shim_actually_runs(env) -> None:
    """The artefact, end to end: shim on PATH -> current -> generation binary."""
    tools, bin_dir = env
    gen = _make_gen(tools, "A", entry_points=["nx"])
    (tools / "current").symlink_to(gen)
    _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    result = subprocess.run([str(bin_dir / "nx"), "doctor"], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ran-nx doctor"


def test_shims_survive_a_flip_and_follow_it(env) -> None:
    """Shims are written once and are not rewritten by a flip: they resolve
    `current` at spawn, which is the property the whole epic is built on."""
    tools, bin_dir = env
    gen_a = _make_gen(tools, "A", entry_points=["nx"])
    gen_b = _make_gen(tools, "B", entry_points=["nx"])
    (gen_b / "bin" / "nx").write_text("#!/bin/sh\necho ran-from-B\n")
    (gen_b / "bin" / "nx").chmod(0o755)

    (tools / "current").symlink_to(gen_a)
    _sh(f'nx_write_shims "{gen_a}"', tools, bin_dir)
    before = subprocess.run([str(bin_dir / "nx")], capture_output=True, text=True).stdout.strip()

    tmp = tools / ".current.tmp"
    tmp.symlink_to(gen_b)
    os.replace(tmp, tools / "current")

    after = subprocess.run([str(bin_dir / "nx")], capture_output=True, text=True).stdout.strip()

    assert before == "ran-nx"
    assert after == "ran-from-B", "the shim must follow the pointer without being rewritten"
