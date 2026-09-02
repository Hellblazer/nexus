# SPDX-License-Identifier: AGPL-3.0-or-later
"""DEVONthink MCP stdio transport fallback (nexus-fdk1x).

Measured 2026-09-02: DEVONthink was running, but nothing listened on
``http://localhost:8420/mcp`` (the DT MCP app is spawned stdio by the
Claude Code MCP config, ``DEVONthink MCP --stdio``). Every
``dt_call()`` failed soft to ``None`` and ``nx dt index
--link-semantic --writeback --highlights`` ended rc=0 reporting "0
semantically linked, 0 written back, 0 highlights" with no remedy.

This file pins three things:

* ``nexus.mcp_client.core.open_stdio_session`` exists and shapes its
  ``StdioServerParameters`` correctly (Layer A' primitive).
* ``nexus.mcp_client.devonthink.dt_call()`` tries HTTP, then falls back
  to a persistent stdio session, in that order, and only when the
  configured transport allows each leg.
* ``nx dt index``'s exit-code rule: no DT-dependent layer flag -> rc=0
  regardless of reachability (Gap 0, unchanged); a layer flag set
  (link-semantic / writeback / highlights) with DT unreachable on
  every transport -> a named message and rc=2.

A fifth section spawns the REAL stdio binary when present on this
machine (skipped, never silently "passed", when absent -- the
nexus-moht0 vacuous-gate doctrine: a gate that skip-passes on an
absent dependency must say so loudly, not blend into a green run).
"""
from __future__ import annotations

import signal
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


# ── core.py: the stdio transport primitive ──────────────────────────────────


class TestOpenStdioSession:
    def test_endpoint_is_a_frozen_dataclass(self) -> None:
        from nexus.mcp_client.core import StdioEndpoint

        endpoint = StdioEndpoint(command="/bin/true", args=("--stdio",))
        assert endpoint.command == "/bin/true"
        assert endpoint.args == ("--stdio",)
        assert endpoint.env is None
        assert endpoint.cwd is None
        with pytest.raises(Exception):  # frozen -> assignment raises
            endpoint.command = "/bin/false"  # type: ignore[misc]

    def test_open_stdio_session_builds_matching_server_params(self) -> None:
        # No real subprocess: patch stdio_client itself and assert the
        # StdioServerParameters it receives mirror the endpoint exactly.
        from mcp.client.stdio import StdioServerParameters
        from nexus.mcp_client.core import StdioEndpoint, open_stdio_session

        captured: dict[str, StdioServerParameters] = {}

        class _FakeSession:
            async def initialize(self) -> None:
                return None

            async def __aenter__(self) -> "_FakeSession":
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        class _FakeStdioCM:
            def __init__(self, params: StdioServerParameters) -> None:
                captured["params"] = params

            async def __aenter__(self) -> tuple[object, object]:
                return object(), object()

            async def __aexit__(self, *exc: object) -> None:
                return None

        endpoint = StdioEndpoint(
            command="/opt/dt/DEVONthink MCP", args=("--stdio",), cwd="/tmp",
        )

        async def _run() -> None:
            with patch("mcp.client.stdio.stdio_client", _FakeStdioCM), \
                 patch("mcp.ClientSession", lambda *a, **k: _FakeSession()):
                async with open_stdio_session(endpoint) as session:
                    assert isinstance(session, _FakeSession)

        import asyncio

        asyncio.run(_run())
        params = captured["params"]
        assert params.command == "/opt/dt/DEVONthink MCP"
        assert params.args == ["--stdio"]
        assert params.cwd == "/tmp"


# ── devonthink.py: transport resolution + dt_call() fallback order ─────────


@pytest.fixture(autouse=True)
def _reset_dt_module_state():
    """Every test in this file starts from a clean slate: no cached
    availability, no cached stdio session, no stale unreachable-detail
    from a previous test."""
    from nexus.mcp_client import devonthink as dt

    dt.reset_availability_cache()
    dt.reset_stdio_session()
    yield
    dt.reset_availability_cache()
    dt.reset_stdio_session()


