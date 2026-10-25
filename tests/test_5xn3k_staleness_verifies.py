# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k AC2: the prose gate resolves a document's identity read-only.

HISTORY. This file used to pin ``_manifest_is_fully_present``, the staleness
gate's fall-through for a document whose fence had nothing to say (RUNFENCE,
nexus-5xn3k.3; reduced to a bare ``return True`` by RDR-191 Phase 6, because
the manifest-chunk FK made its only branch provably dead). nexus-3ajpd
removed it: a document with no ``'complete'`` fence at the file's hash is now
stale (``tests/test_3ajpd_staleness_from_catalog.py`` pins that), so there is
no fall-through left to test. What stays here is the identity lookup the
prose gate runs before it reads the fence.
"""
from __future__ import annotations

from unittest.mock import MagicMock


# ── the PROSE path's read-only identity resolution ───────────────────────────


def test_prose_identity_lookup_never_registers(monkeypatch) -> None:
    """The prose gate resolves identity READ-ONLY.

    This runs while DECIDING whether to skip, for documents that may be
    untouched. Minting a catalog row as a side effect of that decision would
    be a worse bug than the one being fixed, so only by_source_uri is used —
    never register, never ensure-owner.
    """
    from pathlib import Path

    from nexus.doc_indexer import _doc_id_for_path

    cat = MagicMock()
    cat.by_source_uri.return_value = type("E", (), {"tumbler": "1.2.3"})()
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat, raising=False,
    )

    assert _doc_id_for_path(Path("/tmp/doc.md")) == "1.2.3"
    assert cat.by_source_uri.call_count == 1
    assert cat.by_source_uri.call_args[0][0].startswith("file://")
    for forbidden in ("register", "register_owner", "ensure_owner_for_repo"):
        assert not getattr(cat, forbidden).called, (
            f"the staleness path must never call {forbidden}"
        )


def test_prose_identity_miss_yields_empty_id(monkeypatch) -> None:
    """An unidentifiable document yields "": the gate then has no fence to
    read and indexes it (and registers it), never skips it."""
    from pathlib import Path

    from nexus.doc_indexer import _doc_id_for_path

    cat = MagicMock()
    cat.by_source_uri.return_value = None
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat, raising=False,
    )
    assert _doc_id_for_path(Path("/tmp/nope.md")) == ""
