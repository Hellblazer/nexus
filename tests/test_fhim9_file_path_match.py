# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""catalog_search(file_path=...) must not report absence for a path it holds.

nexus-fhim9. The catalog stores a document's path RELATIVE to its owner root
while recording the absolute form in ``source_uri``. An exact ``==`` against
the stored value rejected the absolute path -- the form a caller actually
holds. Measured on the live catalog before the fix, one call apart:

    file_path="tests/test_aspect_worker.py"            -> tumbler 1.1.100
    file_path="/Users/.../tests/test_aspect_worker.py" -> empty

A false NEGATIVE from a tool whose name invites trust. Same family as
nexus-yg70j: an identity that resolves from only one vantage point and reports
absence rather than refusing.

The widening is deliberately narrow, and the negative cases below are the point
-- trading a false negative for a false positive would be a worse bug, since a
basename-only match would collide across every same-named file in a tree.
"""
from __future__ import annotations

import pytest

from nexus.mcp.catalog import _file_path_matches


@pytest.mark.parametrize(
    ("stored", "wanted", "why"),
    [
        ("tests/test_aspect_worker.py", "tests/test_aspect_worker.py", "exact"),
        ("tests/test_aspect_worker.py",
         "/Users/hal/git/nexus/tests/test_aspect_worker.py",
         "the incident: absolute in, relative stored"),
        ("/Users/hal/git/nexus/tests/x.py", "tests/x.py",
         "the mirror case: relative in, absolute stored"),
        ("src/nexus/commands/index.py",
         "/Users/hal/git/nexus/src/nexus/commands/index.py", "nested relative"),
    ],
)
def test_matches(stored: str, wanted: str, why: str) -> None:
    assert _file_path_matches(stored, wanted), why


@pytest.mark.parametrize(
    ("stored", "wanted", "why"),
    [
        # A DISCRIMINATING segment-boundary case. The first case tried here
        # ("a/barfoo.py" vs "/root/a/foo.py") failed under BOTH the anchored
        # and the bare-endswith implementations, so it could not tell them
        # apart -- a mutation replacing `endswith("/" + short_)` with
        # `endswith(short_)` passed the whole file. This pair does discriminate:
        # "/root/tests/x.py".endswith("ests/x.py") is True, while
        # .endswith("/ests/x.py") is False.
        ("ests/x.py", "/root/tests/x.py",
         "SEGMENT boundary: bare endswith matches, anchored must not"),
        ("a/barfoo.py", "/root/a/foo.py", "no shared trailing segments"),
        ("tests/test_aspect_worker.py", "test_aspect_worker.py",
         "basename-only must NOT match — it collides across the tree"),
        ("tests/a/x.py", "tests/b/x.py", "different directories"),
        ("tests/x.py", "", "empty query matches nothing"),
        ("", "tests/x.py", "empty stored matches nothing"),
        ("tests/x.py", "tests/x.pyc", "not a segment suffix"),
    ],
)
def test_does_not_match(stored: str, wanted: str, why: str) -> None:
    assert not _file_path_matches(stored, wanted), why


def test_the_incident_pair_specifically() -> None:
    """The exact two calls from the 2026-08-25 reproduction."""
    stored = "tests/test_aspect_worker.py"
    assert _file_path_matches(stored, "tests/test_aspect_worker.py")
    assert _file_path_matches(
        stored, "/Users/hal.hildebrand/git/nexus/tests/test_aspect_worker.py"
    ), "the absolute form still reports absence"
