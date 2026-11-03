# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The version both MCP servers report in ``serverInfo`` (nexus-sgmyl).

mcp 1.x filled an unset server version with the SDK's own version, so a client
saw ``nexus 1.30.0``. mcp 2.x reports what the constructor is given, and the
default is an empty string. Both servers pass the installed ``conexus`` version,
which is what a client log should show for them.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def server_version() -> str:
    """The installed ``conexus`` version; ``""`` for a source tree with no dist-info."""
    try:
        return version("conexus")
    except PackageNotFoundError:
        return ""
