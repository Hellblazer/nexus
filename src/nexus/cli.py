# SPDX-License-Identifier: AGPL-3.0-or-later
import click

from nexus.commands.collection import collection
from nexus.commands.index import index
from nexus.commands.memory import memory
from nexus.commands.scratch import scratch
from nexus.commands.search_cmd import search_cmd
from nexus.commands.serve import serve
from nexus.commands.store import store


@click.group()
def main() -> None:
    """Nexus — self-hosted semantic search and knowledge management."""


main.add_command(collection)
main.add_command(index)
main.add_command(memory)
main.add_command(scratch)
main.add_command(search_cmd, name="search")
main.add_command(serve)
main.add_command(store)
