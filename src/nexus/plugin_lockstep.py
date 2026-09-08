"""``nx upgrade`` converges the Claude Code plugins too (nexus-2uwag).

RDR-143's SessionStart hook drives the CLI from a plugin update. This is
the other direction: when the installed ``conexus`` / ``sn`` plugin is
strictly behind this wheel, run ``claude plugin update <id> -y`` for it, so
either entry point converges all three (plugin, wheel, data).

Contract, every branch of which ``tests/test_plugin_lockstep.py`` pins:

* Registry: ``~/.claude/plugins/installed_plugins.json`` (v2 schema,
  ``"<plugin>@<marketplace>": [{"version": ...}, ...]``). Absent, unreadable,
  or without a conexus/sn entry means "not a plugin box": nothing to do,
  nothing printed.
* Only plugins strictly BEHIND the wheel are touched. A plugin ahead of the
  wheel is the SessionStart hook's direction and is left alone.
* ``claude`` absent from PATH: one line naming ``/plugin update``; never an
  error.
* The update is ``claude plugin update <id> -y`` with stdin closed: ``-y`` is
  what the CLI requires off a TTY, and it accepts only the marketplace-
  declared command, which is ours. Exit 0 with "updated from A to B" or
  "already at the latest version (X)"; exit 1 with a one-line reason
  otherwise. The CLI refreshes the marketplace clone itself (measured
  2026-09-07 on a marketplace with ``autoUpdate: false``).
* "Already at the latest" while still behind the wheel is the dev-tree /
  pre-tag case (this CLI is ahead of every published plugin): reported as
  such, never as a failure.
* A failed update never fails ``nx upgrade``: data convergence already
  happened, and a lagging plugin is not data loss. The reason and the manual
  command are printed instead.
* A successful update takes effect at the next session start; the CLI says
  "Restart to apply" and so do we, once.
* Scope: each registry entry carries its ``scope`` (user/project/local);
  the update names the same scope, so a project-scope install is updated
  where it lives instead of being reported "updated" at user scope.
* Budget: each update is bounded by ``UPDATE_TIMEOUT_S`` (a fetch of this
  repo's tag measured 5-10 s; the RDR-143 detached action bounds the whole
  ``nx upgrade`` at 120 s, so two plugins must fit well inside it).
* The RDR-143 marker (``~/.config/nexus/cli_lockstep_marker``) is NOT
  written here: it records a CLI upgrade the action confirmed, and this
  step moves the plugin, not the CLI. The next SessionStart therefore sees
  plugin ahead of marker once, nudges, and the action writes the marker
  after finding the CLI already satisfies it. One spurious detached
  action, no loop (critique [24831]).
* Test isolation: ``NX_PLUGIN_REGISTRY`` overrides the registry path, the
  same seam ``NX_LOCKSTEP_MARKER`` gives the marker; the suite's conftest
  parks it at a nonexistent path so no test, subprocess included, can run
  the real ``claude`` against a developer's plugins.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: Plugins this wheel ships; the registry key's marketplace half is taken verbatim.
PLUGINS: tuple[str, ...] = ("conexus", "sn")
#: Per-plugin wall-clock budget for ``claude plugin update`` (a git fetch,
#: measured 5-10 s). Two plugins must stay inside the RDR-143 action's 120 s.
UPDATE_TIMEOUT_S = 45
#: Test-isolation seam for the registry path (see the module docstring).
REGISTRY_ENV = "NX_PLUGIN_REGISTRY"
_UPDATED_RE = re.compile(r"updated from (\S+) to (\S+)")
_LATEST_RE = re.compile(r"already at the latest version \((\S+)\)")


def default_registry_path() -> Path:
    override = os.environ.get(REGISTRY_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """``X.Y.Z`` (optionally ``v``-prefixed) to a tuple; anything else is None."""
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


@dataclass(frozen=True)
class PluginInstall:
    version: str
    #: ``user`` | ``project`` | ``local`` (the CLI's ``-s`` values); ``user`` when absent.
    scope: str = "user"


def registry_entries(registry_path: Path | None = None) -> dict[str, list[dict[str, Any]]] | None:
    """Raw registry entries for the plugins this wheel ships, keyed by
    ``<plugin>@<marketplace>``. The ONE reader of ``installed_plugins.json``
    (nexus-2a5ij: two parsers of one file drift); :func:`installed_plugins`
    and ``nexus.health`` both build on it. Accepts the v2 shape
    (``{"version": 2, "plugins": {...}}``) and the older flat shape (the
    plugin map at top level). None when the file is absent, unreadable, or
    names none of our plugins."""
    path = registry_path or default_registry_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    plugins = data.get("plugins") if isinstance(data.get("plugins"), dict) else data
    found: dict[str, list[dict[str, Any]]] = {}
    for key, entries in plugins.items():
        if not isinstance(key, str) or "@" not in key or key.split("@", 1)[0] not in PLUGINS:
            continue
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        dicts = [e for e in entries if isinstance(e, dict)]
        if dicts:
            found[key] = dicts
    return found or None


def installed_plugins(registry_path: Path | None = None) -> dict[str, PluginInstall] | None:
    """``{"conexus@nexus-plugins": PluginInstall("7.34.1", "user"), ...}``,
    newest PARSEABLE entry per key. None when this is not a plugin box or no
    entry carries an ``X.Y.Z`` version."""
    raw = registry_entries(registry_path)
    if raw is None:
        return None
    found: dict[str, PluginInstall] = {}
    for key, entries in raw.items():
        best: PluginInstall | None = None
        for e in entries:
            if parse_version(e.get("version")) is None:
                continue
            cand = PluginInstall(e["version"], e.get("scope") if isinstance(e.get("scope"), str) else "user")
            if best is None or parse_version(cand.version) > parse_version(best.version):  # type: ignore[operator]
                best = cand
        if best is not None:
            found[key] = best
    return found or None


def wheel_version() -> str:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415 — CLI cold start

    try:
        return version("conexus")
    except PackageNotFoundError:
        return "0.0.0"


@dataclass(frozen=True)
class PluginOutcome:
    plugin_id: str
    installed: str
    #: ``updated`` | ``latest_published`` | ``failed`` | ``dry_run`` | ``unknown``
    status: str
    detail: str = ""
    now: str | None = None
    scope: str = "user"


@dataclass
class LockstepReport:
    #: ``not_a_plugin_box`` | ``in_lockstep`` | ``claude_missing`` | ``ran``
    status: str
    wheel: str
    outcomes: list[PluginOutcome] = field(default_factory=list)
    #: True when at least one update landed and a restart is what makes it live.
    restart_needed: bool = False


Runner = Callable[..., subprocess.CompletedProcess[str]]


def converge_plugins(
    *,
    dry_run: bool = False,
    registry_path: Path | None = None,
    run: Runner = subprocess.run,
    claude_path: str | None = None,
) -> LockstepReport:
    """Bring every installed conexus/sn plugin that is behind this wheel up
    to the marketplace's pinned version. See the module docstring."""
    wheel = wheel_version()
    wheel_v = parse_version(wheel)
    installed = installed_plugins(registry_path)
    if installed is None or wheel_v is None:
        return LockstepReport(status="not_a_plugin_box", wheel=wheel)
    behind = {k: v for k, v in installed.items() if (parse_version(v.version) or (0, 0, 0)) < wheel_v}
    if not behind:
        return LockstepReport(status="in_lockstep", wheel=wheel)
    claude = claude_path or shutil.which("claude")
    if claude is None:
        return LockstepReport(status="claude_missing", wheel=wheel,
                              outcomes=[PluginOutcome(k, v.version, "failed", "claude CLI not on PATH", scope=v.scope)
                                        for k, v in behind.items()])
    report = LockstepReport(status="ran", wheel=wheel)
    for plugin_id, inst in sorted(behind.items()):
        if dry_run:
            report.outcomes.append(PluginOutcome(plugin_id, inst.version, "dry_run",
                                                 f"would run: claude plugin update {plugin_id} -s {inst.scope} -y",
                                                 scope=inst.scope))
            continue
        report.outcomes.append(_update_one(run, claude, plugin_id, inst, wheel_v))
    report.restart_needed = any(o.status == "updated" for o in report.outcomes)
    return report


