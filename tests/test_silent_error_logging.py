# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 1 — verify formerly-silent catch blocks now emit structlog events."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _enable_debug_logging():
    """Temporarily allow DEBUG-level structlog events so capture_logs() can see them."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    yield
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


# ── Site 1: indexer.py _extract_name_from_node decode (field-name path) ───────

def test_extract_name_decode_failed_logs_debug():
    """Site 1: UnicodeDecodeError in child_by_field_name('name').text.decode."""
    from nexus.indexer import _extract_name_from_node

    node = MagicMock()
    node.type = "function_definition"
    # child_by_field_name("name") returns a child whose .text.decode raises
    bad_child = MagicMock()
    bad_child.text = MagicMock()
    bad_child.text.decode = MagicMock(side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad"))

    def field_lookup(name):
        if name == "name":
            return bad_child
        return None

    node.child_by_field_name = field_lookup
    node.children = []

    with capture_logs() as cap:
        result = _extract_name_from_node(node)

    assert result == ""
    assert any(e["event"] == "extract_name_decode_failed" for e in cap)


# ── Site 2: indexer.py _extract_name_from_node decode (children path) ─────────

def test_extract_name_child_decode_failed_logs_debug():
    """Site 2: UnicodeDecodeError in children scan path."""
    from nexus.indexer import _extract_name_from_node

    node = MagicMock()
    node.type = "function_definition"
    node.child_by_field_name = MagicMock(return_value=None)

    bad_child = MagicMock()
    bad_child.type = "identifier"
    bad_child.text = MagicMock()
    bad_child.text.decode = MagicMock(side_effect=AttributeError("no text"))
    node.children = [bad_child]

    with capture_logs() as cap:
        result = _extract_name_from_node(node)

    assert result == ""
    assert any(e["event"] == "extract_name_child_decode_failed" for e in cap)


# ── Site 3: indexer.py get_parser failure ─────────────────────────────────────

def test_get_parser_failed_logs_warning():
    """Site 3: get_parser() failure emits warning-level log."""
    from nexus.indexer import _extract_context

    with patch("tree_sitter_language_pack.get_parser", side_effect=RuntimeError("no parser")):
        with capture_logs() as cap:
            result = _extract_context(b"x = 1", "python", 0, 0)

    assert result == ("", "")
    assert any(
        e["event"] == "get_parser_failed" and e["log_level"] == "warning"
        for e in cap
    )


# ── Site 4: indexer.py tree parse failure ─────────────────────────────────────

def test_tree_parse_failed_logs_debug():
    """Site 4: parser.parse() failure emits debug-level log."""
    from nexus.indexer import _extract_context

    mock_parser = MagicMock()
    mock_parser.parse.side_effect = RuntimeError("bad source")

    with patch("tree_sitter_language_pack.get_parser", return_value=mock_parser):
        with capture_logs() as cap:
            result = _extract_context(b"x = 1", "python", 0, 0)

    assert result == ("", "")
    assert any(e["event"] == "tree_parse_failed" for e in cap)


# ── Site 5: indexer.py _current_head failure ──────────────────────────────────

def test_current_head_failed_logs_debug():
    """Site 5: _current_head() OSError emits debug-level log."""
    from nexus.indexer import _current_head

    with patch("nexus.indexer.subprocess.run", side_effect=OSError("git not found")):
        with capture_logs() as cap:
            result = _current_head(Path("/fake/repo"))

    assert result == ""
    assert any(e["event"] == "current_head_failed" for e in cap)


# ── Site 6: session.py _ppid_of failure ───────────────────────────────────────

def test_ppid_proc_read_failed_logs_debug():
    """Site 6: _ppid_of() /proc read failure emits debug-level log."""
    import sys
    if sys.platform != "linux":
        pytest.skip("_ppid_of reads /proc, only testable on Linux")

    import os
    from nexus.session import _ppid_of

    # Use our own PID so /proc/{pid}/status exists on Linux.
    pid = os.getpid()
    original_read_text = Path.read_text

    def failing_read_text(self, *args, **kwargs):
        if str(self).startswith("/proc/"):
            raise OSError("simulated permission denied")
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", failing_read_text):
        with capture_logs() as cap:
            _ppid_of(pid)

    assert any(e["event"] == "ppid_proc_read_failed" for e in cap)


# ── Site 7: session.py sweep_stale_sessions corrupt file ──────────────────────




# ── Site 8: hooks.py _infer_repo failure ──────────────────────────────────────

def test_infer_repo_git_failed_logs_debug():
    """Site 8: _infer_repo() git failure emits debug-level log."""
    from nexus.hooks import _infer_repo

    with patch("nexus.hooks.subprocess.run", side_effect=RuntimeError("git broken")):
        with capture_logs() as cap:
            result = _infer_repo()

    # Falls back to cwd name
    assert isinstance(result, str) and len(result) > 0
    assert any(e["event"] == "infer_repo_git_failed" for e in cap)


# ── Site 9: hooks.py session_end own record corrupt ───────────────────────────




# ── Site 10: commands/hook.py stdin parse failure ─────────────────────────────

def test_session_start_stdin_parse_failed_logs_debug():
    """Site 10: stdin JSON parse failure in session_start_cmd emits debug log."""
    from nexus.commands.hook import session_start_cmd
    from click.testing import CliRunner

    runner = CliRunner()
    with capture_logs() as cap:
        runner.invoke(session_start_cmd, input="not json\n")

    assert any(e["event"] == "session_start_stdin_parse_failed" for e in cap)


# ── Site 11: commands/index.py hook detection failure ─────────────────────────

def test_hook_detection_failed_logs_debug(tmp_path):
    """Site 11: hook detection exception in index_repo_cmd emits debug log."""
    from nexus.commands.index import index_repo_cmd
    from click.testing import CliRunner

    # Create a minimal git repo so the command doesn't fail before reaching hook detection
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    (tmp_path / "test.txt").write_text("hello")

    # Mock index_repository to skip the actual indexing (needs API keys on CI),
    # and _effective_hooks_dir to trigger the hook detection failure path.
    with patch("nexus.indexer.index_repository", return_value={}), \
         patch("nexus._git_hooks_meta.effective_hooks_dir", side_effect=RuntimeError("broken")):
        with capture_logs() as cap:
            runner = CliRunner()
            runner.invoke(index_repo_cmd, [str(tmp_path)])

    assert any(e["event"] == "hook_detection_failed" for e in cap)


# ── Site 12: commands/doctor.py registry load failure ─────────────────────────

def test_doctor_registry_load_failed_logs_warning():
    """Site 12: enumeration failure during git-hook health check
    emits warning-level log.

    RDR-137 Phase 3.1 (nexus-tts0d.6) moved repo enumeration off
    RepoRegistry onto ``nexus.repos.list_repos_dual``. The warning
    fires when that helper raises; the event name is preserved
    (``doctor_registry_load_failed``) so existing log-scrape pipelines
    keep matching.
    """
    from click.testing import CliRunner
    from nexus.commands.doctor import doctor_cmd

    # Patch at definition site (nexus.repos) because health.py
    # imports lazily inside _check_hooks_for_known_repos.
    with patch("nexus.repos.list_repos_dual", side_effect=RuntimeError("corrupt")):
        with capture_logs() as cap:
            runner = CliRunner()
            runner.invoke(doctor_cmd)

    assert any(
        e["event"] == "doctor_registry_load_failed" and e["log_level"] == "warning"
        for e in cap
    )


# ── Site 13: md_chunker.py frontmatter parse failure ─────────────────────────

def test_frontmatter_parse_failed_logs_warning():
    """Site 13: invalid YAML frontmatter emits warning-level log."""
    from nexus.md_chunker import parse_frontmatter

    bad_md = "---\ninvalid: yaml: [unclosed\n---\n\n# Body\n\nText here."

    with capture_logs() as cap:
        fm, body = parse_frontmatter(bad_md)

    assert fm == {}
    assert any(
        e["event"] == "frontmatter_parse_failed" and e["log_level"] == "warning"
        for e in cap
    )


# ── Site 14: classifier.py _has_shebang failure ──────────────────────────────

def test_has_shebang_read_failed_logs_debug():
    """Site 14: OSError in _has_shebang emits debug-level log."""
    from nexus.classifier import _has_shebang

    fake_path = Path("/nonexistent/file.py")

    with capture_logs() as cap:
        result = _has_shebang(fake_path)

    assert result is False
    assert any(e["event"] == "has_shebang_read_failed" for e in cap)


# ── Site 15: mcp/core.py store_put catalog hook failure (GH #253) ─────────────

def test_catalog_store_hook_failed_logs_warning(tmp_path, monkeypatch):
    """Site 15: _catalog_store_hook exception in MCP store_put emits warning-level log.

    Previously a bare `try/except: pass` swallowed all catalog registration
    failures, creating the inverse-of-#244 orphan shape (T3 row with no
    catalog tumbler) with zero observability.
    """
    import chromadb

    from nexus.db.t1 import T1Database
    from nexus.db.t2 import T2Database
    from nexus.db.t3 import T3Database
    from nexus.mcp_server import (
        _inject_t1,
        _inject_t3,
        _reset_singletons,
        store_put,
    )

    _reset_singletons()
    try:
        import nexus.mcp.core as core_mod
        t2_path = tmp_path / "t2.db"
        monkeypatch.setattr(core_mod, "_t2_ctx", lambda: T2Database(t2_path))

        t1_client = chromadb.EphemeralClient()
        _inject_t1(T1Database(session_id="hook-fail", client=t1_client))

        t3_client = chromadb.EphemeralClient()
        ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
        _inject_t3(T3Database(_client=t3_client, _ef_override=ef))

        # Force the catalog hook to raise.
        # nexus-8g79.10 (V1): the hook moved to nexus.catalog.store_hook
        # (lower layer); patch the canonical location.
        monkeypatch.setattr(
            "nexus.catalog.store_hook.catalog_store_hook",
            MagicMock(side_effect=RuntimeError("simulated catalog failure")),
        )

        with capture_logs() as cap:
            result = store_put(
                content="hook failure test",
                collection="knowledge",
                title="hook-fail-doc",
            )

        # store_put still reports success (non-fatal policy).
        assert "Stored:" in result
        # Failure is now observable.
        assert any(
            e["event"] == "catalog_store_hook_failed" and e["log_level"] == "warning"
            for e in cap
        )
    finally:
        _reset_singletons()
