# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-101 Phase 6: write-time collection naming enforcement.

When the ``[catalog].strict_collection_naming`` config flag is true (or
the operator passes ``strict=True`` explicitly), creating a NEW
collection with a non-conformant name raises ValueError at the
``T3Database.get_or_create_collection`` boundary. Existing collections
are allowed regardless of conformance (read paths must accept legacy
names per RDR-101 §"Phase 6").

Default is opt-in OFF so existing tests and indexers do not break.
The flip to default-ON is a separate, irreversible Phase 6 step.
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


def test_strict_false_default_allows_non_conformant_new(t3_db):
    """Default ``strict=False`` keeps the existing permissive behavior."""
    col = t3_db.get_or_create_collection("knowledge__delos")
    assert col is not None
    assert t3_db.collection_exists("knowledge__delos")


# ── config flag default ──────────────────────────────────────────────────


def test_config_flag_strict_collection_naming_defaults_strict(t3_db, monkeypatch):
    """When ``[catalog].strict_collection_naming`` is true, callers that
    do NOT pass ``strict=`` get ``strict=True`` by default.
    """
    def fake_load_config(repo_root=None):
        return {"catalog": {"strict_collection_naming": True}}

    monkeypatch.setattr("nexus.config.load_config", fake_load_config)

    with pytest.raises(ValueError, match="not conformant"):
        t3_db.get_or_create_collection("knowledge__delos")


def test_config_flag_absent_keeps_permissive_default(t3_db, monkeypatch):
    """No flag in config → permissive default (existing behavior preserved)."""
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda repo_root=None: {"catalog": {}},
    )
    col = t3_db.get_or_create_collection("knowledge__delos")
    assert col is not None


def test_explicit_strict_false_overrides_config_flag(t3_db, monkeypatch):
    """An explicit ``strict=False`` argument wins over a strict config flag.

    Backfill / migration verbs need to construct legacy collections
    even when the flag is on; explicit-false-wins is the escape hatch.
    """
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda repo_root=None: {"catalog": {"strict_collection_naming": True}},
    )
    col = t3_db.get_or_create_collection("knowledge__delos", strict=False)
    assert col is not None
