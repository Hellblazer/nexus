# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``shims.sh`` dispatches to ``shims_core``; it does not reimplement it.

nexus-utpuw.4, the fourth file in the install-shell collapse after layout,
census and gc. ``tests/scripts/test_write_shims.py`` pins what the writer DOES,
through this same entry point, and is unchanged by the collapse. This file pins
that there is only one implementation left to do it.

WHAT THE COLLAPSE FIXED, rather than moved. shims.sh derived the shim set, wrote
it, and then recomputed the OWNED set inline in its prune loop, while
``layout_core.owned_shim_names`` computed the same set for ``nx doctor`` and for
``self_cmd``'s reclaim repair. The comment above that loop said "the two must
agree or the twins drift". Measured against a generation declaring a hostile
entry point that also existed in ``bin/``, before any of this was wired::

    kept by the shell's prune : ['nx', 'nx$(touch${IFS}PWNED)']
    owned_shim_names          : ['nx']

The inline rule never consulted the name allowlist. So a hostile name was OWNED
by the pruner and kept forever, while the writer refused to write it and doctor
-- which walks the owned set -- never looked at it: the one component that would
have removed such a file was the one component that believed it belonged there.
Reachable from history rather than hypothetical, because the pre-nexus-xk7g2
writer used a DENYLIST that admitted exactly that name and interpolated it into
a script placed on the operator's PATH.

``test_a_hostile_shim_left_by_history_is_pruned`` is that measurement, kept.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SHIMS_SH = _REPO / "src" / "nexus" / "_install" / "shims.sh"
_CORE = _REPO / "src" / "nexus" / "_install" / "shims_core.py"

#: The shell function shims.sh exposes, mapped to the core verb it must reach.
_DISPATCHERS = {"nx_write_shims": "write"}


def _shell_function_bodies() -> dict[str, str]:
    """Every ``nx_*`` function shims.sh defines, name -> body text.

    Brace-counting rather than a regex per function: the bodies contain braces
    and a regex stopping at the first ``}`` would report a truncated body as a
    complete one, which here would mean reporting an implementation as a clean
    dispatch.
    """
    text = _SHIMS_SH.read_text()
    bodies: dict[str, str] = {}
    for match in re.finditer(r"^(nx_[A-Za-z0-9_]+)\(\)\s*\{", text, flags=re.M):
        name = match.group(1)
        depth, i = 1, match.end()
        while depth and i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        bodies[name] = text[match.end():i - 1]
    return bodies


def test_every_shell_function_only_dispatches() -> None:
    """A dispatcher with one function still implemented in shell is strictly
    worse than the twin was: it voids the pin while leaving two
    implementations."""
    bodies = _shell_function_bodies()
    assert set(bodies) == set(_DISPATCHERS), (
        f"shims.sh defines {sorted(bodies)}, expected {sorted(_DISPATCHERS)}"
    )
    for name, verb in _DISPATCHERS.items():
        body = bodies[name]
        assert "_nx_shims_core" in body, f"{name} does not reach the core"
        assert verb in body, f"{name} does not name the {verb!r} verb"
        for forbidden in ("mkdir", "chmod", "mv ", "grep", "printf '%s\\n' \"$_nx_ws_body\""):
            assert forbidden not in body, (
                f"{name} still does its own {forbidden!r}; the writing rules "
                "live in shims_core.py now"
            )


