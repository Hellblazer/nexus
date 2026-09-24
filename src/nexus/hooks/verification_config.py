# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The `.nexus.yml` verification block, read IN THE WHEEL (bead nexus-b5ugt).

Port of ``conexus/hooks/scripts/read_verification_config.py``, which was
deleted with the rest of the unexecuted plugin-resident layer at
nexus-z9cz2.

**Why this had to move, and why moving it is the point.** The script was
reached by building a path off ``$CLAUDE_PLUGIN_ROOT``. An ``mcp_tool``
hook runs inside the ``nx-mcp`` server process, and that process does not
get a usable ``CLAUDE_PLUGIN_ROOT``: ``conexus/.mcp.json`` declares the
server env as ``{"CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}"}`` and
Claude Code does not expand ``${...}`` inside an MCP ``env`` block, so
the literal placeholder is what arrives. ``stop_verification`` therefore
read ``{}`` for its config, concluded ``on_stop`` was false, and never
verified anything — silently, for as long as it has been on that tier.

The tempting fix is to make the hook find the script more reliably. That
is the wrong direction: RDR-215 exists to ELIMINATE the plugin-resident
python/bash layer so a native Windows client becomes viable, and every
repair that keeps a subprocess keeps the thing being eliminated. A
function in the wheel needs no plugin root, no interpreter discovery and
no ``.exe`` question on Windows.

Behaviour is carried, not redesigned — including the git-common-dir
resolution added at bead nexus-634ye, which is the reason a worktree
finds the primary checkout's gitignored ``.nexus.yml`` at all.

**Diagnostics go through ``_io._emit``, never a module-level structlog
logger.** stdout is the hook decision channel, an unconfigured structlog
logger writes there by default, and one debug line is enough to corrupt
the envelope. Written the obvious way first and caught immediately: a
no-config run emitted ``verification_config_absent`` onto stdout ahead
of the JSON and the decision failed to parse. ``_emit``'s own docstring
names this as the nexus D9 defect class.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nexus._hook_runtime._io import _emit

__all__ = ["DEFAULTS", "detect_test_command", "find_project_dir", "read_verification_config"]

#: Every key the verification block may carry, with the value that
#: applies when it does not. Both gates default OFF: a verification gate
#: that armed itself on a repo that never asked for one would block
#: closes on every project that installs the plugin.
DEFAULTS: dict[str, Any] = {
    "on_stop": False,
    "on_close": False,
    "test_command": "",
    "lint_command": "",
    "test_timeout": 120,
}

#: Marker file -> the test command it implies. First match wins, so the
#: order is the precedence. Carried verbatim from the script.
DETECT_TABLE: tuple[tuple[str, str], ...] = (
    ("pom.xml", "mvn test"),
    ("build.gradle", "./gradlew test"),
    ("build.gradle.kts", "./gradlew test"),
    ("pyproject.toml", "uv run pytest"),
    ("package.json", "npm test"),
    ("Cargo.toml", "cargo test"),
    ("Makefile", "make test"),
    ("go.mod", "go test ./..."),
)


