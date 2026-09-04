# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ikdvm: every command path and option in the Click tree appears in
docs/cli-reference.md. The reference stays hand-written; this asserts
coverage, never content.

Walks ``nexus.cli.main`` and, for each non-hidden command, requires its
full path (``nx catalog backfill``) or, for a leaf under a documented
group, its bare name to occur in the document; for each non-hidden
``--long`` option, the literal option string. A hidden command or option
is out of scope by the author's own declaration (``hidden=True``), not by
this file's say-so. Deliberate omissions go in the allowlist below with a
reason each; the failure message names the exact string to add.

Non-vacuity: the walk must find a floor of commands and options (the
tree is ~250 commands and ~540 options at 7.30.0), and a planted option
name that is not in the document reds the check.
"""
from __future__ import annotations

from pathlib import Path

import click
import pytest

from nexus.cli import main

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"

#: Command paths (space-joined, ``nx``-prefixed) deliberately undocumented.
#: A path here also covers everything below it.
COMMAND_ALLOWLIST: dict[str, str] = {
    "nx command-context": (
        "agent-relay preamble plumbing (RDR-130 P2): each subcommand prints the "
        "context block its conexus slash command injects; no user runs it by hand"
    ),
}

#: (command path, option) pairs deliberately undocumented.
OPTION_ALLOWLIST: dict[tuple[str, str], str] = {
    ("nx catalog init", "--remote"): "retired option kept so old invocations parse",
    ("nx catalog setup", "--remote"): "retired option kept so old invocations parse",
    ("nx catalog sync", "--message"): "retired option kept so old invocations parse",
}


def walk(root: click.Group, prefix: str = "nx") -> tuple[list[str], list[tuple[str, str]]]:
    """Every non-hidden command path and (path, --option) under *root*."""
    commands: list[str] = []
    options: list[tuple[str, str]] = []

    def visit(cmd: click.Command, path: str) -> None:
        for name, sub in sorted(getattr(cmd, "commands", {}).items()):
            if sub.hidden:
                continue
            sub_path = f"{path} {name}"
            commands.append(sub_path)
            for prm in sub.params:
                if isinstance(prm, click.Option) and not prm.hidden:
                    options.extend((sub_path, o) for o in prm.opts if o.startswith("--"))
            visit(sub, sub_path)

    visit(root, prefix)
    return commands, options


def _allowlisted_command(path: str) -> bool:
    return any(path == p or path.startswith(p + " ") for p in COMMAND_ALLOWLIST)


def _command_documented(path: str, doc: str) -> bool:
    """The full path, or the leaf name in backticks (the reference's
    subcommand tables list leaves under a group heading)."""
    leaf = path.rsplit(" ", 1)[-1]
    return path in doc or f"`{leaf}" in doc


def test_every_command_is_in_the_reference() -> None:
    doc = REFERENCE.read_text()
    commands, _ = walk(main)
    assert len(commands) >= 200, f"walked only {len(commands)} commands; the tree is not the real one"
    missing = [c for c in commands if not _allowlisted_command(c) and not _command_documented(c, doc)]
    assert not missing, (
        f"{len(missing)} command(s) absent from docs/cli-reference.md; add each "
        f"(its full path or its leaf name in backticks) or allowlist it with a reason:\n  "
        + "\n  ".join(missing)
    )


def test_every_option_is_in_the_reference() -> None:
    doc = REFERENCE.read_text()
    _, options = walk(main)
    assert len(options) >= 400, f"walked only {len(options)} options; the tree is not the real one"
    missing = [
        (p, o)
        for p, o in options
        if not _allowlisted_command(p) and (p, o) not in OPTION_ALLOWLIST and o not in doc
    ]
    assert not missing, (
        f"{len(missing)} option(s) absent from docs/cli-reference.md; add the literal "
        f"string under the command's section, or allowlist it with a reason:\n  "
        + "\n  ".join(f"{p} {o}" for p, o in missing)
    )


def test_allowlists_carry_no_dead_rows() -> None:
    commands, options = walk(main)
    live_paths = set(commands)
    dead_cmds = [p for p in COMMAND_ALLOWLIST if not any(c == p or c.startswith(p + " ") for c in live_paths)]
    assert not dead_cmds, f"COMMAND_ALLOWLIST names commands that no longer exist: {dead_cmds}"
    live_opts = set(options)
    dead_opts = [k for k in OPTION_ALLOWLIST if k not in live_opts]
    assert not dead_opts, f"OPTION_ALLOWLIST names options that no longer exist: {dead_opts}"
    doc = REFERENCE.read_text()
    now_documented = [k for k in OPTION_ALLOWLIST if k[1] in doc]
    assert not now_documented, f"OPTION_ALLOWLIST rows whose option is documented after all: {now_documented}"


def test_a_planted_undocumented_option_is_detected() -> None:
    """The detector reds on a tree it has never seen documented."""

    @click.group()
    def fake() -> None:
        pass

    @fake.command("thing")
    @click.option("--absolutely-undocumented-flag")
    @click.option("--secret", hidden=True)
    def thing(absolutely_undocumented_flag, secret) -> None:
        pass

    commands, options = walk(fake)
    assert commands == ["nx thing"]
    assert options == [("nx thing", "--absolutely-undocumented-flag")], "hidden options are out of scope"
    assert "--absolutely-undocumented-flag" not in REFERENCE.read_text()