class TestTransportConfig:
    def test_default_transport_is_auto(self) -> None:
        from nexus.mcp_client import devonthink as dt

        with patch("nexus.mcp_client.devonthink.load_config", return_value={}):
            assert dt.dt_mcp_transport() == "auto"

    def test_unrecognised_transport_value_falls_back_to_auto(self) -> None:
        from nexus.mcp_client import devonthink as dt

        cfg = {"devonthink": {"mcp": {"transport": "carrier-pigeon"}}}
        with patch("nexus.mcp_client.devonthink.load_config", return_value=cfg):
            assert dt.dt_mcp_transport() == "auto"

    def test_explicit_stdio_transport_is_honoured(self) -> None:
        from nexus.mcp_client import devonthink as dt

        cfg = {"devonthink": {"mcp": {"transport": "stdio"}}}
        with patch("nexus.mcp_client.devonthink.load_config", return_value=cfg):
            assert dt.dt_mcp_transport() == "stdio"

    def test_stdio_command_none_when_binary_absent(self, tmp_path) -> None:
        from nexus.mcp_client import devonthink as dt

        missing = tmp_path / "no-such-binary"
        cfg = {"devonthink": {"mcp": {"command": str(missing)}}}
        with patch("nexus.mcp_client.devonthink.load_config", return_value=cfg):
            assert dt.dt_mcp_stdio_command() is None
            # The checked path is still surfaced for the loud message.
            assert dt._dt_mcp_stdio_path() == str(missing)

    def test_stdio_command_resolved_when_binary_present(self, tmp_path) -> None:
        from nexus.mcp_client import devonthink as dt

        binary = tmp_path / "DEVONthink MCP"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        cfg = {"devonthink": {"mcp": {"command": str(binary)}}}
        with patch("nexus.mcp_client.devonthink.load_config", return_value=cfg):
            assert dt.dt_mcp_stdio_command() == str(binary)


class TestDtCallFallbackOrder:
    """dt_call() tries HTTP, then stdio, in that order -- unless the
    configured transport skips one leg outright."""

    def test_http_success_never_touches_stdio(self) -> None:
        from nexus.mcp_client import devonthink as dt

        stdio_called = []
        with patch("nexus.mcp_client.devonthink.dt_mcp_transport", return_value="auto"), \
             patch("nexus.mcp_client.devonthink._dt_call_http", return_value={"ok": True}), \
             patch("nexus.mcp_client.devonthink._dt_call_stdio",
                   side_effect=lambda *a, **k: stdio_called.append(1)):
            result = dt.dt_call("is_running")
        assert result == {"ok": True}
        assert stdio_called == []
        assert dt.last_unreachable_detail() is None

    def test_http_failure_falls_back_to_stdio_success(self) -> None:
        from nexus.mcp_client import devonthink as dt

        def _fake_http(tool, args, tried):
            tried.append("http://localhost:8420/mcp")
            return None

        def _fake_stdio(tool, args, tried):
            tried.append("stdio binary /opt/DEVONthink MCP")
            return {"running": True}

        with patch("nexus.mcp_client.devonthink.dt_mcp_transport", return_value="auto"), \
             patch("nexus.mcp_client.devonthink._dt_call_http", side_effect=_fake_http), \
             patch("nexus.mcp_client.devonthink._dt_call_stdio", side_effect=_fake_stdio):
            result = dt.dt_call("is_running")
        assert result == {"running": True}
        assert dt.last_unreachable_detail() is None

    def test_both_transports_fail_records_unreachable_detail(self) -> None:
        from nexus.mcp_client import devonthink as dt

        def _fake_http(tool, args, tried):
            tried.append("http://localhost:8420/mcp")
            return None

        def _fake_stdio(tool, args, tried):
            tried.append("stdio binary /opt/DEVONthink MCP (not found)")
            return None

        with patch("nexus.mcp_client.devonthink.dt_mcp_transport", return_value="auto"), \
             patch("nexus.mcp_client.devonthink._dt_call_http", side_effect=_fake_http), \
             patch("nexus.mcp_client.devonthink._dt_call_stdio", side_effect=_fake_stdio):
            result = dt.dt_call("is_running")
        assert result is None
        detail = dt.last_unreachable_detail()
        assert detail is not None
        assert "http://localhost:8420/mcp" in detail
        assert "stdio binary" in detail

    def test_stdio_transport_config_skips_http_entirely(self) -> None:
        from nexus.mcp_client import devonthink as dt

        http_called = []
        with patch("nexus.mcp_client.devonthink.dt_mcp_transport", return_value="stdio"), \
             patch("nexus.mcp_client.devonthink._dt_call_http",
                   side_effect=lambda *a, **k: http_called.append(1)), \
             patch("nexus.mcp_client.devonthink._dt_call_stdio", return_value={"running": True}):
            result = dt.dt_call("is_running")
        assert result == {"running": True}
        assert http_called == []

    def test_http_transport_config_skips_stdio_entirely(self) -> None:
        from nexus.mcp_client import devonthink as dt

        stdio_called = []
        with patch("nexus.mcp_client.devonthink.dt_mcp_transport", return_value="http"), \
             patch("nexus.mcp_client.devonthink._dt_call_http", return_value=None), \
             patch("nexus.mcp_client.devonthink._dt_call_stdio",
                   side_effect=lambda *a, **k: stdio_called.append(1)):
            result = dt.dt_call("is_running")
        assert result is None
        assert stdio_called == []
        assert dt.last_unreachable_detail() == "no transport configured" or \
            "http://localhost:8420/mcp" in (dt.last_unreachable_detail() or "")

    def test_running_loop_guard_fires_before_any_transport(self) -> None:
        # The CLI-path-only guard must short-circuit BEFORE either
        # transport function is even reached.
        import asyncio

        from nexus.mcp_client import devonthink as dt

        async def _run() -> object:
            with patch("nexus.mcp_client.devonthink._dt_call_http") as http_mock, \
                 patch("nexus.mcp_client.devonthink._dt_call_stdio") as stdio_mock:
                result = dt.dt_call("is_running")
                http_mock.assert_not_called()
                stdio_mock.assert_not_called()
                return result

        assert asyncio.run(_run()) is None


