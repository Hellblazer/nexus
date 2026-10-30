# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 2 (nexus-6pfgb.55): ``_local_source_path`` returns the path the registration minted.

The catalog's ``file://`` source_uri is the RAW spelling, ``"file://" + path`` (``aspect_readers.uri_for``,
``doc_indexer._curator_identity`` and the engine's ``deriveSourceUri`` all concatenate, nothing percent-encodes).
Parsing it as a URL truncated a path at ``#`` or ``?`` and unquoted a literal ``%25``, so the absolute PATH
scope and ``_file_line_no`` missed or over-matched for such a directory.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.aspect_readers import uri_for
from nexus.search_engine import _local_source_path

PATHS = [
    "/Users/x/plain/a.py",
    "/Users/x/my#dir/a.py",
    "/Users/x/what?/a.py",
    "/Users/x/100%25/a.py",
    "/Users/x/with space/a b.py",
    "/Users/x/a%20b/c.py",
]


@pytest.mark.parametrize("path", PATHS)
def test_the_minted_uri_round_trips_to_its_path(path: str) -> None:
    uri = uri_for("code__test", path)
    assert uri == "file://" + path, "the mint is the raw spelling this reader must invert"
    assert _local_source_path(SimpleNamespace(source_uri=uri)) == path


@pytest.mark.parametrize("uri", ["", "nx-note://n", "https://example.org/a.py", "file://relative/a.py", "file://"])
def test_a_source_that_is_not_an_absolute_file_uri_is_no_local_path(uri: str) -> None:
    assert _local_source_path(SimpleNamespace(source_uri=uri)) == ""
