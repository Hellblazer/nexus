# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve the ``nx ...`` commands a message names through the real click command tree."""
from __future__ import annotations

import re

import click

from nexus.cli import main

_COMMAND = re.compile(r"`nx ([^`]+)`")


def named_commands(text: str) -> list[list[str]]:
    """Each backticked ``nx ...`` command in *text*, as its words with flags and ``<placeholders>`` dropped."""
    return [[w for w in found.split() if not w.startswith(("-", "<"))] for found in _COMMAND.findall(text)]


def _exists(words: list[str]) -> bool:
    cmd: click.Command = main
    for word in words:
        if not isinstance(cmd, click.Group):
            return True  # what follows is the command's arguments
        sub = cmd.get_command(click.Context(cmd), word)
        if sub is None:
            return False
        cmd = sub
    return not isinstance(cmd, click.Group)  # a bare group is not a command a person can run


def missing_commands(text: str) -> list[str]:
    """The ``nx`` commands *text* names that the CLI does not have. A caller also asserts it named some."""
    return [" ".join(words) for words in named_commands(text) if not _exists(words)]
