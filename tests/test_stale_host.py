# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-g6vb4 (GH #1414): MCP-host staleness self-detection — decorate + warn.

After an in-place ``uv tool upgrade conexus`` replaces site-packages under a
live nx-mcp process, a deferred import can read a NEW module off disk that
references names in an already-cached OLD module — an ImportError for a name
that demonstrably exists on disk. ``detect_stale_processes()`` cannot see the
suffering process by construction (``pid == me`` is excluded), so the host
must self-detect:

- ``self_staleness(baseline)`` compares the installed distribution's
  dist-info mtime/version against a baseline captured at process start.
- ``install_stale_host_hook`` wraps the CallToolRequest handler
  (same FastMCP-internals patch as the first-run banner hook): a cheap
  per-call check warn-logs once, and import-shaped errors — raised OR
  returned as FastMCP ``isError`` content — are decorated with an
  actionable restart message. The hook NEVER refuses a call.
"""
from __future__ import annotations

import pytest

from nexus import upgrade_finish
from nexus.mcp import _stale_host


# ── self_staleness ──────────────────────────────────────────────────────────


class TestSelfStaleness:
    def test_fresh_process_is_not_stale(self, monkeypatch):
        monkeypatch.setattr(
            upgrade_finish, "install_mtime_and_version", lambda: (1000.0, "6.14.0")
        )
        st = upgrade_finish.self_staleness((1000.0, "6.14.0"))
        assert st.stale is False
        assert st.started_version == "6.14.0"
        assert st.installed_version == "6.14.0"

    def test_newer_mtime_means_stale(self, monkeypatch):
        monkeypatch.setattr(
            upgrade_finish, "install_mtime_and_version", lambda: (2000.0, "6.14.0")
        )
        st = upgrade_finish.self_staleness((1000.0, "6.14.0"))
        assert st.stale is True

    def test_version_change_means_stale(self, monkeypatch):
        monkeypatch.setattr(
            upgrade_finish, "install_mtime_and_version", lambda: (1000.0, "6.15.0")
        )
        st = upgrade_finish.self_staleness((1000.0, "6.14.0"))
        assert st.stale is True
        assert st.started_version == "6.14.0"
        assert st.installed_version == "6.15.0"

    def test_unresolvable_install_reads_stale_not_crash(self, monkeypatch):
        # The venv was replaced/removed under us: metadata resolution failing
        # IS a disk-changed signal, never an exception out of the hot path.
        def _boom():
            raise RuntimeError("cannot locate conexus dist-info")

        monkeypatch.setattr(upgrade_finish, "install_mtime_and_version", _boom)
        st = upgrade_finish.self_staleness((1000.0, "6.14.0"))
        assert st.stale is True
        assert st.installed_version == "(unresolvable)"


# ── the dispatch hook, real FastMCP path ────────────────────────────────────


def _mk_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test-stale")

    @server.tool(description="echo")
    def echo(value: str) -> str:
        return value

    @server.tool(description="boom")
    def boom(value: str) -> str:
        raise ImportError(
            "cannot import name 'DEFAULT_LEASE_WAIT_BUDGET_S' "
            "from 'nexus.db.service_endpoint'"
        )

    @server.tool(description="plain failure")
    def plain(value: str) -> str:
        raise ValueError("ordinary tool failure")

    return server


def _call(server, tool: str):
    import anyio
    from mcp import types

    low = server._mcp_server
    handler = low.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=tool, arguments={"value": "hi"}),
    )
    return anyio.run(handler, req)


def _patch_install(monkeypatch, tmp_path, clock):
    """Point the hook at a fake install whose state is driven by ``clock``.

    The baseline dist-info path never exists, so the per-call quick-stat
    always falls through to the full ``self_staleness`` resolution — which
    reads ``clock`` — letting tests flip the installed state mid-process.
    """
    missing = tmp_path / "conexus-fake.dist-info"  # never created
    monkeypatch.setattr(
        upgrade_finish, "install_dist_info",
        lambda: (clock["v"][0], clock["v"][1], missing),
    )
    monkeypatch.setattr(
        upgrade_finish, "install_mtime_and_version", lambda: clock["v"]
    )


class TestStaleHostHook:
    def test_install_returns_true_on_real_fastmcp(self, monkeypatch, tmp_path):
        _patch_install(monkeypatch, tmp_path, {"v": (1000.0, "6.14.0")})
        assert _stale_host.install_stale_host_hook(_mk_server()) is True

    def test_install_survives_missing_dist_info(self, monkeypatch):
        def _boom():
            raise RuntimeError("no dist-info (source checkout)")

        monkeypatch.setattr(upgrade_finish, "install_dist_info", _boom)
        assert _stale_host.install_stale_host_hook(_mk_server()) is False

    def test_fresh_host_passthrough(self, monkeypatch, tmp_path):
        _patch_install(monkeypatch, tmp_path, {"v": (1000.0, "6.14.0")})
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        result = _call(server, "echo")
        inner = result.root
        assert not inner.isError
        assert "hi" in inner.content[0].text
        assert "stale" not in inner.content[0].text

    def test_fresh_host_fast_path_skips_full_resolution(
        self, monkeypatch, tmp_path
    ):
        # critic MEDIUM-2: the common never-upgraded case must cost one
        # stat, not an importlib.metadata resolution per call. A REAL
        # dist-info path with an unchanged mtime short-circuits; a full
        # self_staleness resolution would blow the tripwire below.
        dist_info = tmp_path / "conexus-6.14.0.dist-info"
        dist_info.mkdir()
        mtime = dist_info.stat().st_mtime
        monkeypatch.setattr(
            upgrade_finish, "install_dist_info",
            lambda: (mtime, "6.14.0", dist_info),
        )

        def _tripwire(baseline):
            raise AssertionError("full resolution on the fast path")

        monkeypatch.setattr(upgrade_finish, "self_staleness", _tripwire)
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        result = _call(server, "echo")
        assert not result.root.isError
        assert "hi" in result.root.content[0].text

    def test_stale_host_decorates_import_error_result(self, monkeypatch, tmp_path):
        clock = {"v": (1000.0, "6.14.0")}
        _patch_install(monkeypatch, tmp_path, clock)
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        # The upgrade happens AFTER the host started.
        clock["v"] = (2000.0, "6.15.0")
        result = _call(server, "boom")
        inner = result.root
        assert inner.isError
        text = inner.content[0].text
        assert "cannot import name" in text  # original error preserved
        assert "stale MCP host" in text
        assert "6.14.0" in text  # started-under version
        assert "6.15.0" in text  # now-installed version
        assert "restart" in text.lower()

    def test_decoration_names_the_current_version_after_second_upgrade(
        self, monkeypatch, tmp_path
    ):
        # critic LOW-1: the note re-resolves at decoration time, so a second
        # in-place upgrade in the same long-lived process is named, not the
        # first-detected version frozen in the cache.
        clock = {"v": (1000.0, "6.14.0")}
        _patch_install(monkeypatch, tmp_path, clock)
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        clock["v"] = (2000.0, "6.15.0")
        _call(server, "echo")  # staleness detected and cached at 6.15.0
        clock["v"] = (3000.0, "6.16.0")
        result = _call(server, "boom")
        assert "6.16.0" in result.root.content[0].text

    def test_stale_host_leaves_non_import_errors_alone(self, monkeypatch, tmp_path):
        clock = {"v": (1000.0, "6.14.0")}
        _patch_install(monkeypatch, tmp_path, clock)
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        clock["v"] = (2000.0, "6.15.0")
        result = _call(server, "plain")
        inner = result.root
        assert inner.isError
        assert "stale MCP host" not in inner.content[0].text

    def test_fresh_host_never_decorates_import_errors(self, monkeypatch, tmp_path):
        # A genuine import bug on a fresh host must surface undecorated —
        # the decoration must never mask a real defect as staleness.
        _patch_install(monkeypatch, tmp_path, {"v": (1000.0, "6.14.0")})
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        result = _call(server, "boom")
        inner = result.root
        assert inner.isError
        assert "stale MCP host" not in inner.content[0].text

    def test_stale_host_still_serves_and_warns_once(self, monkeypatch, tmp_path):
        # Decorate + warn, never refuse: successful calls keep working when
        # stale, and the staleness warning logs exactly once.
        clock = {"v": (1000.0, "6.14.0")}
        _patch_install(monkeypatch, tmp_path, clock)
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        clock["v"] = (2000.0, "6.15.0")

        warned: list[dict] = []
        monkeypatch.setattr(
            _stale_host, "_warn", lambda **kw: warned.append(kw)
        )
        r1 = _call(server, "echo")
        assert not r1.root.isError
        assert "hi" in r1.root.content[0].text
        r2 = _call(server, "echo")
        assert not r2.root.isError
        assert len(warned) == 1
        assert warned[0]["started_version"] == "6.14.0"
        assert warned[0]["installed_version"] == "6.15.0"


# ── the generation layout: exact signal, informational note ─────────────────
#
# nexus-utpuw.12. Under side-by-side generations the mtime premise above DIES
# -- the running generation's files are never replaced -- so the detector said
# "fresh" forever on every migrated box. The replacement is exact
# (``sys.prefix != readlink(current)``), and it carries a DIFFERENT verdict:
# a skewed holder is CONSISTENT, its tree intact, converging at its next
# spawn. So the GH #1414 decoration is scoped to the layout where in-place
# replacement can still really happen (the legacy uv tree -- an un-migrated
# box, or a stray ``uv tool upgrade`` rebuilding it under a live holder), and
# is SUPPRESSED under a generation, where "this is almost certainly upgrade
# skew, not a code defect" would be active misinformation about a real bug.


import sys as _sys
from pathlib import Path as _Path

from nexus import install_layout


def _gen(tools: _Path, stamp: str) -> _Path:
    g = tools / f"gen-{stamp}"
    (g / "bin").mkdir(parents=True)
    (g / "nexus-install.json").write_text("{}")
    return g


@pytest.fixture
def generation_host(tmp_path, monkeypatch):
    """This process IS ``old``; ``current`` starts as ``old`` too (fresh)."""
    tools = tmp_path / "tools"
    tools.mkdir()
    old = _gen(tools, "20260826T010000Z")
    new = _gen(tools, "20260826T020000Z")
    (tools / "current").symlink_to(old)
    monkeypatch.setenv(install_layout.TOOLS_DIR_ENV, str(tools))
    monkeypatch.setattr(_sys, "prefix", str(old))

    def flip() -> None:
        link = tools / "current"
        tmp = tools / ".current.tmp"
        tmp.symlink_to(new)
        tmp.replace(link)

    return {"tools": tools, "old": old, "new": new, "flip": flip}


class TestStaleHostUnderGenerations:
    def test_installs_without_dist_info(self, generation_host, monkeypatch):
        """The exact rule needs sys.prefix, not dist-info. Gating the
        generation branch behind the OLD baseline would leave the reframe
        half-disabled on exactly the layout it was written for."""
        def _boom():
            raise RuntimeError("no dist-info")

        monkeypatch.setattr(upgrade_finish, "install_dist_info", _boom)
        assert _stale_host.install_stale_host_hook(_mk_server()) is True

    def test_a_matching_generation_is_silent(self, generation_host, monkeypatch):
        noted: list[dict] = []
        monkeypatch.setattr(
            _stale_host, "_note_skew", lambda **kw: noted.append(kw)
        )
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        result = _call(server, "echo")
        assert not result.root.isError
        assert noted == []

    def test_skew_notes_once_and_names_both_generations(
        self, generation_host, monkeypatch
    ):
        noted: list[dict] = []
        monkeypatch.setattr(
            _stale_host, "_note_skew", lambda **kw: noted.append(kw)
        )
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        generation_host["flip"]()
        r1 = _call(server, "echo")
        r2 = _call(server, "echo")
        # never refuses: both calls still serve
        assert "hi" in r1.root.content[0].text
        assert "hi" in r2.root.content[0].text
        assert len(noted) == 1
        assert noted[0]["running"] == str(generation_host["old"])
        assert noted[0]["current"] == str(generation_host["new"])

    def test_a_skewed_generation_host_does_not_decorate_an_import_error(
        self, generation_host, monkeypatch
    ):
        """THE falsifier for the scoping decision. Under generations the
        running tree is intact, so an ImportError is an ordinary defect.
        Decorating it as upgrade skew would send someone chasing a restart
        for a real bug.

        The ``noted`` assertion is load-bearing, not colour: without it this
        test passes on a host that simply read FRESH, which is the state the
        old mtime detector reports forever under generations -- it would go
        green with the scoping deleted. Asserting the skew WAS detected is
        what makes the absent decoration mean something."""
        noted: list[dict] = []
        monkeypatch.setattr(
            _stale_host, "_note_skew", lambda **kw: noted.append(kw)
        )
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        generation_host["flip"]()
        result = _call(server, "boom")
        inner = result.root
        assert inner.isError
        assert len(noted) == 1, "skew must be DETECTED for this test to mean anything"
        assert "cannot import name" in inner.content[0].text
        assert "stale MCP host" not in inner.content[0].text
        assert "restart" not in inner.content[0].text.lower()

    def test_an_absent_current_pointer_does_not_break_a_call(
        self, generation_host, monkeypatch
    ):
        server = _mk_server()
        assert _stale_host.install_stale_host_hook(server) is True
        (generation_host["tools"] / "current").unlink()
        noted: list[dict] = []
        monkeypatch.setattr(
            _stale_host, "_note_skew", lambda **kw: noted.append(kw)
        )
        result = _call(server, "echo")
        assert not result.root.isError
        assert "hi" in result.root.content[0].text
        assert noted == []
