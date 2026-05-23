#!/usr/bin/env python3
"""Read .nexus.yml verification config and output as JSON.

Standalone script for hook consumption — no nexus package imports.
"""
from __future__ import annotations

import sys
if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: nx plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
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


def _find_project_dir() -> Path:
    """Determine project directory from env or cwd."""
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            return p
    return Path.cwd()


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
