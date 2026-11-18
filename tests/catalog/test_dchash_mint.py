# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 3: minting dchash spans from offsets or a quoted passage (gate S-B, S-C)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import nexus.catalog.dchash.mint as mint_mod
from nexus.catalog.dchash import (
    canonical_text,
    dchash_span,
    dchash_span_for_passage,
    parse_dchash_span,
    resolve_dchash_text,
    text_hash,
)
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry

TUMBLER = "1.2.3"


def make_entry(
    *,
    file_path: str = "",
    source_uri: str = "",
    content_type: str = "prose",
    tumbler: str = TUMBLER,
) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler),
        title="t",
        author="",
        year=0,
        content_type=content_type,
        file_path=file_path,
        corpus="",
        physical_collection="docs__1-2__bge-base-en-v15-768__v1",
        chunk_count=1,
        head_hash="",
        indexed_at="",
        source_uri=source_uri,
    )


class FakeCatalog:
    """A catalog reader: resolve by tumbler string, resolve_path as the client does for absolute paths."""

    def __init__(self, *entries: CatalogEntry) -> None:
        self.entries = {str(e.tumbler): e for e in entries}

    def resolve(self, tumbler: Any, *, follow_alias: bool = True) -> CatalogEntry | None:
        return self.entries.get(str(tumbler))

    def resolve_path(self, tumbler: Any) -> Path | None:
        entry = self.entries.get(str(tumbler))
        if entry is None or not entry.file_path:
            return None
        p = Path(entry.file_path)
        return p if p.is_absolute() else None


def doc_catalog(tmp_path: Path, body: bytes, name: str = "doc.md", **kw: Any) -> FakeCatalog:
    f = tmp_path / name
    f.write_bytes(body)
    return FakeCatalog(make_entry(file_path=str(f), **kw))


# ── dchash_span: range (S-C) ────────────────────────────────────────────────


def test_span_whole_document_accepted(tmp_path: Path) -> None:
    body = "Hello world\n"
    cat = doc_catalog(tmp_path, body.encode())
    span = dchash_span(cat, TUMBLER, 0, len(body))
    assert span == f"dchash:{text_hash(body)}:0-{len(body)}"
    assert parse_dchash_span(span) == (text_hash(body), 0, len(body))


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1, 3), (3, 3), (5, 3), (0, 13), (0, 0), (-5, -1)],
)
def test_span_range_refused(tmp_path: Path, start: int, end: int) -> None:
    cat = doc_catalog(tmp_path, b"Hello world\n")  # 12 code points
    with pytest.raises(ValueError) as ei:
        dchash_span(cat, TUMBLER, start, end)
    msg = str(ei.value)
    assert str(start) in msg and str(end) in msg and "12" in msg


def test_span_offsets_are_code_points_of_canonical_text(tmp_path: Path) -> None:
    raw = "﻿a\r\nb\U0001f600c\rd".encode("utf-8")
    canon = canonical_text(raw)
    assert canon == "a\nb\U0001f600c\nd"
    cat = doc_catalog(tmp_path, raw)
    span = dchash_span(cat, TUMBLER, 0, len(canon))
    assert parse_dchash_span(span) == (text_hash(canon), 0, 7)


def test_span_unknown_tumbler(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"abc")
    with pytest.raises(ValueError, match="9.9.9"):
        dchash_span(cat, "9.9.9", 0, 1)


def test_span_invalid_utf8_propagates(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"\xff\xfe bad")
    with pytest.raises(UnicodeDecodeError):
        dchash_span(cat, TUMBLER, 0, 1)


# ── refusals that run before any read (S-B) ─────────────────────────────────


@pytest.fixture
def no_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path: Path) -> str:
        raise AssertionError("the source was read before the refusal")

    monkeypatch.setattr(mint_mod, "read_canonical", boom)


BAD_BYTES = b"%PDF-1.7 \xff\xfe\x00\x80 not utf-8"


def _both(cat: FakeCatalog) -> list[Any]:
    return [
        lambda: dchash_span(cat, TUMBLER, 0, 1),
        lambda: dchash_span_for_passage(cat, TUMBLER, "x"),
    ]


def test_paper_pdf_refused_before_read(tmp_path: Path, no_read: None) -> None:
    cat = doc_catalog(tmp_path, BAD_BYTES, "a.pdf", content_type="paper")
    for call in _both(cat):
        with pytest.raises(ValueError, match=rf"{TUMBLER} is a PDF; dchash spans need a text source"):
            call()


def test_paper_content_type_alone_refused_before_read(tmp_path: Path, no_read: None) -> None:
    # `nx index pdf` registers a PDF as "paper"; the type alone must refuse it.
    cat = doc_catalog(tmp_path, BAD_BYTES, "a.txt", content_type="paper")
    with pytest.raises(ValueError, match="is a PDF"):
        dchash_span(cat, TUMBLER, 0, 1)


def test_malformed_source_uri_does_not_block_minting(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"hello world", "a.md", source_uri="http://[bad")
    assert dchash_span(cat, TUMBLER, 0, 5).endswith(":0-5")


