# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation layout has ONE implementation. This says so, and says how.

nexus-utpuw.1. Two implementations used to exist deliberately, because the
callers have incompatible import constraints:

- ``src/nexus/_install/layout.sh`` is sourced by the generation builder (.2)
  and the shim writer (.4), which run from ``scripts/reinstall-tool.sh`` and
  may run with NOTHING installed. They cannot import nexus.
- ``src/nexus/install_layout.py`` is imported by ``health.py`` and
  ``upgrade_finish.py``, which run after the install and can.

The constraint was importing NEXUS, not running Python. So the logic moved to
``src/nexus/_install/layout_core.py``, which imports nothing from nexus and
runs as a plain script; ``install_layout.py`` re-exports it and ``layout.sh``
dispatches to it. Two copies of one rule drift until the stale one wins an
argument it should not, and the symptom of THIS rule drifting was never a
failure -- it was a doctor reporting green about a tree nobody is running from.

WHAT THIS FILE PINS NOW, in three kinds, because they are not equally strong:

1. STILL GENUINELY TWO STATEMENTS, still drift tests. The twelve ``NX_*``
   constants in layout.sh (kept as shell literals so that sourcing needs no
   python3 -- gc.sh and census.sh read constants and call nothing), the
   receipt field list, ``NX_NEVER_SHIM`` and ``NX_DEPENDENCY_SCRIPTS`` in
   shims.sh, and the declared-scripts query in install_generation.sh. The
   constant check carries its own coverage assert that PARSES layout.sh, so a
   new constant cannot arrive without a twin.

2. WIRING, not drift. Everything that runs a shell function and compares it to
   the Python function now compares the core against itself, since the shell
   function dispatches to that core. Kept because a dispatch layer can still
   be wrong -- a verb under the wrong name, a dropped argument, a swapped pair
   of same-typed arguments, a refusal that leaks a path to stdout -- and those
   tests fail on every one of those.

3. THE SHAPE OF THE DISPATCH, at the bottom of the file. These are what
   replace the drift checks. A HALF-collapsed layout.sh is strictly worse than
   the two twins were, because it voids the pin above while leaving a second
   implementation behind, so the check is not "the functions call the core"
   but "the functions contain nothing but a call to the core". Verified to
   bite: reimplementing one function in shell fails nine tests here, swapping
   two receipt arguments fails four, dropping a consumer's NX_LAYOUT_HOME
   fails one, and leaving an orphaned helper behind fails one.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from nexus import install_layout
from nexus.install_layout import (
    BIN_DIR_ENV,
    SOURCE_KINDS,
    TOOLS_DIR_ENV,
    Receipt,
    bin_dir,
    build_spec,
    current_link,
    generation_dir,
    previous_link,
    receipt_path,
    render_shim,
    tools_dir,
)

_SHELL_LAYOUT = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install" / "layout.sh"

# NOT skipif-gated. layout.sh is a committed file in this repository, so a
# skip here could only ever mean the contract's shell half has been deleted --
# which is the loudest thing this file could possibly report, not a reason to
# report nothing (the nexus-moht0 vacuous-gate doctrine).
def test_the_shell_half_is_present() -> None:
    assert _SHELL_LAYOUT.is_file(), f"{_SHELL_LAYOUT} is missing"


