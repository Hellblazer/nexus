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

    from nexus.session import _ppid_of

    with patch("builtins.open", side_effect=OSError("no such file")):
        with capture_logs() as cap:
            result = _ppid_of(99999)

    assert result is None
    assert any(e["event"] == "ppid_proc_read_failed" for e in cap)


# ── Site 7: session.py sweep_stale_sessions corrupt file ──────────────────────

def test_sweep_corrupt_session_file_logs_debug(tmp_path):
    """Site 7: corrupt session file in sweep_stale_sessions emits debug-level log."""
    from nexus.session import sweep_stale_sessions

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "999.session").write_text("not-json{{{")

    with capture_logs() as cap:
        sweep_stale_sessions(sessions_dir=sessions_dir)

    assert any(e["event"] == "sweep_corrupt_session_file" for e in cap)


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

def test_session_end_own_record_corrupt_logs_debug(tmp_path):
    """Site 9: corrupt own session file in session_end emits debug-level log."""
    import os
    from nexus.hooks import session_end

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    # Write a corrupt file named after this process's parent PID
    ppid = os.getppid()
    corrupt_file = sessions_dir / f"{ppid}.session"
    corrupt_file.write_text("not-valid-json{{{")

    with patch("nexus.hooks.SESSIONS_DIR", sessions_dir), \
         patch("nexus.hooks.find_ancestor_session", return_value=None):
        with capture_logs() as cap:
            session_end()

    assert any(e["event"] == "session_end_own_record_corrupt" for e in cap)


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

    with patch("nexus.commands.hooks._effective_hooks_dir", side_effect=RuntimeError("broken")):
        with capture_logs() as cap:
            runner = CliRunner()
            runner.invoke(index_repo_cmd, [str(tmp_path)])

    assert any(e["event"] == "hook_detection_failed" for e in cap)


# ── Site 12: commands/doctor.py registry load failure ─────────────────────────

def test_doctor_registry_load_failed_logs_warning():
    """Site 12: corrupt registry emits warning-level log in doctor."""
    from nexus.commands.doctor import RepoRegistry, doctor_cmd
    from click.testing import CliRunner

    with patch.object(RepoRegistry, "__init__", side_effect=RuntimeError("corrupt")):
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
