# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1060 — actionable error when collection name overflows 63-char cap.

The scenario: relabeling a voyage-named collection to a local bge token can
push the name past 63 characters when the owner-id is long.  The old error
was a bare ``ValueError: Collection name '...' must be 3–63 characters (got 64)``
with no guidance on how to fix it.

The fix must:
  (a) state the length problem (the 63-char cap was exceeded), and
  (b) reference ``nx catalog collection-name`` so the operator knows how to
      derive a repo-id-conformant name that fits under the cap.

The fix must NOT affect valid (≤63-char) names.
"""
from __future__ import annotations

import pytest

from nexus.corpus import validate_collection_name


# ── Overflow scenario: the exact name from the issue ──────────────────────────

# code__conductor-sys-monitoring-b25083f0__bge-base-en-v15-768__v1 = 64 chars
_OVERFLOW_NAME = "code__conductor-sys-monitoring-b25083f0__bge-base-en-v15-768__v1"


def test_overflow_name_is_actually_64_chars() -> None:
    """Sanity: confirm the fixture name is exactly 64 characters."""
    assert len(_OVERFLOW_NAME) == 64


def test_overflow_raises_value_error() -> None:
    """A >63-char name must raise ValueError (the ChromaDB hard cap)."""
    with pytest.raises(ValueError):
        validate_collection_name(_OVERFLOW_NAME)


def test_overflow_error_mentions_length_problem() -> None:
    """Error message must communicate that the 63-char cap was exceeded."""
    with pytest.raises(ValueError, match="63"):
        validate_collection_name(_OVERFLOW_NAME)


def test_overflow_error_references_catalog_collection_name_command() -> None:
    """Error message must point to ``nx catalog collection-name`` as the remedy."""
    with pytest.raises(ValueError, match=r"nx catalog collection-name"):
        validate_collection_name(_OVERFLOW_NAME)


def test_overflow_error_contains_both_signals() -> None:
    """Both the length problem and the remediation hint appear in one error."""
    with pytest.raises(ValueError) as exc_info:
        validate_collection_name(_OVERFLOW_NAME)
    msg = str(exc_info.value)
    assert "63" in msg, f"expected '63' in error message, got: {msg!r}"
    assert "nx catalog collection-name" in msg, (
        f"expected 'nx catalog collection-name' in error message, got: {msg!r}"
    )


# ── Content-type prefix extracted in the hint ─────────────────────────────────

def test_overflow_error_includes_content_type_hint_code() -> None:
    """For a ``code__`` name that overflows, the hint mentions ``--content-type code``."""
    with pytest.raises(ValueError) as exc_info:
        validate_collection_name(_OVERFLOW_NAME)
    assert "code" in str(exc_info.value)


def test_overflow_error_includes_content_type_hint_knowledge() -> None:
    """For a ``knowledge__`` name that overflows, the hint mentions ``--content-type knowledge``."""
    knowledge_overflow = "knowledge__conductor-sys-monitoring-b25083f0__bge-base-en-v15-768__v1"
    assert len(knowledge_overflow) > 63
    with pytest.raises(ValueError) as exc_info:
        validate_collection_name(knowledge_overflow)
    assert "knowledge" in str(exc_info.value)
    assert "nx catalog collection-name" in str(exc_info.value)


# ── Non-overflow names are unaffected ─────────────────────────────────────────

def test_exactly_63_chars_passes() -> None:
    """A 63-character name must still validate without error (boundary condition)."""
    # 63 chars: "a" * 63
    name = "a" * 63
    validate_collection_name(name)  # must not raise


def test_conformant_collection_name_passes() -> None:
    """A typical conformant collection name passes without any error."""
    # code__1-36__bge-base-en-v15-768__v1 = 35 chars (the canonical form from the issue)
    name = "code__1-36__bge-base-en-v15-768__v1"
    assert len(name) <= 63
    validate_collection_name(name)  # must not raise


def test_short_known_voyage_name_passes() -> None:
    """Standard voyage-named collection passes validation unchanged."""
    validate_collection_name("code__nexus-abc123__voyage-code-3__v1")
