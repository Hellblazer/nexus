# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 1: canonical text, its hash, and the dchash span grammar."""

from __future__ import annotations

import hashlib

import pytest

from nexus.catalog.dchash import (
    DCHASH_PREFIX,
    canonical_text,
    parse_dchash_span,
    text_hash,
)

H = "a" * 64


def test_prefix() -> None:
    assert DCHASH_PREFIX == "dchash:"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (b"abc", "abc"),
        (b"a\r\nb", "a\nb"),
        (b"a\rb", "a\nb"),
        (b"a\r\r\nb", "a\n\nb"),
        (b"\xef\xbb\xbfabc", "abc"),
        (b"\xef\xbb\xbf\xef\xbb\xbfabc", "﻿abc"),
        (b"x\xef\xbb\xbfy", "x﻿y"),
        (b"  trailing  \n\n", "  trailing  \n\n"),
        ("é".encode(), "é"),
        (b"", ""),
    ],
)
def test_canonical_text(raw: bytes, want: str) -> None:
    assert canonical_text(raw) == want


def test_canonical_text_strict_utf8() -> None:
    with pytest.raises(UnicodeDecodeError):
        canonical_text(b"\xff\xfe")


def test_text_hash_is_lowercase_sha256_of_utf8() -> None:
    assert text_hash("hé") == hashlib.sha256("hé".encode()).hexdigest()
    assert text_hash("x") == text_hash("x").lower()


def test_crlf_and_lf_hash_alike() -> None:
    assert text_hash(canonical_text(b"a\r\nb")) == text_hash(canonical_text(b"a\nb"))


@pytest.mark.parametrize(
    ("span", "want"),
    [
        (f"dchash:{H}:0-1", (H, 0, 1)),
        (f"dchash:{H}:10-200", (H, 10, 200)),
    ],
)
def test_parse_ok(span: str, want: tuple[str, int, int]) -> None:
    assert parse_dchash_span(span) == want


@pytest.mark.parametrize(
    "span",
    [
        f"dchash:{H}:5-5",
        f"dchash:{H}:6-5",
        f"dchash:{H}:01-5",
        f"dchash:{H}:0-05",
        f"dchash:{H}:-1-5",
        f"dchash:{H}",
        f"dchash:{H}:0-5 ",
        f" dchash:{H}:0-5",
        f"dchash:{H}:0-5\n",
        f"dchash:{'A' * 64}:0-5",
        f"dchash:{'a' * 63}:0-5",
        f"dchash:{'a' * 65}:0-5",
        f"dchash:{H}:١-٢",
        f"DCHASH:{H}:0-5",
        f"chash:{H}:0-5",
        f"chash:{H}",
        "",
    ],
)
def test_parse_refuses(span: str) -> None:
    with pytest.raises(ValueError) as e:
        parse_dchash_span(span)
    assert repr(span) in str(e.value)
