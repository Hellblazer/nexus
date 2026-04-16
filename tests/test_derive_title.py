# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for ``derive_title`` and the markdown/PDF source_title
fallback chain (nexus-8l6).

Pre-fix: markdown chunker emitted ``source_title=''`` whenever the
frontmatter didn't carry an explicit ``title:`` key, so ``nx store
list`` displayed every markdown doc as ``untitled``.

Post-fix: a four-step fallback chain — frontmatter ``title``, first
H1, normalised filename stem, raw stem — guarantees a non-empty
``source_title`` for every markdown chunk. PDFs reuse the same chain
when neither Docling nor MinerU populate the field.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── derive_title helper ────────────────────────────────────────────────────


def test_derive_title_extracts_first_h1() -> None:
    from nexus.indexer_utils import derive_title

    body = "# Real Title\n\nFirst paragraph.\n\n## Subsection"
    assert (
        derive_title(Path("docs/whatever.md"), body) == "Real Title"
    )


def test_derive_title_skips_h2_and_lower() -> None:
    """Only ``# `` headers count — H2/H3 are not document titles."""
    from nexus.indexer_utils import derive_title

    body = "Some intro\n\n## Section\n\nBody."
    out = derive_title(Path("path/component-inventory.md"), body)
    assert out == "Component Inventory"  # falls back to filename


def test_derive_title_normalises_underscores_and_dashes() -> None:
    from nexus.indexer_utils import derive_title

    out = derive_title(
        Path("docs/GROSSBERG_NLP_CHAT_ARCHITECTURE.md"), body=""
    )
    assert out == "Grossberg NLP Chat Architecture"


def test_derive_title_normalises_dashes() -> None:
    from nexus.indexer_utils import derive_title

    out = derive_title(
        Path("docs/papers/carpenter-grossberg-1987-art1.pdf"), body=None
    )
    assert out == "Carpenter Grossberg 1987 ART1"


def test_derive_title_strips_leading_h1_marker() -> None:
    """The H1 line ``# Title Here`` returns just ``Title Here``."""
    from nexus.indexer_utils import derive_title

    body = "#   Title  With  Padding   "
    assert derive_title(Path("x.md"), body) == "Title  With  Padding"


def test_derive_title_handles_empty_body() -> None:
    from nexus.indexer_utils import derive_title

    out = derive_title(Path("docs/foo_bar.md"), body="")
    assert out == "Foo Bar"


def test_derive_title_handles_missing_body() -> None:
    from nexus.indexer_utils import derive_title

    assert derive_title(Path("docs/foo_bar.md"), body=None) == "Foo Bar"


def test_derive_title_preserves_known_initialisms() -> None:
    """Common all-caps tokens stay all-caps after normalisation."""
    from nexus.indexer_utils import derive_title

    out = derive_title(Path("docs/api_url_pdf.md"), body="")
    assert out == "API URL PDF"


# ── _markdown_chunks integration ────────────────────────────────────────────


@pytest.fixture()
def md_path(tmp_path: Path) -> Path:
    return tmp_path / "component-inventory.md"


def test_markdown_chunks_uses_h1_when_no_frontmatter(md_path: Path) -> None:
    from nexus.doc_indexer import _markdown_chunks

    md_path.write_text(
        "# Component Inventory\n\n## A\n\nFirst\n\n## B\n\nSecond"
    )
    chunks = _markdown_chunks(
        md_path, "h", "voyage-context-3", "Z", "test_corpus",
    )
    assert chunks
    # Every chunk's source_title is the H1 title.
    for _, _, meta in chunks:
        assert meta.get("source_title") == "Component Inventory"


def test_markdown_chunks_frontmatter_title_wins_over_h1(
    md_path: Path,
) -> None:
    from nexus.doc_indexer import _markdown_chunks

    md_path.write_text(
        "---\ntitle: Frontmatter Title\n---\n# H1 Title\n\nBody"
    )
    chunks = _markdown_chunks(
        md_path, "h", "voyage-context-3", "Z", "test_corpus",
    )
    assert chunks[0][2]["source_title"] == "Frontmatter Title"


def test_markdown_chunks_falls_back_to_filename_when_no_title(
    md_path: Path,
) -> None:
    """No frontmatter title, no H1 → normalised filename."""
    from nexus.doc_indexer import _markdown_chunks

    md_path.write_text("Just body content, no title at all.\n")
    chunks = _markdown_chunks(
        md_path, "h", "voyage-context-3", "Z", "test_corpus",
    )
    assert chunks[0][2]["source_title"] == "Component Inventory"


def test_markdown_chunks_never_emits_empty_source_title(
    md_path: Path,
) -> None:
    """SC-bug guard: no markdown chunk path produces empty source_title."""
    from nexus.doc_indexer import _markdown_chunks

    md_path.write_text("just text")
    chunks = _markdown_chunks(
        md_path, "h", "voyage-context-3", "Z", "test_corpus",
    )
    assert chunks[0][2]["source_title"]
