# SPDX-License-Identifier: AGPL-3.0-or-later
import sys

import click

# Issue #370: line-buffer stdout/stderr at CLI entry so progress lines
# from long-running commands (nx index repo, nx enrich aspects) flush
# immediately when running in non-interactive contexts (background
# process, piped output, subprocess). Python's default stdout buffering
# is line-buffered when attached to a terminal but FULLY buffered
# otherwise, which leaves progress invisible for 10+ minutes on large
# repos. ``reconfigure(line_buffering=True)`` lands the same flush-on-
# newline behaviour regardless of terminal attachment.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        # AttributeError: pre-3.7 ``reconfigure`` is missing (we're 3.12+
        # so this branch is dead, but defensive); OSError: stream is
        # closed or doesn't support reconfigure (e.g. captured by pytest's
        # capsys, which monkey-patches stdout/stderr to non-text streams).
        pass

from nexus.commands.catalog import catalog
from nexus.commands.collection import collection
from nexus.commands.console import console
from nexus.commands.context_cmd import context
from nexus.commands.config_cmd import config_group
from nexus.commands.doc import doc
from nexus.commands.doctor import doctor_cmd
from nexus.commands.dt import dt
from nexus.commands.enrich import enrich
from nexus.commands.hook import hook_group
from nexus.commands.hooks import hooks
from nexus.commands.index import index
from nexus.commands.memory import memory
from nexus.commands.mineru import mineru_group
from nexus.commands.plan import plan as plan_group
from nexus.commands.scratch import scratch
from nexus.commands.search_cmd import search_cmd
from nexus.commands.store import store
from nexus.commands.t3 import t3 as t3_group
from nexus.commands.taxonomy_cmd import taxonomy
from nexus.commands.tier_status import tier_status_cmd
from nexus.commands.upgrade import upgrade

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

    # RDR-101 Phase 3 follow-up D (nexus-o6aa.9.9): TTY-gated upgrade
    # prompt. When the catalog is in bootstrap-fallback mode, surface
    # a one-time stderr warning to the operator so the silent split
    # state does not linger unnoticed. Suppressed in non-TTY contexts
    # (CI / cron / MCP / scripted runs) and via NEXUS_NO_PROMPTS=1.
    # Hook is here, at the top-level Click group, so it fires once per
    # CLI invocation rather than per Catalog construction.
    from nexus.commands._migration_prompt import maybe_emit_bootstrap_prompt
    maybe_emit_bootstrap_prompt()


main.add_command(catalog)
main.add_command(collection)
main.add_command(console)
main.add_command(context)
main.add_command(config_group, name="config")
main.add_command(doc)
main.add_command(dt)
main.add_command(enrich)
main.add_command(doctor_cmd, name="doctor")
hook_group.hidden = True
main.add_command(hook_group, name="hook")
main.add_command(hooks)
main.add_command(index)
main.add_command(memory)
main.add_command(mineru_group, name="mineru")
main.add_command(plan_group, name="plan")
main.add_command(scratch)
main.add_command(search_cmd, name="search")
main.add_command(store)
main.add_command(t3_group, name="t3")
main.add_command(taxonomy)
main.add_command(tier_status_cmd, name="tier-status")
main.add_command(upgrade)
