#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-143 SessionStart hook: plugin<->CLI version lockstep (Shape B).

The blocking, stdlib-only SessionStart entry point. It detects skew
between the installed plugin version (the marketplace surface) and the
last-confirmed CLI version (a per-user marker), and when they diverge it
(a) emits an additionalContext nudge so the in-session model knows an
upgrade is in flight, and (b) dispatches a DETACHED action that performs
the extras-preserving two-command upgrade. The hook returns immediately
and NEVER wedges synchronous SessionStart (CA-4), NEVER writes the marker
(the action owns that, on confirmed upgrade only), and NEVER raises
(fail-safe exit 0).

nexus-konsk (P0, 2026-09-11): a same-version plugin-only cut (RDR-197)
never moves plugin.json's ``version`` field, so the skew check above can
never see it -- ``plugin-v7.41.0-1`` shipped to nobody automatically this
way (measured; see RDR-197's Validation section). RDR-197's original CA-2
("the lockstep hook stays silent on a plugin-cut install") is REVISED by
this fix: the hook now ALSO checks, with no network, whether each
installed plugin's registry ``gitCommitSha`` still matches what the local
marketplace clone's pinned ``source.ref`` resolves to (plain
``git rev-parse <ref>^{commit}`` plumbing -- no
``claude plugin marketplace update`` fetch, so a not-yet-refreshed clone
is a false negative here, never a false positive; the DEFINITIVE check,
which does refresh over the network, is
``nexus.plugin_lockstep.converge_plugins``'s own ref-drift step, run
inside the ``nx upgrade`` this dispatches). A mismatch gets the same
treatment as a version mismatch: one nudge line naming the drift, plus a
detached dispatch -- see ``detect_ref_drift`` and
``dispatch_ref_drift_action``.

Stdlib-only: this runs under whichever interpreter ``_run_python_hook.sh``
resolves. Since nexus-4ti7e that is the installed generation's python when
one exists, but a ``uv tool install conexus`` deployment or a box with no
generation still gets a bare python that cannot import the ``conexus``
package, so the hook stays stdlib-only (same constraint as
``t2_prefix_scan.py`` / ``preflight.py``) -- the ref-drift functions below
duplicate ``nexus.plugin_lockstep``'s registry/marketplace readers rather
than importing them, the same trade this file already made for the
version check.
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

import json
import os
import subprocess
from pathlib import Path

DEBUG = os.environ.get("NX_HOOK_DEBUG", "0") == "1"

# Detached action script lives beside this hook; launched via the same
# interpreter-selection wrapper so it picks a >=3.12 python.
_SCRIPTS_DIR = Path(__file__).resolve().parent
_LAUNCHER = _SCRIPTS_DIR / "_run_python_hook.sh"
_ACTION = _SCRIPTS_DIR / "version_lockstep_action.py"

#: The plugins this wheel ships -- same set as
#: ``nexus.plugin_lockstep.PLUGINS``, duplicated per the stdlib-only
#: constraint above.
PLUGINS: tuple[str, ...] = ("conexus", "sn")
#: Same env-var names as ``nexus.plugin_lockstep`` (its ``REGISTRY_ENV`` /
#: ``MARKETPLACES_ENV``) so this hook's no-network probe and ``nx
#: upgrade``'s own network-refreshed confirm read the identical files, and
#: so the suite's one ``_isolate_plugin_registry`` autouse fixture
#: isolates both at once.
_REGISTRY_ENV = "NX_PLUGIN_REGISTRY"
_MARKETPLACES_ENV = "NX_PLUGIN_MARKETPLACES"
#: Bound per ``git rev-parse`` call -- plain local-clone plumbing, no
#: network, measured well under 50ms typical for a one-or-two-plugin
#: check. This is a defensive cap against a pathological hang, not the
#: expected cost; the whole hook still has to fit inside hooks.json's 5s
#: SessionStart timeout for this matcher.
_GIT_TIMEOUT_S = 2
#: The sentinel ``dispatch_ref_drift_action`` passes instead of a CLI
#: version -- ``version_lockstep_action.py`` carries the identical
#: literal under the same name;
#: ``tests/hooks/test_version_lockstep_hook.py::
#: TestRefDriftSentinelMatchesAction`` pins the two together so they
#: cannot drift apart silently.
_REF_DRIFT_SENTINEL = "__ref_drift__"