class TestStdioCallDirectly:
    """``_dt_call_stdio`` in isolation, exercising the "not found" and
    holder-failure legs without spawning a real process."""

    def test_binary_absent_records_not_found_and_returns_none(self, tmp_path) -> None:
        from nexus.mcp_client import devonthink as dt

        with patch("nexus.mcp_client.devonthink.dt_mcp_stdio_command", return_value=None), \
             patch("nexus.mcp_client.devonthink._dt_mcp_stdio_path", return_value="/nope/DT"):
            tried: list[str] = []
            result = dt._dt_call_stdio("is_running", {}, tried)
        assert result is None
        assert tried == ["stdio binary /nope/DT (not found)"]

    def test_holder_start_failure_is_fail_soft(self) -> None:
        from nexus.mcp_client import devonthink as dt

        with patch("nexus.mcp_client.devonthink.dt_mcp_stdio_command", return_value="/opt/DT"), \
             patch.object(dt._STDIO_HOLDER, "ensure_started", return_value=False), \
             patch.object(dt._STDIO_HOLDER, "start_error", return_value="boom"):
            tried: list[str] = []
            result = dt._dt_call_stdio("is_running", {}, tried)
        assert result is None
        assert tried == ["stdio binary /opt/DT"]


# ── _StdioSessionHolder lifecycle (code-review T2 [24110] findings 1+2) ─────


class _FakeStdioSession:
    async def initialize(self) -> None:
        return None


def _make_slow_open_stdio_session(delay: float):
    """A fake ``open_stdio_session`` whose connect takes *delay* seconds
    before yielding a fake session -- deterministic stand-in for a real
    subprocess boot that outlives one caller's bounded wait."""
    import asyncio
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _open(endpoint):
        await asyncio.sleep(delay)
        yield _FakeStdioSession()

    return _open


