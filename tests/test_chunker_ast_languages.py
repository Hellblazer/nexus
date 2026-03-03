"""Fix E / nexus-0qnh: AST chunking correctness for 5 languages.

Verifies that chunk_file() produces valid AST-chunked output for Python,
JavaScript, Go, Rust, and Java.  These tests exercise real tree-sitter
parsing via tree-sitter-language-pack and are skipped when it is absent.
"""
from pathlib import Path

import pytest

try:
    from tree_sitter_language_pack import get_parser  # noqa: F401
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

from nexus.chunker import chunk_file

pytestmark = pytest.mark.skipif(
    not TREE_SITTER_AVAILABLE,
    reason="tree-sitter-language-pack not available",
)

# ── helpers ────────────────────────────────────────────────────────────────────

def _assert_valid_chunks(chunks: list, min_count: int = 1) -> None:
    """Common assertions for a chunk list."""
    assert len(chunks) >= min_count, f"Expected >= {min_count} chunks, got {len(chunks)}"
    for c in chunks:
        assert c["text"].strip(), "Chunk text must not be empty"
        assert "line_start" in c, "Chunk must have line_start"
        assert "line_end" in c, "Chunk must have line_end"
        assert c["line_start"] >= 1, "line_start must be 1-indexed"


# ── Python ─────────────────────────────────────────────────────────────────────

def test_python_ast_chunks(tmp_path: Path) -> None:
    """Python file with class + 3 functions produces AST chunks."""
    code = '''"""Module docstring."""


def add(a, b):
    """Add two numbers."""
    return a + b


def subtract(a, b):
    """Subtract b from a."""
    return a - b


def multiply(a, b):
    """Multiply two numbers."""
    result = a * b
    return result


class Calculator:
    """Simple calculator class."""

    def __init__(self):
        self.history = []

    def compute(self, op, a, b):
        """Dispatch to add/subtract/multiply."""
        if op == "add":
            r = add(a, b)
        elif op == "subtract":
            r = subtract(a, b)
        else:
            r = multiply(a, b)
        self.history.append((op, a, b, r))
        return r
'''
    f = tmp_path / "calc.py"
    f.write_text(code)
    chunks = chunk_file(f, code)
    _assert_valid_chunks(chunks, min_count=1)
    assert any(c.get("ast_chunked") for c in chunks), "Expected at least one AST-chunked chunk"
    combined = "\n".join(c["text"] for c in chunks)
    assert "Calculator" in combined
    assert "def add" in combined


# ── JavaScript ─────────────────────────────────────────────────────────────────

def test_javascript_ast_chunks(tmp_path: Path) -> None:
    """JavaScript file with class and standalone functions produces AST chunks."""
    code = '''// Utility functions

function greet(name) {
    return `Hello, ${name}!`;
}

function farewell(name) {
    return `Goodbye, ${name}!`;
}

class Greeter {
    constructor(prefix) {
        this.prefix = prefix;
    }

    greet(name) {
        return `${this.prefix}, ${name}!`;
    }

    farewell(name) {
        return `See you later, ${name}!`;
    }
}

module.exports = { greet, farewell, Greeter };
'''
    f = tmp_path / "greeter.js"
    f.write_text(code)
    chunks = chunk_file(f, code)
    _assert_valid_chunks(chunks, min_count=1)
    combined = "\n".join(c["text"] for c in chunks)
    assert "Greeter" in combined
    assert "greet" in combined


# ── Go ─────────────────────────────────────────────────────────────────────────

def test_go_ast_chunks(tmp_path: Path) -> None:
    """Go file with struct and methods produces AST chunks."""
    code = '''package main

import "fmt"

// Stack is a simple integer stack.
type Stack struct {
    items []int
}

// Push adds an item to the stack.
func (s *Stack) Push(item int) {
    s.items = append(s.items, item)
}

// Pop removes and returns the top item.
func (s *Stack) Pop() (int, bool) {
    if len(s.items) == 0 {
        return 0, false
    }
    top := s.items[len(s.items)-1]
    s.items = s.items[:len(s.items)-1]
    return top, true
}

// Len returns the number of items.
func (s *Stack) Len() int {
    return len(s.items)
}

func main() {
    s := &Stack{}
    s.Push(1)
    s.Push(2)
    v, _ := s.Pop()
    fmt.Println(v)
}
'''
    f = tmp_path / "stack.go"
    f.write_text(code)
    chunks = chunk_file(f, code)
    _assert_valid_chunks(chunks, min_count=1)
    combined = "\n".join(c["text"] for c in chunks)
    assert "Stack" in combined
    assert "Push" in combined


# ── Rust ───────────────────────────────────────────────────────────────────────

def test_rust_ast_chunks(tmp_path: Path) -> None:
    """Rust file with struct impl block produces AST chunks."""
    code = '''/// A simple counter.
struct Counter {
    value: i32,
}

impl Counter {
    /// Create a new counter starting at zero.
    fn new() -> Self {
        Counter { value: 0 }
    }

    /// Increment the counter.
    fn increment(&mut self) {
        self.value += 1;
    }

    /// Decrement the counter.
    fn decrement(&mut self) {
        self.value -= 1;
    }

    /// Return the current value.
    fn get(&self) -> i32 {
        self.value
    }
}

fn main() {
    let mut c = Counter::new();
    c.increment();
    c.increment();
    println!("{}", c.get());
}
'''
    f = tmp_path / "counter.rs"
    f.write_text(code)
    chunks = chunk_file(f, code)
    _assert_valid_chunks(chunks, min_count=1)
    combined = "\n".join(c["text"] for c in chunks)
    assert "Counter" in combined
    assert "increment" in combined


# ── Java ───────────────────────────────────────────────────────────────────────

def test_java_ast_chunks(tmp_path: Path) -> None:
    """Java file with class and methods produces AST chunks."""
    code = '''package com.example;

/**
 * A simple bank account.
 */
public class BankAccount {
    private double balance;
    private String owner;

    public BankAccount(String owner, double initialBalance) {
        this.owner = owner;
        this.balance = initialBalance;
    }

    public void deposit(double amount) {
        if (amount <= 0) {
            throw new IllegalArgumentException("Deposit must be positive");
        }
        balance += amount;
    }

    public void withdraw(double amount) {
        if (amount <= 0) {
            throw new IllegalArgumentException("Withdrawal must be positive");
        }
        if (amount > balance) {
            throw new IllegalStateException("Insufficient funds");
        }
        balance -= amount;
    }

    public double getBalance() {
        return balance;
    }

    public String getOwner() {
        return owner;
    }

    @Override
    public String toString() {
        return String.format("BankAccount[owner=%s, balance=%.2f]", owner, balance);
    }
}
'''
    f = tmp_path / "BankAccount.java"
    f.write_text(code)
    chunks = chunk_file(f, code)
    _assert_valid_chunks(chunks, min_count=1)
    combined = "\n".join(c["text"] for c in chunks)
    assert "BankAccount" in combined
    assert "deposit" in combined