def debug(msg: str) -> None:
    """Print a debug line to stderr when NX_HOOK_DEBUG=1."""
    if DEBUG:
        print(f"[version-lockstep-hook] {msg}", file=sys.stderr)


def marker_path() -> Path:
    """Per-user marker recording the last CLI version confirmed in lockstep.

    Lives under ``~/.config/nexus/`` so it survives ``/plugin update``
    (CLAUDE_PLUGIN_ROOT is replaced wholesale on update). ``NX_LOCKSTEP_MARKER``
    overrides the location for tests.
    """
    override = os.environ.get("NX_LOCKSTEP_MARKER")
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus" / "cli_lockstep_marker"


def read_plugin_version() -> str | None:
    """Read the plugin version from ``${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json``.

    Returns None on any failure (missing env, missing file, malformed JSON,
    absent ``version`` key). The hook must never fail.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        debug("CLAUDE_PLUGIN_ROOT unset")
        return None
    try:
        data = json.loads((Path(root) / ".claude-plugin" / "plugin.json").read_text())
        version = data.get("version")
        return version if isinstance(version, str) and version else None
    except (OSError, ValueError) as exc:
        debug(f"could not read plugin.json: {exc}")
        return None


def read_marker() -> str | None:
    """Return the marker's recorded version, or None when absent/unreadable."""
    try:
        return marker_path().read_text().strip() or None
    except OSError:
        return None


def _wrap_context(msg: str) -> str:
    """Wrap *msg* as the SessionStart additionalContext JSON nudge (CA-1
    contract). The one place that builds the envelope, so every caller
    (version mismatch, ref drift, or both at once) emits the identical
    shape."""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": msg,
            }
        }
    )


def build_context(target_version: str) -> str:
    """Build the SessionStart additionalContext JSON nudge for a CLI
    version mismatch (CA-1 contract)."""
    msg = (
        f"The conexus CLI (nx) may be out of lockstep with plugin "
        f"v{target_version}. An extras-preserving upgrade has been dispatched "
        f"in the background; it takes effect on your next session. No action "
        f"needed now."
    )
    return _wrap_context(msg)


def dispatch_action(target_version: str) -> None:
    """Fire the detached upgrade action and return immediately (CA-4).

    Uses Popen with detached stdio so synchronous SessionStart is never
    blocked. We deliberately do not wait()/communicate().
    """
    cmd = ["bash", str(_LAUNCHER), str(_ACTION), target_version]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        debug(f"dispatched detached action for v{target_version}")
    except OSError as exc:
        debug(f"failed to dispatch action: {exc}")


# ---------------------------------------------------------------------------
# nexus-konsk: same-version plugin-only ref-drift detection (no network).
# ---------------------------------------------------------------------------


