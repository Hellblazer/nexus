# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k AC2: the staleness gate must verify, not assume.

HISTORY. Root cause (recorded on the bead): both gates ask
``col.get(where={"content_hash": h}, limit=1)`` — "does ANY chunk with this
content hash exist?" — which ONE survivor satisfies. An index that dies
mid-write commits an arbitrary subset, so every later re-index finds a
survivor, returns 0, and reports success. The document is permanently
un-indexable by any supported command.

RUNFENCE (nexus-5xn3k.3, design memo §3.4) replaced ``_manifest_is_fully_
present``'s client-side T3 paging (a 300-id-per-page ``col.get(ids=...)``
loop) with ONE engine-side ``manifest/verify`` call.

RDR-191 PHASE 6 REBASE (nexus-o8dil.33), 2026-08-15. The manifest-chunk FK
(``catalog-029-manifest-chunk-fk.xml``, VALIDATEd, deployed
engine-service-v0.1.76) makes the ``missing`` question ``manifest_verify``
answered PROVABLY ALWAYS FALSE for any manifest row that exists at all: the
FK guarantees every ``catalog_document_chunks`` row references a matching
``nexus.chunks`` row at write time. ``_manifest_is_fully_present``'s ONLY
branch that ever returned ``False`` — the ``missing`` check — became dead
code the moment the FK validated, independent of whether the underlying
route/client method survived RDR-191 Phase 6's retirement of
``manifest_verify`` (it did not — see ``http_catalog_client.py``). The
function is now an unconditional ``return True``; this file's scenario-based
mocking of ``cat.manifest_verify`` is retired along with it. See
``nexus.doc_indexer._manifest_is_fully_present``'s own docstring for the
full argument, including why this is a DIFFERENT question from
``CatalogRepository.completeIndexRun``'s still-live, still load-bearing
write-path completeness check (which keeps using the SAME underlying SQL
function, deliberately not dropped).
"""
from __future__ import annotations

import logging

from unittest.mock import MagicMock

import structlog

from nexus.doc_indexer import _manifest_is_fully_present


class _Col:
    """T3 collection stub. UNUSED by ``_manifest_is_fully_present`` (the
    presence check moved server-side under RUNFENCE, then became a bare
    ``return True`` under RDR-191 Phase 6) — kept only because the
    function's signature still takes *col* (every caller/test needs no
    further edit)."""


def test_always_true_regardless_of_doc_id(monkeypatch) -> None:
    """RDR-191 Phase 6: the FK makes the underlying question provably
    always-false, so the function is unconditionally True — no engine call,
    no catalog read, no mock to configure."""
    assert _manifest_is_fully_present(_Col(), "1.1.1") is True
    assert _manifest_is_fully_present(_Col(), "some-other-doc") is True


def test_true_even_with_no_doc_id(monkeypatch) -> None:
    """An unresolvable doc_id ("") still returns True — same answer as
    every other input now, but pinned separately since it used to be a
    distinct early-return branch."""
    assert _manifest_is_fully_present(_Col(), "") is True


def test_never_touches_the_catalog(monkeypatch) -> None:
    """Regression guard: a future edit that reintroduces a catalog/engine
    call here must be caught — the whole point of RDR-191 Phase 6's
    simplification is that this function no longer needs one."""
    cat = MagicMock()
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader",
        lambda *a, **k: cat, raising=False,
    )
    assert _manifest_is_fully_present(_Col(), "1.1.1") is True
    assert not cat.method_calls, (
        f"_manifest_is_fully_present must not touch the catalog reader at "
        f"all any more: {cat.method_calls}"
    )


def test_no_warning_logged_since_there_is_nothing_to_fail(monkeypatch, caplog) -> None:
    """The old fail-open+WARNING contract existed because a real read could
    fail. There is no read left to fail, so nothing should log."""
    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    with caplog.at_level(logging.WARNING, logger="nexus.doc_indexer"):
        assert _manifest_is_fully_present(_Col(), "1.1.1") is True
    assert not any(
        r.msg == "index_manifest_presence_check_failed" for r in caplog.records
    ), "no read is attempted any more, so nothing should log a read failure"


# ── the PROSE path's read-only identity resolution ───────────────────────────


def test_prose_identity_lookup_never_registers(monkeypatch) -> None:
    """The prose gate resolves identity READ-ONLY.

    This runs while DECIDING whether to skip, for documents that may be
    untouched. Minting a catalog row as a side effect of that decision would
    be a worse bug than the one being fixed, so only by_source_uri is used —
    never register, never ensure-owner. Unaffected by RDR-191 Phase 6 — this
    tests _doc_id_for_path, not _manifest_is_fully_present.
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