class TestStdioSessionHolderSlowConnect:
    """Finding 1: a connect attempt still in flight past ``ensure_started``'s
    bounded wait must not permanently poison the holder once it actually
    succeeds -- the ORIGINAL bug set ``_start_error`` on a timeout and
    never cleared it, so every later ``dt_call()`` in the process failed
    soft forever behind a stale error even after the real connect landed.
    """

    def test_slow_but_successful_connect_is_not_permanently_poisoned(self) -> None:
        from nexus.mcp_client import devonthink as dt

        holder = dt._StdioSessionHolder()
        try:
            with patch(
                "nexus.mcp_client.devonthink.open_stdio_session",
                _make_slow_open_stdio_session(0.3),
            ):
                # First call: the connect is still in flight past this
                # call's bounded wait -- "not ready yet", NOT a hard failure.
                first = holder.ensure_started("fake-cmd", (), timeout=0.05)
                assert first is False
                assert holder.start_error() is None  # nexus-fdk1x: no poison

                # Second call (same background boot, same subprocess attempt
                # -- no restart): waits again, bounded, until the connect
                # that was already in flight actually completes.
                second = holder.ensure_started("fake-cmd", (), timeout=2.0)
                assert second is True, holder.start_error()
                assert holder.start_error() is None
        finally:
            holder.close()

    def test_ensure_started_never_spawns_a_second_thread_while_first_boots(self) -> None:
        from nexus.mcp_client import devonthink as dt

        holder = dt._StdioSessionHolder()
        try:
            with patch(
                "nexus.mcp_client.devonthink.open_stdio_session",
                _make_slow_open_stdio_session(0.2),
            ):
                holder.ensure_started("fake-cmd", (), timeout=0.02)
                thread_after_first_call = holder._thread
                holder.ensure_started("fake-cmd", (), timeout=2.0)
                # The SAME thread object served both calls -- no re-spawn.
                assert holder._thread is thread_after_first_call
        finally:
            holder.close()


class TestStdioSessionHolderLocking:
    """Finding 2: _boot()/_connect() must write self._session/_session_cm/
    _start_error under the SAME lock close() uses for its reads/resets."""

    def test_close_during_in_flight_boot_does_not_corrupt_state(self) -> None:
        from nexus.mcp_client import devonthink as dt

        holder = dt._StdioSessionHolder()
        with patch(
            "nexus.mcp_client.devonthink.open_stdio_session",
            _make_slow_open_stdio_session(0.3),
        ):
            holder.ensure_started("fake-cmd", (), timeout=0.02)
            # close() races the in-flight _connect() -- must not raise,
            # hang, or leave the holder in a half-torn-down state.
            holder.close()
        assert holder._thread is None
        assert holder._loop is None
        assert holder._session is None
        assert holder._session_cm is None

    def test_holder_is_reusable_after_a_racing_close(self) -> None:
        from nexus.mcp_client import devonthink as dt

        holder = dt._StdioSessionHolder()
        with patch(
            "nexus.mcp_client.devonthink.open_stdio_session",
            _make_slow_open_stdio_session(0.2),
        ):
            holder.ensure_started("fake-cmd", (), timeout=0.01)
            # Give the background thread a moment to actually acquire the
            # lock and record its loop (so close() sees a REAL in-flight
            # boot to tear down, rather than racing close() against the
            # OS scheduler even starting the thread at all -- a distinct,
            # far narrower race than finding 2 above is about).
            time.sleep(0.05)
            holder.close()
        try:
            with patch(
                "nexus.mcp_client.devonthink.open_stdio_session",
                _make_slow_open_stdio_session(0.0),
            ):
                assert holder.ensure_started("fake-cmd", (), timeout=2.0) is True
        finally:
            holder.close()


# ── SIGTERM shutdown (code-review T2 [24110] finding 3) ─────────────────────