def _registry_path() -> Path:
    override = os.environ.get(_REGISTRY_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def _marketplaces_path() -> Path:
    override = os.environ.get(_MARKETPLACES_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / "plugins" / "known_marketplaces.json"


def _our_plugin_shas() -> dict[str, tuple[str, str]]:
    """``{"<plugin>@<marketplace>": (marketplace_name, git_commit_sha)}``
    for every registry entry naming a plugin this wheel ships that also
    carries a ``gitCommitSha``. Mirrors
    ``nexus.plugin_lockstep.registry_entries``'s v2/flat-shape tolerance,
    but inline (see module docstring). Any unreadable/malformed input is
    an empty dict, never an error -- the same "not a plugin box" posture
    the CLI-side reader uses.
    """
    try:
        data = json.loads(_registry_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    plugins = data.get("plugins") if isinstance(data.get("plugins"), dict) else data
    if not isinstance(plugins, dict):
        return {}
    found: dict[str, tuple[str, str]] = {}
    for key, entries in plugins.items():
        if not isinstance(key, str) or "@" not in key:
            continue
        plugin_short, marketplace_name = key.split("@", 1)
        if plugin_short not in PLUGINS:
            continue
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        sha: str | None = None
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("gitCommitSha"), str) and e["gitCommitSha"]:
                sha = e["gitCommitSha"]
        if sha:
            found[key] = (marketplace_name, sha)
    return found


def _marketplace_install_location(marketplace_name: str) -> Path | None:
    """The local clone directory for *marketplace_name*, per
    ``known_marketplaces.json``. ``None`` on anything unreadable or
    absent -- refuse, never guess (mirrors
    ``plugin_lockstep._marketplace_install_location``)."""
    try:
        data = json.loads(_marketplaces_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(marketplace_name)
    if not isinstance(entry, dict):
        return None
    loc = entry.get("installLocation")
    return Path(loc) if isinstance(loc, str) and loc else None


def _pinned_ref_for_plugin(plugin_short: str, install_location: Path) -> str | None:
    """The marketplace's currently pinned ``source.ref`` for
    *plugin_short*, read from the ALREADY-ON-DISK
    ``<install_location>/.claude-plugin/marketplace.json`` -- no fetch, so
    a stale clone reads its own stale pin (the "no network" contract: a
    false negative until something else refreshes the clone, never a
    false positive). ``None`` on any unreadable/unexpected shape."""
    try:
        data = json.loads((install_location / ".claude-plugin" / "marketplace.json").read_text())
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        return None
    for p in plugins:
        if isinstance(p, dict) and p.get("name") == plugin_short:
            source = p.get("source")
            ref = source.get("ref") if isinstance(source, dict) else None
            return ref if isinstance(ref, str) and ref else None
    return None


def _resolve_ref_sha(install_location: Path, ref: str) -> str | None:
    """The commit *ref* resolves to inside the local clone -- plain git
    plumbing, no network. ``None`` on any failure (unresolvable ref, git
    absent, timeout)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(install_location), "rev-parse", f"{ref}^{{commit}}"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        debug(f"ref resolve failed for {ref} in {install_location}: {exc}")
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def detect_ref_drift() -> list[tuple[str, str, str]]:
    """``[(plugin_id, was_sha, now_sha), ...]`` for every installed
    plugin whose registry ``gitCommitSha`` no longer matches what its
    marketplace's pinned ref resolves to, checked entirely with data
    ALREADY on disk (no ``claude plugin marketplace update`` fetch, no
    ``claude`` invocation at all) -- bounded to a handful of ``git
    rev-parse`` calls against local clones, comfortably inside the 5s
    SessionStart budget (hooks.json) and measured well under 200ms for
    the common one-or-two-plugin case.

    Refuse-not-guess (nexus-konsk): a plugin with no registry entry, no
    ``gitCommitSha``, no ``known_marketplaces.json`` entry, no local
    clone, or no resolvable ref is skipped silently for THAT plugin --
    exactly ``nexus.plugin_lockstep._check_ref_drift``'s own posture.
    Because this never refreshes the clone, it is a best-effort ADDITION
    on top of that definitive, network-refreshed check (run inside the
    ``nx upgrade`` this dispatches): a false negative here just means the
    next session (or a manual ``nx upgrade``) catches it instead -- never
    an incorrect action.
    """
    drift: list[tuple[str, str, str]] = []
    locations: dict[str, Path | None] = {}
    for plugin_id, (marketplace_name, sha) in sorted(_our_plugin_shas().items()):
        if marketplace_name not in locations:
            locations[marketplace_name] = _marketplace_install_location(marketplace_name)
        install_location = locations[marketplace_name]
        if install_location is None:
            continue
        plugin_short = plugin_id.split("@", 1)[0]
        ref = _pinned_ref_for_plugin(plugin_short, install_location)
        if ref is None:
            continue
        target_sha = _resolve_ref_sha(install_location, ref)
        if target_sha is None or target_sha == sha:
            continue
        drift.append((plugin_id, sha, target_sha))
    return drift


def build_ref_drift_context(drift: list[tuple[str, str, str]]) -> str:
    """SessionStart additionalContext nudge for the nexus-konsk ref-drift
    path -- one clear line naming which plugin(s) drifted and the sha
    move, so it reads distinctly from the ordinary version-lockstep
    nudge above."""
    names = ", ".join(f"{pid} ({was[:7]} -> {now[:7]})" for pid, was, now in drift)
    msg = (
        f"A plugin-only release moved {names} without changing its version "
        f"(RDR-197). A background `nx upgrade` reinstall has been "
        f"dispatched; it takes effect on your next session (or run /mcp). "
        f"No action needed now."
    )
    return _wrap_context(msg)


def _combined_context(target_version: str, drift: list[tuple[str, str, str]]) -> str:
    """The rare case where a CLI version mismatch AND a plugin-only ref
    drift are both detected in the same session. SessionStart's
    additionalContext is a single string and ``main`` prints exactly one
    JSON line, so both nudges fold into one payload here rather than two
    ``print`` calls (a second JSON line on its own would go unparsed by
    whatever reads this hook's stdout)."""
    names = ", ".join(f"{pid} ({was[:7]} -> {now[:7]})" for pid, was, now in drift)
    msg = (
        f"The conexus CLI (nx) may be out of lockstep with plugin "
        f"v{target_version}, and a plugin-only release moved {names} "
        f"without changing its version (RDR-197). Both upgrades have been "
        f"dispatched in the background; they take effect on your next "
        f"session. No action needed now."
    )
    return _wrap_context(msg)


def dispatch_ref_drift_action() -> None:
    """Fire the detached ref-drift reinstall and return immediately
    (CA-4, same detached-subprocess contract as ``dispatch_action``):
    reuses the SAME action script and machinery, with the sentinel
    target instead of a CLI version, since ref drift needs only the
    plugin-lockstep step inside ``nx upgrade`` -- never a binary upgrade
    (the wheel version is unchanged by construction, RDR-197's whole
    point).

    Blast radius (nexus-konsk critique [25316], SIGNIFICANT finding):
    the reinstall this triggers runs ``claude plugin uninstall`` THEN
    ``claude plugin install`` (``nexus.plugin_lockstep._check_ref_drift``)
    against a plugin whose hooks/MCP servers may be loaded in THIS live
    session. RDR-143's CA-4 already accepted the identical next-session
    shape for the binary-upgrade path: a detached action completes AFTER
    the current session has already started against the old tree, so the
    fix lands on the NEXT session, never the current one (RDR-143 CA-4,
    "the new CLI takes effect on the next session, not the current
    one"). This is that same accepted shape, not a new hazard class --
    the nudge above says so and ``render()``'s own "restart the session"
    line (``src/nexus/plugin_lockstep.py``) is the same instruction a
    manual ``nx upgrade`` already prints on a successful ref-move.
    """
    cmd = ["bash", str(_LAUNCHER), str(_ACTION), _REF_DRIFT_SENTINEL]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        debug("dispatched detached ref-drift reinstall")
    except OSError as exc:
        debug(f"failed to dispatch ref-drift action: {exc}")


def main() -> None:
    """Detect skew (version mismatch, and separately, nexus-konsk ref
    drift), nudge + dispatch on either. Always fail-safe."""
    try:
        plugin_version = read_plugin_version()
        version_mismatch = bool(plugin_version) and read_marker() != plugin_version
        if version_mismatch:
            dispatch_action(plugin_version)  # type: ignore[arg-type]
        else:
            debug("marker matches plugin version, or nothing to compare")

        # The two checks are independent: a version mismatch's dispatch
        # only sometimes runs `nx upgrade` (its own no-op fast path skips
        # it when the CLI already satisfies the target), so this hook
        # never assumes the version-mismatch path alone would have caught
        # a same-version ref drift on a DIFFERENT plugin.
        drift = detect_ref_drift()
        if drift:
            dispatch_ref_drift_action()

        if version_mismatch and drift:
            print(_combined_context(plugin_version, drift))  # type: ignore[arg-type]
        elif version_mismatch:
            print(build_context(plugin_version))  # type: ignore[arg-type]
        elif drift:
            print(build_ref_drift_context(drift))
    except Exception as exc:  # noqa: BLE001 - hook must never raise
        debug(f"swallowed unexpected error: {exc}")


if __name__ == "__main__":
    main()
    sys.exit(0)
