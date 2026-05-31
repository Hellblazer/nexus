# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-139 Layer D — ``extraction_source`` chunk provenance.

The provenance field records how a chunk's text was sourced:
``file`` (the on-disk file, the default) | ``dt_content`` | ``dt_ocr`` |
``dt_transcribe`` (DEVONthink-extracted, for non-file-backed records).

To stay under Chroma's 32-key metadata cap the default ``file`` value is
dropped by :func:`normalize` (absent == file), exactly like the ``bib_*``
placeholder-drop. Only DT-sourced chunks spend the extra key.
"""
from __future__ import annotations

import pytest

from nexus.metadata_schema import (
    ALLOWED_TOP_LEVEL,
    MAX_SAFE_TOP_LEVEL_KEYS,
    make_chunk_metadata,
    normalize,
    validate,
)


def test_extraction_source_is_allowed() -> None:
    assert "extraction_source" in ALLOWED_TOP_LEVEL


def test_allowed_set_still_within_cap() -> None:
    """Adding the provenance key keeps the schema within the hard cap."""
    assert len(ALLOWED_TOP_LEVEL) <= MAX_SAFE_TOP_LEVEL_KEYS


def test_normalize_drops_file_default() -> None:
    """``extraction_source=file`` (the default) is dropped — absent == file."""
    out = normalize({"extraction_source": "file"}, content_type="pdf")
    assert "extraction_source" not in out


def test_normalize_drops_empty() -> None:
    out = normalize({"extraction_source": ""}, content_type="pdf")
    assert "extraction_source" not in out


@pytest.mark.parametrize("src", ["dt_content", "dt_ocr", "dt_transcribe"])
def test_normalize_keeps_dt_sourced(src: str) -> None:
    out = normalize({"extraction_source": src}, content_type="pdf")
    assert out["extraction_source"] == src


def test_make_chunk_metadata_defaults_to_file_and_drops_it() -> None:
    meta = make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-05-30T00:00:00Z",
        embedding_model="voyage-context-3",
    )
    assert "extraction_source" not in meta  # default file -> dropped


def test_make_chunk_metadata_stamps_dt_source() -> None:
    meta = make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-05-30T00:00:00Z",
        embedding_model="voyage-context-3",
        extraction_source="dt_content",
    )
    assert meta["extraction_source"] == "dt_content"
    validate(meta)  # stays writeable / under the key cap
