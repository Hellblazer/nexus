# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``uri_for`` must return the same identity no matter where the process stands.

nexus-yg70j defect B / nexus-3e4s. ``source_uri`` is a persistent IDENTITY
field. Deriving it with ``os.path.abspath`` makes it a function of the calling
process's cwd, which is not a property of the document.

WHY THE EXISTING TEST DID NOT CATCH THIS, because that is the whole lesson.
``tests/test_aspect_readers.py`` asserted:

    uri = uri_for(collection, "src/cli.py")
    assert uri == "file://" + os.path.abspath("src/cli.py")

It computes its EXPECTED value with the same call the code under test makes.
That is a tautology: it passes from any cwd, and it passes whether the answer
is right or wrong. It asserts the implementation, not the contract. A test that
cannot fail for the bug it covers is not coverage.

The contract these tests state instead: SAME INPUT -> SAME OUTPUT, from
anywhere; and never a path this function had to invent.

Observed cost of the gap: after the 7.16.2 chdir fix the aspect-worker stands
in ~/.config/nexus, so a relative "docs/rdr/x.md" minted
file:///Users/<u>/.config/nexus/docs/rdr/x.md -- a plausible URI for a file
that does not exist. Before that fix the same input CRASHED the daemon. Loud
failure to quiet wrong answer, neither caught by the suite.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.aspect_readers import uri_for

REPO = "/Users/example/git/nexus"
REL = "docs/rdr/rdr-199-indexing-lifecycle.md"
ABS = f"{REPO}/{REL}"


@pytest.fixture
def three_cwds(tmp_path: Path):
    """Three unrelated directories to evaluate the same input from."""
    dirs = []
    for name in ("alpha", "beta/nested", "gamma"):
        d = tmp_path / name
        d.mkdir(parents=True)
        dirs.append(d)
    return dirs


def _from(cwd: Path, *args, **kw):
    prev = os.getcwd()
    try:
        os.chdir(cwd)
        return uri_for(*args, **kw)
    finally:
        os.chdir(prev)


class TestCwdIndependence:
    """The contract. Each of these fails against the pre-fix implementation."""

    @pytest.mark.parametrize("collection", ["rdr__nexus", "docs__corpus", "code__nx"])
    def test_absolute_path_identical_from_every_cwd(self, collection, three_cwds):
        results = {_from(d, collection, ABS) for d in three_cwds}
        assert len(results) == 1, f"absolute path varied by cwd: {results}"
        assert results.pop() == f"file://{ABS}"

    @pytest.mark.parametrize("collection", ["rdr__nexus", "docs__corpus", "code__nx"])
    def test_relative_with_root_identical_from_every_cwd(self, collection, three_cwds):
        results = {_from(d, collection, REL, repo_root=REPO) for d in three_cwds}
        assert len(results) == 1, f"anchored relative varied by cwd: {results}"
        assert results.pop() == f"file://{ABS}", (
            "a relative path anchored on an explicit repo_root must equal the "
            "absolute form — same document, same identity"
        )

    @pytest.mark.parametrize("collection", ["rdr__nexus", "docs__corpus", "code__nx"])
    def test_relative_without_root_is_none_everywhere(self, collection, three_cwds):
        """THE REGRESSION PIN. Pre-fix this returned a different, plausible,
        WRONG URI from each cwd. None is a missing identity: detectable and
        recoverable. A cwd-anchored URI is a wrong identity: neither."""
        results = {_from(d, collection, REL) for d in three_cwds}
        assert results == {None}, (
            f"relative path without a root must never be guessed at; got {results}"
        )

    def test_never_invents_a_path_containing_the_cwd(self, three_cwds):
        """Stated as the property rather than the value, so it still holds if
        the URI format changes."""
        for d in three_cwds:
            for got in (_from(d, "rdr__nexus", REL), _from(d, "rdr__nexus", ABS)):
                if got is not None:
                    assert str(d) not in got, f"leaked cwd {d} into {got}"


class TestBehaviourPreserved:
    """Everything the old tests actually meant, kept."""

    def test_chroma_collections_unchanged(self):
        assert uri_for("knowledge__delos", "/papers/aleph.pdf") == (
            "chroma://knowledge__delos//papers/aleph.pdf"
        )
        assert uri_for("future__x", "src") == "chroma://future__x/src"

    def test_empty_source_path_is_none(self):
        assert uri_for("rdr__nexus", "") is None
        assert uri_for("knowledge__delos", "") is None

    def test_normalises_without_resolving(self):
        assert uri_for("rdr__nexus", f"{REPO}/./docs/../{REL}") == f"file://{ABS}"


def test_the_old_assertion_shape_is_a_tautology() -> None:
    """Pins WHY this file exists, so nobody reinstates the weaker assertion.

    `abspath(x) == abspath(x)` holds under any implementation, including a
    broken one. If a future test reaches for that shape again, this is the
    counter-example: it is true even when uri_for is wrong.
    """
    rel = "src/cli.py"
    assert os.path.abspath(rel) == os.path.abspath(rel)  # vacuous, by construction
    cwd_a = os.path.abspath(rel)
    prev = os.getcwd()
    try:
        os.chdir("/")
        assert os.path.abspath(rel) != cwd_a, (
            "abspath IS cwd-dependent — which is exactly why an assertion "
            "written in terms of it cannot detect cwd-dependence"
        )
    finally:
        os.chdir(prev)
