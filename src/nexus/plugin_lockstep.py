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
* A plugin strictly BEHIND the wheel is updated by version (below); a
  plugin ahead of the wheel is the SessionStart hook's direction and is
  left alone. A plugin exactly AT the wheel's version gets the separate
  ref-drift check below instead.
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
  ``nx upgrade`` at 120 s, so two plugins must fit well inside it). The
  ref-drift check's COMMON case (no drift found) is cheap and fits inside
  the same budget; its RARE worst case (both plugins actually drifted,
  each needing a full uninstall+install) is accepted to exceed 120 s -- a
  timed-out detached action just retries next session, never a hang.
* A plugin whose registry version already EQUALS the wheel is also checked
  for a moved release ref (nexus-konsk): a plugin-only cut (RDR-197) can
  land a new commit under an anchored tag (``plugin-v{version}-{n}``)
  without ever moving the client version, and ``claude plugin update``
  reports "already at the latest version" and does nothing in that case
  (measured, nexus-semdv) -- invisible to the version-only comparison
  above. For each such plugin, its marketplace is refreshed
  (``claude plugin marketplace update <name>``, at most once per DISTINCT
  marketplace per call, shared across every plugin on it) and the
  marketplace's own pinned ``source.ref`` is resolved to a commit inside
  the refreshed local clone (``~/.claude/plugins/marketplaces/<name>``)
  and compared against the registry's own ``gitCommitSha``. A mismatch
  means the ref moved to content the install does not have yet, so this
  runs ``claude plugin uninstall <id> -s <scope> -y`` THEN
  ``claude plugin install <id> -s <scope> -y`` (not ``update`` -- an
  anchored ref names no new *version* for ``update`` to move to; and not
  a bare ``install`` alone -- measured against the real CLI, nexus-konsk
  gate rehearsal 2026-09-11, ``install`` on an already-installed plugin
  at the SAME declared version is a no-op even when the ref moved
  underneath it, so the two-step dance is load-bearing, not belt and
  suspenders) and then RE-READS the registry to CONFIRM the sha actually
  moved, rather than trusting the exit code or an output string. No
  marketplace info resolvable (no ``known_marketplaces.json`` entry, no
  local clone, no matching plugin entry, no resolvable ref) means
  "cannot tell" and is skipped silently: this layer is a best-effort
  ADDITION on top of the version-based flow above, never a new way for
  ``nx upgrade`` to fail.
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
#: Bound for ``claude plugin marketplace update <name>`` -- a git fetch of
#: one repo, refreshed at most once per distinct marketplace per call
#: (memoised across every plugin that shares a marketplace).
MARKETPLACE_REFRESH_TIMEOUT_S = 20
#: Bound for resolving a ref to a commit inside the already-fetched local
#: marketplace clone -- no network, plain git plumbing.
REF_RESOLVE_TIMEOUT_S = 10
#: Bound for ``claude plugin uninstall`` -- a local filesystem removal,
#: no network, so a tighter budget than ``UPDATE_TIMEOUT_S`` is warranted.
UNINSTALL_TIMEOUT_S = 15
#: Test-isolation seam for the registry path (see the module docstring).
REGISTRY_ENV = "NX_PLUGIN_REGISTRY"
#: Test-isolation seam for the known-marketplaces registry (the ref-drift
#: check's source for each marketplace's local clone location).
MARKETPLACES_ENV = "NX_PLUGIN_MARKETPLACES"
_UPDATED_RE = re.compile(r"updated from (\S+) to (\S+)")
_LATEST_RE = re.compile(r"already at the latest version \((\S+)\)")


