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
#
# nexus-vwu1 (GH #621): also force UTF-8 with replacement on Windows
# where the default cp1252 console can't encode the status glyphs nx
# emits (checkmarks, crosses, ellipses, em-dashes). Without this,
# every ``click.echo`` carrying a non-ASCII char crashes with
# UnicodeEncodeError. ``errors="replace"`` is the conservative tail:
# a console that genuinely can't render a glyph gets a "?" instead of
# a stack trace. POSIX hosts are left untouched (modern Linux/macOS
# default to UTF-8 already).
_IS_WINDOWS = sys.platform == "win32"
for _stream in (sys.stdout, sys.stderr):
    try:
        if _IS_WINDOWS:
            _stream.reconfigure(
                encoding="utf-8", errors="replace", line_buffering=True,
            )
        else:
            _stream.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        # AttributeError: pre-3.7 ``reconfigure`` is missing (we're 3.12+
        # so this branch is dead, but defensive); OSError: stream is
        # closed or doesn't support reconfigure (e.g. captured by pytest's
        # capsys, which monkey-patches stdout/stderr to non-text streams).
        pass

from nexus.commands.catalog import catalog
from nexus.commands.collection import collection
from nexus.commands.command_context import command_context
from nexus.commands.console import console
from nexus.commands.context_cmd import context
from nexus.commands.config_cmd import config_group
from nexus.commands.uninstall import uninstall_cmd
from nexus.commands.daemon import daemon_group
from nexus.commands.doc import doc
from nexus.commands.doctor import doctor_cmd
from nexus.commands.dt import dt
from nexus.commands.enrich import enrich
from nexus.commands.hook import hook_group
from nexus.commands.hooks import hooks
from nexus.commands.index import index
from nexus.commands.init import init_cmd
from nexus.commands.memory import memory
from nexus.commands.migration_cmd import migration_cmd
from nexus.commands.mineru import mineru_group
from nexus.commands.plan import plan as plan_group
from nexus.commands.rdr import rdr as rdr_group
from nexus.commands.remediation_cmd import forensics_cmd, remediate_cmd
from nexus.commands.scratch import scratch
from nexus.commands.search_cmd import search_cmd
from nexus.commands.service_cmd import service
from nexus.commands.store import store
from nexus.commands.t3 import t3 as t3_group
from nexus.commands.taxonomy_cmd import taxonomy
from nexus.commands.tenant_cmd import tenant
from nexus.commands.tier_status import tier_status_cmd
from nexus.commands.aspects import aspects_group
from nexus.commands.upgrade import upgrade

@click.group()
@click.version_option(package_name="conexus", prog_name="nx")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Nexus — self-hosted semantic search and knowledge management."""
    from nexus.logging_setup import configure_logging  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

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
    from nexus.commands._migration_prompt import maybe_emit_bootstrap_prompt  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
    maybe_emit_bootstrap_prompt()

    # nexus-gynt2: stranded-install detector. Disarmed (a pure constant
    # check, no filesystem access) on every migration-capable release; at
    # N+1 it trips a LOUD stderr banner on EVERY invocation while pre-PG
    # data sits unmigrated — correctness class, no stamp-once suppression.
    # ORDER MATTERS (critique 21029 Critical 1): this block must run BEFORE
    # the upgrade-finish trigger below — check_version_transition rewrites
    # the last_seen_version stamp to the running version, and the detector
    # reads that stamp as the pre-PG era. Detector first = the first
    # invocation after a direct hop onto N+1 still reports the true era.
    try:
        from nexus.config import detect_stranded_install_default  # noqa: PLC0415 — deferred import

        _stranded = detect_stranded_install_default()
        if _stranded is not None:
            click.echo(f"[stranded-install] {_stranded.message}", err=True)
    except Exception:  # noqa: BLE001 — the detector must never break CLI startup
        import structlog  # noqa: PLC0415 — deferred import
        structlog.get_logger(__name__).warning(
            "stranded_install_check_failed", exc_info=True,
        )

    # nexus-4xgfy: finish-the-upgrade auto-trigger. uv offers no
    # post-install hook, so the first invocation after a version change
    # runs the safe finish pass (restart detached stale daemons; name the
    # session-bound ones) and says so in one line. Same-version runs are a
    # single stat() — effectively free.
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import
        from nexus.upgrade_finish import check_version_transition  # noqa: PLC0415 — deferred import

        _summary = check_version_transition(nexus_config_dir())
        if _summary:
            click.echo(f"[upgrade-finish] {_summary}", err=True)
    except Exception:  # noqa: BLE001 — the trigger must never break CLI startup
        import structlog  # noqa: PLC0415 — deferred import
        structlog.get_logger(__name__).warning(
            "upgrade_finish_trigger_failed", exc_info=True,
        )


main.add_command(catalog)
main.add_command(collection)
main.add_command(command_context, name="command-context")
main.add_command(console)
main.add_command(context)
main.add_command(config_group, name="config")
main.add_command(daemon_group, name="daemon")
main.add_command(doc)
main.add_command(dt)
main.add_command(enrich)
main.add_command(doctor_cmd, name="doctor")
hook_group.hidden = True
main.add_command(hook_group, name="hook")
main.add_command(hooks)
main.add_command(index)
main.add_command(init_cmd, name="init")
main.add_command(memory)
main.add_command(migration_cmd, name="migration")
main.add_command(mineru_group, name="mineru")
main.add_command(plan_group, name="plan")
main.add_command(rdr_group, name="rdr")
main.add_command(forensics_cmd, name="forensics")
main.add_command(remediate_cmd, name="remediate")
main.add_command(scratch)
main.add_command(search_cmd, name="search")
main.add_command(service, name="service")
main.add_command(store)
main.add_command(t3_group, name="t3")
main.add_command(taxonomy)
main.add_command(tenant, name="tenant")
main.add_command(tier_status_cmd, name="tier-status")
main.add_command(aspects_group, name="aspects")
main.add_command(upgrade)
main.add_command(uninstall_cmd, name="uninstall")


if __name__ == "__main__":
    # Enables ``python -m nexus.cli``, the fallback argv that
    # ``nexus.commands.daemon._resolve_nx_bin`` emits when the ``nx``
    # console script is not on PATH (e.g. launchd/systemd autostart
    # environments). Without this guard that fallback ran nothing and
    # exited 0, so daemon autostart silently never started (nexus-n8sbw).
    main()
