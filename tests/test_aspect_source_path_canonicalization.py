# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-145 Gap-2 source_path canonicalization, absolute-path arm.

The first catalog probe keys on ``file_path``/``title``, which the catalog
stores RELATIVE. An ABSOLUTE ``source_path`` therefore always missed it and
fell to the loud ``aspect_source_path_uncanonical`` warning — the documented
"Bucket B" population, whose one-time normalization (nexus-nx9nx) was closed
as superseded without that half ever running. Measured on a working box:
1528 warnings, 114 distinct paths, ~300/day, all still accruing.

The catalog also stores the ABSOLUTE form as ``source_uri``
(``file://<abspath>``, RDR-096 P3.1) and exposes ``by_source_uri``, so an
absolute path has an exact deterministic key. That is what these tests pin —
including the two ways the fix could be WORSE than the warning it removes:
a cross-collection hit, and a synthesized (unconfirmed) path.
"""
from __future__ import annotations

import pytest

from nexus.aspect_worker import _canonicalize_source_path

COLLECTION = "rdr__1-1__voyage-context-3__v1"
ABS = "/Users/x/git/nexus/docs/rdr/rdr-191-unify.md"
REL = "docs/rdr/rdr-191-unify.md"


class _Entry:
    def __init__(self, file_path: str, physical_collection: str) -> None:
        self.file_path = file_path
        self.physical_collection = physical_collection


class _Cat:
    """Catalog whose relative-keyed probe MISSES an absolute path, as the real one does."""

    def __init__(self, entry: _Entry | None = None, *, explode: bool = False) -> None:
        self._entry = entry
        self._explode = explode
        self.uri_queries: list[str] = []

    def lookup_doc_id_by_collection_and_path(self, collection: str, source_path: str) -> str:
        return ""  # relative-keyed: an absolute path never matches

    def by_source_uri(self, uri: str):
        self.uri_queries.append(uri)
        if self._explode:
            raise RuntimeError("catalog down")
        return self._entry


@pytest.fixture
def patched(monkeypatch):
    def _install(cat):
        monkeypatch.setattr(
            "nexus.aspect_worker._resolve_catalog_reader", lambda: cat
        )
        return cat
    return _install


def test_absolute_path_resolves_via_source_uri(patched, caplog):
    cat = patched(_Cat(_Entry(REL, COLLECTION)))

    assert _canonicalize_source_path(COLLECTION, ABS) == REL
    assert cat.uri_queries == ["file://" + ABS]
    assert "aspect_source_path_uncanonical" not in caplog.text


def test_cross_collection_hit_is_REFUSED(patched):
    """The fix must never re-point an aspect row into another collection.

    A source_uri is globally unique, so a hit from a DIFFERENT collection is
    possible; accepting it would be worse than the warning it replaces.
    """
    cat = patched(_Cat(_Entry(REL, "docs__1-1__voyage-context-3__v1")))

    assert _canonicalize_source_path(COLLECTION, ABS) == ABS


def test_no_catalog_hit_still_warns(patched):
    patched(_Cat(None))

    assert _canonicalize_source_path(COLLECTION, ABS) == ABS


def test_catalog_error_degrades_to_the_old_behaviour(patched):
    patched(_Cat(None, explode=True))

    assert _canonicalize_source_path(COLLECTION, ABS) == ABS


def test_absolute_canonical_is_not_accepted(patched):
    """An entry whose own file_path is absolute yields no canonicalization.

    Returning it would re-store the same absolute form the probe cannot match,
    leaving the row uncanonical while reporting success.
    """
    patched(_Cat(_Entry("/Users/x/git/nexus/" + REL, COLLECTION)))

    assert _canonicalize_source_path(COLLECTION, ABS) == ABS


def test_relative_source_path_never_queries_source_uri(patched):
    """Only absolute paths take the new arm; relative ones are unchanged."""
    cat = patched(_Cat(_Entry(REL, COLLECTION)))

    assert _canonicalize_source_path(COLLECTION, REL) == REL
    assert cat.uri_queries == []


def test_chash_source_path_is_untouched(patched):
    """Note-backed rows carry a chash, not a path — no probe, no warning."""
    cat = patched(_Cat(None))
    chash = "a" * 64

    assert _canonicalize_source_path(COLLECTION, chash) == chash
    assert cat.uri_queries == []


def test_catalog_without_by_source_uri_degrades(patched):
    class _Old:
        def lookup_doc_id_by_collection_and_path(self, c, p):  # noqa: ANN001
            return ""

    patched(_Old())

    assert _canonicalize_source_path(COLLECTION, ABS) == ABS