def default_registry_path() -> Path:
    override = os.environ.get(REGISTRY_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def default_marketplaces_path() -> Path:
    override = os.environ.get(MARKETPLACES_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / "plugins" / "known_marketplaces.json"


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
    #: The registry's own ``gitCommitSha`` for this entry, when present.
    #: ``None`` for a registry shape/entry that carries no such field --
    #: the ref-drift check treats that as "cannot tell" (see module docstring).
    git_commit_sha: str | None = None


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
            cand = PluginInstall(
                e["version"],
                e.get("scope") if isinstance(e.get("scope"), str) else "user",
                e.get("gitCommitSha") if isinstance(e.get("gitCommitSha"), str) else None,
            )
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
    #: | ``ref_moved`` | ``ref_check_failed`` (the last two: nexus-konsk, the
    #: same-version-ref-move check; see module docstring).
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
    marketplaces_path: Path | None = None,
    run: Runner = subprocess.run,
    claude_path: str | None = None,
) -> LockstepReport:
    """Bring every installed conexus/sn plugin that is behind this wheel up
    to the marketplace's pinned version, AND (nexus-konsk) reinstall a
    plugin whose version already equals the wheel but whose release ref
    moved under it (a same-version plugin-only cut). See the module
    docstring for both flows."""
    wheel = wheel_version()
    wheel_v = parse_version(wheel)
    installed = installed_plugins(registry_path)
    if installed is None or wheel_v is None:
        return LockstepReport(status="not_a_plugin_box", wheel=wheel)
    behind = {k: v for k, v in installed.items() if (parse_version(v.version) or (0, 0, 0)) < wheel_v}
    at_wheel = {k: v for k, v in installed.items() if parse_version(v.version) == wheel_v}
    if not behind and not at_wheel:
        return LockstepReport(status="in_lockstep", wheel=wheel)
    claude = claude_path or shutil.which("claude")
    if claude is None:
        return LockstepReport(status="claude_missing", wheel=wheel,
                              outcomes=[PluginOutcome(k, v.version, "failed", "claude CLI not on PATH", scope=v.scope)
                                        for k, v in sorted((behind | at_wheel).items())])
    report = LockstepReport(status="ran", wheel=wheel)
    for plugin_id, inst in sorted(behind.items()):
        if dry_run:
            report.outcomes.append(PluginOutcome(plugin_id, inst.version, "dry_run",
                                                 f"would run: claude plugin update {plugin_id} -s {inst.scope} -y",
                                                 scope=inst.scope))
            continue
        report.outcomes.append(_update_one(run, claude, plugin_id, inst, wheel_v))
    if not dry_run:
        # At most one marketplace refresh per DISTINCT marketplace, shared
        # across every plugin on it, to stay inside the RDR-143 budget.
        refreshed: dict[str, bool] = {}
        for plugin_id, inst in sorted(at_wheel.items()):
            outcome = _check_ref_drift(run, claude, plugin_id, inst, registry_path, marketplaces_path, refreshed)
            if outcome is not None:
                report.outcomes.append(outcome)
    report.restart_needed = any(o.status in ("updated", "ref_moved") for o in report.outcomes)
    if not report.outcomes:
        # Every `behind` plugin always produces an outcome (dry_run or
        # _update_one's return is never None); an empty list here means
        # every `at_wheel` plugin's ref-drift check found nothing to
        # report -- back to the silent "in lockstep" verdict, exactly as
        # when there was no ref-drift check to run at all.
        report.status = "in_lockstep"
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


def _marketplace_install_location(marketplace_name: str, marketplaces_path: Path | None) -> Path | None:
    """The local clone directory for *marketplace_name*, per
    ``known_marketplaces.json``. ``None`` when the file is absent,
    unreadable, carries no such marketplace, or the entry has no usable
    ``installLocation`` -- always "cannot tell", never an error."""
    path = marketplaces_path or default_marketplaces_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(marketplace_name)
    if not isinstance(entry, dict):
        return None
    loc = entry.get("installLocation")
    return Path(loc) if isinstance(loc, str) and loc else None


def _refresh_marketplace(run: Runner, claude: str, marketplace_name: str) -> None:
    """Best-effort ``claude plugin marketplace update <name>``: refreshes
    the local clone so :func:`_pinned_ref_for_plugin` reads current data.
    Never raises -- a failed refresh just means the read that follows may
    see stale content, which the next ``nx upgrade`` run will retry."""
    try:
        run([claude, "plugin", "marketplace", "update", marketplace_name],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=MARKETPLACE_REFRESH_TIMEOUT_S, check=False)
    except (subprocess.TimeoutExpired, OSError) as e:
        _log.warning("plugin_lockstep_marketplace_refresh_failed", marketplace=marketplace_name, error=str(e))


def _pinned_ref_for_plugin(plugin_short_name: str, install_location: Path) -> str | None:
    """The marketplace's currently pinned ``source.ref`` for
    *plugin_short_name*, read from ``<install_location>/.claude-plugin/
    marketplace.json``. ``None`` on any unreadable/unexpected shape."""
    try:
        data = json.loads((install_location / ".claude-plugin" / "marketplace.json").read_text())
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        return None
    for p in plugins:
        if isinstance(p, dict) and p.get("name") == plugin_short_name:
            source = p.get("source")
            ref = source.get("ref") if isinstance(source, dict) else None
            return ref if isinstance(ref, str) and ref else None
    return None


def _resolve_ref_sha(run: Runner, install_location: Path, ref: str) -> str | None:
    """The commit *ref* resolves to inside the (already refreshed) local
    marketplace clone -- no network, plain git plumbing. ``None`` on any
    failure (unresolvable ref, git absent, blind checkout)."""
    try:
        proc = run(["git", "-C", str(install_location), "rev-parse", f"{ref}^{{commit}}"],
                   capture_output=True, text=True, timeout=REF_RESOLVE_TIMEOUT_S, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _run_claude_step(run: Runner, claude: str, verb: str, plugin_id: str, scope: str,
                     timeout_s: int) -> tuple[bool, str]:
    """Run one ``claude plugin <verb> <id> -s <scope> -y`` step for the
    ref-drift reinstall. Returns ``(ok, last_line)`` -- ``ok`` is only the
    process outcome (exit 0), never a claim about content; the caller
    re-reads the registry to confirm that separately."""
    cmd = [claude, "plugin", verb, plugin_id, "-s", scope, "-y"]
    try:
        proc = run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return False, f"claude plugin {verb} timed out after {timeout_s}s"
    except OSError as e:
        return False, f"could not run claude: {e}"
    text = (proc.stdout or "") + (proc.stderr or "")
    last = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
    if proc.returncode != 0:
        return False, last or f"exit {proc.returncode}"
    return True, last


def _check_ref_drift(run: Runner, claude: str, plugin_id: str, inst: PluginInstall,
                     registry_path: Path | None, marketplaces_path: Path | None,
                     refreshed: dict[str, bool]) -> PluginOutcome | None:
    """The nexus-konsk check: a plugin already at the wheel's version may
    still be missing content a same-version anchored cut shipped, because
    ``claude plugin update`` never reinstalls a plugin whose VERSION did
    not move. Returns ``None`` when nothing is confirmed different (no
    marketplace info, no resolvable ref/sha, or the sha already matches --
    every one of these is "cannot tell" or "no drift", never an error) --
    silence exactly as :func:`converge_plugins` is silent when in lockstep.
    Returns an outcome only when a drift was found and acted on."""
    if "@" not in plugin_id:
        return None
    plugin_short, marketplace_name = plugin_id.split("@", 1)
    install_location = _marketplace_install_location(marketplace_name, marketplaces_path)
    if install_location is None:
        return None
    if marketplace_name not in refreshed:
        _refresh_marketplace(run, claude, marketplace_name)
        refreshed[marketplace_name] = True
    ref = _pinned_ref_for_plugin(plugin_short, install_location)
    if ref is None:
        return None
    target_sha = _resolve_ref_sha(run, install_location, ref)
    if target_sha is None or inst.git_commit_sha is None or target_sha == inst.git_commit_sha:
        return None
    have = inst.version
    was = inst.git_commit_sha[:7]
    # `claude plugin install` ALONE on an already-installed plugin at the
    # SAME declared version is a no-op even when the underlying ref moved
    # (measured against the real CLI, nexus-konsk gate rehearsal 2026-09-11
    # -- the documentation this module once trusted, nexus-semdv, was never
    # itself verified for bare `install`; only uninstall-then-install was).
    # There is no `claude plugin update --force` and no `--reinstall`: the
    # two-step dance IS the verified path.
    ok, last = _run_claude_step(run, claude, "uninstall", plugin_id, inst.scope, UNINSTALL_TIMEOUT_S)
    if not ok:
        _log.warning("plugin_lockstep_ref_check_uninstall_failed", plugin=plugin_id, last=last)
        return PluginOutcome(plugin_id, have, "ref_check_failed", last, scope=inst.scope)
    ok, last = _run_claude_step(run, claude, "install", plugin_id, inst.scope, UPDATE_TIMEOUT_S)
    if not ok:
        _log.warning("plugin_lockstep_ref_check_install_failed", plugin=plugin_id, last=last)
        return PluginOutcome(plugin_id, have,
                             "ref_check_failed", f"uninstalled but reinstall failed: {last}", scope=inst.scope)
    # Never trust the exit code or an output string alone (the CLI's own
    # printed text for `install` on an already-installed plugin is not a
    # pinned contract the way `update`'s "updated from A to B" is) --
    # RE-READ the registry and confirm the sha actually moved.
    now_installed = installed_plugins(registry_path) or {}
    now_sha = now_installed.get(plugin_id, inst).git_commit_sha
    if now_sha != target_sha:
        _log.warning("plugin_lockstep_ref_check_unconfirmed", plugin=plugin_id, expected=target_sha, now=now_sha)
        return PluginOutcome(plugin_id, have, "ref_check_failed",
                             f"uninstall+install exited 0 but the registry sha is still not {target_sha[:7]}: not confirmed",
                             scope=inst.scope)
    _log.info("plugin_lockstep_ref_moved", plugin=plugin_id, before=was, after=target_sha[:7])
    return PluginOutcome(plugin_id, have, "ref_moved", f"{was} -> {target_sha[:7]}", now=have, scope=inst.scope)


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
            case "ref_moved":
                echo(f"Plugin update: {o.plugin_id} {o.installed}: picked up a plugin-only release ({o.detail})")
            case "ref_check_failed":
                echo(f"Plugin update: {o.plugin_id} {o.installed}: a plugin-only release exists but reinstall "
                     f"failed: {o.detail}. Run: claude plugin uninstall {o.plugin_id} -s {o.scope} -y "
                     f"&& claude plugin install {o.plugin_id} -s {o.scope} -y")
            case _:
                echo(f"Plugin update: {o.plugin_id} {o.installed} still behind conexus {report.wheel}: {o.detail}. "
                     f"Run: claude plugin update {o.plugin_id} -s {o.scope} -y")
    if report.restart_needed:
        echo("Plugin update: restart the Claude Code session (or run /mcp) to load the updated plugin")
