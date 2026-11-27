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

#: The message the telemetry report gives when ``telemetry`` is on and the account names no engine (RDR-259 Decision 9).
TELEMETRY_NO_ENGINE = (
    "telemetry is on, but no engine is configured under the account's config directory; "
    "an engine named only in the environment is not used for telemetry"
)
_PLUGIN_RECORD_NOTE = (
    "the plugin's version is not in Claude Code's record of installed plugins; "
    "run python3 <plugin root>/hooks/scripts/_audience.py show for it"
)
_DEFAULT_TENANT = "default"


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


def telemetry_endpoint_missing(config_dir: Path) -> bool:
    """True when no engine is named under *config_dir*, the account's ``.config/nexus``.

    This must agree with the plugin's ``_endpoint_resolve.pinned_endpoint(config_dir)`` (nexus-sby49.10): the same three sources and
    no environment leg. An engine is a ``credentials.service_url`` in ``config.yml`` (no ``user:password@`` in it) with a token
    (a fresh data-token lease for that URL, the file's ``service_token``, or a live lease's token when the lease names that same
    origin), or a live local supervisor lease alone; and no bearer goes over plain http off this machine (nexus-ec37o), so an
    ``http`` URL that is not loopback names no engine either. Nothing here reads ``NX_SERVICE_*`` or any other environment variable.
    """
    from nexus._log_safe_url import has_userinfo  # noqa: PLC0415
    from nexus.config import persisted_credentials  # noqa: PLC0415
    from nexus.db.service_endpoint import host_port_url, is_loopback_host, normalize_origin, origins_equal  # noqa: PLC0415

    def carries_a_bearer(url: str) -> bool:
        origin = normalize_origin(url)
        return origin is not None and (origin[0] == "https" or is_loopback_host(origin[1]))

    lease = _lease(config_dir)
    if lease is not None and not lease.get("token"):
        lease = None
    lease_url = host_port_url(lease["host"], lease["port"]) if lease is not None else ""
    try:
        credentials = persisted_credentials(config_dir)
    except Exception:  # noqa: BLE001 -- an unreadable config.yml names no engine
        credentials = {}
    url = credentials.get("service_url", "").strip().rstrip("/")
    if url:
        if has_userinfo(url) or not carries_a_bearer(url):
            return True
        if credentials.get("service_token", "").strip() or (lease is not None and origins_equal(url, lease_url)):
            return False
        return _data_token(config_dir, url) is None
    return lease is None or not carries_a_bearer(lease_url)


def _lease(config_dir: Path) -> dict[str, str] | None:
    """The live local supervisor lease under *config_dir* as ``{"host", "port", "token"}``, or None."""
    from nexus.daemon.service_registry import ServiceRegistry, service_identity  # noqa: PLC0415

    try:
        record = ServiceRegistry(dir=config_dir, tier="storage_service").discover(service_identity())
        if record is None:
            return None
        endpoint = record.endpoint
        if int(endpoint.get("port", 0)) <= 0:
            return None
        return {"host": str(endpoint.get("host", "127.0.0.1")), "port": str(endpoint["port"]), "token": str(endpoint.get("token", ""))}
    except Exception:  # noqa: BLE001 -- any failure reads as no lease
        return None


def _data_token(config_dir: Path, url: str) -> str | None:
    from nexus.db.data_token import DataTokenManager  # noqa: PLC0415

    try:
        return DataTokenManager(config_dir=config_dir).fresh_lease_token(url, _DEFAULT_TENANT, near_expiry_threshold=0.0)
    except Exception:  # noqa: BLE001
        return None


def build_report(cwd: str | None) -> dict:
    """The reader's report, with the versions nx knows and the telemetry missing-endpoint line when it applies."""
    from nexus.hooks import _audience  # noqa: PLC0415

    plugin = plugin_version_from_record()
    report = _audience.build_report(cwd, versions={"plugin": plugin, "nx": cli_version()})
    if plugin is None:
        report["versions"]["note"] = _PLUGIN_RECORD_NOTE
    telemetry = next((row for row in report["settings"] if row["key"] == "telemetry"), None)
    try:
        home = _audience.account_home()
    except Exception:  # noqa: BLE001 -- the reader already reports an account home that cannot be read
        home = None
    if telemetry is not None and telemetry["value"] is True and home:
        if telemetry_endpoint_missing(Path(home) / ".config" / "nexus"):
            problem = _audience.Problem(_audience.USER_LABEL, "telemetry", TELEMETRY_NO_ENGINE, "no events are sent", "environment")
            report["problems"].append(dict(problem._asdict()))
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
