# SPDX-License-Identifier: AGPL-3.0-or-later
"""Both MCP servers report the installed conexus version (nexus-sgmyl, map item 7).

mcp 1.x filled an unset server version with the SDK's own version string (a
client log read ``nexus 1.30.0``). mcp 2.x reports whatever the constructor was
given and the default is ``""``, which a client shows as a blank version. Both
servers pass ``nexus.mcp._server_version.server_version()``.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import pytest

from nexus.mcp._server_version import server_version


def _installed() -> str:
    try:
        return version("conexus")
    except PackageNotFoundError:  # pragma: no cover - a source tree with no dist-info
        pytest.skip("conexus is not installed in this environment")


def test_server_version_is_the_installed_conexus_version() -> None:
    assert server_version() == _installed() != ""


@pytest.mark.parametrize("module", ["nexus.mcp.core", "nexus.mcp.catalog"])
def test_each_server_announces_it_at_initialize(module: str) -> None:
    """The value a client reads in ``serverInfo.version``: built from the server's
    own initialization options, the same object ``Server.run`` hands the runner."""
    import importlib

    installed = _installed()
    low = importlib.import_module(module).mcp._lowlevel_server
    assert low.version == installed
    assert low.create_initialization_options().server_version == installed


def test_a_source_tree_without_dist_info_reports_empty_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.mcp import _server_version

    def _missing(_name: str) -> str:
        raise PackageNotFoundError("conexus")

    monkeypatch.setattr(_server_version, "version", _missing)
    assert _server_version.server_version() == ""
