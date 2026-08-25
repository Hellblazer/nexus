# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation-layout contract: path algebra, receipt schema, shim template.

nexus-utpuw.1 (P0). Nothing here installs anything. These tests pin the
on-disk contract that the generation builder (.2), the atomic flip (.3), the
shim writer (.4) and the Python consumers (.9) all depend on, so that a later
phase changing the layout has to change a test that says why.

Three of these are tripwires rather than coverage, and are named as such:

``test_defaults_are_recomputed_when_home_moves``
    ``release-sandbox.sh`` and ``tests/e2e/run.sh`` isolate ONLY by
    redirecting ``$HOME``. A module-level ``DEFAULT = Path.home() / ...``
    constant would satisfy every other test in this file and would silently
    make the sandbox clobber the live install.

``test_exec_line_does_not_pass_through_the_current_link``
    The single subtlest constraint in the design. ``Modules/getpath.py``
    looks for ``pyvenv.cfg`` next to the executable AS INVOKED, before it
    resolves symlinks, so a shim that exec'd through ``current`` would leak
    that component into ``sys.prefix``/``sys.path`` and a later flip would
    retarget every not-yet-imported module in a live process -- nexus-q3xrx
    reproduced exactly, by the mechanism that was supposed to prevent it.

``test_relative_override_is_refused``
    A relative ``NX_TOOLS_DIR`` would anchor the whole install at whatever
    the caller's CWD happened to be. This project has already paid for a
    moving CWD once (the nexus-yg70j chdir fix).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from nexus.install_layout import (
    BIN_DIR_ENV,
    CURRENT_LINK_NAME,
    GENERATION_PREFIX,
    INSTALLER_SCHEMA,
    RECEIPT_NAME,
    RECEIPT_SCHEMA,
    SOURCE_KINDS,
    TOOLS_DIR_ENV,
    InstallLayoutError,
    Receipt,
    bin_dir,
    build_spec,
    current_link,
    generation_dir,
    receipt_path,
    render_shim,
    tools_dir,
)


@pytest.fixture(autouse=True)
def _clean_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this file may inherit an operator's real overrides."""
    monkeypatch.delenv(TOOLS_DIR_ENV, raising=False)
    monkeypatch.delenv(BIN_DIR_ENV, raising=False)


# --------------------------------------------------------------------------
# path algebra: defaults
# --------------------------------------------------------------------------

def test_defaults_are_home_derived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert tools_dir() == tmp_path / ".local" / "share" / "nexus" / "tools"
    assert bin_dir() == tmp_path / ".local" / "bin"


def test_defaults_are_recomputed_when_home_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TRIPWIRE. Caching a $HOME-derived default at import time passes every
    other test here and silently makes the e2e sandboxes -- which isolate by
    $HOME redirection alone -- write into the operator's live install."""
    first = tmp_path / "home-a"
    second = tmp_path / "home-b"

    monkeypatch.setenv("HOME", str(first))
    a_tools, a_bin = tools_dir(), bin_dir()

    monkeypatch.setenv("HOME", str(second))
    b_tools, b_bin = tools_dir(), bin_dir()

    assert a_tools != b_tools, "tools_dir() cached a $HOME-derived default"
    assert a_bin != b_bin, "bin_dir() cached a $HOME-derived default"
    assert b_tools.is_relative_to(second)
    assert b_bin.is_relative_to(second)


