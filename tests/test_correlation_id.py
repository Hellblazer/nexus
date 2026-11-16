# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-167 Phase 1, Python half (nexus-dngtz.1.1): one correlation id, one ContextVar.

The id is bound once per ``nx`` invocation and once per MCP ``tools/call``, and reset by token
when the invocation or call ends. A structlog processor renders it from that one store, so there
is no ``bind_contextvars`` and nothing left bound afterwards.

Log assertions read the output of the real configured chain (``configure_logging`` plus a stream
handler on the root logger). ``structlog.testing.capture_logs`` is NOT used: it replaces the
processor list, so ``add_correlation_id`` would never run.
"""
from __future__ import annotations

import io
import logging
import re
import subprocess
import sys
from importlib.metadata import version
from types import SimpleNamespace

import anyio
import click
import pytest
import structlog
from click.testing import CliRunner

from nexus import correlation
from nexus.cli import main
from nexus.db.client_identity import CLIENT_VERSION_HEADER, client_identity_headers
from nexus.logging_setup import configure_logging
from nexus.mcp import _tool_call_log

# KeyValueRenderer renders values with repr(), so a string value is quoted.
_HEX32 = re.compile(r"correlation_id='([0-9a-f]{32})'")


@pytest.fixture(autouse=True)
def _restore_structlog_after_test():
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


@pytest.fixture
def rendered() -> io.StringIO:
    """The real configured chain's output: configure_logging, then a stream on the root logger."""
    configure_logging("cli", verbose=True)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    yield stream
    root.removeHandler(handler)


def test_cli_invocation_logs_a_32_hex_id_and_leaves_nothing_bound() -> None:
    seen: list[str | None] = []

    @click.command(name="corr-probe")
    def probe() -> None:
        seen.append(correlation.current_correlation_id())
        structlog.get_logger("corr-probe").warning("probe_event", detail="x")

    main.add_command(probe)
    try:
        result = CliRunner().invoke(main, ["corr-probe"])
    finally:
        main.commands.pop("corr-probe", None)

    assert result.exit_code == 0, result.output
    match = _HEX32.search(result.output)
    assert match is not None, result.output
    assert seen == [match.group(1)]
    assert correlation.current_correlation_id() is None


def test_each_cli_invocation_gets_its_own_id() -> None:
    seen: list[str | None] = []

    @click.command(name="corr-probe")
    def probe() -> None:
        seen.append(correlation.current_correlation_id())

    main.add_command(probe)
    try:
        CliRunner().invoke(main, ["corr-probe"])
        CliRunner().invoke(main, ["corr-probe"])
    finally:
        main.commands.pop("corr-probe", None)

    assert len(seen) == 2 and None not in seen
    assert seen[0] != seen[1]


def _install_logged():
    server = SimpleNamespace(middleware=[])
    assert _tool_call_log.install_tool_call_log(server) == "applied"
    return next(m for m in server.middleware if getattr(m, "_nx_tool_call_log", False))


def test_concurrent_tool_calls_get_different_ids_and_leave_nothing_bound() -> None:
    entry = _install_logged()
    recorded: list[str | None] = []

    async def _next(_ctx):
        await anyio.sleep(0.01)  # let the sibling call bind before this one reads
        recorded.append(correlation.current_correlation_id())
        return {"content": [], "isError": False}

    async def _run() -> None:
        async def _one() -> None:
            ctx = SimpleNamespace(method="tools/call", params={"name": "t", "arguments": {}})
            await entry(ctx, _next)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_one)
            tg.start_soon(_one)

    anyio.run(_run)

    assert len(recorded) == 2
    assert all(r is not None and re.fullmatch(r"[0-9a-f]{32}", r) for r in recorded)
    assert recorded[0] != recorded[1]
    assert correlation.current_correlation_id() is None


def test_a_failing_tool_call_still_resets_the_id() -> None:
    entry = _install_logged()

    async def _boom(_ctx):
        raise RuntimeError("tool failed")

    ctx = SimpleNamespace(method="tools/call", params={"name": "t", "arguments": {}})
    with pytest.raises(RuntimeError):
        anyio.run(entry, ctx, _boom)
    assert correlation.current_correlation_id() is None


def test_other_methods_stay_unbound() -> None:
    entry = _install_logged()
    recorded: list[str | None] = []

    async def _next(_ctx):
        recorded.append(correlation.current_correlation_id())
        return {}

    anyio.run(entry, SimpleNamespace(method="tools/list", params={}), _next)
    assert recorded == [None]


def test_unbound_log_line_has_no_correlation_id_and_headers_are_unchanged(rendered: io.StringIO) -> None:
    structlog.get_logger("corr-unbound").warning("plain_event", detail="x")
    out = rendered.getvalue()
    assert "plain_event" in out
    assert "correlation_id" not in out
    assert correlation.correlation_headers() == {}
    # tests/test_z0o2p24_client_version_header.py:50 states the same dictionary.
    assert client_identity_headers() == {CLIENT_VERSION_HEADER: version("conexus")}


def test_bound_headers_carry_the_id() -> None:
    token = correlation.bind("a" * 32)
    try:
        assert correlation.correlation_headers() == {correlation.HEADER: "a" * 32}
    finally:
        correlation.reset(token)
    assert correlation.correlation_headers() == {}
    assert correlation.HEADER == "X-Correlation-ID"


def test_new_ids_are_32_lowercase_hex_and_distinct() -> None:
    a, b = correlation.new_correlation_id(), correlation.new_correlation_id()
    assert re.fullmatch(r"[0-9a-f]{32}", a) and re.fullmatch(r"[0-9a-f]{32}", b)
    assert a != b


def test_request_id_stays_the_idempotency_key_beside_the_bound_id(rendered: io.StringIO) -> None:
    """The keyed_sweep_* lines (db/http_vector_client.py) pass request_id=<idempotency key>."""
    cid = correlation.new_correlation_id()
    token = correlation.bind(cid)
    try:
        structlog.get_logger("corr-collision").warning("keyed_sweep_status_poll_failed", request_id="k1", attempt=2)
    finally:
        correlation.reset(token)
    out = rendered.getvalue()
    assert "request_id='k1'" in out
    assert f"correlation_id='{cid}'" in out


def test_an_explicit_correlation_id_on_the_event_is_kept(rendered: io.StringIO) -> None:
    token = correlation.bind("b" * 32)
    try:
        structlog.get_logger("corr-explicit").warning("explicit_event", correlation_id="c" * 32)
    finally:
        correlation.reset(token)
    out = rendered.getvalue()
    assert "correlation_id='" + "c" * 32 + "'" in out
    assert "b" * 32 not in out


def test_importing_the_module_does_not_import_nexus_db() -> None:
    code = (
        "import sys, nexus.correlation; "
        "print(any(k == 'nexus.db' or k.startswith('nexus.db.') for k in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_the_conftest_reset_fixture_is_autouse(request: pytest.FixtureRequest) -> None:
    """No test sees an id another test bound: the conftest fixture runs for every test."""
    assert "_correlation_id_reset" in request.fixturenames
    assert correlation.current_correlation_id() is None
