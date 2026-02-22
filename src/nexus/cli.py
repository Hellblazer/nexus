# SPDX-License-Identifier: AGPL-3.0-or-later
import click

from nexus.commands.memory import memory


@click.group()
def main() -> None:
    """Nexus — self-hosted semantic search and knowledge management."""


main.add_command(memory)
