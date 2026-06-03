# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-126 §3 (nexus-cy8g1): first-run banner contract.

On the first MCP startup the server surfaces a one-shot banner telling
the user the host T2 daemon was installed (or was already configured)
and how to remove it in-chat. The banner is delivered on the first tool
response by prepending to ``content[0].text``; a best-effort
``notifications/message`` is a secondary channel.

Load-bearing marker contract (locked here with exact assertions):

- The marker ``<config>/.mcp_first_run_complete`` is written ONLY after a
  delivery channel SUCCEEDS — NEVER on a prepend exception. A failed
  delivery must leave the banner pending so it retries on the next tool
  call rather than silently burning the one-shot.
- ``maybe_banner`` returns ``None`` once the marker exists.
- Two text variants keyed on install status, each carrying the unit path
  and the in-chat uninstall instruction.

The marker lives under ``nexus_config_dir()`` which honours
``NEXUS_CONFIG_DIR``; tests redirect it to a tmp path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.daemon.installer import InstallStatus
from nexus.mcp import _first_run


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    # Reset any module-level pending banner between tests.
    _first_run._clear_pending_banner()
    yield
    _first_run._clear_pending_banner()


class _Block:
    """Minimal stand-in for an MCP TextContent block (has ``.text``)."""

    def __init__(self, text: str) -> None:
        self.text = text


class TestMarkerPath:
    def test_marker_under_config_dir(self) -> None:
        marker = _first_run._first_run_marker_path()
        assert marker.name == ".mcp_first_run_complete"
        assert marker.parent.name == "cfg"


class TestMaybeBanner:
    def test_newly_installed_variant(self) -> None:
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/Users/x/Library/LaunchAgents/com.nexus.t2.plist")
        )
        assert spec is not None
        assert "installed" in spec.text.lower()
        assert "com.nexus.t2.plist" in spec.text
        # In-chat uninstall instruction must be present in both variants.
        assert "daemon_uninstall" in spec.text

    def test_already_present_variant(self) -> None:
        spec = _first_run.maybe_banner(
            InstallStatus.ALREADY_PRESENT, Path("/Users/x/Library/LaunchAgents/com.nexus.t2.plist")
        )
        assert spec is not None
        assert "already configured" in spec.text.lower()
        assert "com.nexus.t2.plist" in spec.text
        assert "daemon_uninstall" in spec.text

    def test_failed_status_returns_none(self) -> None:
        # A failed install has no daemon to announce; no banner.
        assert _first_run.maybe_banner(InstallStatus.FAILED, None) is None

    def test_returns_none_when_marker_exists(self) -> None:
        _first_run.mark_shown()
        assert (
            _first_run.maybe_banner(
                InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
            )
            is None
        )


class TestMarkShown:
    def test_mark_shown_creates_marker_and_parents(self) -> None:
        marker = _first_run._first_run_marker_path()
        assert not marker.exists()
        _first_run.mark_shown()
        assert marker.exists()

    def test_mark_shown_idempotent(self) -> None:
        _first_run.mark_shown()
        _first_run.mark_shown()  # must not raise
        assert _first_run._first_run_marker_path().exists()


