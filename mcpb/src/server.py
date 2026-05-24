#!/usr/bin/env python3
"""Nexus MCP server entry point for the Claude Desktop .mcpb extension.

Imports and runs ``nexus.mcp.core:main``, the same entry point the
Claude Code plugin invokes via the ``nx-mcp`` console script. Before
handing off, performs a best-effort stale-install check: if the
locally installed ``conexus`` package is older than the latest on
PyPI, emit a one-line warning to stderr naming the GitHub release URL
to re-download.

The check is intentionally non-fatal — network failures, timeouts,
or PyPI being unreachable do not block server startup. Opt out
entirely by setting ``NX_MCPB_SKIP_UPDATE_CHECK=1`` in the
environment.
"""
from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version

_RELEASE_URL = "https://github.com/Hellblazer/nexus/releases/latest"
_PYPI_URL = "https://pypi.org/pypi/conexus/json"
_TIMEOUT_SECONDS = 3.0


def _parse_version(v: str) -> tuple[int, ...]:
    """Cheap PEP 440 numeric prefix extract: '5.0.1.dev0' -> (5, 0, 1)."""
    parts: list[int] = []
    for chunk in v.split("."):
        digits = ""
        for c in chunk:
            if c.isdigit():
                digits += c
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _check_stale_install() -> None:
    """Best-effort: warn to stderr if conexus is behind PyPI's latest.

    Silent on network failure, timeout, missing package metadata, or
    when the installed version is current. Costs one ~3s HTTPS GET.
    """
    if os.environ.get("NX_MCPB_SKIP_UPDATE_CHECK"):
        return

    try:
        installed = _pkg_version("conexus")
    except PackageNotFoundError:
        return  # editable / unusual install layout; skip silently

    try:
        import json
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            _PYPI_URL,
            headers={"User-Agent": f"conexus-mcpb/{installed}"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            latest = json.loads(resp.read().decode("utf-8"))["info"]["version"]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError, TimeoutError):
        return  # offline / PyPI hiccup; skip silently

    if _parse_version(installed) >= _parse_version(latest):
        return  # current or ahead (dev install); no warning

    msg = (
        f"[conexus-mcpb] installed conexus={installed}, latest on PyPI={latest}. "
        f"Re-download the .mcpb from {_RELEASE_URL} and re-install in Claude "
        "Desktop to upgrade. (Set NX_MCPB_SKIP_UPDATE_CHECK=1 to silence.)"
    )
    print(msg, file=sys.stderr, flush=True)


def main() -> None:
    _check_stale_install()
    from nexus.mcp.core import main as _nx_mcp_main
    _nx_mcp_main()


if __name__ == "__main__":
    main()
