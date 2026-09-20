#!/usr/bin/env python3
"""Read .nexus.yml verification config and output as JSON.

Standalone script for hook consumption — no nexus package imports.
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
from pathlib import Path

DEBUG = os.environ.get("NX_HOOK_DEBUG", "0") == "1"

DEFAULTS = {
    "on_stop": False,
    "on_close": False,
    "test_command": "",
    "lint_command": "",
    "test_timeout": 120,
}

DETECT_TABLE = [
    ("pom.xml",            "mvn test"),
    ("build.gradle",       "./gradlew test"),
    ("build.gradle.kts",   "./gradlew test"),
    ("pyproject.toml",     "uv run pytest"),
    ("package.json",       "npm test"),
    ("Cargo.toml",         "cargo test"),
    ("Makefile",           "make test"),
    ("go.mod",             "go test ./..."),
]


def _debug(msg: str) -> None:
    if DEBUG:
        print(f"[read-verification-config] {msg}", file=sys.stderr)


def _git_common_root(start: Path) -> Path | None:
    """The checkout that owns *start*'s git COMMON dir, or None.

    In an ordinary clone this is *start*'s own repo root. In a linked
    worktree it is the PRIMARY checkout, because every worktree of a
    repo shares one common dir.

    `.nexus.yml` is gitignored by design (docs/configuration.md: "It is
    gitignored by default") — a per-developer file, one per repo, so it
    exists in the primary and in no worktree. Resolving it from the cwd
    therefore returned DEFAULTS in every worktree, and for the
    verification section that means on_stop and on_close both false: the
    close gate silently off, everywhere anyone actually works, from the
    moment this project moved to one-session-one-worktree. Bead
    nexus-634ye.

    Keying per-repo state on the common dir rather than the checkout is
    what this codebase already does for the two other things that have
    to be one-per-repo across worktrees — the engine build lease and the
    stamped-jar cache both live in the git common dir for exactly this
    reason. Config is the third.
    """
    try:
        import subprocess  # noqa: PLC0415 — stdlib, and only on this path

        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001 — no git, not on PATH, timed out
        _debug(f"git common-dir lookup failed: {exc}")
        return None
    if out.returncode != 0:
        _debug(f"not a git checkout: {start}")
        return None
    common = Path(out.stdout.strip())
    if not common.is_absolute():
        common = (start / common).resolve()
    # <root>/.git -> <root>. A bare repo has no working tree to hold a
    # config file, so anything not ending in .git is unusable here.
    if common.name != ".git":
        _debug(f"unusable common dir (bare or unexpected): {common}")
        return None
    return common.parent


def _find_project_dir() -> Path:
    """Where to read `.nexus.yml` from.

    In order: an explicit ``CLAUDE_PROJECT_DIR``, the cwd, then the
    checkout that owns the cwd's git common dir. The first candidate
    that actually HAS a `.nexus.yml` wins, rather than the first that
    merely exists — otherwise a worktree, which always exists and never
    carries the file, shadows the primary that does.

    When no candidate has one, the first candidate is returned
    unchanged. That preserves the previous behaviour for the no-config
    case, which is also what ``_detect_test_command`` wants: marker
    files like pyproject.toml ARE in every worktree, and the detected
    test command should describe the tree being worked in.
    """
    candidates: list[Path] = []
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            candidates.append(p)
    cwd = Path.cwd()
    if cwd not in candidates:
        candidates.append(cwd)
    common_root = _git_common_root(cwd)
    if common_root is not None and common_root not in candidates:
        candidates.append(common_root)

    for candidate in candidates:
        if (candidate / ".nexus.yml").is_file():
            _debug(f"config from {candidate}")
            return candidate
    _debug(f"no .nexus.yml in any of {[str(c) for c in candidates]}")
    return candidates[0]


def _detect_test_command(project_dir: Path) -> str:
    """Auto-detect test command from marker files. First match wins."""
    for marker, command in DETECT_TABLE:
        if (project_dir / marker).exists():
            _debug(f"detected {marker} → {command}")
            return command
    return ""


def _read_config(project_dir: Path) -> dict:
    """Read .nexus.yml and extract verification section, merging with defaults."""
    result = dict(DEFAULTS)
    nexus_yml = project_dir / ".nexus.yml"

    if not nexus_yml.exists():
        _debug(f"no .nexus.yml at {nexus_yml}")
        return result

    try:
        import yaml
        with nexus_yml.open() as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        _debug(f"failed to parse {nexus_yml}: {exc}")
        return result

    if not isinstance(data, dict):
        _debug("YAML root is not a dict")
        return result

    verification = data.get("verification", {})
    if not isinstance(verification, dict):
        _debug("verification section is not a dict")
        return result

    # Merge only known keys
    for key in DEFAULTS:
        if key in verification:
            result[key] = verification[key]

    return result


def main() -> None:
    project_dir = _find_project_dir()
    config = _read_config(project_dir)

    # Auto-detect test command if not explicitly set and at least one gate is enabled
    if not config["test_command"] and (config["on_stop"] or config["on_close"]):
        config["test_command"] = _detect_test_command(project_dir)

    print(json.dumps(config))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if DEBUG:
            print(f"[read-verification-config] unhandled: {exc}", file=sys.stderr)
        # Always output valid JSON even on error
        print(json.dumps(DEFAULTS))
    sys.exit(0)
