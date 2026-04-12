# SPDX-License-Identifier: AGPL-3.0-or-later
import click

from nexus.commands.catalog import catalog
from nexus.commands.collection import collection
from nexus.commands.config_cmd import config_group
from nexus.commands.doctor import doctor_cmd
from nexus.commands.enrich import enrich
from nexus.commands.hook import hook_group
from nexus.commands.hooks import hooks
from nexus.commands.index import index
from nexus.commands.memory import memory
from nexus.commands.mineru import mineru_group
from nexus.commands.scratch import scratch
from nexus.commands.search_cmd import search_cmd
from nexus.commands.store import store
from nexus.commands.taxonomy_cmd import taxonomy

@click.group()
@click.version_option(package_name="conexus", prog_name="nx")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Nexus — self-hosted semantic search and knowledge management."""
    from nexus.logging_setup import configure_logging

    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    configure_logging("cli", verbose=verbose)


main.add_command(catalog)
main.add_command(collection)
main.add_command(config_group, name="config")
main.add_command(enrich)
main.add_command(doctor_cmd, name="doctor")
hook_group.hidden = True
main.add_command(hook_group, name="hook")
main.add_command(hooks)
main.add_command(index)
main.add_command(memory)
main.add_command(mineru_group, name="mineru")
main.add_command(scratch)
main.add_command(search_cmd, name="search")
main.add_command(store)
main.add_command(taxonomy)
