# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx settings: inspect the conexus project and user settings files (RDR-259 Decision 5).

``show`` and ``check`` import ``nexus.hooks._audience``, the byte copy of the plugin's reader, so the report is the hooks' own
reading and equals ``python3 <plugin root>/hooks/scripts/_audience.py show`` except for the version lines. There is no writer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import click

from nexus.hooks.telemetry_endpoint import telemetry_endpoint_missing  # noqa: F401 -- the report's decision lives there now

_PLUGIN_RECORD_NOTE = (
    "the plugin's version is not in Claude Code's record of installed plugins; "
    "run python3 <plugin root>/hooks/scripts/_audience.py show for it"
)


def cli_version() -> str | None:
    """The installed CLI's version."""
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version("conexus")
    except PackageNotFoundError:
        return None


def plugin_version_from_record() -> str | None:
    """The conexus plugin's version from Claude Code's record of installed plugins (``plugins/installed_plugins.json`` under
    ``CLAUDE_CONFIG_DIR`` or ``~/.claude``), or None when the record is absent, unreadable or names no conexus entry. A user-scope
    entry wins over another scope's."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    try:
        with open(os.path.join(base, "plugins", "installed_plugins.json"), encoding="utf-8") as handle:
            plugins = json.load(handle)["plugins"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(plugins, dict):
        return None
    found: list[tuple[bool, str]] = []
    for name, entries in plugins.items():
        if not (isinstance(name, str) and name.split("@", 1)[0] == "conexus" and isinstance(entries, list)):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("version"), str) and entry["version"]:
                found.append((entry.get("scope") != "user", entry["version"]))
    return min(found)[1] if found else None


def build_report(cwd: str | None) -> dict:
    """The reader's report (the telemetry missing-endpoint line is the reader's own), with the versions nx knows."""
    from nexus.hooks import _audience  # noqa: PLC0415

    plugin = plugin_version_from_record()
    report = _audience.build_report(cwd, versions={"plugin": plugin, "nx": cli_version()})
    if plugin is None:
        report["versions"]["note"] = _PLUGIN_RECORD_NOTE
    return report


def _render(report: dict) -> str:
    from nexus.hooks import _audience  # noqa: PLC0415

    text = _audience.render_report(report)
    note = report["versions"].get("note")
    if not note:
        return text
    lines = text.splitlines()
    lines.insert(lines.index("files:"), "  note: " + note)
    return "\n".join(lines) + "\n"


@click.group("settings")
def settings_group() -> None:
    """Inspect the conexus project and user settings files.

    The project file is .nexus/project.json in the repository and the user file is conexus.json under .config/nexus in your
    account home. The user file is read from the operating system's account home even when NEXUS_CONFIG_DIR is set, because that
    is the file the hooks read. There is no command that writes either file; edit them by hand.
    """


@settings_group.command("show")
@click.option("--cwd", "cwd", type=click.Path(file_okay=False), default=None, help="Directory to read the settings for. Defaults to the current directory.")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def show(cwd: str | None, as_json: bool) -> None:
    """Print both files, every setting's value and where it came from, the texts the hooks print, and any problems.

    Also prints the plugin's version and the CLI's, and warns when they differ. The output equals
    `python3 <plugin root>/hooks/scripts/_audience.py show` except for the version lines.
    """
    report = build_report(cwd)
    click.echo(json.dumps(report, indent=2, ensure_ascii=False) if as_json else _render(report), nl=as_json)


@settings_group.command("check")
@click.option("--cwd", "cwd", type=click.Path(file_okay=False), default=None, help="Directory to read the settings for. Defaults to the current directory.")
def check(cwd: str | None) -> None:
    """Print only the problems, and exit 1 when a settings file has one.

    A problem in the machine, such as no account home or a remote branch that is missing in a shallow clone, is printed but does
    not change the exit code, so a project can run this in CI.
    """
    from nexus.hooks import _audience  # noqa: PLC0415

    problems = [_audience.Problem(**p) for p in build_report(cwd)["problems"]]
    for p in problems:
        click.echo(("environment, not counted: " if p.category == "environment" else "") + _audience.format_problem(p))
    counted = [p for p in problems if p.category != "environment"]
    if not problems:
        click.echo("no problems")
    if counted:
        raise SystemExit(1)