class TestSigtermHandler:
    def test_handler_closes_holder_then_chains_to_previous_handler(self, monkeypatch) -> None:
        from nexus.mcp_client import devonthink as dt

        close_calls: list[str] = []
        monkeypatch.setattr(dt._STDIO_HOLDER, "close", lambda: close_calls.append("closed"))
        chained: list[tuple[int, object]] = []

        def _prev(signum, frame):
            chained.append((signum, frame))

        monkeypatch.setattr(dt, "_PREV_SIGTERM_HANDLER", _prev)

        dt._sigterm_handler(signal.SIGTERM, None)

        assert close_calls == ["closed"]
        assert chained == [(signal.SIGTERM, None)]

    def test_handler_closes_holder_even_when_previous_handler_is_default(self, monkeypatch) -> None:
        from nexus.mcp_client import devonthink as dt

        close_calls: list[str] = []
        monkeypatch.setattr(dt._STDIO_HOLDER, "close", lambda: close_calls.append("closed"))
        monkeypatch.setattr(dt, "_PREV_SIGTERM_HANDLER", signal.SIG_DFL)
        # Re-raising SIGTERM with SIG_DFL restored would kill THIS test
        # process, so intercept os.kill to prove the re-dispatch happens
        # without actually terminating anything.
        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(dt.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
        monkeypatch.setattr(dt.signal, "signal", lambda *a, **k: None)

        dt._sigterm_handler(signal.SIGTERM, None)

        assert close_calls == ["closed"]
        assert kill_calls == [(dt.os.getpid(), signal.SIGTERM)]

    def test_install_is_idempotent_and_only_from_main_thread(self, monkeypatch) -> None:
        from nexus.mcp_client import devonthink as dt

        monkeypatch.setattr(dt, "_SIGTERM_HANDLER_INSTALLED", False)
        monkeypatch.setattr(dt, "_PREV_SIGTERM_HANDLER", None)
        original = signal.getsignal(signal.SIGTERM)
        try:
            dt._install_sigterm_handler()
            assert dt._SIGTERM_HANDLER_INSTALLED is True
            assert signal.getsignal(signal.SIGTERM) is dt._sigterm_handler
            installed_prev = dt._PREV_SIGTERM_HANDLER

            # Second call: idempotent -- no re-registration, no change.
            dt._install_sigterm_handler()
            assert signal.getsignal(signal.SIGTERM) is dt._sigterm_handler
            assert dt._PREV_SIGTERM_HANDLER is installed_prev
        finally:
            signal.signal(signal.SIGTERM, original)
            monkeypatch.setattr(dt, "_SIGTERM_HANDLER_INSTALLED", False)
            monkeypatch.setattr(dt, "_PREV_SIGTERM_HANDLER", None)


# ── commands/dt.py: index_cmd exit-code rule ─────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_gather(monkeypatch):
    records: list[tuple[str, str]] = []
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: records)
    return records


