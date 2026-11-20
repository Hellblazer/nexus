# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 P3 fix round (critic S3): the client's root-derived rule equals the engine's ``rootDerivedUri``.

The census flag and the repository hint count a document as "outside its root" when the engine's owner move would
REWRITE it: a root-derived row (a ``file://`` URI that ends in its relative, normalized ``file_path``) whose current URI
differs from the one a fresh registration at the owner's root gives. ``hints.derived_root_uri`` is that rule on the
client, and this table is the engine's ``CatalogMoves.rootDerivedUri`` (plus ``CatalogRepository.deriveSourceUri``)
evaluated by hand, line for line, on the POSIX engine. A tripwire test reads the Java method and fails when one of the
clauses the table depends on disappears or changes, so an edit of the engine rule cannot drift from the client silently.

A Windows-shaped root: the engine resolves the root with ``java.nio`` on its own host, where ``C:\\repo`` is not an
absolute path, so ``deriveSourceUri`` answers "" and ``rootDerivedUri`` null: the owner move SKIPS every row. The client
mirrors that: such a root derives nothing, so nothing is flagged and no line is printed for a move that would change
no row (the first cut compared against ``file://C:\\repo/`` and flagged every document)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.catalog.engine_writes import hints

_NO = None

#: (repo_root, file_path, source_uri, what the engine derives: the new URI, or None when the row is skipped)
_TABLE = [
    # the row already at the root: derived equals current ("unchanged")
    ("/r", "src/a.py", "file:///r/src/a.py", "file:///r/src/a.py"),
    # a row under the old root: derived is the new one ("rewrite")
    ("/new", "src/a.py", "file:///old/src/a.py", "file:///new/src/a.py"),
    ("/new/", "src/a.py", "file:///old/src/a.py", "file:///new/src/a.py"),          # Paths.get drops the trailing slash
    ("//new", "src/a.py", "file:///old/src/a.py", "file:///new/src/a.py"),          # Paths.get("//new") is /new
    ("/new", "src/../lib/b.py", "file:///old/lib/b.py", "file:///new/lib/b.py"),    # normalize()
    ("/new", "./a.py", "file:///old/a.py", "file:///new/a.py"),
    ("/new", "a//b.py", "file:///old/a/b.py", "file:///new/a/b.py"),
    ("/new", "src/dir/", "file:///old/src/dir", "file:///new/src/dir"),             # a trailing slash on file_path
    ("/new", "a b/ü.py", "file:///old/a b/ü.py", "file:///new/a b/ü.py"),           # no escaping on either side
    # skipped: not root-derived
    ("/new", "/abs/a.py", "file:///abs/a.py", _NO),                                   # an absolute file_path
    ("/new", "../x.py", "file:///old/../x.py", _NO),                                  # leaves the root
    ("/new", "..foo/a.py", "file:///old/..foo/a.py", _NO),                            # norm.startsWith("..") is literal
    ("/new", "a/..", "file:///old/", _NO),                                            # normalizes to nothing
    ("/new", ".", "file:///old/.", _NO),
    ("/new", "", "file:///old/a.py", _NO),
    ("/new", "   ", "file:///old/a.py", _NO),                                         # isBlank
    ("/new", "a.py", "", _NO),
    ("/new", "a.py", "https://example.org/a.py", _NO),                                # another scheme
    ("/new", "a.py", "nx-note://c/a.py", _NO),
    ("/new", "a.py", "file:///old/b.py", _NO),                                        # the URI does not end in /file_path
    ("/new", "a.py", "file:///old/xa.py", _NO),                                       # ... as a whole segment
    ("/new", "a\x00b.py", "file:///old/a\x00b.py", _NO),                              # InvalidPathException
    # a root the engine's host does not read as absolute derives "" and the row is skipped
    ("C:\\Users\\me\\repo", "src/a.py", "file:///C:/Users/me/repo/src/a.py", _NO),
    ("C:/Users/me/repo", "src/a.py", "file:///C:/Users/me/repo/src/a.py", _NO),
    ("\\\\host\\share\\repo", "src/a.py", "file://host/share/repo/src/a.py", _NO),
    ("relative/root", "src/a.py", "file:///old/src/a.py", _NO),
    ("", "src/a.py", "file:///old/src/a.py", _NO),
]


@pytest.mark.parametrize(("root", "file_path", "source_uri", "expected"), _TABLE)
def test_derived_root_uri_equals_the_engines_rule(root, file_path, source_uri, expected):
    assert hints.derived_root_uri(file_path, source_uri, root) == expected


def test_outside_root_is_a_row_the_owner_move_would_rewrite():
    class _Doc:
        def __init__(self, file_path, source_uri):
            self.file_path, self.source_uri = file_path, source_uri

    docs = [
        _Doc("src/a.py", "file:///r/src/a.py"),      # unchanged
        _Doc("src/b.py", "file:///old/src/b.py"),    # rewrite: outside
        _Doc("/abs/c.py", "file:///abs/c.py"),       # skipped
        _Doc("d.py", "https://example.org/d.py"),    # skipped
    ]
    assert hints.count_docs_outside_root(docs, "/r") == 1
    assert hints.count_docs_outside_root(docs, "C:\\Users\\me\\repo") == 0, "a Windows-shaped root: the engine skips every row"
    assert hints.count_docs_outside_root([], "/r") == 0


def test_the_engines_rule_still_has_the_clauses_the_table_encodes():
    """A tripwire, not a parity proof: when the Java method changes shape, the table above must be re-derived."""
    java = Path(__file__).resolve().parents[2] / "service/src/main/java/dev/nexus/service/db/CatalogMoves.java"
    assert java.is_file(), "non-vacuity: the engine source is where the test expects it"
    text = java.read_text(encoding="utf-8")
    body = re.search(r"private static String rootDerivedUri\(DocRow d, String root\) \{(.*?)\n    \}\n", text, re.S)
    assert body, "rootDerivedUri moved or changed signature: re-derive hints._TABLE parity"
    src = re.sub(r"\s+", " ", body.group(1))
    for clause in (
        'fp == null || fp.isBlank() || su == null || !su.startsWith("file://")',
        "p = Paths.get(fp);",
        "catch (InvalidPathException e) { return null; }",
        "if (p.isAbsolute()) return null;",
        "String norm = p.normalize().toString();",
        'norm.isEmpty() || norm.startsWith("..") || !su.endsWith("/" + norm)',
        'CatalogRepository.deriveSourceUri("", fp, root);',
        "derived.isEmpty() ? null : derived",
    ):
        assert clause in src, f"the engine's rootDerivedUri no longer contains: {clause}"
    derive = re.search(r"public static String deriveSourceUri\(String sourceUri, String filePath, String repoRoot\) \{(.*?)\n    \}\n",
                       (java.parent / "CatalogRepository.java").read_text(encoding="utf-8"), re.S)
    assert derive, "deriveSourceUri moved or changed signature"
    d = re.sub(r"\s+", " ", derive.group(1))
    for clause in ("if (!p.isAbsolute()) {", "if (!root.isAbsolute()) return \"\";", "p = root.resolve(filePath);",
                   'return "file://" + p.normalize();'):
        assert clause in d, f"the engine's deriveSourceUri no longer contains: {clause}"
