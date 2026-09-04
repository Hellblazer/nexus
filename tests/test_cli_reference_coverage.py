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

import re
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
                    # secondary_opts is the --no-x half of a --x/--no-x
                    # toggle; 27 exist and 4 were undocumented while the
                    # walk ignored them (review [24393] Major 2).
                    options.extend(
                        (sub_path, o)
                        for o in [*prm.opts, *prm.secondary_opts]
                        if o.startswith("--")
                    )
            visit(sub, sub_path)

    visit(root, prefix)
    return commands, options


def _sections(doc: str) -> list[str]:
    """The reference split at its ``##``/``###`` headings, each piece one
    section body. Coverage is judged inside a section that names the
    command, so ``--dry-run`` under ``nx catalog prune-stale`` cannot vouch
    for ``--dry-run`` on an undocumented command elsewhere (critique
    [24392] finding 2: 32 commands shared that flag, 11 leaves shared
    ``list``)."""
    return re.split(r"\n#{2,3} ", "\n" + doc)


def _mentioning_sections(path: str, doc: str) -> list[str]:
    """Sections that name *path*: its full path, or its leaf in backticks
    together with its parent group's name."""
    parts = path.split(" ")
    leaf = parts[-1]
    # The parent must appear as a command, ``nx catalog``, not as a word
    # in prose: "catalog" occurs in the nx store section and that section
    # has a `list` leaf too (review [24393] Major 1, falsified live).
    parent = " ".join(parts[:-1]) if len(parts) > 2 else ""
    out = []
    for sec in _sections(doc):
        if path in sec or (f"`{leaf}" in sec and (not parent or parent in sec)):
            out.append(sec)
    return out


def _allowlisted_command(path: str) -> bool:
    return any(path == p or path.startswith(p + " ") for p in COMMAND_ALLOWLIST)


def _command_documented(path: str, doc: str) -> bool:
    """The full path anywhere, or the leaf name in backticks inside a
    section that also names its parent group (the reference's subcommand
    tables list leaves under a group heading)."""
    return bool(_mentioning_sections(path, doc))


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
        if not _allowlisted_command(p)
        and (p, o) not in OPTION_ALLOWLIST
        and not any(o in sec for sec in _mentioning_sections(p, doc))
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
    now_documented = [
        k for k in OPTION_ALLOWLIST if any(k[1] in sec for sec in _mentioning_sections(k[0], doc))
    ]
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


def test_an_option_documented_under_a_same_named_leaf_elsewhere_does_not_count() -> None:
    """The section heuristic: `list` under nx store, with --offset, must
    not vouch for nx catalog list --offset; and prose mentioning "catalog"
    is not the command nx catalog."""
    doc = (
        "## nx store\n\nThe catalog is separate.\n\n| `list` | List entries. `--offset N` |\n\n"
        "## nx catalog\n\n| `list` | List documents |\n"
    )
    assert _mentioning_sections("nx catalog list", doc) == ["nx catalog\n\n| `list` | List documents |\n"]
    assert not any("--offset" in sec for sec in _mentioning_sections("nx catalog list", doc))
    assert any("--offset" in sec for sec in _mentioning_sections("nx store list", doc))
