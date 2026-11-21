# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.10 (nexus-p2m1x.15), the client half of the rule test.

After P2.10 no engine path moves ``chroma://`` residue or an ``nx-note://<file-routed>`` value, so both keep the
segment they were written with when their documents move onto a prefix-free label (the engine tests are
``NoteUriResidueRuleTest``, ``NoteUriMoveTest``, ``DocumentMoveTest`` and ``CatalogReconcileCommitNoteUriRuleTest``). This
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


def _forward_function_body(text: str) -> str:
    """The forward (not rollback) body of catalog-049-1's function: the first CDATA block of that changeset."""
    start = text.index('<changeSet id="catalog-049-1"')
    cdata = text.index("<![CDATA[", start)
    return text[cdata: text.index("]]>", cdata)]


def _prefix_tests(segment: str, column: str) -> list[str]:
    """The prefixes tested with ``starts_with(<column>, '<prefix>')`` in ``segment``, in order."""
    return re.findall(rf"starts_with\({re.escape(column)}, '([a-z]+__)'\)", segment)


def test_every_uri_rewrite_arm_of_the_function_carries_the_whole_frozen_guard():
    """catalog-049-1 rewrites a note URI in three places (the collision pre-check, the update UPDATE, the repoint UPDATE).
    Each ``nx-note://`` arm must test the old collection against EVERY frozen prefix before its ``THEN``, counted per arm so
    one guarded arm cannot cover for another, and no ``chroma://`` arm may be left."""
    body = _forward_function_body((CHANGELOG / "catalog-049-note-uri-residue.xml").read_text())
    arms = body.split("starts_with(d.source_uri, 'nx-note://' || d.physical_collection || '/')")[1:]
    assert len(arms) == 3, f"expected the three nx-note:// arms of the function, found {len(arms)}"
    for number, arm in enumerate(arms, start=1):
        before_then = arm[: arm.index("THEN")]
        assert _prefix_tests(before_then, "d.physical_collection") == list(FILE_ROUTED_PREFIXES), (
            f"arm {number} does not test the old collection against exactly FILE_ROUTED_PREFIXES, in order, before THEN"
        )
    assert "'chroma://'" not in body, "a chroma:// arm is left in the function"


def test_every_move_template_and_verification_carries_the_whole_frozen_guard_and_the_file_types():
    """Six texts (three templates, three verifications): each tests its collection column against every frozen prefix
    and names exactly the file-typed content types. A regex that matches nothing fails; it never passes."""
    text = (CHANGELOG / "catalog-049-note-uri-residue.xml").read_text()
    templates = re.findall(r"\$t\$(.*?)\$t\$", text, re.DOTALL)
    verifications = re.findall(r"\$v\$(.*?)\$v\$", text, re.DOTALL)
    assert len(templates) == 3 and len(verifications) == 3, (len(templates), len(verifications))
    columns = ["physical_collection", "collection", "collection"]
    content_types = [p.removesuffix("__") for p in FILE_ROUTED_PREFIXES]
    for kind, texts in (("template", templates), ("verification", verifications)):
        for number, (body, column) in enumerate(zip(texts, columns, strict=True), start=1):
            assert _prefix_tests(body, f"t.{column}") == list(FILE_ROUTED_PREFIXES), (
                f"{kind} {number}: t.{column} is not tested against exactly FILE_ROUTED_PREFIXES"
            )
            groups = re.findall(r"c\.content_type IN \(([^)]*)\)", body)
            assert len(groups) == 1, f"{kind} {number}: expected one content_type list, found {len(groups)}"
            assert sorted(t.strip().strip("'") for t in groups[0].split(",")) == sorted(content_types)