def test_pdf_content_type_refused_before_read(tmp_path: Path, no_read: None) -> None:
    cat = doc_catalog(tmp_path, BAD_BYTES, "a.txt", content_type="pdf")
    with pytest.raises(ValueError, match="is a PDF"):
        dchash_span(cat, TUMBLER, 0, 1)


def test_prose_entry_with_upper_pdf_suffix_refused_before_read(tmp_path: Path, no_read: None) -> None:
    cat = doc_catalog(tmp_path, BAD_BYTES, "A.PDF", content_type="prose")
    for call in _both(cat):
        with pytest.raises(ValueError, match=rf"{TUMBLER} is a PDF; dchash spans need a text source"):
            call()


def test_pdf_suffix_in_source_uri_path_refused(tmp_path: Path, no_read: None) -> None:
    f = tmp_path / "x.dat"
    f.write_bytes(BAD_BYTES)
    cat = FakeCatalog(make_entry(file_path=str(f), source_uri="file:///somewhere/Doc.PdF?v=2"))
    with pytest.raises(ValueError, match="is a PDF"):
        dchash_span(cat, TUMBLER, 0, 1)


def test_pdf_in_query_string_only_is_not_a_pdf(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text("hello")
    cat = FakeCatalog(make_entry(file_path=str(f), source_uri="https://h/doc?name=a.pdf"))
    assert dchash_span(cat, TUMBLER, 0, 5).endswith(":0-5")


def test_nx_note_refused_before_read(no_read: None) -> None:
    cat = FakeCatalog(make_entry(file_path="", source_uri="nx-note://abc"))
    for call in _both(cat):
        with pytest.raises(ValueError, match=rf"{TUMBLER} has no file source"):
            call()


def test_nx_note_with_absolute_file_path_still_refused(tmp_path: Path, no_read: None) -> None:
    f = tmp_path / "n.md"
    f.write_text("note")
    cat = FakeCatalog(make_entry(file_path=str(f), source_uri="nx-note://abc"))
    with pytest.raises(ValueError, match="has no file source"):
        dchash_span(cat, TUMBLER, 0, 1)


def test_no_source_refused_before_read(no_read: None) -> None:
    cat = FakeCatalog(make_entry(file_path="relative/only.md", source_uri=""))
    for call in _both(cat):
        with pytest.raises(ValueError, match="has no file source"):
            call()


def test_unknown_tumbler_for_passage() -> None:
    with pytest.raises(ValueError, match="9.9.9"):
        dchash_span_for_passage(FakeCatalog(), "9.9.9", "x")


# ── dchash_span_for_passage ─────────────────────────────────────────────────


def test_passage_found_and_round_trips(tmp_path: Path) -> None:
    body = "Intro line.\nthe passage we quote and more.\nEnd.\n"
    cat = doc_catalog(tmp_path, body.encode())
    span = dchash_span_for_passage(cat, TUMBLER, "the passage we quote")
    _h, start, end = parse_dchash_span(span)
    assert body[start:end] == "the passage we quote"
    entry = cat.entries[TUMBLER]
    assert resolve_dchash_text(entry, span, cat) == "the passage we quote"


def test_passage_with_crlf_matches_lf_text(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"one\ntwo\nthree\n")
    span = dchash_span_for_passage(cat, TUMBLER, "one\r\ntwo")
    assert parse_dchash_span(span)[1:] == (0, 7)
    assert parse_dchash_span(dchash_span_for_passage(cat, TUMBLER, "two\rthree"))[1:] == (4, 13)


def test_passage_found_in_crlf_file(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"one\r\ntwo\r\nthree\r\n")
    span = dchash_span_for_passage(cat, TUMBLER, "two\nthree")
    assert parse_dchash_span(span)[1:] == (4, 13)
    entry = cat.entries[TUMBLER]
    assert resolve_dchash_text(entry, span, cat) == "two\nthree"


def test_offsets_after_emoji_are_code_points(tmp_path: Path) -> None:
    body = "\U0001f600\U0001f600 then the quote here"
    cat = doc_catalog(tmp_path, body.encode())
    span = dchash_span_for_passage(cat, TUMBLER, "the quote")
    _h, start, end = parse_dchash_span(span)
    assert start == 8  # two emoji, then " then " (6 code points)
    assert body[start:end] == "the quote"


def test_passage_empty_refused(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"abc")
    with pytest.raises(ValueError, match="empty"):
        dchash_span_for_passage(cat, TUMBLER, "")


def test_passage_absent_refused(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"abc def")
    with pytest.raises(ValueError, match="passage not found"):
        dchash_span_for_passage(cat, TUMBLER, "xyz")


def test_passage_repeated_refused_with_count(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"cat sat. a cat ran.")
    with pytest.raises(ValueError, match=r"passage occurs 2 times; quote more text"):
        dchash_span_for_passage(cat, TUMBLER, "cat")


def test_passage_overlapping_refused(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"aaa")
    with pytest.raises(ValueError, match=r"passage occurs 2 times"):
        dchash_span_for_passage(cat, TUMBLER, "aa")


def test_passage_invalid_utf8_propagates(tmp_path: Path) -> None:
    cat = doc_catalog(tmp_path, b"\xff\xfe bad")
    with pytest.raises(UnicodeDecodeError):
        dchash_span_for_passage(cat, TUMBLER, "x")