def _update_one(run: Runner, claude: str, plugin_id: str, inst: PluginInstall,
                wheel_v: tuple[int, int, int]) -> PluginOutcome:
    have = inst.version
    cmd = [claude, "plugin", "update", plugin_id, "-s", inst.scope, "-y"]
    try:
        proc = run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=UPDATE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        _log.warning("plugin_lockstep_timeout", plugin=plugin_id, timeout_s=UPDATE_TIMEOUT_S)
        return PluginOutcome(plugin_id, have, "failed", f"claude plugin update timed out after {UPDATE_TIMEOUT_S}s", scope=inst.scope)
    except OSError as e:
        _log.warning("plugin_lockstep_spawn_failed", plugin=plugin_id, error=str(e))
        return PluginOutcome(plugin_id, have, "failed", f"could not run claude: {e}", scope=inst.scope)
    text = (proc.stdout or "") + (proc.stderr or "")
    last = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
    if proc.returncode != 0:
        _log.warning("plugin_lockstep_update_failed", plugin=plugin_id, rc=proc.returncode, last=last)
        return PluginOutcome(plugin_id, have, "failed", last or f"exit {proc.returncode}", scope=inst.scope)
    if (m := _UPDATED_RE.search(text)) and parse_version(m[2]) is not None:
        _log.info("plugin_lockstep_updated", plugin=plugin_id, before=m[1], after=m[2])
        return PluginOutcome(plugin_id, have, "updated", now=m[2], scope=inst.scope)
    if m := _LATEST_RE.search(text):
        published = m[1]
        pv = parse_version(published)
        if pv is not None and pv < wheel_v:
            return PluginOutcome(plugin_id, have, "latest_published",
                                 f"{published} is the newest published plugin; this CLI is ahead of every release",
                                 now=published, scope=inst.scope)
        return PluginOutcome(plugin_id, have, "updated", now=published, scope=inst.scope)
    # Exit 0 with a shape this parser does not know: NOT a confirmed update
    # (no silent fallback for a correctness fact). Report what the CLI said
    # and let the next `nx upgrade` re-read the registry.
    _log.warning("plugin_lockstep_unrecognised_output", plugin=plugin_id, last=last)
    return PluginOutcome(plugin_id, have, "unknown", detail=last or "claude plugin update exited 0 with no recognisable verdict",
                         scope=inst.scope)


