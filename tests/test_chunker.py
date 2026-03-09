"""AC5/AC6: AST chunking and line-based fallback chunking."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.chunker import chunk_file, _enforce_byte_cap, _line_chunk, _CHUNK_MAX_BYTES


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

def test_chunk_file_custom_chunk_lines_produces_more_chunks(tmp_path: Path) -> None:
    """chunk_file() with a smaller chunk_lines produces more chunks than the default."""
    f = tmp_path / "code.xyz"  # unknown ext → line fallback
    content = "\n".join(f"line {i}" for i in range(300))
    f.write_text(content)

    chunks_50 = chunk_file(f, content, chunk_lines=50)
    chunks_150 = chunk_file(f, content, chunk_lines=150)

    assert len(chunks_50) > len(chunks_150)


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
    mock_node1.start_char_idx = 0  # starts at beginning of file
    mock_node2 = MagicMock()
    mock_node2.text = "def bar():\n    pass"
    mock_node2.metadata = {}
    mock_node2.start_char_idx = 21  # "def foo():\n    pass\n\n" = 21 chars

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


# ── Config extensions use line chunking, not AST ────────────────────────────


def test_yaml_uses_line_chunking_not_ast() -> None:
    """YAML files should use line-based chunking, not AST (they're prose now)."""
    content = "key: value\nlist:\n  - item1\n  - item2\n"
    chunks = chunk_file(Path("config.yaml"), content)
    assert chunks
    assert not chunks[0].get("ast_chunked", False)


def test_yml_uses_line_chunking_not_ast() -> None:
    """YML files should use line-based chunking, not AST (they're prose now)."""
    content = "key: value\nlist:\n  - item1\n  - item2\n"
    chunks = chunk_file(Path("config.yml"), content)
    assert chunks
    assert not chunks[0].get("ast_chunked", False)


def test_toml_uses_line_chunking_not_ast() -> None:
    """TOML files should use line-based chunking, not AST (they're prose now)."""
    content = '[section]\nkey = "value"\n'
    chunks = chunk_file(Path("pyproject.toml"), content)
    assert chunks
    assert not chunks[0].get("ast_chunked", False)


# ── Edge cases: empty / whitespace / single-line ─────────────────────────────


def test_line_chunk_empty_string_returns_empty() -> None:
    assert _line_chunk("") == []


def test_chunk_file_empty_content_returns_empty(tmp_path: Path) -> None:
    """Truly empty content (no lines at all) produces no chunks."""
    f = tmp_path / "blank.txt"
    f.write_text("")
    assert chunk_file(f, f.read_text()) == []


def test_chunk_file_single_line(tmp_path: Path) -> None:
    """Single-line file produces exactly one chunk."""
    f = tmp_path / "one.txt"
    f.write_text("only line")
    chunks = chunk_file(f, f.read_text())
    assert len(chunks) == 1
    assert chunks[0]["line_start"] == 1
    assert chunks[0]["line_end"] == 1
    assert "only line" in chunks[0]["text"]


def test_chunk_file_ast_returns_empty_nodes_falls_back(tmp_path: Path) -> None:
    """When AST splitter returns empty node list, fall back to line chunks."""
    f = tmp_path / "empty_ast.py"
    f.write_text("x = 1\n")
    with patch("nexus.chunker._make_code_splitter", return_value=[]):
        chunks = chunk_file(f, f.read_text())
    assert len(chunks) >= 1
    assert all(c["ast_chunked"] is False for c in chunks)


def test_line_chunk_file_shorter_than_chunk_lines() -> None:
    """File with fewer lines than chunk_lines → single chunk covering all lines."""
    content = "\n".join(f"line {i}" for i in range(5))
    chunks = _line_chunk(content, chunk_lines=150)
    assert len(chunks) == 1
    assert chunks[0][0] == 1
    assert chunks[0][1] == 5


def test_line_chunk_exact_chunk_lines() -> None:
    """File with exactly chunk_lines lines → single chunk."""
    content = "\n".join(f"line {i}" for i in range(10))
    chunks = _line_chunk(content, chunk_lines=10)
    assert len(chunks) == 1
    assert chunks[0][1] == 10


# ── Byte-cap enforcement ──────────────────────────────────────────────────────

def test_line_chunk_respects_max_bytes() -> None:
    """Every chunk produced must be ≤ max_bytes when lines are long."""
    # 200 lines, each 200 bytes → 200-line window = 40 000 bytes, well over 16 000.
    content = "\n".join("x" * 200 for _ in range(200))
    chunks = _line_chunk(content, chunk_lines=150, max_bytes=_CHUNK_MAX_BYTES)
    assert chunks
    for ls, le, text in chunks:
        assert len(text.encode()) <= _CHUNK_MAX_BYTES, (
            f"Chunk lines {ls}-{le} is {len(text.encode())} bytes (limit {_CHUNK_MAX_BYTES})"
        )


def test_line_chunk_single_oversized_line_is_split_at_cap() -> None:
    """A single line larger than max_bytes is split into multiple chunks (not truncated)."""
    big_line = "z" * 20_000  # 20 KB — exceeds SAFE_CHUNK_BYTES
    chunks = _line_chunk(big_line, chunk_lines=150, max_bytes=_CHUNK_MAX_BYTES)
    assert len(chunks) >= 1
    for _, _, text in chunks:
        assert len(text.encode()) <= _CHUNK_MAX_BYTES


def test_line_chunk_byte_cap_no_gaps() -> None:
    """With very long lines, byte-capping must not drop any lines."""
    lines = ["a" * 300 for _ in range(60)]  # each chunk is ~300*N bytes
    content = "\n".join(lines)
    chunks = _line_chunk(content, chunk_lines=150, max_bytes=_CHUNK_MAX_BYTES)
    # Reassemble: all original lines must appear in at least one chunk
    recovered = set()
    for _, _, text in chunks:
        for ln in text.splitlines():
            recovered.add(ln)
    assert recovered == set(lines)


def test_enforce_byte_cap_passthrough_when_small() -> None:
    """Chunks already under the limit pass through unchanged."""
    chunks = [{"text": "small", "chunk_index": 0, "chunk_count": 1, "line_start": 1, "line_end": 1}]
    result = _enforce_byte_cap(chunks, max_bytes=_CHUNK_MAX_BYTES)
    assert result == chunks


def test_enforce_byte_cap_splits_oversized_ast_node() -> None:
    """An AST node exceeding the byte cap is split into sub-chunks."""
    # Use a small cap so test data stays reasonable.
    cap = 500
    text = "\n".join(f"    statement_{i:04d} = do_something_complex()" for i in range(50))
    assert len(text.encode()) > cap, "test data must actually exceed the cap"
    chunk = {
        "text": text,
        "chunk_index": 0,
        "chunk_count": 1,
        "line_start": 10,
        "line_end": 59,
        "ast_chunked": True,
        "file_path": "src/big.py",
    }
    result = _enforce_byte_cap([chunk], max_bytes=cap)
    assert len(result) > 1
    for c in result:
        assert len(c["text"].encode()) <= cap
    # chunk_index and chunk_count renumbered
    assert [c["chunk_index"] for c in result] == list(range(len(result)))
    assert all(c["chunk_count"] == len(result) for c in result)


def test_chunk_file_ast_oversized_node_is_split(tmp_path: Path) -> None:
    """chunk_file splits an oversized AST node via _enforce_byte_cap."""
    f = tmp_path / "big.py"
    # ~44 chars/line × 500 lines ≈ 22 000 bytes > _CHUNK_MAX_BYTES
    big_body = "\n".join(f"    variable_{i:04d} = compute_value({i})" for i in range(500))
    f.write_text(f"def huge():\n{big_body}\n")

    big_node = MagicMock()
    big_node.text = f"def huge():\n{big_body}"
    assert len(big_node.text.encode()) > _CHUNK_MAX_BYTES, "test data must exceed cap"
    big_node.metadata = {}
    big_node.start_char_idx = 0  # single node, starts at file beginning

    with patch("nexus.chunker._make_code_splitter", return_value=[big_node]):
        chunks = chunk_file(f, f.read_text())

    assert len(chunks) > 1
    for c in chunks:
        assert len(c["text"].encode()) <= _CHUNK_MAX_BYTES


# ── AST line range accuracy (RDR-016) ────────────────────────────────────────

def test_chunk_file_ast_line_ranges(tmp_path: Path) -> None:
    """AST chunks report accurate per-chunk line ranges, not the whole-file extent.

    With the bug: every chunk from a file gets line_start=1, line_end=<total lines>.
    With the fix: each chunk's line_start and line_end reflect the actual code slice.
    """
    # 8-line file: Foo on lines 1-3, blank lines 4-5, Bar on lines 6-8
    content = (
        "class Foo:\n"           # line 1, char  0
        "    def a(self):\n"     # line 2, char 11
        "        return 1\n"     # line 3, char 28
        "\n"                     # line 4, char 46
        "\n"                     # line 5, char 47
        "class Bar:\n"           # line 6, char 48
        "    def b(self):\n"     # line 7, char 59
        "        return 2\n"     # line 8, char 76
    )
    f = tmp_path / "two_classes.py"
    f.write_text(content)

    node1 = MagicMock()
    node1.text = "class Foo:\n    def a(self):\n        return 1"
    node1.metadata = {}
    node1.start_char_idx = 0  # char 0 → line 1

    node2 = MagicMock()
    node2.text = "class Bar:\n    def b(self):\n        return 2"
    node2.metadata = {}
    node2.start_char_idx = 48  # char 48 → line 6 (5 newlines precede it)

    with patch("nexus.chunker._make_code_splitter", return_value=[node1, node2]):
        chunks = chunk_file(f, content)

    assert len(chunks) == 2

    # Chunk 0: class Foo (lines 1-3)
    assert chunks[0]["line_start"] == 1, f"expected 1, got {chunks[0]['line_start']}"
    assert chunks[0]["line_end"] == 3, f"expected 3, got {chunks[0]['line_end']}"

    # Chunk 1: class Bar (lines 6-8)
    assert chunks[1]["line_start"] == 6, f"expected 6, got {chunks[1]['line_start']}"
    assert chunks[1]["line_end"] == 8, f"expected 8, got {chunks[1]['line_end']}"


def test_chunk_file_ast_empty_text_node(tmp_path: Path) -> None:
    """An empty-text AST node must not produce line_end < line_start."""
    f = tmp_path / "empty_node.py"
    content = "x = 1\n"
    f.write_text(content)

    mock_node = MagicMock()
    mock_node.text = ""
    mock_node.metadata = {}
    mock_node.start_char_idx = 0

    with patch("nexus.chunker._make_code_splitter", return_value=[mock_node]):
        chunks = chunk_file(f, content)

    assert len(chunks) == 1
    assert chunks[0]["line_start"] <= chunks[0]["line_end"]


def test_chunk_file_ast_none_start_char_idx(tmp_path: Path) -> None:
    """When node.start_char_idx is None, falls back to line_start=1 without error."""
    f = tmp_path / "none_idx.py"
    content = "def foo():\n    pass\n"
    f.write_text(content)

    mock_node = MagicMock()
    mock_node.text = "def foo():\n    pass"
    mock_node.metadata = {}
    mock_node.start_char_idx = None  # explicit None → defensive fallback path

    with patch("nexus.chunker._make_code_splitter", return_value=[mock_node]):
        chunks = chunk_file(f, content)

    assert len(chunks) == 1
    assert chunks[0]["line_start"] == 1
    assert chunks[0]["line_end"] >= chunks[0]["line_start"]


# ── Phase 2a: single-line truncation (escape hatch fix) ───────────────────────

def test_line_chunk_single_oversized_line_is_split() -> None:
    """A single line exceeding max_bytes is split into multiple chunks."""
    max_bytes = 50
    big_line = "x" * 200  # 200 bytes > 50
    chunks = _line_chunk(big_line, chunk_lines=150, max_bytes=max_bytes)
    assert len(chunks) >= 1
    for _, _, text in chunks:
        assert len(text.encode()) <= max_bytes, (
            f"Each chunk must fit in max_bytes: got {len(text.encode())} bytes (limit {max_bytes})"
        )


def test_enforce_byte_cap_single_oversized_node_is_truncated() -> None:
    """A single-line AST node exceeding max_bytes must be truncated, not emitted as-is."""
    max_bytes = 50
    big_text = "a" * 200  # 200 bytes > 50, single line (no newlines)
    chunks = [{"text": big_text, "line_start": 1, "line_end": 1, "chunk_index": 0, "chunk_count": 1}]
    result = _enforce_byte_cap(chunks, max_bytes=max_bytes)
    assert len(result) >= 1
    for c in result:
        assert len(c["text"].encode()) <= max_bytes, (
            f"Single-line oversized node must be truncated: got {len(c['text'].encode())} bytes (limit {max_bytes})"
        )
