# SPDX-License-Identifier: AGPL-3.0-or-later
import logging

import click
import structlog

from nexus.commands.collection import collection
from nexus.commands.config_cmd import config_group
from nexus.commands.doctor import doctor_cmd
from nexus.commands.hook import hook_group
from nexus.commands.index import index
from nexus.commands.memory import memory
from nexus.commands.pm import pm
from nexus.commands.scratch import scratch
from nexus.commands.search_cmd import search_cmd
from nexus.commands.serve import serve
from nexus.commands.store import store

def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )
    # Suppress noisy HTTP wire-trace loggers even in verbose mode
    for noisy in ("httpx", "httpcore", "chromadb.telemetry", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@click.group()
@click.version_option(package_name="conexus", prog_name="nx")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Nexus — self-hosted semantic search and knowledge management."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _configure_logging(verbose)


main.add_command(collection)
main.add_command(config_group, name="config")
main.add_command(doctor_cmd, name="doctor")
hook_group.hidden = True
main.add_command(hook_group, name="hook")
main.add_command(index)
main.add_command(memory)
main.add_command(pm)
main.add_command(scratch)
main.add_command(search_cmd, name="search")
main.add_command(serve)
main.add_command(store)