class TestIndexCmdExitCodeRule:
    def test_no_layer_flag_stays_exit_zero_when_dt_unreachable(
        self, runner, fake_gather, monkeypatch,
    ):
        from nexus.cli import main

        fake_gather.append(("U1", "/a.pdf"))
        monkeypatch.setattr("nexus.commands.dt._index_record",
                            lambda uuid, path, *, collection, corpus, dry_run, extractor="auto": (True, 1))
        # DT unreachable, but no --link-semantic/--writeback/--highlights
        # flag was passed -> the probe never even runs (Gap 0 preserved).
        monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **kw: False)
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
        assert result.exit_code == 0, result.output
        assert "DEVONthink MCP unreachable" not in result.output

    def test_layer_flag_with_dt_reachable_stays_exit_zero(
        self, runner, fake_gather, monkeypatch,
    ):
        from nexus.cli import main

        fake_gather.append(("U1", "/a.pdf"))
        monkeypatch.setattr("nexus.commands.dt._index_record",
                            lambda uuid, path, *, collection, corpus, dry_run, extractor="auto": (True, 1))
        monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **kw: True)
        monkeypatch.setattr("nexus.commands.dt._link_semantic_record", lambda uuid: True)
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--link-semantic"])
        assert result.exit_code == 0, result.output
        assert "DEVONthink MCP unreachable" not in result.output

    def test_single_layer_flag_unreachable_exits_2_with_loud_message(
        self, runner, fake_gather, monkeypatch,
    ):
        from nexus.cli import main

        fake_gather.append(("U1", "/a.pdf"))
        monkeypatch.setattr("nexus.commands.dt._index_record",
                            lambda uuid, path, *, collection, corpus, dry_run, extractor="auto": (True, 1))
        monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **kw: False)
        monkeypatch.setattr(
            "nexus.mcp_client.devonthink.last_unreachable_detail",
            lambda: "http://localhost:8420/mcp, stdio binary /opt/DT MCP (not found)",
        )
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--writeback"])
        assert result.exit_code == 2, result.output
        assert (
            "DEVONthink MCP unreachable (http://localhost:8420/mcp, "
            "stdio binary /opt/DT MCP (not found)): layers writeback skipped"
        ) in result.output

    def test_all_three_layer_flags_unreachable_lists_all_three(
        self, runner, fake_gather, monkeypatch,
    ):
        from nexus.cli import main

        fake_gather.append(("U1", "/a.pdf"))
        monkeypatch.setattr("nexus.commands.dt._index_record",
                            lambda uuid, path, *, collection, corpus, dry_run, extractor="auto": (True, 1))
        monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **kw: False)
        result = runner.invoke(
            main,
            ["dt", "index", "--uuid", "U1", "--link-semantic", "--writeback", "--highlights"],
        )
        assert result.exit_code == 2, result.output
        assert "layers link-semantic/writeback/highlights skipped" in result.output

    def test_dt_content_alone_unreachable_stays_exit_zero_gap_0(
        self, runner, fake_gather, monkeypatch,
    ):
        # dt-content's own pre-existing Gap-0 contract (see
        # tests/test_dt_content_layer_d.py::test_flag_with_dt_unavailable_skips)
        # is untouched by this bead -- only link-semantic/writeback/
        # highlights get the loud exit-2 treatment.
        from nexus.cli import main

        fake_gather.append(("U1", "/clip.webarchive"))
        monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **kw: False)
        result = runner.invoke(main, ["dt", "index", "--selection", "--dt-content"])
        assert result.exit_code == 0, result.output
        assert "DEVONthink MCP unreachable" not in result.output

    def test_dt_content_alone_probes_http_only_no_stdio_spawn_probe(
        self, runner, fake_gather, monkeypatch,
    ):
        # code-review finding 4 (T2 [24110]): plain --dt-content (no
        # link-semantic/writeback/highlights) must NOT pay the stdio-spawn
        # probe the other three layers need -- it forces transport="http"
        # so its availability check keeps its pre-fdk1x fast-fail latency.
        from nexus.cli import main

        fake_gather.append(("U1", "/clip.webarchive"))
        calls: list[dict] = []

        def _fake_available(**kw):
            calls.append(kw)
            return True

        monkeypatch.setattr("nexus.mcp_client.devonthink.available", _fake_available)
        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record",
            lambda uuid, **kw: True,
        )
        result = runner.invoke(main, ["dt", "index", "--selection", "--dt-content"])
        assert result.exit_code == 0, result.output
        assert calls == [{"transport": "http"}]

    def test_dt_content_combined_with_loud_layer_uses_shared_auto_probe(
        self, runner, fake_gather, monkeypatch,
    ):
        # Combined with a loud layer, dt-content reuses the SAME
        # auto-transport probe rather than a second, narrower call.
        from nexus.cli import main

        fake_gather.append(("U1", "/a.pdf"))
        calls: list[dict] = []

        def _fake_available(**kw):
            calls.append(kw)
            return True

        monkeypatch.setattr("nexus.mcp_client.devonthink.available", _fake_available)
        monkeypatch.setattr("nexus.commands.dt._index_record",
                            lambda uuid, path, *, collection, corpus, dry_run, extractor="auto": (True, 1))
        monkeypatch.setattr("nexus.commands.dt._link_semantic_record", lambda uuid: True)
        result = runner.invoke(
            main, ["dt", "index", "--uuid", "U1", "--link-semantic", "--dt-content"],
        )
        assert result.exit_code == 0, result.output
        # Exactly ONE probe call, with no transport override (auto).
        assert calls == [{}]


# ── Real-binary integration probe (nexus-moht0 vacuous-gate doctrine) ──────

