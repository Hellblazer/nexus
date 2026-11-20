# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.10 (nexus-p2m1x.15), the client half of the rule test.

After P2.10 no engine path moves ``chroma://`` residue or an ``nx-note://<file-routed>`` value, so both keep the
segment they were written with when their documents move onto a prefix-free label (the engine tests are
``NoteUriResidueRuleTest``, ``NoteUriMoveTest``, ``DocumentMoveTest`` and ``CatalogReconcileCommitBehaviourTest``). This
file pins what the CLIENT twin says of those values, and that the prefix list the SQL of the changeset names is the
frozen one: the three URI-string classifiers stay on their list (RDR-229 item 12).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.aspect_readers import FILE_ROUTED_PREFIXES, canonical_note_uri, is_note_uri

CHANGELOG = Path(__file__).resolve().parents[1] / "service/src/main/resources/db/changelog"


@pytest.mark.parametrize("uri", [
    "chroma://docs__old__minilm-l6-v2-384__v1/a.md",   # residue left on its old segment
    "chroma://code__old/x/y.py",
    "chroma://rdr__old/rdr-1.md",
    "nx-note://docs__old__minilm-l6-v2-384__v1/b.md",   # an nx-note:// value with a file-routed segment
    "nx-note://rdr__old/b.md",
])
def test_a_value_left_on_its_file_routed_segment_is_no_note(uri):
    """Both stay classed file-routed by ``is_note_uri`` whichever collection their row is filed under now, so the
    stale-note repair (which skips what the classifier rejects) never sees them."""
    assert canonical_note_uri(uri) == uri
    assert not is_note_uri(canonical_note_uri(uri))


@pytest.mark.parametrize("uri", ["chroma://plain-label/a.md", "nx-note://plain-label/b.md"])
def test_the_same_value_on_a_prefix_free_label_would_be_a_note(uri):
    """Why moveTo must not carry it there: the classifier reads only the segment, so a move onto a prefix-free label
    would turn residue into a note."""
    assert is_note_uri(canonical_note_uri(uri))


def _prefixes_named_by(text: str, pattern: str) -> set[str]:
    return set(re.findall(pattern, text))


def test_the_changesets_prefix_lists_are_the_frozen_list():
    """catalog-049's three URI rewrites and its three moves test a segment against the same list as ``NoteUri`` and
    ``aspect_readers``; a prefix added on one side only fails here."""
    text = (CHANGELOG / "catalog-049-note-uri-residue.xml").read_text()
    frozen = set(FILE_ROUTED_PREFIXES)
    function_guard = _prefixes_named_by(text, r"starts_with\(d\.physical_collection, '([a-z]+__)'\)")
    assert function_guard == frozen, "the function's moveTo guard names a different list than FILE_ROUTED_PREFIXES"
    for column in ("physical_collection", "collection"):
        named = _prefixes_named_by(text, rf"starts_with\(t\.{column}, '([a-z]+__)'\)")
        if named:
            assert named == frozen, f"a move's prefix-free test on t.{column} names a different list"
    # the moves repair what the content type says: code, docs and rdr are the frozen list's names without the suffix
    content_types = set(re.findall(r"c\.content_type IN \(([^)]*)\)", text))
    assert content_types, "no move names the file-typed content types"
    for group in content_types:
        assert {t.strip().strip("'") for t in group.split(",")} == {p.removesuffix("__") for p in frozen}