def _git_common_root(start: Path) -> Path | None:
    """The checkout that owns *start*'s git COMMON dir, or None.

    In an ordinary clone this is *start*'s own repo root. In a linked
    worktree it is the PRIMARY checkout, because every worktree of a repo
    shares one common dir.

    ``.nexus.yml`` is gitignored by design (``docs/configuration.md``:
    "It is gitignored by default") — a per-developer file, one per repo,
    so it exists in the primary and in no worktree. Resolving it from the
    cwd returned DEFAULTS in every worktree, which for the verification
    block means both gates off: the close gate silently disabled
    everywhere anyone actually works, from the day this project moved to
    one-session-one-worktree. Bead nexus-634ye.

    Keying per-repo state on the common dir rather than the checkout is
    what this codebase already does for the two other things that must be
    one-per-repo across worktrees — the engine build lease and the
    cached service jar both live in the git common dir for this reason.
    """
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: a hook process pays its import cost on every invocation, and a module-scope import of this pulls structlog + ~231 modules (measured on verification_config: 14ms/106 -> 62-84ms/337). Deferred, it is paid only when we actually spawn

    try:
        out = run_bounded(
            ["git", "-C", str(start), "rev-parse", "--git-common-dir"],
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001 — no git, not on PATH, timed out
        _emit("debug", "verification_config_git_lookup_failed", error=str(exc))
        return None
    if out.returncode != 0:
        return None
    common = Path(out.stdout.strip())
    if not common.is_absolute():
        common = (start / common).resolve()
    # <root>/.git -> <root>. A bare repo has no working tree to hold a
    # config file, so anything not ending in .git is unusable here.
    if common.name != ".git":
        return None
    return common.parent


def find_project_dir(cwd: Path | None = None) -> Path:
    """Where to read `.nexus.yml` from.

    In order: an explicit ``CLAUDE_PROJECT_DIR``, *cwd*, then the checkout
    that owns *cwd*'s git common dir. The first candidate that actually
    HAS a ``.nexus.yml`` wins, rather than the first that merely exists —
    otherwise a worktree, which always exists and never carries the file,
    shadows the primary that does.

    When no candidate has one, the first candidate is returned unchanged.
    That keeps the previous behaviour for the no-config case, which is
    also what :func:`detect_test_command` wants: marker files like
    ``pyproject.toml`` ARE in every worktree, and the detected test
    command should describe the tree being worked in.
    """
    here = cwd or Path.cwd()
    candidates: list[Path] = []
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            candidates.append(p)
    if here not in candidates:
        candidates.append(here)
    common_root = _git_common_root(here)
    if common_root is not None and common_root not in candidates:
        candidates.append(common_root)

    for candidate in candidates:
        if (candidate / ".nexus.yml").is_file():
            return candidate
    return candidates[0]


def detect_test_command(project_dir: Path) -> str:
    """The test command *project_dir*'s marker files imply, or ""."""
    for marker, command in DETECT_TABLE:
        if (project_dir / marker).exists():
            return command
    return ""


def read_verification_config(cwd: Path | None = None) -> dict[str, Any]:
    """The verification block, merged over :data:`DEFAULTS`.

    Never raises. Every failure — no file, unreadable YAML, a root that
    is not a mapping, a ``verification`` key that is not a mapping —
    yields DEFAULTS, matching the script's ``|| echo '{}'`` posture. A
    verification reader that threw would take down the hook that called
    it, and a hook must never take down the event it observes.
    """
    project_dir = find_project_dir(cwd)
    result = dict(DEFAULTS)
    nexus_yml = project_dir / ".nexus.yml"

    if not nexus_yml.is_file():
        _emit("debug", "verification_config_absent", path=str(nexus_yml))
        return _with_detected_command(result, project_dir)

    try:
        import yaml  # noqa: PLC0415 — deferred: only a real read pays the import

        with nexus_yml.open() as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:  # noqa: BLE001 — carried: absence is not an error
        _emit("debug", "verification_config_unparseable", path=str(nexus_yml), error=str(exc))
        return _with_detected_command(result, project_dir)

    if not isinstance(data, dict):
        return _with_detected_command(result, project_dir)
    verification = data.get("verification", {})
    if not isinstance(verification, dict):
        return _with_detected_command(result, project_dir)

    # Merge only known keys, so an unrecognised key in a user's file is
    # ignored rather than injected into a dict callers index blindly.
    for key in DEFAULTS:
        if key in verification:
            result[key] = verification[key]
    return _with_detected_command(result, project_dir)


def _with_detected_command(config: dict[str, Any], project_dir: Path) -> dict[str, Any]:
    """Fill ``test_command`` by detection, but only if a gate is armed.

    Carried from the script's ``main()``: detection runs only when
    ``test_command`` is unset AND at least one gate is on, so a repo that
    never asked for verification does not get a command it will never run.
    """
    if not config["test_command"] and (config["on_stop"] or config["on_close"]):
        config["test_command"] = detect_test_command(project_dir)
    return config