class TestDeliveryAndMarkerDiscipline:
    def test_successful_delivery_prepends_and_marks(self) -> None:
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)

        blocks = [_Block("original tool output")]
        delivered = _first_run.deliver_pending_banner(blocks)

        assert delivered is True
        assert blocks[0].text.startswith(spec.text)
        assert "original tool output" in blocks[0].text
        # Marker written only after successful delivery.
        assert _first_run._first_run_marker_path().exists()
        # Pending cleared — second call is a no-op.
        assert _first_run.deliver_pending_banner([_Block("second")]) is False

    def test_no_pending_banner_is_noop(self) -> None:
        assert _first_run.deliver_pending_banner([_Block("x")]) is False
        assert not _first_run._first_run_marker_path().exists()

    def test_prepend_failure_does_not_write_marker_and_retains_pending(self) -> None:
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)

        # Malformed result: empty content list -> prepend raises internally.
        delivered = _first_run.deliver_pending_banner([])

        assert delivered is False
        # THE load-bearing assertion: marker NOT written on failed delivery.
        assert not _first_run._first_run_marker_path().exists()

        # Retry on a well-formed result now succeeds and marks.
        blocks = [_Block("retry output")]
        assert _first_run.deliver_pending_banner(blocks) is True
        assert blocks[0].text.startswith(spec.text)
        assert _first_run._first_run_marker_path().exists()

    def test_marker_write_failure_after_prepend_still_delivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the prepend succeeds but mark_shown() raises, the banner was
        delivered (return True, queue cleared) and the marker is absent so
        the next startup re-queues — an at-most-double show, never a burn."""
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)

        def _boom() -> None:
            raise OSError("read-only config dir")

        monkeypatch.setattr(_first_run, "mark_shown", _boom)
        blocks = [_Block("output")]
        delivered = _first_run.deliver_pending_banner(blocks)

        assert delivered is True
        assert blocks[0].text.startswith(spec.text)
        assert not _first_run._first_run_marker_path().exists()
        # Queue cleared this session (no double-show within the session).
        assert _first_run.deliver_pending_banner([_Block("next")]) is False

    def test_block_without_text_attr_is_treated_as_failure(self) -> None:
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)

        class _NoText:
            pass

        delivered = _first_run.deliver_pending_banner([_NoText()])
        assert delivered is False
        assert not _first_run._first_run_marker_path().exists()


class TestDispatchHook:
    """The CallToolRequest wrapper prepends the pending banner to the
    first real tool response, then no-ops once the marker is written."""

    def test_hook_prepends_banner_to_first_tool_result(self) -> None:
        import anyio
        from mcp import types
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("test-banner")

        @server.tool(description="echo")
        def echo(value: str) -> str:
            return value

        installed = _first_run.install_banner_dispatch_hook(server)
        assert installed is True

        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)

        low = server._mcp_server
        handler = low.request_handlers[types.CallToolRequest]

        def _call() -> types.ServerResult:
            req = types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(
                    name="echo", arguments={"value": "hello"}
                ),
            )
            return anyio.run(handler, req)

        result = _call()
        content = result.root.content
        text = content[0].text
        assert "hello" in text
        assert spec.text in text
        assert _first_run._first_run_marker_path().exists()

        # Second call: banner already delivered -> unchanged.
        result2 = _call()
        assert spec.text not in result2.root.content[0].text


class TestInstructionsChannel:
    """RDR-126 §3 amendment (nexus-vlo2b): the banner's PRIMARY channel is the
    MCP server `instructions` field (delivered in the initialize handshake),
    because Claude Desktop paraphrases away the content-prepend in tool results.
    Content-prepend stays as the Claude Code fallback."""

    class _Low:
        def __init__(self, instructions=None):
            self.instructions = instructions

    class _Server:
        def __init__(self, instructions=None):
            self._mcp_server = TestInstructionsChannel._Low(instructions)

    def test_injects_banner_into_instructions_and_marks(self) -> None:
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)

        server = self._Server(instructions="Base instructions.")
        ok = _first_run.apply_first_run_banner_instructions(server)

        assert ok is True
        instr = server._mcp_server.instructions
        assert "Base instructions." in instr            # base preserved
        assert spec.text in instr                        # banner present
        assert "surface this to the user" in instr.lower()  # relay-framed
        assert _first_run._first_run_marker_path().exists()  # one-shot marked

    def test_clears_pending_so_content_prepend_does_not_double_fire(self) -> None:
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)

        assert _first_run.apply_first_run_banner_instructions(self._Server()) is True
        # Pending consumed: the content-prepend dispatch path is now a no-op.
        assert _first_run.deliver_pending_banner([_Block("x")]) is False

    def test_no_pending_banner_is_noop(self) -> None:
        server = self._Server(instructions="Base.")
        assert _first_run.apply_first_run_banner_instructions(server) is False
        assert server._mcp_server.instructions == "Base."
        assert not _first_run._first_run_marker_path().exists()

    def test_injection_failure_retains_pending_for_content_prepend_fallback(self) -> None:
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)

        class _Broken:
            pass  # no _mcp_server attribute -> AttributeError inside

        ok = _first_run.apply_first_run_banner_instructions(_Broken())
        assert ok is False
        # Banner stays pending so the dispatch-hook fallback still delivers it,
        # and the one-shot is NOT burned.
        assert not _first_run._first_run_marker_path().exists()
        blocks = [_Block("tool output")]
        assert _first_run.deliver_pending_banner(blocks) is True
        assert blocks[0].text.startswith(spec.text)

    def test_first_run_marker_present_means_nothing_to_apply(self) -> None:
        _first_run.mark_shown()
        # maybe_banner returns None -> nothing queued -> apply is a no-op.
        assert _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        ) is None
        server = self._Server(instructions="Base.")
        assert _first_run.apply_first_run_banner_instructions(server) is False
        assert server._mcp_server.instructions == "Base."

    def test_end_to_end_banner_in_initialize_instructions(self) -> None:
        """Real FastMCP + client: the banner reaches InitializeResult.instructions."""
        import anyio
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("test-banner-instr", instructions="Base.")

        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)
        assert _first_run.apply_first_run_banner_instructions(server) is True

        # The low-level server now carries the banner; FastMCP surfaces it as
        # the InitializeResult.instructions at handshake.
        instr = server._mcp_server.instructions
        assert spec.text in instr
        assert instr.startswith("Base.")

    def test_marker_write_failure_still_clears_pending_and_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If injection succeeds but mark_shown() raises (e.g. read-only config),
        the function still clears pending + returns True; the marker stays absent
        so the next startup re-queues (at-most-double show, never a burn)."""
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)

        def _boom() -> None:
            raise OSError("read-only config dir")

        monkeypatch.setattr(_first_run, "mark_shown", _boom)
        server = self._Server(instructions="Base.")
        ok = _first_run.apply_first_run_banner_instructions(server)

        assert ok is True
        assert spec.text in server._mcp_server.instructions
        assert not _first_run._first_run_marker_path().exists()
        # pending cleared this session -> dispatch hook is a no-op
        assert _first_run.deliver_pending_banner([_Block("x")]) is False

    def test_composes_with_embedder_notice_no_clobber(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """core.main() runs apply_first_run_banner_instructions then
        apply_embedder_notice; both append to instructions. Verify the banner
        segment survives a subsequent embedder-notice append (no clobber)."""
        spec = _first_run.maybe_banner(
            InstallStatus.NEWLY_INSTALLED, Path("/x/com.nexus.t2.plist")
        )
        assert spec is not None
        _first_run.queue_banner(spec)
        server = self._Server(instructions="Base.")
        assert _first_run.apply_first_run_banner_instructions(server) is True

        # Force an embedder notice and apply it onto the same low-level server.
        monkeypatch.setattr(_first_run, "embedder_startup_notice", lambda: "embedder: use bge")
        assert _first_run.apply_embedder_notice(server) is True

        instr = server._mcp_server.instructions
        assert "Base." in instr
        assert spec.text in instr            # banner not clobbered
        assert "embedder: use bge" in instr  # embedder appended after
