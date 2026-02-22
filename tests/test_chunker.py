"""AC5/AC6: AST chunking and line-based fallback chunking."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.chunker import chunk_file, _line_chunk


# ── Line-based fallback (_line_chunk) ─────────────────────────────────────────

def test_line_chunk_short_file_single_chunk() -> None:
    """File shorter than chunk_lines → exactly one chunk."""
    content = "\n".join(f"line {i}" for i in range(10))
    chunks = _line_chunk(content, chunk_lines=150)
    assert len(chunks) == 1
    assert chunks[0][0] == 1   # line_start (1-indexed)
    assert chunks[0][1] == 10  # line_end


def test_line_chunk_splits_long_file() -> None:
    """300-line file with 150-line chunks produces at least 2 chunks."""
    content = "\n".join(f"line {i}" for i in range(300))
    chunks = _line_chunk(content, chunk_lines=150)
    assert len(chunks) >= 2


def test_line_chunk_overlap() -> None:
    """Consecutive chunks overlap by ≈15%: second chunk starts before first ends."""
    content = "\n".join(f"x{i}" for i in range(300))
    chunks = _line_chunk(content, chunk_lines=100, overlap=0.15)
    assert len(chunks) >= 2
    # Second chunk starts before first chunk's end (they share lines)
    assert chunks[1][0] <= chunks[0][1]


def test_line_chunk_correct_line_numbers() -> None:
    """Line numbers are 1-indexed and span the full file."""
    content = "\n".join(f"x{i}" for i in range(10))
    chunks = _line_chunk(content, chunk_lines=150)
    assert chunks[0][0] == 1
    assert chunks[0][1] == 10


def test_line_chunk_content_matches_lines() -> None:
    """Third element of each chunk tuple is the actual chunk text."""
    content = "alpha\nbeta\ngamma"
    chunks = _line_chunk(content, chunk_lines=150)
    assert len(chunks) == 1
    assert "alpha" in chunks[0][2]
    assert "gamma" in chunks[0][2]


# ── chunk_file: line fallback for unknown extension ───────────────────────────

def test_chunk_file_unknown_extension_uses_line_fallback(tmp_path: Path) -> None:
    """Unknown extension → line fallback, ast_chunked=False."""
    f = tmp_path / "data.xyz"
    f.write_text("\n".join(f"line {i}" for i in range(50)))

    chunks = chunk_file(f, f.read_text())

    assert len(chunks) >= 1
    assert all(c["ast_chunked"] is False for c in chunks)


def test_chunk_file_line_fallback_metadata(tmp_path: Path) -> None:
    """Line fallback chunks carry the required metadata fields."""
    f = tmp_path / "script.unknown_ext"
    f.write_text("\n".join(f"code {i}" for i in range(20)))

    chunks = chunk_file(f, f.read_text())

    assert len(chunks) == 1
    c = chunks[0]
    assert c["file_path"] == str(f)
    assert c["filename"] == "script.unknown_ext"
    assert c["file_extension"] == ".unknown_ext"
    assert c["ast_chunked"] is False
    assert c["chunk_index"] == 0
    assert c["chunk_count"] == 1
    assert "line_start" in c
    assert "line_end" in c


# ── chunk_file: AST path ──────────────────────────────────────────────────────

def test_chunk_file_python_calls_codesplitter(tmp_path: Path) -> None:
    """Python files trigger CodeSplitter; results marked ast_chunked=True."""
    f = tmp_path / "module.py"
    f.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")

    mock_node1 = MagicMock()
    mock_node1.text = "def foo():\n    pass"
    mock_node1.metadata = {}
    mock_node2 = MagicMock()
    mock_node2.text = "def bar():\n    pass"
    mock_node2.metadata = {}

    with patch("nexus.chunker._make_code_splitter", return_value=[mock_node1, mock_node2]):
        chunks = chunk_file(f, f.read_text())

    assert len(chunks) == 2
    assert all(c["ast_chunked"] is True for c in chunks)
    assert all(c["file_extension"] == ".py" for c in chunks)
    assert chunks[0]["chunk_index"] == 0
    assert chunks[1]["chunk_index"] == 1
    assert all(c["chunk_count"] == 2 for c in chunks)


def test_chunk_file_ast_failure_falls_back_to_lines(tmp_path: Path) -> None:
    """If CodeSplitter raises, fall back to line chunking silently."""
    f = tmp_path / "tricky.py"
    f.write_text("def foo():\n    pass\n")

    with patch("nexus.chunker._make_code_splitter", side_effect=Exception("parse error")):
        chunks = chunk_file(f, f.read_text())

    assert len(chunks) >= 1
    assert all(c["ast_chunked"] is False for c in chunks)
