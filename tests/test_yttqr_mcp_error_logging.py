# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-yttqr — structured logging on MCP tool-error returns + the md_chunker
printf-in-structlog fix.

The MCP read-path handlers historically returned ``f"Error: {e}"`` with NO
server-side log; ``_mcp_tool_error`` now logs the exception structured (with the
traceback in ``exc_info``, never in the agent-facing string) and adds a
remediation hint when the failure looks like the backing service is unreachable.
"""
from __future__ import annotations

import structlog
from structlog.testing import capture_logs

from nexus.mcp.core import _mcp_tool_error


class TestMcpToolError:
    def test_logs_structured_event_with_error_field(self) -> None:
        with capture_logs() as cap:
            _mcp_tool_error("search", ValueError("boom"))
        events = [e for e in cap if e.get("event") == "mcp_search_failed"]
        assert len(events) == 1, "must emit exactly one structured event named per tool"
        assert events[0]["error"] == "boom", "the exception text must be a structured field"
        assert events[0]["log_level"] == "error"

    def test_plain_error_returns_bare_message_no_traceback(self) -> None:
        out = _mcp_tool_error("store_put", ValueError("bad input"))
        assert out == "Error: bad input"
        assert "Traceback" not in out, "traceback must stay server-side, never in the return"

    def test_connection_failure_returns_remediation_hint(self) -> None:
        out = _mcp_tool_error("query", ConnectionRefusedError("Connection refused"))
        assert out.startswith("Error: ")
        assert "nx doctor" in out, "connection failures must carry an actionable remediation hint"

    def test_permission_error_is_not_mislabeled_as_unreachable(self) -> None:
        # PermissionError (OSError subclass) on a locked SQLite file is NOT a
        # daemon-down condition — it must NOT get the "restart the daemon" hint.
        out = _mcp_tool_error("memory_put", PermissionError("attempt to write a readonly database"))
        assert "nx doctor" not in out
        assert out == "Error: attempt to write a readonly database"

    def test_bare_connect_substring_does_not_false_positive(self) -> None:
        # An error merely mentioning a collection named 'connect_test' must not be
        # mislabeled as service-unreachable (bare 'connect' marker was removed).
        out = _mcp_tool_error("search", ValueError("Invalid collection name: connect_test"))
        assert "nx doctor" not in out

    def test_timeout_message_string_also_triggers_hint(self) -> None:
        # Non-OSError whose message still signals unreachability (e.g. an httpx wrapper).
        out = _mcp_tool_error("collection_list", RuntimeError("max retries exceeded; timed out"))
        assert "nx doctor" in out

    def test_event_name_is_per_tool(self) -> None:
        with capture_logs() as cap:
            _mcp_tool_error("memory_get", KeyError("k"))
        assert any(e.get("event") == "mcp_memory_get_failed" for e in cap)

    def test_session_unauthorized_returns_reconnect_hint(self) -> None:
        # nexus-ngcpo Finding/(d): a T1 401 (SESSION_UNAUTHORIZED_MARKER) must
        # get equivalent "reconnect" guidance to commands/scratch.py's
        # _clean_service_errors, not a bare error repr.
        from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER

        exc = RuntimeError(f"{SESSION_UNAUTHORIZED_MARKER} on /v1/t1/put: unauthorized")
        out = _mcp_tool_error("scratch", exc)
        assert "reconnect" in out.lower()
        assert "conexus mcp" in out.lower()
        assert SESSION_UNAUTHORIZED_MARKER in out, "the underlying marker text is still surfaced"
        assert "nx doctor" not in out, (
            "a 401 is an auth failure, not a connectivity failure — must not "
            "get the unrelated daemon-restart hint"
        )

    def test_session_unauthorized_checked_before_connection_hint(self) -> None:
        # The two hints are mutually exclusive in practice, but assert the
        # 401 branch wins if a marker string somehow also matched a
        # connection-error substring, so the more specific/actionable hint
        # is never shadowed by the generic one.
        from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER

        exc = RuntimeError(f"{SESSION_UNAUTHORIZED_MARKER}: connection reset by peer")
        out = _mcp_tool_error("scratch", exc)
        assert "reconnect the conexus mcp" in out.lower()


class TestMdChunkerStructuredWarning:
    def test_fallback_warning_carries_error_field(self) -> None:
        # Force semantic chunking to raise so the fallback warning fires, and assert
        # the rendered event carries the error value (non-vacuous — the old %s form
        # dropped it entirely).
        from nexus import md_chunker

        chunker = md_chunker.SemanticMarkdownChunker()
        if not getattr(chunker, "md", None):
            # markdown-it not available in this env → semantic path not taken; skip.
            import pytest
            pytest.skip("markdown-it not available; semantic-chunking path inactive")

        orig = chunker._semantic_chunking
        chunker._semantic_chunking = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("xyz-fail"))
        try:
            with capture_logs() as cap:
                chunker.chunk("# heading\n\nsome text\n", {})
        finally:
            chunker._semantic_chunking = orig
        warns = [e for e in cap if e.get("event") == "semantic_chunking_failed_fallback_naive"]
        assert warns, "fallback must emit the structured warning event"
        assert warns[0]["error"] == "xyz-fail", "error value must survive (old %s form dropped it)"
