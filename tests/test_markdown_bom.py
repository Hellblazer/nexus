# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0ib6r (2): a markdown file saved with a UTF-8 BOM (Windows PowerShell 5.1's
``Set-Content -Encoding UTF8``, Notepad's older "UTF-8") is indexed as if it had none.
The BOM kept its frontmatter from being seen, so title, year and author fell back to the
defaults, and it started the first chunk. ``nx store put`` dropped it already (nexus-3ppub)."""
from __future__ import annotations

from pathlib import Path

from nexus.doc_indexer import _parse_md_frontmatter

_DOC = "---\ntitle: Kernel notes\ndate: 2024-03-01\nauthor: Sam\n---\n\n# Body\n\ntext\n"


def test_frontmatter_is_read_through_a_bom(tmp_path: Path) -> None:
    plain, bom = tmp_path / "plain.md", tmp_path / "bom.md"
    plain.write_text(_DOC, encoding="utf-8")
    bom.write_bytes(b"\xef\xbb\xbf" + _DOC.encode("utf-8"))
    assert _parse_md_frontmatter(bom)[:2] == ("Kernel notes", 2024)
    assert _parse_md_frontmatter(bom) == _parse_md_frontmatter(plain)


def test_markdown_chunks_are_the_same_with_and_without_a_bom(tmp_path: Path) -> None:
    """nx index md: the chunk text, ids and offsets of a BOM file equal its BOM-free twin's."""
    from nexus.doc_indexer import _markdown_chunks

    plain, bom = tmp_path / "plain" / "doc.md", tmp_path / "bom" / "doc.md"
    plain.parent.mkdir()
    bom.parent.mkdir()
    plain.write_text(_DOC, encoding="utf-8")
    bom.write_bytes(b"\xef\xbb\xbf" + _DOC.encode("utf-8"))
    got = _markdown_chunks(bom, "h", "model-ctx", "2026-01-01", "corp")
    want = _markdown_chunks(plain, "h", "model-ctx", "2026-01-01", "corp")
    assert [(i, t) for i, t, _ in got] == [(i, t) for i, t, _ in want]
    assert not got[0][1].startswith("﻿")
    assert [m.get("chunk_start_char") for *_, m in got] == [m.get("chunk_start_char") for *_, m in want]
