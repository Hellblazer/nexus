# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-101 Phase 6 + RDR-103 Phase 5: write-time collection naming enforcement.

Creating a NEW collection with a non-conformant name raises ValueError
at the ``T3Database.get_or_create_collection`` boundary. Existing
collections are allowed regardless of conformance (read paths must
accept legacy names per RDR-101 §"Phase 6").

RDR-103 Phase 5 (``nexus-yqnr.7``) flipped the default from opt-in
permissive to unconditional strict. The ``strict`` parameter and the
``[catalog].strict_collection_naming`` config flag are slated for
removal alongside the legacy registry helpers; until they go, an
explicit ``strict=False`` argument still functions as the operator
escape hatch (used by backfill / migration verbs).
"""
from __future__ import annotations

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t3 import T3Database


@pytest.fixture()
def t3_db():
    db = T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )
    for raw in list(db._client.list_collections()):
        name = raw if isinstance(raw, str) else getattr(raw, "name", str(raw))
        try:
            db._client.delete_collection(name)
        except Exception:
            pass
    return db


# ── strict=True parameter ────────────────────────────────────────────────


def test_strict_true_rejects_non_conformant_new(t3_db):
    """A NEW collection with a non-conformant name is rejected when strict=True."""
    with pytest.raises(ValueError, match="not conformant"):
        t3_db.get_or_create_collection("knowledge__delos", strict=True)


def test_strict_true_accepts_conformant_new(t3_db):
    """A NEW collection with a conformant name is accepted when strict=True."""
    name = "knowledge__1-1__voyage-context-3__v1"
    col = t3_db.get_or_create_collection(name, strict=True)
    assert col is not None
    assert t3_db.collection_exists(name)


def test_strict_true_accepts_existing_non_conformant(t3_db):
    """An EXISTING non-conformant collection still resolves under strict=True.

    The validator only fires for new creation; legacy collections must
    continue to be readable / writable per RDR-101 §"Phase 6"
    (failing-loud at read time is rejected as operationally hostile).
    """
    name = "knowledge__delos"
    t3_db.get_or_create_collection(name, strict=False)
    # Now resolve again with strict=True; should not raise.
    col = t3_db.get_or_create_collection(name, strict=True)
    assert col is not None


def test_default_rejects_legacy_two_segment(t3_db):
    """RDR-103 Phase 5 invariant: a NEW legacy 2-segment name is
    rejected without any caller-side opt-in. Pins the irreversible
    flip from permissive to strict default.
    """
    with pytest.raises(ValueError, match="not conformant"):
        t3_db.get_or_create_collection("code__legacy-cafe1234")


def test_default_rejects_non_conformant_new(t3_db):
    """Default behavior post Phase 5 flip: non-conformant new
    collections are rejected even without any config flag set.
    """
    with pytest.raises(ValueError, match="not conformant"):
        t3_db.get_or_create_collection("knowledge__delos")


# ── config flag default ──────────────────────────────────────────────────


def test_config_flag_does_not_re_enable_permissive(t3_db, monkeypatch):
    """Even with the legacy ``[catalog].strict_collection_naming=false``
    flag, the post-flip default cannot be downgraded back to permissive.
    Only an explicit ``strict=False`` keyword argument escapes the guard.
    """
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda repo_root=None: {"catalog": {"strict_collection_naming": False}},
    )
    with pytest.raises(ValueError, match="not conformant"):
        t3_db.get_or_create_collection("knowledge__delos")


def test_config_flag_absent_uses_strict_default(t3_db, monkeypatch):
    """No flag in config → strict default (post Phase 5 flip)."""
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda repo_root=None: {"catalog": {}},
    )
    with pytest.raises(ValueError, match="not conformant"):
        t3_db.get_or_create_collection("knowledge__delos")


def test_explicit_strict_false_escape_hatch(t3_db, monkeypatch):
    """An explicit ``strict=False`` argument remains the operator
    escape hatch for backfill / migration verbs that need to construct
    legacy collections after the default flipped.
    """
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda repo_root=None: {"catalog": {"strict_collection_naming": True}},
    )
    col = t3_db.get_or_create_collection("knowledge__delos", strict=False)
    assert col is not None
