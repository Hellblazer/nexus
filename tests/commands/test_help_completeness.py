# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fnhe: every CLI command must expose ``--help`` text.

Walks the Click group hierarchy rooted at ``nexus.cli.main`` and
asserts that every reachable command (and group) has a non-empty
``help`` attribute. Hidden commands (``cmd.hidden == True``) are
skipped.

Originally parametrized over every command (400+ tests). Collapsed
to loop-and-collect on the test_suite_reduction sweep — failure
messages enumerate every offender so per-command isolation in CI
output is not actually lost.
"""
from __future__ import annotations

import re

import click
from click.testing import CliRunner

from nexus.cli import main


def _iter_commands(
    group: click.Group, prefix: str = ""
) -> list[tuple[str, click.Command]]:
    out: list[tuple[str, click.Command]] = []
    for name, cmd in sorted(group.commands.items()):
        if getattr(cmd, "hidden", False):
            continue
        full = f"{prefix} {name}".strip()
        out.append((full, cmd))
        if isinstance(cmd, click.Group):
            out.extend(_iter_commands(cmd, prefix=full))
    return out


_DISCOVERED_COMMANDS = _iter_commands(main)


def test_at_least_one_command_discovered() -> None:
    assert len(_DISCOVERED_COMMANDS) > 10, (
        f"expected to discover many commands; found {len(_DISCOVERED_COMMANDS)}"
    )


def test_every_command_has_help_text() -> None:
    offenders: list[str] = []
    for full, cmd in _DISCOVERED_COMMANDS:
        if not (cmd.help or "").strip():
            offenders.append(full)
    assert not offenders, (
        f"{len(offenders)} commands missing help text (both help= and docstring empty): "
        f"{offenders}"
    )


def test_every_command_help_invocation_succeeds() -> None:
    runner = CliRunner()
    failures: list[str] = []
    for full, _cmd in _DISCOVERED_COMMANDS:
        args = full.split() + ["--help"]
        result = runner.invoke(main, args)
        if result.exit_code != 0:
            failures.append(f"`nx {' '.join(args)}` exited {result.exit_code}")
        elif "Usage:" not in result.output:
            failures.append(f"`nx {' '.join(args)}` missing Usage block")
    assert not failures, "\n".join(failures)


def test_help_text_is_not_just_placeholder() -> None:
    pat = re.compile(r"\b(TODO|FIXME|TBD)\b")
    offenders = [full for full, cmd in _DISCOVERED_COMMANDS if pat.search(cmd.help or "")]
    assert not offenders, f"placeholder tokens in help text for: {offenders}"
