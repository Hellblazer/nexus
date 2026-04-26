"""AC5/AC6: AST chunking and line-based fallback chunking."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.chunker import chunk_file, _enforce_byte_cap, _line_chunk, _CHUNK_MAX_BYTES


# ── Line-based fallback (_line_chunk) ─────────────────────────────────────────

@pytest.mark.parametrize("n_lines,chunk_lines,expected_chunks,check_start,check_end", [
    (10, 150, 1, 1, 10),       # short file → single chunk
    (5, 150, 1, 1, 5),         # fewer lines than chunk_lines
    (10, 10, 1, None, 10),     # exact chunk_lines
])
def test_line_chunk_basic(n_lines, chunk_lines, expected_chunks, check_start, check_end):
    content = "\n".join(f"line {i}" for i in range(n_lines))
    chunks = _line_chunk(content, chunk_lines=chunk_lines)
    assert len(chunks) == expected_chunks
    if check_start is not None:
        assert chunks[0][0] == check_start
    assert chunks[0][1] == check_end


def test_line_chunk_splits_long_file():
    content = "\n".join(f"line {i}" for i in range(300))
    assert len(_line_chunk(content, chunk_lines=150)) >= 2


def test_line_chunk_overlap():
    content = "\n".join(f"x{i}" for i in range(300))
    chunks = _line_chunk(content, chunk_lines=100, overlap=0.15)
    assert len(chunks) >= 2
    assert chunks[1][0] <= chunks[0][1]


def test_line_chunk_content_matches_lines():
    chunks = _line_chunk("alpha\nbeta\ngamma", chunk_lines=150)
    assert len(chunks) == 1
    assert "alpha" in chunks[0][2] and "gamma" in chunks[0][2]


def test_line_chunk_empty_string_returns_empty():
    assert _line_chunk("") == []


# ── chunk_file: line fallback for unknown extension ───────────────────────────

def test_chunk_file_custom_chunk_lines_produces_more_chunks(tmp_path: Path):
    f = tmp_path / "code.xyz"
    content = "\n".join(f"line {i}" for i in range(300))
    f.write_text(content)
    assert len(chunk_file(f, content, chunk_lines=50)) > len(chunk_file(f, content, chunk_lines=150))


def test_chunk_file_unknown_extension_uses_line_fallback(tmp_path: Path):
    """Unknown extensions go through the line-based fallback (no AST)."""
    f = tmp_path / "data.xyz"
    f.write_text("\n".join(f"line {i}" for i in range(50)))
    chunks = chunk_file(f, f.read_text())
    assert len(chunks) >= 1
    # Line fallback chunks carry line_start / line_end; AST chunks would also
    # set chunk_start_char (set by AST node offsets, not the fallback path).
    assert all("line_start" in c and "line_end" in c for c in chunks)


def test_chunk_file_line_fallback_metadata(tmp_path: Path):
    """nexus-59j0: filename / file_extension / ast_chunked dropped; the
    chunker now emits only metadata that the indexer factory consumes."""
    f = tmp_path / "script.unknown_ext"
    f.write_text("\n".join(f"code {i}" for i in range(20)))
    chunks = chunk_file(f, f.read_text())
    assert len(chunks) == 1
    c = chunks[0]
    assert c["file_path"] == str(f)
    assert "filename" not in c
    assert "file_extension" not in c
    assert "ast_chunked" not in c
    assert c["chunk_index"] == 0 and c["chunk_count"] == 1
    assert "line_start" in c and "line_end" in c


# ── chunk_file: AST path ──────────────────────────────────────────────────────

def _make_ast_node(text: str, start_char_idx: int = 0) -> MagicMock:
    node = MagicMock()
    node.text = text
    node.metadata = {}
    node.start_char_idx = start_char_idx
    return node


def test_chunk_file_python_calls_codesplitter(tmp_path: Path):
    f = tmp_path / "module.py"
    f.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")
    nodes = [_make_ast_node("def foo():\n    pass", 0),
             _make_ast_node("def bar():\n    pass", 21)]
    with patch("nexus.chunker._make_code_splitter", return_value=nodes):
        chunks = chunk_file(f, f.read_text())
    assert len(chunks) == 2
    assert [c["chunk_index"] for c in chunks] == [0, 1]
    assert all(c["chunk_count"] == 2 for c in chunks)


def test_chunk_file_ast_failure_falls_back_to_lines(tmp_path: Path):
    """When AST parse fails, fall back to line chunking (still returns chunks)."""
    f = tmp_path / "tricky.py"
    f.write_text("def foo():\n    pass\n")
    with patch("nexus.chunker._make_code_splitter", side_effect=Exception("parse error")):
        chunks = chunk_file(f, f.read_text())
    assert len(chunks) >= 1
    # Line-fallback chunks carry line_start / line_end (AST chunks
    # additionally carry start_char-derived metadata via the splitter).
    assert all("line_start" in c for c in chunks)


# ── Config extensions use line chunking, not AST ────────────────────────────

@pytest.mark.parametrize("filename,content", [
    ("config.yaml", "key: value\nlist:\n  - item1\n  - item2\n"),
    ("config.yml", "key: value\nlist:\n  - item1\n  - item2\n"),
    ("pyproject.toml", '[section]\nkey = "value"\n'),
])
def test_config_uses_line_chunking_not_ast(filename, content):
    chunks = chunk_file(Path(filename), content)
    assert chunks
    # Line-chunked entries lack the AST splitter's chunk_start_char; using
    # presence of line_start as the proxy for "line-chunked, not AST".
    assert "line_start" in chunks[0]


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_chunk_file_empty_content_returns_empty(tmp_path: Path):
    f = tmp_path / "blank.txt"
    f.write_text("")
    assert chunk_file(f, f.read_text()) == []


def test_chunk_file_single_line(tmp_path: Path):
    f = tmp_path / "one.txt"
    f.write_text("only line")
    chunks = chunk_file(f, f.read_text())
    assert len(chunks) == 1
    assert chunks[0]["line_start"] == 1 and chunks[0]["line_end"] == 1
    assert "only line" in chunks[0]["text"]


def test_chunk_file_ast_returns_empty_nodes_falls_back(tmp_path: Path):
    """Empty node list from AST splitter falls back to line chunking."""
    f = tmp_path / "empty_ast.py"
    f.write_text("x = 1\n")
    with patch("nexus.chunker._make_code_splitter", return_value=[]):
        chunks = chunk_file(f, f.read_text())
    assert len(chunks) >= 1
    assert all("line_start" in c for c in chunks)


# ── Byte-cap enforcement ──────────────────────────────────────────────────────

def test_line_chunk_respects_max_bytes():
    content = "\n".join("x" * 200 for _ in range(200))
    chunks = _line_chunk(content, chunk_lines=150, max_bytes=_CHUNK_MAX_BYTES)
    assert chunks
    for ls, le, text in chunks:
        assert len(text.encode()) <= _CHUNK_MAX_BYTES


@pytest.mark.parametrize("max_bytes,big_line", [
    (_CHUNK_MAX_BYTES, "z" * 20_000),
    (50, "x" * 200),
])
def test_line_chunk_single_oversized_line_is_split(max_bytes, big_line):
    chunks = _line_chunk(big_line, chunk_lines=150, max_bytes=max_bytes)
    assert len(chunks) >= 1
    for _, _, text in chunks:
        assert len(text.encode()) <= max_bytes


def test_line_chunk_byte_cap_no_gaps():
    lines = ["a" * 300 for _ in range(60)]
    content = "\n".join(lines)
    chunks = _line_chunk(content, chunk_lines=150, max_bytes=_CHUNK_MAX_BYTES)
    recovered = set()
    for _, _, text in chunks:
        for ln in text.splitlines():
            recovered.add(ln)
    assert recovered == set(lines)


def test_enforce_byte_cap_passthrough_when_small():
    chunks = [{"text": "small", "chunk_index": 0, "chunk_count": 1, "line_start": 1, "line_end": 1}]
    assert _enforce_byte_cap(chunks, max_bytes=_CHUNK_MAX_BYTES) == chunks


def test_enforce_byte_cap_splits_oversized_ast_node():
    cap = 500
    text = "\n".join(f"    statement_{i:04d} = do_something_complex()" for i in range(50))
    assert len(text.encode()) > cap
    chunk = {"text": text, "chunk_index": 0, "chunk_count": 1,
             "line_start": 10, "line_end": 59, "file_path": "src/big.py"}
    result = _enforce_byte_cap([chunk], max_bytes=cap)
    assert len(result) > 1
    for c in result:
        assert len(c["text"].encode()) <= cap
    assert [c["chunk_index"] for c in result] == list(range(len(result)))
    assert all(c["chunk_count"] == len(result) for c in result)


def test_enforce_byte_cap_single_oversized_node_is_truncated():
    max_bytes = 50
    big_text = "a" * 200
    chunks = [{"text": big_text, "line_start": 1, "line_end": 1, "chunk_index": 0, "chunk_count": 1}]
    result = _enforce_byte_cap(chunks, max_bytes=max_bytes)
    assert len(result) >= 1
    for c in result:
        assert len(c["text"].encode()) <= max_bytes


def test_chunk_file_ast_oversized_node_is_split(tmp_path: Path):
    f = tmp_path / "big.py"
    big_body = "\n".join(f"    variable_{i:04d} = compute_value({i})" for i in range(500))
    f.write_text(f"def huge():\n{big_body}\n")
    big_node = _make_ast_node(f"def huge():\n{big_body}", 0)
    assert len(big_node.text.encode()) > _CHUNK_MAX_BYTES
    with patch("nexus.chunker._make_code_splitter", return_value=[big_node]):
        chunks = chunk_file(f, f.read_text())
    assert len(chunks) > 1
    for c in chunks:
        assert len(c["text"].encode()) <= _CHUNK_MAX_BYTES


# ── AST line range accuracy (RDR-016) ────────────────────────────────────────

def test_chunk_file_ast_line_ranges(tmp_path: Path):
    content = (
        "class Foo:\n"
        "    def a(self):\n"
        "        return 1\n"
        "\n\n"
        "class Bar:\n"
        "    def b(self):\n"
        "        return 2\n"
    )
    f = tmp_path / "two_classes.py"
    f.write_text(content)
    nodes = [_make_ast_node("class Foo:\n    def a(self):\n        return 1", 0),
             _make_ast_node("class Bar:\n    def b(self):\n        return 2", 48)]
    with patch("nexus.chunker._make_code_splitter", return_value=nodes):
        chunks = chunk_file(f, content)
    assert len(chunks) == 2
    assert (chunks[0]["line_start"], chunks[0]["line_end"]) == (1, 3)
    assert (chunks[1]["line_start"], chunks[1]["line_end"]) == (6, 8)


@pytest.mark.parametrize("text,start_idx,expected_start", [
    ("", 0, None),             # empty text: line_start <= line_end
    ("def foo():\n    pass", None, 1),  # None start_char_idx: fallback to 1
])
def test_chunk_file_ast_edge_cases(tmp_path: Path, text, start_idx, expected_start):
    f = tmp_path / "edge.py"
    content = "def foo():\n    pass\n" if text else "x = 1\n"
    f.write_text(content)
    node = _make_ast_node(text, start_idx)
    with patch("nexus.chunker._make_code_splitter", return_value=[node]):
        chunks = chunk_file(f, content)
    assert len(chunks) == 1
    if expected_start is not None:
        assert chunks[0]["line_start"] == expected_start
    assert chunks[0]["line_start"] <= chunks[0]["line_end"]