def render(report: LockstepReport, echo: Callable[[str], Any]) -> None:
    """One line per plugin touched; silence in lockstep or off a plugin box."""
    if report.status in ("not_a_plugin_box", "in_lockstep"):
        return
    if report.status == "claude_missing":
        ids = ", ".join(o.plugin_id for o in report.outcomes)
        echo(f"Plugin update: {ids} behind conexus {report.wheel}, but the claude CLI is not on PATH; "
             "run /plugin update in Claude Code")
        return
    for o in report.outcomes:
        match o.status:
            case "updated":
                echo(f"Plugin update: {o.plugin_id} {o.installed} -> {o.now}")
            case "latest_published":
                echo(f"Plugin update: {o.plugin_id} {o.installed} -> {o.now}: {o.detail}")
            case "dry_run":
                echo(f"Plugin update: {o.plugin_id} {o.installed} behind conexus {report.wheel}; {o.detail}")
            case "unknown":
                echo(f"Plugin update: {o.plugin_id} {o.installed}: claude plugin update exited 0 but said "
                     f"'{o.detail}'; not confirmed. Check: claude plugin list")
            case _:
                echo(f"Plugin update: {o.plugin_id} {o.installed} still behind conexus {report.wheel}: {o.detail}. "
                     f"Run: claude plugin update {o.plugin_id} -s {o.scope} -y")
    if report.restart_needed:
        echo("Plugin update: restart the Claude Code session (or run /mcp) to load the updated plugin")