# --------------------------------------------------------------------------
# path algebra: the five states an override can be in
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("env_var", "resolve"),
    [(TOOLS_DIR_ENV, tools_dir), (BIN_DIR_ENV, bin_dir)],
    ids=["tools", "bin"],
)
class TestOverrideSurface:
    """Both variables obey one rule. Two hand-written copies would drift."""

    def test_absolute_override_is_used_verbatim(
        self, env_var: str, resolve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "unused-home"))
        monkeypatch.setenv(env_var, str(tmp_path / "explicit"))
        assert resolve() == tmp_path / "explicit"

    def test_empty_override_is_treated_as_unset(
        self, env_var: str, resolve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An exported-but-empty var must not resolve to Path("") -- which is
        Path(".") -- and root the entire install at the caller's CWD."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(env_var, "")
        assert resolve().is_relative_to(tmp_path)
        assert resolve() != Path()

    def test_whitespace_only_override_is_treated_as_unset(
        self, env_var: str, resolve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(env_var, "   ")
        assert resolve().is_relative_to(tmp_path)

    def test_relative_override_is_refused(
        self, env_var: str, resolve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TRIPWIRE. Accepting a relative override costs a stranded install
        pointed at whatever the caller's CWD was; refusing costs one message."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(env_var, "relative/tools")
        with pytest.raises(InstallLayoutError) as excinfo:
            resolve()
        assert env_var in str(excinfo.value), "the error must name the variable to fix"
        assert "absolute" in str(excinfo.value).lower()

    def test_tilde_override_is_expanded(
        self, env_var: str, resolve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A shell expands ~ before we see it; a config file or a launchd
        plist does not. Python must expand it so the twins agree."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(env_var, "~/somewhere")
        assert resolve() == tmp_path / "somewhere"

    def test_trailing_whitespace_is_stripped(
        self, env_var: str, resolve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(env_var, f"  {tmp_path / 'padded'}  ")
        assert resolve() == tmp_path / "padded"


# --------------------------------------------------------------------------
# path algebra: derived paths
# --------------------------------------------------------------------------

def test_generation_and_pointer_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOOLS_DIR_ENV, str(tmp_path / "tools"))
    gen = generation_dir("20260825T041200Z")
    assert gen == tmp_path / "tools" / f"{GENERATION_PREFIX}20260825T041200Z"
    assert current_link() == tmp_path / "tools" / CURRENT_LINK_NAME
    assert receipt_path(gen) == gen / RECEIPT_NAME


def test_derived_paths_accept_an_explicit_tools_root(tmp_path: Path) -> None:
    """Callers that already resolved a root (an installer building into a
    sandbox) must not have to mutate the environment to be understood."""
    gen = generation_dir("s", tools=tmp_path)
    assert gen == tmp_path / f"{GENERATION_PREFIX}s"
    assert current_link(tools=tmp_path) == tmp_path / CURRENT_LINK_NAME


@pytest.mark.parametrize(
    "stamp",
    ["", "  ", "a/b", "../escape", ".", "..", "with space", "tab\there"],
)
def test_generation_dir_refuses_a_stamp_that_is_not_one_path_component(
    stamp: str, tmp_path: Path,
) -> None:
    """A stamp is interpolated into a path that a GC pass will later delete."""
    with pytest.raises(InstallLayoutError):
        generation_dir(stamp, tools=tmp_path)


def test_receipt_path_refuses_a_relative_generation(tmp_path: Path) -> None:
    with pytest.raises(InstallLayoutError):
        receipt_path(Path("gen-x"))


# --------------------------------------------------------------------------
# receipt schema
# --------------------------------------------------------------------------

def _receipt(**over) -> Receipt:
    fields = dict(
        version="7.18.0",
        spec="conexus[local]==7.18.0",
        source_kind="directory",
        source="/Users/someone/git/nexus",
        extras=["local"],
        python="3.12.8",
        base_interpreter="/uv/python/cpython-3.12.8-macos/bin/python3.12",
        created_at="2026-08-25T04:12:00Z",
    )
    fields.update(over)
    return Receipt(**fields)


def test_receipt_round_trips_through_json(tmp_path: Path) -> None:
    original = _receipt()
    restored = Receipt.from_json(original.to_json())
    assert restored == original
    assert restored.extras == ["local"], "extras are the 768->384 embedder P0"
    assert restored.source_kind == "directory"


def test_receipt_stamps_both_schema_numbers() -> None:
    payload = json.loads(_receipt().to_json())
    assert payload["schema"] == RECEIPT_SCHEMA
    assert payload["installer_schema"] == INSTALLER_SCHEMA


def test_receipt_json_is_stable_and_key_sorted() -> None:
    """A receipt is read by humans during an incident and diffed by tests."""
    text = _receipt().to_json()
    keys = list(json.loads(text).keys())
    assert keys == sorted(keys)
    assert text == _receipt().to_json()
    assert text.endswith("\n")


def test_receipt_normalises_extras() -> None:
    r = _receipt(extras=["local", "local", "dev"], spec="conexus[dev,local]==7.18.0")
    assert r.extras == ["dev", "local"], "spec construction in .2 must be deterministic"


def test_receipt_accepts_no_extras() -> None:
    bare = _receipt(extras=[], spec="conexus==7.18.0")
    assert Receipt.from_json(bare.to_json()).extras == []


@pytest.mark.parametrize("kind", SOURCE_KINDS)
def test_receipt_accepts_every_declared_source_kind(kind: str) -> None:
    assert Receipt.from_json(_receipt(source_kind=kind).to_json()).source_kind == kind


def test_receipt_refuses_an_unknown_source_kind() -> None:
    with pytest.raises(InstallLayoutError):
        _receipt(source_kind="tarball")


def test_receipt_tolerates_fields_a_newer_installer_added() -> None:
    """GC keeps the previous generation for free rollback, so an OLDER nx
    will read a receipt a NEWER one wrote. Unknown keys are not an error."""
    payload = json.loads(_receipt().to_json())
    payload["a_field_from_the_future"] = {"nested": True}
    restored = Receipt.from_json(json.dumps(payload))
    assert restored.version == "7.18.0"


def test_receipt_refuses_a_schema_it_cannot_interpret() -> None:
    payload = json.loads(_receipt().to_json())
    payload["schema"] = RECEIPT_SCHEMA + 1
    with pytest.raises(InstallLayoutError) as excinfo:
        Receipt.from_json(json.dumps(payload))
    assert str(RECEIPT_SCHEMA + 1) in str(excinfo.value)


def test_receipt_refuses_a_missing_required_field() -> None:
    payload = json.loads(_receipt().to_json())
    del payload["base_interpreter"]
    with pytest.raises(InstallLayoutError) as excinfo:
        Receipt.from_json(json.dumps(payload))
    assert "base_interpreter" in str(excinfo.value)


def test_receipt_refuses_malformed_json() -> None:
    with pytest.raises(InstallLayoutError):
        Receipt.from_json("{not json")


# --------------------------------------------------------------------------
# shim template
# --------------------------------------------------------------------------

def _exec_line(shim: str) -> str:
    lines = [ln for ln in shim.splitlines() if ln.strip().startswith("exec ")]
    assert len(lines) == 1, f"expected exactly one exec line, got {lines}"
    return lines[0]


def test_shim_is_posix_sh(tmp_path: Path) -> None:
    shim = render_shim("nx", tools=tmp_path)
    assert shim.startswith("#!/bin/sh\n"), "a shim runs on every nx invocation"
    assert shim.endswith("\n")


def test_shim_reads_the_pointer_before_it_execs(tmp_path: Path) -> None:
    shim = render_shim("nx", tools=tmp_path)
    lines = shim.splitlines()
    readlink_at = next(
        i for i, ln in enumerate(lines)
        if "readlink" in ln and not ln.lstrip().startswith("#")
    )
    exec_at = next(i for i, ln in enumerate(lines) if ln.strip().startswith("exec "))
    assert readlink_at < exec_at


def test_exec_line_does_not_pass_through_the_current_link(tmp_path: Path) -> None:
    """TRIPWIRE. This is what fails if someone later "simplifies" the shim to
    exec "$TOOLS/current/bin/nx" -- which looks equivalent and is not, because
    CPython resolves pyvenv.cfg against the UNRESOLVED invocation path."""
    shim = render_shim("nx", tools=tmp_path)
    pointer = str(tmp_path / CURRENT_LINK_NAME)

    exec_line = _exec_line(shim)
    assert pointer not in exec_line, (
        "the exec target resolves through the current symlink; a flip would "
        "retarget every not-yet-imported module in a live process (nexus-q3xrx)"
    )
    assert '"$NX_GEN/bin/nx"' in exec_line, "exec must target the readlink RESULT"

    # ...and the assertion above is scoped, not vacuous: the pointer path is
    # legitimately present elsewhere in the shim, as readlink's argument.
    assert pointer in shim


def test_shim_bakes_an_absolute_tools_path(tmp_path: Path) -> None:
    """Shims are HOME-independent once written, which is exactly why they must
    be rewritten when NX_TOOLS_DIR changes and cannot be shared across
    sandboxes."""
    shim = render_shim("nx", tools=tmp_path)
    assert str(tmp_path / CURRENT_LINK_NAME) in shim
    assert "$HOME" not in shim
    assert "~" not in shim


def test_shim_forwards_arguments(tmp_path: Path) -> None:
    assert '"$@"' in _exec_line(render_shim("nx", tools=tmp_path))


@pytest.mark.parametrize("command", ["nx", "nx-mcp", "nx-mcp-catalog", "mineru-api"])
def test_shim_targets_the_named_command(command: str, tmp_path: Path) -> None:
    assert f'"$NX_GEN/bin/{command}"' in _exec_line(render_shim(command, tools=tmp_path))


@pytest.mark.parametrize("command", ["", "  ", "a/b", "..", "with space"])
def test_render_shim_refuses_a_command_that_is_not_one_path_component(
    command: str, tmp_path: Path,
) -> None:
    with pytest.raises(InstallLayoutError):
        render_shim(command, tools=tmp_path)


def test_render_shim_uses_the_resolved_tools_dir_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOOLS_DIR_ENV, str(tmp_path / "tools"))
    assert str(tmp_path / "tools" / CURRENT_LINK_NAME) in render_shim("nx")


# --------------------------------------------------------------------------
# shim template, executed -- the template is a program, not a string
# --------------------------------------------------------------------------

def _install_shim(tmp_path: Path, command: str = "nx") -> Path:
    shim = tmp_path / command
    shim.write_text(render_shim(command, tools=tmp_path))
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim


def _make_generation(tmp_path: Path, stamp: str, command: str = "nx") -> Path:
    gen = generation_dir(stamp, tools=tmp_path)
    (gen / "bin").mkdir(parents=True)
    target = gen / "bin" / command
    target.write_text(f'#!/bin/sh\necho "{stamp} $*"\n')
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return gen


def test_a_rendered_shim_execs_the_generation_the_pointer_names(tmp_path: Path) -> None:
    gen = _make_generation(tmp_path, "genA")
    current_link(tools=tmp_path).symlink_to(gen)
    shim = _install_shim(tmp_path)

    r = subprocess.run([str(shim), "doctor", "--json"], capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "genA doctor --json"


def test_a_flip_redirects_the_next_invocation_only(tmp_path: Path) -> None:
    """The whole design in one assertion: the pointer moves, the shim follows
    it on the NEXT spawn, and the old generation is still on disk."""
    gen_a = _make_generation(tmp_path, "genA")
    gen_b = _make_generation(tmp_path, "genB")
    pointer = current_link(tools=tmp_path)
    pointer.symlink_to(gen_a)
    shim = _install_shim(tmp_path)

    before = subprocess.run([str(shim)], capture_output=True, text=True).stdout.strip()

    tmp_pointer = tmp_path / ".current.tmp"
    tmp_pointer.symlink_to(gen_b)
    os.replace(tmp_pointer, pointer)

    after = subprocess.run([str(shim)], capture_output=True, text=True).stdout.strip()

    assert before == "genA"
    assert after == "genB"
    assert (gen_a / "bin" / "nx").exists(), "the old generation was deleted by a flip"


def test_a_shim_over_a_missing_pointer_fails_loudly(tmp_path: Path) -> None:
    shim = _install_shim(tmp_path)
    r = subprocess.run([str(shim)], capture_output=True, text=True)
    assert r.returncode == 70, "EX_UNAVAILABLE, not a silent success"
    assert "nexus" in r.stderr.lower()
    assert str(current_link(tools=tmp_path)) in r.stderr


def test_a_shim_over_a_regular_file_pointer_fails_loudly(tmp_path: Path) -> None:
    """readlink refuses a non-symlink, and that must not be mistaken for a
    generation named by the file's contents."""
    current_link(tools=tmp_path).parent.mkdir(parents=True, exist_ok=True)
    current_link(tools=tmp_path).write_text("/somewhere/else\n")
    shim = _install_shim(tmp_path)
    r = subprocess.run([str(shim)], capture_output=True, text=True)
    assert r.returncode == 70


def test_a_shim_over_a_dangling_pointer_does_not_silently_succeed(tmp_path: Path) -> None:
    current_link(tools=tmp_path).parent.mkdir(parents=True, exist_ok=True)
    current_link(tools=tmp_path).symlink_to(tmp_path / "gen-reaped")
    shim = _install_shim(tmp_path)
    r = subprocess.run([str(shim)], capture_output=True, text=True)
    assert r.returncode != 0
    assert r.stdout.strip() == ""


# --------------------------------------------------------------------------
# injection: the rendered shim is a shell script, and its interpolation sites
# are double-quoted strings -- a different hazard alphabet from a path's
# --------------------------------------------------------------------------

#: Every one of these passed the ORIGINAL denylist, which rejected separators,
#: traversals and whitespace. ``nx$(touch${IFS}PWNED)`` was executed for real
#: from a rendered shim on 2026-08-25 before the allowlist replaced it.
_INJECTION = [
    "nx$(touch${IFS}PWNED)",
    "nx`touch${IFS}PWNED`",
    'nx";touch${IFS}PWNED;"',
    "nx${IFS}PWNED",
    "nx\\",
    'nx"',
    "nx'",
    "nx;id",
    "nx|id",
    "nx&id",
    "nx>out",
    "nx\nid",
    "-nx",
    ".nx",
    "nx\x00id",
]


@pytest.mark.parametrize("payload", _INJECTION)
def test_render_shim_refuses_shell_metacharacters(payload: str, tmp_path: Path) -> None:
    """TRIPWIRE. Audit finding F1 has .4 deriving the shim set from the
    installed distribution's entry_points metadata -- third-party wheel data.
    A name that survives validation reaches a double-quoted string in a script
    the operator then runs."""
    with pytest.raises(InstallLayoutError):
        render_shim(payload, tools=tmp_path)


@pytest.mark.parametrize("payload", _INJECTION)
def test_generation_dir_refuses_shell_metacharacters(payload: str, tmp_path: Path) -> None:
    """A stamp reaches the same sinks by way of the pointer path baked into
    every shim, and names a directory GC will later delete."""
    with pytest.raises(InstallLayoutError):
        generation_dir(payload, tools=tmp_path)


def test_a_rendered_shim_cannot_be_made_to_run_an_injected_command(tmp_path: Path) -> None:
    """The property itself, not the validator: no accepted command name
    produces a shim that executes anything but the generation binary."""
    marker = tmp_path / "PWNED"
    for payload in _INJECTION:
        try:
            body = render_shim(payload, tools=tmp_path)
        except InstallLayoutError:
            continue
        shim = tmp_path / "shim"
        shim.write_text(body)
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        subprocess.run(["sh", str(shim)], capture_output=True, cwd=tmp_path)
        assert not marker.exists(), f"{payload!r} injected a command into the shim"


@pytest.mark.parametrize("accepted", ["nx", "nx-mcp", "nx-mcp-catalog",
                                      "nx-session-end-launcher", "mineru",
                                      "mineru-api", "nx_2", "a.b-c_d"])
def test_the_allowlist_still_admits_every_real_console_script(
    accepted: str, tmp_path: Path,
) -> None:
    """An allowlist that also refuses the legitimate names is not a fix."""
    assert f'"$NX_GEN/bin/{accepted}"' in render_shim(accepted, tools=tmp_path)


# --------------------------------------------------------------------------
# override parity: python must not resolve a form the shell half refuses
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env_var", [TOOLS_DIR_ENV, BIN_DIR_ENV])
def test_a_username_tilde_is_refused_rather_than_expanded(
    env_var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Path.expanduser`` resolves ``~someuser`` out of the passwd database;
    a POSIX shell does not. Expanding it here would put the install somewhere
    the shell half cannot name."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(env_var, "~root/tools")
    with pytest.raises(InstallLayoutError):
        tools_dir() if env_var == TOOLS_DIR_ENV else bin_dir()


# --------------------------------------------------------------------------
# extras and spec are one fact
# --------------------------------------------------------------------------

def test_receipt_refuses_extras_the_spec_never_asked_for() -> None:
    """A receipt claiming [local] over a plain spec round-tripped perfectly
    before this check, and the next install would read extras the installed
    tree does not have -- the 768->384 embedder downgrade, reopened."""
    with pytest.raises(InstallLayoutError) as excinfo:
        _receipt(extras=["local"], spec="conexus==7.18.0")
    assert "local" in str(excinfo.value)


def test_receipt_refuses_a_spec_whose_extras_are_not_recorded() -> None:
    with pytest.raises(InstallLayoutError):
        _receipt(extras=[], spec="conexus[local]==7.18.0")


@pytest.mark.parametrize(
    ("spec", "extras"),
    [
        ("conexus==7.18.0", []),
        ("conexus[local]==7.18.0", ["local"]),
        ("conexus[dev,local]==7.18.0", ["dev", "local"]),
        ("conexus[local, dev]==7.18.0", ["dev", "local"]),
        (".", []),
        (".[local]", ["local"]),
        ("/Users/someone/git/nexus[local]", ["local"]),
        ("conexus[local]>=7.0,<8", ["local"]),
    ],
)
def test_receipt_accepts_every_spec_form_the_builder_produces(
    spec: str, extras: list[str],
) -> None:
    """.2 builds both source kinds and both extras states; none may be
    refused by a check meant to catch a DESYNC."""
    assert _receipt(spec=spec, extras=extras).extras == extras


def test_a_source_path_containing_brackets_is_not_read_as_extras() -> None:
    """``/Users/x/my[weird]repo`` is a directory name, not a PEP 508 group."""
    assert _receipt(spec="/Users/x/my[weird]repo", extras=[], source_kind="directory")


def test_receipt_coerces_a_hand_edited_string_schema() -> None:
    payload = json.loads(_receipt().to_json())
    payload["schema"] = "1"
    assert Receipt.from_json(json.dumps(payload)).version == "7.18.0"


# --------------------------------------------------------------------------
# spec construction: the PEP 508 fixup lives here, not in each caller
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("base", "extras", "version", "expected"),
    [
        ("conexus", [], "7.18.0", "conexus==7.18.0"),
        ("conexus", ["local"], "7.18.0", "conexus[local]==7.18.0"),
        ("conexus", ["local", "dev"], "7.18.0", "conexus[dev,local]==7.18.0"),
        ("conexus", ["local", "local"], "7.18.0", "conexus[local]==7.18.0"),
        (".", [], "", "."),
        (".", ["local"], "", ".[local]"),
        ("/Users/someone/git/nexus", ["local"], "", "/Users/someone/git/nexus[local]"),
    ],
)
def test_build_spec_puts_extras_before_the_pin(
    base: str, extras: list[str], version: str, expected: str,
) -> None:
    """``conexus==7.18.0[local]`` is not a valid requirement; the fixup that
    prevents it lived in a shell script and .2 would have restated it."""
    assert build_spec(base, extras, version) == expected


@pytest.mark.parametrize("extras", [[], ["local"], ["local", "dev"]])
def test_a_receipt_built_from_build_spec_is_consistent_by_construction(
    extras: list[str],
) -> None:
    """The cross-field check and the constructor must agree, or one of them
    is wrong and the builder is caught between them."""
    spec = build_spec("conexus", extras, "7.18.0")
    assert _receipt(spec=spec, extras=extras).extras == sorted(set(extras))
