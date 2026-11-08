# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 2 (nexus-6pfgb.55): ``_local_source_path`` returns the path the registration minted.

The catalog's ``file://`` source_uri is the RAW spelling, ``"file://" + path`` (``aspect_readers.uri_for``,
``doc_indexer._curator_identity`` and the engine's ``deriveSourceUri`` all concatenate, nothing percent-encodes).
Parsing it as a URL truncated a path at ``#`` or ``?`` and unquoted a literal ``%25``, so the absolute PATH
scope and ``_file_line_no`` missed or over-matched for such a directory.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import nexus.search_engine as se
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


@pytest.mark.skipif(os.name == "nt", reason="PATHS are POSIX absolute paths; uri_for mints none for them on Windows")
@pytest.mark.parametrize("path", PATHS)
def test_the_minted_uri_round_trips_to_its_path(path: str) -> None:
    uri = uri_for("code__test", path)
    assert uri == "file://" + path, "the mint is the raw spelling this reader must invert"
    assert _local_source_path(SimpleNamespace(source_uri=uri)) == path


@pytest.mark.parametrize("uri", ["", "nx-note://n", "https://example.org/a.py", "file://relative/a.py", "file://"])
def test_a_source_that_is_not_an_absolute_file_uri_is_no_local_path(uri: str) -> None:
    assert _local_source_path(SimpleNamespace(source_uri=uri)) == ""


# nexus-rfvyy.9 item 3: on Windows every registration mints ``"file://" + "C:\\..."`` (two slashes, then the drive),
# which the ``file:///`` check refused, so those documents had no local path for a PATH scope or a line lookup.
WINDOWS = [
    ("file://C:\\Users\\a\\x.md", "C:\\Users\\a\\x.md", "C:\\Users\\a\\x.md"),
    ("file:///C:/Users/a/x.md", "C:/Users/a/x.md", "C:\\Users\\a\\x.md"),
    ("file://C:/Users/a/my#dir/x.md", "C:/Users/a/my#dir/x.md", "C:\\Users\\a\\my#dir\\x.md"),
    ("file://" + "C:\\Users\\a\\what?\\x.py", "C:\\Users\\a\\what?\\x.py", "C:\\Users\\a\\what?\\x.py"),
    ("file://d:\\100%25\\x.md", "d:\\100%25\\x.md", "d:\\100%25\\x.md"),
]


@pytest.mark.parametrize(("uri", "posix", "nt"), WINDOWS)
def test_a_windows_drive_uri_is_a_local_path(uri: str, posix: str, nt: str, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = SimpleNamespace(source_uri=uri)
    monkeypatch.setattr(se, "os", SimpleNamespace(name="posix"))
    assert _local_source_path(entry) == posix, "kept as written off Windows"
    monkeypatch.setattr(se, "os", SimpleNamespace(name="nt"))
    assert _local_source_path(entry) == nt, "native separators on Windows, so a PATH scope and open() match"


@pytest.mark.parametrize("uri", ["file://\\\\server\\share\\x.md", "file:////server/share/x.md", "file://C:", "file://CC:/x"])
def test_a_unc_or_malformed_drive_uri_is_no_local_path(uri: str) -> None:
    assert _local_source_path(SimpleNamespace(source_uri=uri)) == ""