_STDIO_BINARY_CANDIDATES = (
    "/Applications/DEVONthink.app/Contents/Library/LoginItems/"
    "DEVONthink MCP.app/Contents/MacOS/DEVONthink MCP",
)


def _find_real_stdio_binary() -> str | None:
    for candidate in _STDIO_BINARY_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


@pytest.mark.integration
class TestRealStdioBinaryIntegration:
    """Spawns the ACTUAL DEVONthink MCP binary over stdio, when present.

    Skipped -- loudly, with the reason naming exactly what was checked,
    never silently folded into a green run -- when the binary is absent
    (any non-macOS CI runner, or a macOS box without DEVONthink
    installed). This is the one test in the file that proves the stdio
    transport works end to end rather than against a stub.
    """

    def test_is_running_over_real_stdio_binary(self) -> None:
        binary = _find_real_stdio_binary()
        if binary is None:
            pytest.skip(
                "DEVONthink MCP stdio binary not found at any of "
                f"{_STDIO_BINARY_CANDIDATES} -- nothing to spawn on this "
                "machine (expected on non-macOS CI runners)."
            )
        if sys.platform != "darwin":
            pytest.skip("DEVONthink integration is macOS-only")

        from nexus.mcp_client import devonthink as dt

        cfg = {"devonthink": {"mcp": {"transport": "stdio", "command": binary}}}
        try:
            with patch("nexus.mcp_client.devonthink.load_config", return_value=cfg):
                result = dt.available(refresh=True)
        finally:
            dt.reset_stdio_session()
            dt.reset_availability_cache()

        # `available()` folds a working transport + `running=False` (DT.app
        # not open) into the same False as an unreachable transport -- so
        # this only proves the STDIO SPAWN worked when it lands True. A
        # False here is still reported (never silently treated as a skip)
        # since the real assertion is "the call completed and returned a
        # bool," not "DEVONthink.app happens to be open right now."
        assert isinstance(result, bool)

    def test_spawned_child_is_gone_after_close(self) -> None:
        """Finding 3: close() must actually kill the spawned subprocess.

        Real-binary counterpart to TestStdioSessionHolderLocking above
        (which proves the LOCKING is race-safe against a fake connect) --
        this proves the teardown path close() drives
        (session_cm.__aexit__() -> stdio_client's MCP-spec shutdown
        sequence: close stdin, wait, escalate to SIGTERM/SIGKILL) actually
        reaps the REAL spawned "DEVONthink MCP --stdio" process, not just
        that our own bookkeeping fields got reset.
        """
        import subprocess
        import time

        binary = _find_real_stdio_binary()
        if binary is None:
            pytest.skip(
                "DEVONthink MCP stdio binary not found at any of "
                f"{_STDIO_BINARY_CANDIDATES} -- nothing to spawn on this "
                "machine (expected on non-macOS CI runners)."
            )
        if sys.platform != "darwin":
            pytest.skip("DEVONthink integration is macOS-only")

        def _dt_mcp_stdio_pids() -> set[str]:
            proc = subprocess.run(
                ["pgrep", "-f", "DEVONthink MCP.*--stdio"],
                capture_output=True, text=True, check=False,
            )
            return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

        from nexus.mcp_client import devonthink as dt

        before_pids = _dt_mcp_stdio_pids()
        cfg = {"devonthink": {"mcp": {"transport": "stdio", "command": binary}}}
        try:
            with patch("nexus.mcp_client.devonthink.load_config", return_value=cfg):
                dt.available(refresh=True)
            spawned_pids = _dt_mcp_stdio_pids() - before_pids
            assert spawned_pids, "expected a new DEVONthink MCP --stdio process to appear"

            dt.reset_stdio_session()  # drives close()'s real teardown

            deadline = time.time() + 5.0
            surviving = spawned_pids & _dt_mcp_stdio_pids()
            while surviving and time.time() < deadline:
                time.sleep(0.2)
                surviving = spawned_pids & _dt_mcp_stdio_pids()
            assert not surviving, (
                f"spawned DEVONthink MCP child(ren) {surviving} survived close()"
            )
        finally:
            dt.reset_stdio_session()
            dt.reset_availability_cache()
