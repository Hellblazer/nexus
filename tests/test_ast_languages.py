# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for AST-based code chunking across multiple languages."""
from pathlib import Path

from nexus.chunker import chunk_file


# ── Python ──────────────────────────────────────────────────────────────────────

def test_python_function_extraction():
    """chunk_file extracts Python function definitions with correct metadata."""
    src = "def hello(name: str) -> str:\n    return f'Hello {name}'\n\ndef goodbye():\n    pass\n"
    chunks = chunk_file(Path("example.py"), src)
    assert len(chunks) >= 1
    assert chunks[0]["file_path"].endswith(".py")
    assert any("hello" in c["text"] for c in chunks)


def test_python_class_extraction():
    """chunk_file extracts Python class definitions."""
    src = (
        "class Greeter:\n"
        "    def __init__(self, name: str):\n"
        "        self.name = name\n"
        "\n"
        "    def greet(self) -> str:\n"
        "        return f'Hi {self.name}'\n"
    )
    chunks = chunk_file(Path("greeter.py"), src)
    assert len(chunks) >= 1
    assert any("Greeter" in c["text"] for c in chunks)


def test_python_empty_file():
    """chunk_file handles an empty Python file gracefully."""
    chunks = chunk_file(Path("empty.py"), "")
    assert isinstance(chunks, list)
    assert len(chunks) == 0


# ── JavaScript ──────────────────────────────────────────────────────────────────

def test_javascript_function_extraction():
    """chunk_file extracts JavaScript function/class definitions."""
    src = (
        "function greet(name) {\n"
        "  return 'Hello ' + name;\n"
        "}\n"
        "\n"
        "class Animal {\n"
        "  constructor(name) {\n"
        "    this.name = name;\n"
        "  }\n"
        "  speak() {\n"
        "    return this.name;\n"
        "  }\n"
        "}\n"
    )
    chunks = chunk_file(Path("app.js"), src)
    assert len(chunks) >= 1
    assert chunks[0]["file_path"].endswith(".js")
    assert any("greet" in c["text"] or "Animal" in c["text"] for c in chunks)


# ── TypeScript ──────────────────────────────────────────────────────────────────

def test_typescript_function_extraction():
    """chunk_file extracts TypeScript typed function definitions."""
    src = (
        "interface Config {\n"
        "  host: string;\n"
        "  port: number;\n"
        "}\n"
        "\n"
        "function createServer(config: Config): void {\n"
        "  console.log(config.host);\n"
        "}\n"
    )
    chunks = chunk_file(Path("server.ts"), src)
    assert len(chunks) >= 1
    assert chunks[0]["file_path"].endswith(".ts")


# ── Go ──────────────────────────────────────────────────────────────────────────

def test_go_function_extraction():
    """chunk_file extracts Go function and struct definitions."""
    src = (
        "package main\n"
        "\n"
        "type Server struct {\n"
        "    Host string\n"
        "    Port int\n"
        "}\n"
        "\n"
        "func (s *Server) Start() error {\n"
        "    return nil\n"
        "}\n"
        "\n"
        "func NewServer(host string, port int) *Server {\n"
        "    return &Server{Host: host, Port: port}\n"
        "}\n"
    )
    chunks = chunk_file(Path("main.go"), src)
    assert len(chunks) >= 1
    assert chunks[0]["file_path"].endswith(".go")
    assert any("Server" in c["text"] for c in chunks)


# ── Rust ────────────────────────────────────────────────────────────────────────

def test_rust_function_extraction():
    """chunk_file extracts Rust fn and impl block definitions."""
    src = (
        "struct Config {\n"
        "    host: String,\n"
        "    port: u16,\n"
        "}\n"
        "\n"
        "impl Config {\n"
        "    fn new(host: String, port: u16) -> Self {\n"
        "        Config { host, port }\n"
        "    }\n"
        "}\n"
        "\n"
        "fn main() {\n"
        "    let cfg = Config::new(\"localhost\".to_string(), 8080);\n"
        "}\n"
    )
    chunks = chunk_file(Path("main.rs"), src)
    assert len(chunks) >= 1
    assert chunks[0]["file_path"].endswith(".rs")


# ── Java ────────────────────────────────────────────────────────────────────────

def test_java_class_extraction():
    """chunk_file extracts Java class and method definitions."""
    src = (
        "public class Calculator {\n"
        "    private int value;\n"
        "\n"
        "    public Calculator(int initial) {\n"
        "        this.value = initial;\n"
        "    }\n"
        "\n"
        "    public int add(int n) {\n"
        "        this.value += n;\n"
        "        return this.value;\n"
        "    }\n"
        "}\n"
    )
    chunks = chunk_file(Path("Calculator.java"), src)
    assert len(chunks) >= 1
    assert chunks[0]["file_path"].endswith(".java")
    assert any("Calculator" in c["text"] for c in chunks)


# ── Metadata fields ─────────────────────────────────────────────────────────────

def test_chunk_metadata_fields():
    """Every chunk carries the keys the indexer factory consumes.

    nexus-59j0 dropped ``filename`` / ``file_extension`` / ``ast_chunked``
    (cargo — the indexer factory ignored them, normalize() dropped them
    from T3 storage)."""
    src = "def foo():\n    pass\n\ndef bar():\n    pass\n"
    chunks = chunk_file(Path("meta.py"), src)
    required = {"file_path",
                "chunk_index", "chunk_count", "line_start", "line_end", "text"}
    for chunk in chunks:
        assert required.issubset(chunk.keys()), f"Missing keys: {required - set(chunk.keys())}"
        assert "filename" not in chunk
        assert "file_extension" not in chunk
        assert "ast_chunked" not in chunk


def test_chunk_line_numbers_are_valid():
    """line_start and line_end are positive integers with start <= end."""
    src = "class A:\n    def m(self):\n        return 1\n\ndef standalone():\n    pass\n"
    chunks = chunk_file(Path("lines.py"), src)
    for chunk in chunks:
        assert chunk["line_start"] >= 1
        assert chunk["line_end"] >= chunk["line_start"]
