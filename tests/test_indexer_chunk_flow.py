# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix E5 / nexus-eae0: _extract_context unit tests.

Tests _extract_context from nexus.indexer (introduced in Fix A / nexus-2tob):
- Python class + method: returns ('MyClass', 'my_method')
- Java class + method: returns ('BankAccount', 'deposit')
- Unsupported language: returns ('', '')
- Chunk spanning two sibling methods: returns ('Foo', '')
- Decorated Python function: returns method name from wrapped function_definition
"""
import pytest

try:
    from tree_sitter_language_pack import get_parser  # noqa: F401
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

from nexus.indexer import _extract_context

pytestmark = pytest.mark.skipif(
    not TREE_SITTER_AVAILABLE,
    reason="tree-sitter-language-pack not available",
)


# ── Python ─────────────────────────────────────────────────────────────────────

def test_extract_context_python() -> None:
    """Python class + method: chunk inside method body returns both names."""
    source = b"""class MyClass:
    def my_method(self):
        x = 1
        return x
"""
    # 0-indexed: class spans 0-3, method spans 1-3, chunk is lines 2-3
    result = _extract_context(source, "python", 2, 3)
    assert result == ("MyClass", "my_method"), f"Got {result!r}"


# ── Java ───────────────────────────────────────────────────────────────────────

def test_extract_context_java() -> None:
    """Java class + method: chunk inside method body returns both names."""
    source = b"""public class BankAccount {
    private double balance;

    public void deposit(double amount) {
        if (amount <= 0) {
            throw new IllegalArgumentException("Deposit must be positive");
        }
        balance += amount;
    }
}
"""
    # class spans 0-9, deposit method spans 3-8, chunk is lines 5-7
    result = _extract_context(source, "java", 5, 7)
    assert result == ("BankAccount", "deposit"), f"Got {result!r}"


# ── Unsupported language ───────────────────────────────────────────────────────

def test_extract_context_unsupported_language() -> None:
    """Unknown language key returns ('', '') without raising."""
    result = _extract_context(b"x = 1\ny = 2\n", "cobol", 0, 1)
    assert result == ("", ""), f"Expected ('', ''), got {result!r}"


# ── Multi-method span ──────────────────────────────────────────────────────────

def test_extract_context_multi_method_span() -> None:
    """Chunk spanning two sibling methods yields class but empty method name.

    When neither method fully encloses the chunk, method_name stays ''.
    Only the enclosing class should be returned.
    """
    source = b"""class Foo:
    def alpha(self):
        return 1

    def beta(self):
        return 2
"""
    # alpha spans lines 1-2, beta spans lines 4-5
    # chunk 1-5 spans both — no single method fully encloses it
    result = _extract_context(source, "python", 1, 5)
    assert result[0] == "Foo", f"Expected class='Foo', got {result!r}"
    assert result[1] == "", f"Expected method='', got {result!r} (chunk spans two methods)"


# ── Decorated Python functions ─────────────────────────────────────────────────

def test_extract_context_decorated_python_function() -> None:
    """Decorated Python function: chunk inside body returns correct method name.

    Regression for C3: decorated_definition nodes have no direct identifier
    child — the name is on the wrapped function_definition.  _extract_name_from_node
    must recurse into the inner node to return the correct name.
    """
    source = b"""class MyService:
    @staticmethod
    def process(data):
        return data.strip()
"""
    # decorated_definition spans lines 1-3, function_definition spans lines 2-3
    # chunk is lines 3 (return statement)
    result = _extract_context(source, "python", 3, 3)
    assert result == ("MyService", "process"), (
        f"Decorated method name not extracted: got {result!r}"
    )
