# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fnhe: every CLI command must expose ``--help`` text.

Walks the Click group hierarchy rooted at ``nexus.cli.main`` and
asserts that every reachable command (and group) has a non-empty
``help`` attribute. The motivating critique pointed out that users
who tab-complete into a new subcommand should get a useful
``--help`` page instead of a bare usage line.

Hidden commands (``cmd.hidden == True``, e.g. the ``hook`` group)
are skipped: they exist for plumbing, not for users to discover.
"""
from __future__ import annotations

import re

import click
import pytest
from click.testing import CliRunner

from nexus.cli import main


def _iter_commands(
    group: click.Group, prefix: str = ""
) -> list[tuple[str, click.Command]]:
    """Walk the Click hierarchy; return ``[(full_name, cmd), ...]`` pairs.

    Skips commands marked ``hidden=True`` (e.g. the internal ``hook``
    group). Recurses into nested ``click.Group`` instances so subgroups
    like ``daemon t2 install`` are visited as well.
    """
    out: list[tuple[str, click.Command]] = []
    for name, cmd in sorted(group.commands.items()):
        if getattr(cmd, "hidden", False):
            continue
        full = f"{prefix} {name}".strip()
        out.append((full, cmd))
        if isinstance(cmd, click.Group):
            out.extend(_iter_commands(cmd, prefix=full))
    return out


# Module-level so pytest's parametrize id list is computed once at import.
_DISCOVERED_COMMANDS = _iter_commands(main)


def test_at_least_one_command_discovered() -> None:
    """Sanity check: the walker actually found the registered commands.

    Guards against a future refactor that accidentally moves the registration
    or empties the dispatch table; without this assertion an empty parametrize
    would silently no-op the rest of the suite.
    """
    assert len(_DISCOVERED_COMMANDS) > 10, (
        f"expected to discover many commands; found {len(_DISCOVERED_COMMANDS)}"
    )


@pytest.mark.parametrize(
    "full_name, cmd",
    _DISCOVERED_COMMANDS,
    ids=[full for full, _ in _DISCOVERED_COMMANDS],
)
def test_command_has_help_text(full_name: str, cmd: click.Command) -> None:
    """Every visible command/group has ``help`` text (docstring or help=).

    Click pulls ``help`` from the explicit ``help=`` kwarg first, then the
    decorated function's docstring. Either path satisfies us; we only fail
    when both are empty or whitespace-only.
    """
    help_text = (cmd.help or "").strip()
    assert help_text, (
        f"command `nx {full_name}` is missing help text "
        f"(both ``help=`` and docstring are empty). "
        "Users tab-completing to this command see a bare usage line."
    )


@pytest.mark.parametrize(
    "full_name, cmd",
    _DISCOVERED_COMMANDS,
    ids=[full for full, _ in _DISCOVERED_COMMANDS],
)
def test_command_help_invocation_succeeds(
    full_name: str, cmd: click.Command
) -> None:
    """Running ``<cmd> --help`` exits 0 and prints a Usage line.

    Catches the case where ``help`` text is present but the command
    body raises at import-time when --help walks its docstring, or
    where Click's own help renderer fails because of bad option
    declarations.
    """
    runner = CliRunner()
    args = full_name.split() + ["--help"]
    result = runner.invoke(main, args)
    assert result.exit_code == 0, (
        f"`nx {' '.join(args)}` exited {result.exit_code}; output:\n{result.output}"
    )
    # Click renders the help with a "Usage:" line at the top.
    assert "Usage:" in result.output, (
        f"`nx {' '.join(args)}` did not produce a Usage block:\n{result.output}"
    )


def test_help_text_is_not_just_placeholder() -> None:
    """Catch tokens like ``TODO``, ``FIXME``, or naked ``TBD`` in help text."""
    pat = re.compile(r"\b(TODO|FIXME|TBD)\b")
    offenders: list[str] = []
    for full, cmd in _DISCOVERED_COMMANDS:
        if pat.search(cmd.help or ""):
            offenders.append(full)
    assert not offenders, (
        f"placeholder tokens in help text for: {offenders}"
    )