def test_the_shell_half_neither_writes_nor_deletes() -> None:
    """Nothing anywhere in the file may write or remove a shim. A helper
    outside the functions would satisfy the check above and still be a second
    writer. Comments are stripped: the header discusses ``rm`` and the atomic
    replace while explaining that neither happens here any more."""
    code = "\n".join(
        line for line in _SHIMS_SH.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    for forbidden in ("rm -f", "rm -rf", "mv -f", "chmod ", "mkdir "):
        assert forbidden not in code, (
            f"shims.sh still does its own {forbidden!r}; shims_core.py writes "
            "and prunes, and this file dispatches"
        )


def test_the_usage_status_is_stated_equal_in_both() -> None:
    """Restated rather than imported, so a refusal never depends on a sibling
    loading -- which is the one duplication this arc keeps on purpose, and is
    therefore pinned."""
    from nexus._install.shims_core import SHIMS_USAGE_EXIT

    layout_sh = (_SHIMS_SH.parent / "layout.sh").read_text()
    match = re.search(r"^NX_LAYOUT_USAGE_EXIT=(\d+)", layout_sh, flags=re.M)
    assert match, "NX_LAYOUT_USAGE_EXIT not found in layout.sh"
    assert int(match.group(1)) == SHIMS_USAGE_EXIT


def _make_gen(tools: Path, name: str, entry_points: list[str]) -> Path:
    gen = tools / f"gen-{name}"
    (gen / "bin").mkdir(parents=True)
    (gen / "nexus-install.json").write_text("{}\n")
    # The names are DATA, read from a file, never interpolated into the stub's
    # source: interpolating them made the stub evaluate `$(touch ...)` itself,
    # so the payload fired in the harness and the writer only ever received the
    # harmless prefix.
    names_file = gen / "declared-entry-points.txt"
    names_file.write_text("".join(f"{ep}\n" for ep in entry_points))
    python = gen / "bin" / "python"
    python.write_text(f'#!/bin/sh\ncat "{names_file}"\n')
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    for name_ in [*entry_points, "pip", "activate"]:
        target = gen / "bin" / name_
        if target.exists():
            continue
        target.write_text('#!/bin/sh\necho hi\n')
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return gen


def _sh(snippet: str, tools: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{_SHIMS_SH}"; {snippet}'],
        capture_output=True, text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tools.parent / "home"),
            "NX_TOOLS_DIR": str(tools),
            "NX_BIN_DIR": str(bin_dir),
        },
    )


_HOSTILE = "nx$(touch${IFS}PWNED)"


def test_a_hostile_shim_left_by_history_is_pruned(tmp_path: Path) -> None:
    """THE measurement that motivated the collapse, kept as a pin.

    A shim bearing our marker and our pointer, at a name the allowlist refuses,
    is exactly what the pre-nexus-xk7g2 denylist writer could leave behind. The
    old inline owned-set called it ours and kept it; the shared rule does not,
    so it is pruned. Nothing else in the tree would have noticed: doctor walks
    the owned set, and the owned set is the thing that was wrong.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gen = _make_gen(tools, "A", ["nx", _HOSTILE])
    pointer = tools / "current"

    stale = bin_dir / _HOSTILE
    stale.write_text(
        f'#!/bin/sh\n# Generated by nexus.\nexec "{pointer}/bin/{_HOSTILE}" "$@"\n'
    )
    stale.chmod(0o755)

    res = _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    assert res.returncode == 0, res.stderr
    assert not stale.exists(), (
        "a hostile-named shim this install wrote in an earlier era survived the "
        "prune; the writer refuses to write it and doctor never walks it, so "
        "the prune is the only thing that can remove it"
    )
    assert (bin_dir / "nx").is_file(), "a hostile sibling must not abort the good ones"
    # It is said out loud, in both directions: refused on the way in, removed on
    # the way out. Silence would leave an operator with a file nothing explains.
    assert _HOSTILE in res.stderr, res.stderr
    assert not (bin_dir / "PWNED").exists()
    assert not (tmp_path / "PWNED").exists()
    assert not (Path.cwd() / "PWNED").exists()


def test_the_dispatch_keeps_stdout_clean(tmp_path: Path) -> None:
    """stdout is RESERVED for results here. Callers write
    ``dir=$(nx_tools_dir) || exit 1``, and a diagnostic on stdout is how a
    caller ends up installing into it.

    This bit during the collapse and is the reason ``shims_core`` uses
    ``declared_console_scripts_detail``: the warning form routes to structlog
    whenever structlog imports, which in an installed venv PRINTS TO STDOUT.
    So this test must run with the venv's python3 first on PATH -- which, run
    under ``uv run pytest``, it is. A bare system python3 has no structlog and
    would pass while proving nothing.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gen = _make_gen(tools, "A", ["nx", "bad name"])

    res = _sh(f'nx_write_shims "{gen}"', tools, bin_dir)

    assert res.returncode == 0, res.stderr
    assert res.stdout == "", f"the dispatch printed to stdout: {res.stdout!r}"
    assert "bad name" in res.stderr, (
        f"a skipped entry point must say so, on stderr: {res.stderr!r}"
    )
