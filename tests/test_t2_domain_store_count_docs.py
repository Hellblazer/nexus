# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ume6q: the T2 "nine domain stores" count, wherever prose states
it, must agree with ``nexus.db.storage_mode.T2_FACADE_STORES`` -- the
set ``T2Database.__init__`` validates at construction, and which
``tests/db/test_storage_mode.py::test_t2_facade_stores_matches_
t2database_ast_construction`` separately pins as exactly what
``__init__`` constructs.

Companion to ``tests/test_collection_reindex_shared_client_fanout.py``
et al. (the fan-out coverage side of the same bead) -- this file is the
DOC side: before this test existed, ``docs/container-integration.md``,
``src/nexus/db/AGENTS.md``, root ``AGENTS.md``, and
``src/nexus/db/t2/__init__.py``'s own module docstring each spelled out
"nine domain stores" as free prose with nothing checking it against the
actual store count, so an add/remove of a domain store (as ``tuples``
itself was, RDR-205) could silently leave every one of these stale.

This file only maps ``len(T2_FACADE_STORES)`` to its English cardinal
word (only small numbers are ever plausible for a manually-composed
facade) and checks that word appears in each doc's "<word> domain
stores" / "<word> T2 domain stores" phrasing.

Non-vacuity: a store added or removed without updating every doc red
this test with a clear "N domain stores (docs say M)" message naming
the stale file, rather than a silent pass.
"""
from __future__ import annotations

import pathlib

import pytest

from nexus.db.storage_mode import T2_FACADE_STORES

pytestmark = pytest.mark.lint

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_CARDINAL_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve",
}

#: (doc path, phrase template) -- ``{word}`` is filled with the cardinal
#: word for len(T2_FACADE_STORES).
_DOC_CHECKS: tuple[tuple[pathlib.Path, str], ...] = (
    (_REPO_ROOT / "AGENTS.md", "{word} domain stores behind a `T2Database` facade"),
    (_REPO_ROOT / "src" / "nexus" / "db" / "AGENTS.md", "{word} domain stores + `T2Database` facade"),
    (_REPO_ROOT / "docs" / "container-integration.md", "the {word} T2 domain stores"),
    (_REPO_ROOT / "src" / "nexus" / "db" / "t2" / "__init__.py", "{word} domain stores behind a composing facade"),
)


def test_t2_facade_stores_count_is_plausible():
    """Non-vacuity floor: T2_FACADE_STORES must be a plausible, non-empty
    count (catches an accidentally-empty or wildly-wrong constant before
    it silently passes every doc check below)."""
    assert 1 <= len(T2_FACADE_STORES) <= 12, (
        f"T2_FACADE_STORES has an implausible length "
        f"({len(T2_FACADE_STORES)}: {T2_FACADE_STORES})"
    )


@pytest.mark.parametrize("doc_path,phrase_template", _DOC_CHECKS, ids=lambda v: getattr(v, "name", v))
def test_doc_states_the_t2_facade_store_count(doc_path: pathlib.Path, phrase_template: str):
    n = len(T2_FACADE_STORES)
    word = _CARDINAL_WORDS.get(n)
    assert word is not None, (
        f"no cardinal word mapped for a T2_FACADE_STORES length of {n} "
        f"({T2_FACADE_STORES}) -- extend _CARDINAL_WORDS"
    )
    expected = phrase_template.format(word=word)
    text = doc_path.read_text()
    assert expected in text, (
        f"{doc_path.relative_to(_REPO_ROOT)} does not contain the "
        f"expected phrase {expected!r} -- T2_FACADE_STORES currently has "
        f"{n} stores ({T2_FACADE_STORES}), so this doc has drifted from "
        f"the code (or the phrasing changed and this test's template "
        f"needs updating to match)"
    )
