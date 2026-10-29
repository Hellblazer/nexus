# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-b0y6w class e: which path the post-store document hook is keyed on.

Only a caller with a ``base_path`` (``nx index rdr``) has a relative spelling to get wrong and pays the catalog
read; ``nx index md``, ``nx dt index`` and collection reindex make none. The catalog reader is a fake here: the
substrate-backed end to end is ``tests/test_index_rdr_aspect_identity.py``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nexus.doc_indexer import _catalog_absolute_identity, _hook_source_key


class _Reader:
    def __init__(self, file_path: str | None, *, boom: bool = False) -> None:
        self.file_path, self.boom, self.reads, self.closed = file_path, boom, 0, False

    def by_doc_id(self, doc_id: str):  # noqa: ARG002
        self.reads += 1
        if self.boom:
            raise RuntimeError("catalog down")
        return None if self.file_path is None else SimpleNamespace(file_path=self.file_path)

    def close(self) -> None:
        self.closed = True


def _with(reader: _Reader):
    return patch("nexus.catalog.factory.make_catalog_reader", return_value=reader)


def test_no_base_path_means_no_catalog_read(tmp_path: Path) -> None:
    reader = _Reader("/abs/doc.md")
    with _with(reader):
        assert _hook_source_key(tmp_path / "a.md", None, "1.1") is None
    assert reader.reads == 0


def test_a_catalog_absolute_path_is_the_key_itself(tmp_path: Path) -> None:
    catalog_spelling = "/private/var/curated/docs/rdr/rdr-001.md"
    reader = _Reader(catalog_spelling)
    with _with(reader):
        key = _hook_source_key(tmp_path / "docs" / "rdr" / "rdr-001.md", tmp_path, "1.1")
    assert key == catalog_spelling and reader.reads == 1 and reader.closed


def test_a_relative_catalog_path_keeps_the_base_path_relative_key(tmp_path: Path) -> None:
    with _with(_Reader("docs/rdr/rdr-001.md")):
        key = _hook_source_key(tmp_path / "docs" / "rdr" / "rdr-001.md", tmp_path, "1.1")
    assert key == "docs/rdr/rdr-001.md"


def test_a_failed_read_warns_naming_the_relative_key_and_uses_it(tmp_path: Path) -> None:
    import structlog

    with structlog.testing.capture_logs() as logs, _with(_Reader("/x", boom=True)):
        key = _hook_source_key(tmp_path / "docs" / "a.md", tmp_path, "1.1")
    assert key == "docs/a.md"
    warned = [r for r in logs if r["log_level"] == "warning"]
    assert [r["event"] for r in warned] == ["catalog_absolute_identity_lookup_failed_relative_key_used"]


@pytest.mark.parametrize("doc_id", ["", None])
def test_no_doc_id_reads_nothing(doc_id) -> None:
    reader = _Reader("/abs/doc.md")
    with _with(reader):
        assert _catalog_absolute_identity(doc_id) == ""
    assert reader.reads == 0