def _sh(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Source the shell twin in a controlled environment and run *snippet*.

    ``env`` is the COMPLETE environment: nothing of the operator's leaks in,
    so a stray NX_TOOLS_DIR on the developer's shell cannot make this pass.
    """
    # NX_LAYOUT_HOME is the convention every consumer follows: layout.sh
    # dispatches to layout_core.py beside it, and a sourced file cannot find
    # its own directory under POSIX sh. This runs under `sh`, where BASH_SOURCE
    # does not exist -- which is exactly why the convention is explicit rather
    # than a ${BASH_SOURCE[0]} trick that would have worked under bash and
    # failed silently here.
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NX_LAYOUT_HOME": str(_SHELL_LAYOUT.parent),
    }
    return subprocess.run(
        ["sh", "-c", f'. "{_SHELL_LAYOUT}"; {snippet}'],
        capture_output=True,
        text=True,
        env={**base, **env},
    )


def _shell_says(snippet: str, env: dict[str, str]) -> str:
    r = _sh(snippet, env)
    assert r.returncode == 0, f"shell twin failed: {r.stderr}"
    return r.stdout.rstrip("\n")


# The environments both halves must answer identically for. Each entry is one
# of the five states an override can be in, for each of the two variables.
def _environments(tmp_path: Path) -> list[tuple[str, dict[str, str]]]:
    home = str(tmp_path / "home")
    return [
        ("defaults", {"HOME": home}),
        ("tools-absolute", {"HOME": home, TOOLS_DIR_ENV: str(tmp_path / "t")}),
        ("bin-absolute", {"HOME": home, BIN_DIR_ENV: str(tmp_path / "b")}),
        ("both-absolute", {
            "HOME": home,
            TOOLS_DIR_ENV: str(tmp_path / "t"),
            BIN_DIR_ENV: str(tmp_path / "b"),
        }),
        ("tools-empty", {"HOME": home, TOOLS_DIR_ENV: ""}),
        ("bin-empty", {"HOME": home, BIN_DIR_ENV: ""}),
        ("both-empty", {"HOME": home, TOOLS_DIR_ENV: "", BIN_DIR_ENV: ""}),
        ("tools-whitespace", {"HOME": home, TOOLS_DIR_ENV: "   "}),
        ("tools-padded", {"HOME": home, TOOLS_DIR_ENV: f"  {tmp_path / 't'}  "}),
        ("tools-tilde", {"HOME": home, TOOLS_DIR_ENV: "~/tilde-tools"}),
        ("bin-tilde", {"HOME": home, BIN_DIR_ENV: "~/tilde-bin"}),
        ("home-elsewhere", {"HOME": str(tmp_path / "other-home")}),
    ]


def _python_says(resolve, env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> str:
    for var in ("HOME", TOOLS_DIR_ENV, BIN_DIR_ENV):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return str(resolve())


@pytest.mark.parametrize("case", [c[0] for c in _environments(Path("/nowhere"))])
def test_both_halves_resolve_the_same_directories(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = dict(_environments(tmp_path))[case]

    for label, resolve, fn in (
        ("tools", tools_dir, "nx_tools_dir"),
        ("bin", bin_dir, "nx_bin_dir"),
    ):
        shell = _shell_says(fn, env)
        python = _python_says(resolve, env, monkeypatch)
        assert shell == python, (
            f"{label} dir has drifted between the two halves for case {case!r}:\n"
            f"  shell:  {shell}\n"
            f"  python: {python}"
        )


def test_both_halves_agree_on_the_derived_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {"HOME": str(tmp_path / "home"), TOOLS_DIR_ENV: str(tmp_path / "tools")}
    monkeypatch.setenv(TOOLS_DIR_ENV, env[TOOLS_DIR_ENV])
    monkeypatch.setenv("HOME", env["HOME"])

    gen = generation_dir("20260825T041200Z")
    assert _shell_says("nx_generation_dir 20260825T041200Z", env) == str(gen)
    assert _shell_says("nx_current_link", env) == str(current_link())
    assert _shell_says("nx_previous_link", env) == str(previous_link())
    assert _shell_says(f'nx_receipt_path "{gen}"', env) == str(receipt_path(gen))


def test_both_halves_render_a_byte_identical_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim is the artefact the whole design rests on. A one-character
    difference between the half that WRITES it and the half that reviews it
    is exactly the drift this file exists to catch."""
    env = {"HOME": str(tmp_path / "home"), TOOLS_DIR_ENV: str(tmp_path / "tools")}
    monkeypatch.setenv(TOOLS_DIR_ENV, env[TOOLS_DIR_ENV])
    monkeypatch.setenv("HOME", env["HOME"])

    for command in ("nx", "nx-mcp", "nx-mcp-catalog", "nx-session-end-launcher", "mineru"):
        shell = _shell_says(f"nx_render_shim {command}", env)
        python = render_shim(command).rstrip("\n")
        assert shell == python, f"the {command} shim differs between the halves"


#: Every ``NX_*`` constant ``layout.sh`` defines, mapped to the core attribute
#: that must carry the same value. EXHAUSTIVE by assertion, not by hand: the
#: test below parses layout.sh for its own constant definitions and fails if
#: this table does not cover every one of them. Two of these -- the build-claim
#: marker and the usage exit -- had NO Python twin until the collapse, so they
#: were the two layout names that could have drifted with nothing to notice.
_CONSTANT_TWINS = {
    "NX_GENERATION_PREFIX": "GENERATION_PREFIX",
    "NX_CURRENT_LINK_NAME": "CURRENT_LINK_NAME",
    "NX_PREVIOUS_LINK_NAME": "PREVIOUS_LINK_NAME",
    "NX_LEGACY_GENERATION_NAME": "LEGACY_GENERATION_NAME",
    "NX_RECEIPT_NAME": "RECEIPT_NAME",
    "NX_BUILDING_MARKER_NAME": "BUILDING_MARKER_NAME",
    "NX_RECEIPT_SCHEMA": "RECEIPT_SCHEMA",
    "NX_INSTALLER_SCHEMA": "INSTALLER_SCHEMA",
    "NX_SHIM_NO_CURRENT_EXIT": "SHIM_NO_CURRENT_EXIT",
    "NX_LAYOUT_USAGE_EXIT": "LAYOUT_USAGE_EXIT",
    "NX_SOURCE_KINDS": "SOURCE_KINDS",
    # NX_RECEIPT_FIELDS has no constant twin by design -- the Python side
    # derives it from the dataclass, which
    # test_both_halves_name_the_same_receipt_fields already pins.
    "NX_RECEIPT_FIELDS": None,
}


def _shell_constant_names() -> set[str]:
    """The ``NX_*`` constants layout.sh assigns at its own top level.

    Parsed from assignments rather than grepped for the token, because the
    file MENTIONS most of these names in comments explaining them -- a grep
    would count a sentence as a definition and the coverage assert below
    would pass while covering nothing.
    """
    pattern = re.compile(r"^(NX_[A-Z0-9_]+)=", re.MULTILINE)
    return set(pattern.findall(_SHELL_LAYOUT.read_text()))


def test_the_constant_table_covers_every_shell_constant() -> None:
    """Non-vacuity for the test below: a value-by-value comparison proves
    nothing about a constant the table forgot to list."""
    uncovered = _shell_constant_names() - set(_CONSTANT_TWINS)
    assert not uncovered, (
        f"layout.sh defines {sorted(uncovered)}, which _CONSTANT_TWINS does not "
        f"list. Add the Python twin to nexus._install.layout_core and map it "
        f"here, or map it to None with the reason, as NX_RECEIPT_FIELDS is."
    )


@pytest.mark.parametrize(
    "shell_name", sorted(name for name, py in _CONSTANT_TWINS.items() if py)
)
def test_both_halves_carry_the_same_constant(shell_name: str, tmp_path: Path) -> None:
    """Every named layout constant, compared by VALUE across the two halves.

    A constant is the cheapest thing to let drift and the hardest to notice
    when it does: nothing crashes, an install simply writes ``.nx-building``
    where a reaper looks for something else and the tree is collected out from
    under a live build.
    """
    attr = _CONSTANT_TWINS[shell_name]
    shell = _shell_says(f'printf "%s" "${shell_name}"', {"HOME": str(tmp_path / "home")})
    python = getattr(install_layout, attr)

    if isinstance(python, tuple):  # SOURCE_KINDS: a shell word list
        assert shell.split() == list(python), (
            f"{shell_name} and {attr} disagree: {shell.split()} vs {list(python)}"
        )
    else:
        assert shell == str(python), (
            f"{shell_name} and {attr} disagree: {shell!r} vs {str(python)!r}"
        )


def test_both_halves_name_the_same_receipt_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The builder (.2) writes the receipt from shell; the consumers (.9) read
    it from Python. Neither half may invent or drop a field alone."""
    env = {"HOME": str(tmp_path / "home")}
    shell_fields = _shell_says("echo $NX_RECEIPT_FIELDS", env).split()
    python_fields = [f.name for f in dataclasses.fields(Receipt)]
    assert sorted(shell_fields) == sorted(python_fields), (
        f"receipt fields have drifted:\n"
        f"  shell only:  {sorted(set(shell_fields) - set(python_fields))}\n"
        f"  python only: {sorted(set(python_fields) - set(shell_fields))}"
    )


def test_a_receipt_written_by_the_shell_half_parses_in_the_python_half(
    tmp_path: Path,
) -> None:
    """End-to-end on the one artefact that crosses the language boundary."""
    env = {"HOME": str(tmp_path / "home")}
    written = _shell_says(
        "nx_render_receipt 7.18.0 'conexus[local]==7.18.0' directory /src local "
        "3.12.8 /uv/python/cpython-3.12.8/bin/python3.12 2026-08-25T04:12:00Z",
        env,
    )
    receipt = Receipt.from_json(written)
    assert receipt.version == "7.18.0"
    assert receipt.extras == ["local"], "extras are the 768->384 embedder P0"
    assert receipt.source_kind == "directory"
    assert json.loads(written)["base_interpreter"].endswith("python3.12")


@pytest.mark.parametrize("env_var", [TOOLS_DIR_ENV, BIN_DIR_ENV])
def test_both_halves_refuse_a_relative_override(env_var: str, tmp_path: Path) -> None:
    """A refusal that exists in only one half is worse than no refusal: the
    install lands where the lenient half says and the strict half calls it
    missing."""
    fn = "nx_tools_dir" if env_var == TOOLS_DIR_ENV else "nx_bin_dir"
    r = _sh(fn, {"HOME": str(tmp_path / "home"), env_var: "relative/path"})
    assert r.returncode != 0, "the shell half accepted a relative override"
    assert env_var in r.stderr
    assert r.stdout.strip() == "", "a refusal must not also print a path"


def test_the_shell_half_sets_no_shell_options() -> None:
    """It is sourced, so any option it sets lands in the CALLER's shell and
    changes how that caller handles an unrelated failure. The repo's other
    sourceable libs (tests/e2e/lib/*.sh) hold the same line."""
    text = _SHELL_LAYOUT.read_text()
    assert "\nset -e" not in text, "set -e in a sourced library changes its callers"
    assert "\nset -u" not in text, "set -u in a sourced library changes its callers"
    assert "\nset -o" not in text, "set -o in a sourced library changes its callers"


# --------------------------------------------------------------------------
# the findings a stacked review turned up on 2026-08-25, pinned so that a
# later phase reintroducing any of them is a red test rather than a surprise
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env_var", [TOOLS_DIR_ENV, BIN_DIR_ENV])
def test_both_halves_refuse_a_username_tilde(env_var: str, tmp_path: Path) -> None:
    """Python's expanduser resolves ~someuser out of the passwd database and
    a POSIX shell does not, so the halves disagreed about where an install
    lands. Neither expands it now; both refuse."""
    fn = "nx_tools_dir" if env_var == TOOLS_DIR_ENV else "nx_bin_dir"
    env = {"HOME": str(tmp_path / "home"), env_var: "~root/tools"}
    assert _sh(fn, env).returncode != 0

    # A hand-built MonkeyPatch() has no teardown: without the context manager
    # this leaked NX_BIN_DIR='~root/tools' AND the tmp HOME into the worker's
    # process env for every later test — measured as an ordering flake in
    # test_trigger_wrapper_echoes_actions_and_pending (7.24.1 battery).
    with pytest.raises(Exception), pytest.MonkeyPatch.context() as mp:  # InstallLayoutError
        _python_says(tools_dir if env_var == TOOLS_DIR_ENV else bin_dir, env, mp)


_INJECTION = [
    "nx$(touch${IFS}PWNED)",
    "nx`touch${IFS}PWNED`",
    'nx";touch${IFS}PWNED;"',
    "nx;id",
    "nx|id",
    "-nx",
]


@pytest.mark.parametrize("payload", _INJECTION)
def test_both_halves_refuse_shell_metacharacters(payload: str, tmp_path: Path) -> None:
    """A name accepted by only one half is worse than one accepted by both:
    the installer writes a shim the reviewer's tests never see."""
    # The payload travels in the ENVIRONMENT, never interpolated into the
    # snippet. Writing `nx_render_shim "{payload}"` makes the harness itself
    # an injection site: the outer shell evaluates $(...) before the function
    # is ever called, so the test passes a harmless "nx" and reports green
    # while the payload runs. Measured -- that is exactly how this test first
    # failed. Parameter expansion does not recurse, so "$PAYLOAD" is literal.
    env = {"HOME": str(tmp_path / "home"), "PAYLOAD": payload}
    r = _sh('nx_render_shim "$PAYLOAD"', env)
    assert r.returncode != 0, f"the shell half accepted {payload!r}"
    assert r.stdout.strip() == "", "a refusal must not also print a shim"


def test_both_halves_name_the_same_source_kinds(tmp_path: Path) -> None:
    shell = _shell_says("echo $NX_SOURCE_KINDS", {"HOME": str(tmp_path)}).split()
    assert sorted(shell) == sorted(SOURCE_KINDS)


def test_the_shell_half_escapes_a_receipt_value_that_carries_json_syntax(
    tmp_path: Path,
) -> None:
    """A source path containing a quote or a backslash produced INVALID JSON,
    which the Python half then refused outright -- an install that succeeds
    and leaves an unreadable receipt."""
    nasty = '/Users/some"one/git\\nexus'
    written = _shell_says(
        f"nx_render_receipt 7.18.0 'conexus[local]==7.18.0' directory '{nasty}' "
        "local 3.12.8 /uv/py/bin/python3.12 2026-08-25T04:12:00Z",
        {"HOME": str(tmp_path)},
    )
    assert json.loads(written)["source"] == nasty
    assert Receipt.from_json(written).source == nasty


def test_the_shell_half_refuses_a_receipt_value_it_cannot_escape(tmp_path: Path) -> None:
    """A newline in a value cannot be represented by the escaper, so it is
    refused where the message can name it -- not emitted as broken JSON."""
    r = _sh(
        "nx_render_receipt 7.18.0 'conexus[local]==7.18.0' directory "
        "\"$(printf 'a\\nb')\" local 3.12.8 /uv/py 2026-08-25T04:12:00Z",
        {"HOME": str(tmp_path)},
    )
    assert r.returncode != 0
    assert r.stdout.strip() == ""


#: A pair chosen because it ACTUALLY collates differently: byte order puts
#: "Dev" first (D is 0x44, a is 0x61), a UTF-8 locale's case-insensitive
#: collation puts "all" first. The first version of this test used
#: ("local", "Dev"), which orders identically under both -- so it passed
#: under the mutation it existed to catch. Verify any replacement pair
#: against `_a_locale_that_collates_differently` before trusting it.
_COLLATION_PROBE = ("all", "Dev")


def _plain_sort(values: tuple[str, ...], locale_name: str) -> list[str]:
    r = subprocess.run(
        ["sort"], input="\n".join(values) + "\n", capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": locale_name},
    )
    return r.stdout.split()


def _a_locale_that_collates_differently() -> str | None:
    """A locale on THIS box under which `sort` disagrees with byte order."""
    for candidate in ("en_US.UTF-8", "en_GB.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8"):
        if _plain_sort(_COLLATION_PROBE, candidate) != sorted(_COLLATION_PROBE):
            return candidate
    return None


def test_the_shell_halfs_extras_order_does_not_depend_on_the_locale(
    tmp_path: Path,
) -> None:
    """`sort` collates differently under a UTF-8 locale, so the two halves
    wrote different receipts for the same extras depending on whose shell ran
    the install. The shell half pins LC_ALL=C to match Python's sorted()."""
    locale_name = _a_locale_that_collates_differently()
    if locale_name is None:
        # NOT a skip. If no locale here collates differently from byte order,
        # the behavioural assertion below cannot exercise the hazard -- so the
        # fix gets pinned in the source instead of reporting a pass it did not
        # earn. This still fails if someone removes LC_ALL=C.
        # The shell half no longer sorts: extras are sorted by Python's
        # sorted(), which is byte order and cannot be moved by a locale. So
        # the source-level fallback is now a pin on the DISPATCH -- if the
        # shell half ever grows its own sort again, this is what notices.
        body = _SHELL_LAYOUT.read_text()
        assert "sort" not in body, (
            "no locale on this box collates differently from byte order, so the "
            "behavioural check cannot run -- and layout.sh has grown a sort of "
            "its own again, which is the hazard this test exists for"
        )
        return

    # Non-vacuity: prove the probe really does discriminate here, so a green
    # below means the pin worked rather than that nothing was different.
    assert _plain_sort(_COLLATION_PROBE, locale_name) != sorted(_COLLATION_PROBE)

    call = ("nx_render_receipt 7.18.0 'conexus[Dev,all]==7.18.0' registry conexus "
            "'all,Dev' 3.12.8 /uv/py 2026-08-25T04:12:00Z")
    home = {"HOME": str(tmp_path)}
    under_locale = _shell_says(call, {**home, "LC_ALL": locale_name, "LANG": locale_name})
    under_c = _shell_says(call, {**home, "LC_ALL": "C"})

    assert json.loads(under_locale)["extras"] == sorted(set(_COLLATION_PROBE))
    assert json.loads(under_locale)["extras"] == json.loads(under_c)["extras"]


def test_the_shell_rendered_shim_actually_runs(tmp_path: Path) -> None:
    """Byte-equality with the Python half plus a Python-rendered execution
    test covers this transitively, but only while the byte-equality test
    passes. This closes the loop on the artefact the installer really writes."""
    tools = tmp_path / "tools"
    gen = tools / "gen-A"
    (gen / "bin").mkdir(parents=True)
    target = gen / "bin" / "nx"
    target.write_text('#!/bin/sh\necho "genA $*"\n')
    target.chmod(0o755)
    (tools / "current").symlink_to(gen)

    body = _shell_says(f'nx_render_shim nx "{tools}"', {"HOME": str(tmp_path)})
    shim = tmp_path / "nx"
    shim.write_text(body + "\n")
    shim.chmod(0o755)

    r = subprocess.run([str(shim), "doctor"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "genA doctor"


@pytest.mark.parametrize(
    ("base", "extras", "version"),
    [
        ("conexus", "", "7.18.0"),
        ("conexus", "local", "7.18.0"),
        ("conexus", "local,dev", "7.18.0"),
        (".", "", ""),
        (".", "local", ""),
    ],
)
def test_both_halves_build_the_same_spec(
    base: str, extras: str, version: str, tmp_path: Path,
) -> None:
    """The builder runs from shell and the reader validates from Python. A
    spec they assemble differently is a receipt that fails its own check."""
    shell = _shell_says(f"nx_build_spec '{base}' '{extras}' '{version}'",
                        {"HOME": str(tmp_path)})
    python = build_spec(base, [e for e in extras.split(",") if e], version)
    assert shell == python


def test_both_halves_name_the_same_never_shim_set() -> None:
    """``NEVER_SHIM`` / ``NX_NEVER_SHIM``: the names that live in a venv's
    ``bin/`` and are never shimmed into the shared bin dir.

    It became a twin because a consumer needed it. ``nx doctor``'s
    generation-layout check derives "the names nexus owns" from
    ``<current>/bin`` in order to notice uv reclaiming a shim — and
    ``~/.local/bin`` is SHARED, so without subtracting this set a stray
    ``python`` symlink from pyenv, asdf or homebrew reads as evidence that uv
    took our shims and hard-fails a healthy install (RG-C, nexus-utpuw.11).

    Two copies of that set drifting apart would put the shim WRITER and the
    shim CHECKER in disagreement about what nexus owns, and the check would
    then be wrong in whichever direction the drift went: crying wolf, or going
    quiet on a real reclaim.
    """
    import subprocess

    from nexus.install_layout import NEVER_SHIM

    shims_sh = _SHELL_LAYOUT.parent / "shims.sh"
    r = subprocess.run(
        ["bash", "-c", f'. "{shims_sh}"; printf "%s" "$NX_NEVER_SHIM"'],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    shell_set = frozenset(r.stdout.split())

    assert shell_set == NEVER_SHIM, (
        f"the halves disagree on what is never shimmed:\n"
        f"  shell-only:  {sorted(shell_set - NEVER_SHIM)}\n"
        f"  python-only: {sorted(NEVER_SHIM - shell_set)}"
    )


def _shell_word_list(var: str) -> frozenset[str]:
    """The space-separated value of ``var="..."`` in the shell shim writer."""
    text = (_SHELL_LAYOUT.parent / "shims.sh").read_text()
    match = re.search(rf'^{var}="([^"]*)"', text, flags=re.M)
    assert match, f"{var} not found in shims.sh"
    return frozenset(match.group(1).split())


def test_both_halves_agree_on_the_dependency_scripts() -> None:
    """DEPENDENCY_SCRIPTS is the Python twin of NX_DEPENDENCY_SCRIPTS: the
    owned-shim set doctor and the takeover repair derive (GH #1487,
    nexus-50hm9) must be the set nx_write_shims writes, or a reclaimed
    dependency shim goes unreported on one side."""
    assert install_layout.DEPENDENCY_SCRIPTS == _shell_word_list("NX_DEPENDENCY_SCRIPTS")


def _code_lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_both_halves_run_the_same_declared_scripts_query() -> None:
    """The Python half asks a generation which console scripts it declares
    with the SAME interpreter snippet the shell writer's _nx_declared_scripts
    runs (GH #1487, nexus-50hm9): doctor and the takeover repair must derive
    the owned set exactly as nx_write_shims does, or the two halves disagree
    about which shims exist. Compared line-for-line ignoring comments."""
    shell = (_SHELL_LAYOUT.parent / "shims.sh").read_text()
    match = re.search(r"_nx_declared_scripts\(\) \{.*?-c '\n(.*?)\n' \"\$2\"", shell, flags=re.S)
    assert match, "_nx_declared_scripts's inline python not found in shims.sh"
    assert _code_lines(match.group(1)) == _code_lines(install_layout._DECLARED_SCRIPTS_QUERY)


# ===========================================================================
# THE COLLAPSE: layout.sh dispatches, it does not reimplement
#
# Everything above this line was written when layout.sh carried its own
# implementation of the layout rules, and it compared the two implementations
# against each other. layout.sh now dispatches to layout_core.py, which is the
# module the Python side imports -- so those comparisons are, for the
# behavioural rules, a comparison of the core against itself.
#
# They are KEPT rather than deleted, because they are still worth their run
# time as WIRING tests: a verb dispatched under the wrong name, a dropped
# argument, a swapped pair of same-typed arguments, or a refusal that starts
# leaking a path to stdout all fail them, and every one of those is a live
# hazard in a dispatch layer. What they no longer do is catch drift, because
# there is no longer a second implementation to drift.
#
# What replaces them is below: pins on the SHAPE of the dispatch itself, which
# is the new thing that can be got wrong. The constant tests, the receipt-field
# test, the NEVER_SHIM / DEPENDENCY_SCRIPTS tests and the declared-scripts
# query test are NOT in this category -- they compare shell literals in
# layout.sh, shims.sh and install_generation.sh against Python, and those are
# still genuinely two statements of one fact.
# ===========================================================================

#: The shell functions layout.sh exposes, mapped to the core verb each must
#: dispatch to. The verb is the function name minus ``nx_``, and the test below
#: asserts that mapping holds in BOTH directions rather than trusting it.
_DISPATCHERS = {
    "nx_source_kind": "source_kind",
    "nx_tools_dir": "tools_dir",
    "nx_bin_dir": "bin_dir",
    "nx_generation_dir": "generation_dir",
    "nx_current_link": "current_link",
    "nx_previous_link": "previous_link",
    "nx_root": "root",
    "nx_receipt_path": "receipt_path",
    "nx_render_shim": "render_shim",
    "nx_build_spec": "build_spec",
    "nx_render_receipt": "render_receipt",
}


def _shell_function_bodies() -> dict[str, str]:
    """Every ``nx_*`` function layout.sh defines, name -> body text.

    A brace-counting scan rather than a regex per function: the bodies contain
    braces, and a regex that stops at the first ``}`` would report a truncated
    body as a complete one -- which for THIS test would mean reporting an
    implementation as a clean dispatch.
    """
    text = _SHELL_LAYOUT.read_text()
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


def test_every_shell_function_is_a_dispatch_and_nothing_else() -> None:
    """The collapse, stated as a property of the file.

    A HALF-collapsed layout.sh is strictly worse than the two twins were: it
    voids the drift pin above while leaving a second implementation in place.
    So the check is not "the functions call the core" but "the functions
    contain NOTHING BUT a call to the core" -- one dispatch line each, no
    branching, no case statement, no sed, no printf of a result.
    """
    bodies = _shell_function_bodies()
    assert set(bodies) == set(_DISPATCHERS), (
        f"layout.sh's nx_* functions are {sorted(bodies)}, the dispatch table "
        f"says {sorted(_DISPATCHERS)}"
    )
    for name, body in bodies.items():
        statements = [
            line.strip() for line in body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(statements) == 1, (
            f"{name} has {len(statements)} statements, so it is implementing "
            f"something rather than dispatching it:\n" + "\n".join(statements)
        )
        assert statements[0].startswith("_nx_core "), (
            f"{name} does not dispatch to the core: {statements[0]!r}"
        )
        # A one-line body reads `{ _nx_core tools_dir; }`, so the verb arrives
        # with the statement separator attached.
        verb = statements[0].split()[1].rstrip(";")
        assert verb == _DISPATCHERS[name], (
            f"{name} dispatches {verb!r}, expected {_DISPATCHERS[name]!r}"
        )


def test_the_dispatch_table_matches_the_cores_verbs() -> None:
    """Both directions. A shell function dispatching a verb the core does not
    have fails at runtime in an install path; a core verb no shell function
    reaches is dead weight that will be assumed live by the next reader."""
    from nexus._install.layout_core import _VERBS

    shell_verbs = set(_DISPATCHERS.values())
    assert shell_verbs == set(_VERBS), (
        f"shell dispatches {sorted(shell_verbs - set(_VERBS))} that the core "
        f"does not define, and the core defines "
        f"{sorted(set(_VERBS) - shell_verbs)} that no shell function reaches"
    )


def test_layout_sh_carries_no_implementation_helpers() -> None:
    """The private helpers whose logic moved must be GONE, not orphaned.

    An unused ``_nx_json_escape`` left behind is a second escaper sitting in
    the file, one edit away from being called again by something that finds it
    there and assumes it is the way this file does that job.
    """
    # COMMENTS STRIPPED FIRST. The file explains, in prose, that `_nx_root`
    # used to be private and why that was wrong -- and a plain substring scan
    # counts that sentence as a definition. This test failed on its own
    # docstring the first time it ran, which is the whole argument for not
    # grepping a name you could parse for.
    code = "\n".join(
        line for line in _SHELL_LAYOUT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    for gone in ("_nx_json_escape", "_nx_resolve_dir", "_nx_require_component",
                 "_nx_bad_component", "_nx_root"):
        assert gone not in code, (
            f"{gone} is still in layout.sh; its logic is in layout_core.py now, "
            f"and a leftover copy is what the collapse exists to remove"
        )


def test_an_argument_swap_would_be_caught(tmp_path: Path) -> None:
    """render_receipt takes EIGHT same-typed strings positionally.

    Nothing about the types stops the dispatcher passing them in the wrong
    order, and every wrong order still produces valid JSON. So each field is
    given a value that names itself, and every field is checked -- rather than
    spot-checking two of them and calling the wiring proved.
    """
    fields = {
        "version": "V-version",
        "spec": "S-spec",
        "source_kind": "registry",
        "source": "S-source",
        "python": "P-python",
        "base_interpreter": "B-base",
        "created_at": "C-created",
    }
    written = _shell_says(
        "nx_render_receipt '{version}' '{spec}' '{source_kind}' '{source}' "
        "'' '{python}' '{base_interpreter}' '{created_at}'".format(**fields),
        {"HOME": str(tmp_path)},
    )
    parsed = json.loads(written)
    for key, expected in fields.items():
        assert parsed[key] == expected, (
            f"receipt field {key} came back as {parsed[key]!r}, not {expected!r} "
            f"-- the dispatcher is passing arguments in the wrong order"
        )


@pytest.mark.parametrize("snippet", [
    "nx_tools_dir",
    "nx_bin_dir",
    "nx_current_link",
    "nx_generation_dir 20260920T000000Z",
    "nx_render_shim nx",
    "nx_build_spec conexus local 7.55.1",
])
def test_an_unset_layout_home_is_refused_loudly(snippet: str, tmp_path: Path) -> None:
    """The one new failure mode the dispatch introduces.

    layout.sh cannot find layout_core.py on its own, so a consumer that forgets
    the convention gets a refusal -- never a guess. Guessing here resolves
    paths against somebody else's tree, which is the failure this whole file
    exists to prevent. stdout must stay EMPTY: callers write
    ``dir=$(nx_tools_dir) || exit 1``, and a refusal that also prints a path is
    how one ends up installing into it.
    """
    r = subprocess.run(
        ["sh", "-c", f'. "{_SHELL_LAYOUT}"; {snippet}'],
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    assert r.returncode == install_layout.LAYOUT_USAGE_EXIT, (
        f"expected EX_USAGE ({install_layout.LAYOUT_USAGE_EXIT}), got {r.returncode}"
    )
    assert r.stdout == "", f"a refusal printed to stdout: {r.stdout!r}"
    assert "NX_LAYOUT_HOME" in r.stderr, r.stderr


def test_a_missing_core_is_refused_loudly(tmp_path: Path) -> None:
    """NX_LAYOUT_HOME set but wrong -- an incomplete install, not a typo.

    Distinguished from the unset case on purpose: the remedies differ, and
    "layout.sh without layout_core.py beside it" is a shipping bug worth its
    own message.
    """
    r = subprocess.run(
        ["sh", "-c", f'. "{_SHELL_LAYOUT}"; nx_tools_dir'],
        capture_output=True, text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "NX_LAYOUT_HOME": str(tmp_path / "nowhere"),
        },
    )
    assert r.returncode == install_layout.LAYOUT_USAGE_EXIT
    assert r.stdout == ""
    assert "layout_core.py" in r.stderr, r.stderr


def test_every_consumer_that_sources_layout_sets_the_convention() -> None:
    """The cross-file convention, pinned where it is easy to forget.

    A new script that sources layout.sh and omits NX_LAYOUT_HOME fails at
    RUNTIME, in an install path, possibly only on a fresh machine. This finds
    it at test time instead. Scanned over the whole repo rather than a fixed
    list, so a consumer added later is covered without anyone remembering to
    add it here.
    """
    root = Path(__file__).resolve().parents[1]
    searched = [
        path for pattern in ("src/nexus/_install/*.sh", "scripts/*.sh")
        for path in root.glob(pattern)
        if path.name != "layout.sh"
    ]
    # Non-vacuity: a glob that matches nothing would pass this test silently.
    assert searched, "found no candidate scripts at all -- the globs are wrong"

    sourcing = [p for p in searched if re.search(r'^\s*\.\s+"[^"]*/layout\.sh"',
                                                 p.read_text(), re.MULTILINE)]
    assert sourcing, "no script appears to source layout.sh -- the pattern is wrong"

    missing = [p.name for p in sourcing if "NX_LAYOUT_HOME=" not in p.read_text()]
    assert not missing, (
        f"{missing} source layout.sh without setting NX_LAYOUT_HOME first. "
        f"layout.sh dispatches to layout_core.py beside it and cannot find its "
        f"own directory when sourced, so the sourcing script must name it."
    )
