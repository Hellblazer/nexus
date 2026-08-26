# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The Python and shell statements of the generation layout must agree.

nexus-utpuw.1. Two implementations exist deliberately, for the same reason
``tests/_fence_home.py`` and ``tests/e2e/lib/fence_home.sh`` both exist:
the callers have incompatible import constraints.

- ``src/nexus/_install/layout.sh`` is sourced by the generation builder (.2)
  and the shim writer (.4), which run from ``scripts/reinstall-tool.sh`` and
  may run with NOTHING installed. They cannot import nexus.
- ``src/nexus/install_layout.py`` is imported by ``health.py`` and
  ``upgrade_finish.py``, which run after the install and can.

Two copies of one rule drift until the stale one wins an argument it should
not. If the shell half drifts, an install lands in a directory the Python half
cannot find -- and the symptom is not a failure, it is a doctor that reports
green about a tree nobody is running from. So this pins the contract instead
of trusting the comments in either file.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

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
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
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

    with pytest.raises(Exception):  # InstallLayoutError
        _python_says(tools_dir if env_var == TOOLS_DIR_ENV else bin_dir, env,
                     pytest.MonkeyPatch())


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
        assert "LC_ALL=C sort" in _SHELL_LAYOUT.read_text(), (
            "no locale on this box collates differently from byte order, so the "
            "behavioural check cannot run -- and layout.sh no longer pins "
            "LC_ALL=C either, so nothing is checking this at all"
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
