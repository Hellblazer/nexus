# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k AC2: the staleness gate must verify, not assume.

Root cause (recorded on the bead): both gates ask
``col.get(where={"content_hash": h}, limit=1)`` — "does ANY chunk with this
content hash exist?" — which ONE survivor satisfies. An index that dies
mid-write commits an arbitrary subset, so every later re-index finds a
survivor, returns 0, and reports success. The document is permanently
un-indexable by any supported command.

RUNFENCE (nexus-5xn3k.3, design memo §3.4) replaced ``_manifest_is_fully_
present``'s client-side T3 paging (a 300-id-per-page ``col.get(ids=...)``
loop) with ONE engine-side ``manifest/verify`` call — this file's mocks were
updated in lockstep (mock ``cat.manifest_verify`` instead of
``cat.get_manifest`` + ``col.get(ids=...)``); the function's SIGNATURE and
FAIL-OPEN CONTRACT are unchanged, so every scenario this file pins still
holds.
"""
from __future__ import annotations

import logging

from unittest.mock import MagicMock

import structlog

from nexus.doc_indexer import _manifest_is_fully_present


class _Col:
    """T3 collection stub. UNUSED by ``_manifest_is_fully_present`` post-
    RUNFENCE (the presence check moved server-side) — kept only because the
    function's signature still takes *col* (bead: keep the signature
    unchanged so callers/tests need no further edits)."""


def _cat(verify_result: dict | None = None, verify_exc: Exception | None = None) -> MagicMock:
    cat = MagicMock()
    if verify_exc is not None:
        cat.manifest_verify.side_effect = verify_exc
    else:
        cat.manifest_verify.return_value = verify_result
    return cat


def _patch_cat(monkeypatch, cat) -> None:
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader",
        lambda *a, **k: cat, raising=False,
    )


def test_one_surviving_chunk_does_not_mark_the_document_whole(monkeypatch) -> None:
    """THE BUG. The manifest names 3 chunks; T3 holds 1. Pre-fix the gate saw
    that single survivor and skipped forever."""
    cat = _cat({"referenced": 3, "present": 1, "missing": 2})
    _patch_cat(monkeypatch, cat)
    assert _manifest_is_fully_present(_Col(), "1.1.1") is False
    cat.manifest_verify.assert_called_once_with("1.1.1")


def test_fully_present_manifest_still_skips(monkeypatch) -> None:
    """NON-VACUITY: an intact document must NOT be re-embedded every pass."""
    _patch_cat(monkeypatch, _cat({"referenced": 2, "present": 2, "missing": 0}))
    assert _manifest_is_fully_present(_Col(), "1.1.1") is True


def test_empty_manifest_is_left_to_reconcile(monkeypatch) -> None:
    """chunk_count>0 with an EMPTY manifest is the GH #1397 ghost class, which
    `nx catalog reconcile` owns. This gate must not silently claim it."""
    _patch_cat(monkeypatch, _cat({"referenced": 0, "present": 0, "missing": 0}))
    assert _manifest_is_fully_present(_Col(), "1.1.1") is True


def test_unreadable_catalog_fails_open_and_logs_warning(monkeypatch, caplog) -> None:
    """FAIL-OPEN on the hot path: a transient read failure must not force a
    full re-embed of an intact document. The damage is durable and will be
    caught next pass; a false positive here is expensive.

    ac4id lesson: this must log at WARNING, never DEBUG — a verify that
    silently stops working recreates the exact bug this arc exists to fix.
    """
    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    _patch_cat(monkeypatch, _cat(verify_exc=RuntimeError("service down")))
    with caplog.at_level(logging.WARNING, logger="nexus.doc_indexer"):
        assert _manifest_is_fully_present(_Col(), "1.1.1") is True
    assert any(
        r.msg == "index_manifest_presence_check_failed" and r.levelname == "WARNING"
        for r in caplog.records
    ), f"expected a WARNING-level index_manifest_presence_check_failed log, got: {[(r.levelname, r.msg) for r in caplog.records]}"


def test_verify_is_called_exactly_once_not_paged(monkeypatch) -> None:
    """RUNFENCE (nexus-5xn3k.3): the presence check is now ONE engine call
    regardless of manifest size — the 300-id client-side paging this used to
    do lives server-side now. Regression guard against re-introducing a
    client-side loop."""
    cat = _cat({"referenced": 701, "present": 701, "missing": 0})
    _patch_cat(monkeypatch, cat)
    assert _manifest_is_fully_present(_Col(), "1.1.1") is True
    assert cat.manifest_verify.call_count == 1


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


def test_prose_identity_miss_fails_open(monkeypatch) -> None:
    """An unidentifiable document yields "" -> no expected set -> no evidence
    of damage. A miss must never force a spurious re-embed."""
    from pathlib import Path

    from nexus.doc_indexer import _doc_id_for_path

    cat = MagicMock()
    cat.by_source_uri.return_value = None
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat, raising=False,
    )
    assert _doc_id_for_path(Path("/tmp/nope.md")) == ""
    assert _manifest_is_fully_present(_Col(), "") is True
