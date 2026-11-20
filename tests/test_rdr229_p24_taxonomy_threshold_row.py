# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.4 (nexus-p2m1x.9): the taxonomy projection threshold reads the
catalog row's ``content_type``, not the name's prefix.

The contract is RDR-229 Technical Design item 1's three outcomes:

* Row     -> the threshold of the row's ``content_type`` (knowledge 0.50,
  docs/rdr 0.55, code and anything else 0.70), even when the name's prefix
  says otherwise. This is the behavioural change.
* NoRow   -> the name derivation (today's table); the only signal for an
  unregistered or empty collection.
* Unreadable (the listing read raised) -> the same name derivation, logged
  once; a taxonomy run never fails on it.

An explicit ``--threshold`` short-circuits all three. The tests drive the
real callers (``nx taxonomy project`` single-source, ``--backfill``,
``--prune-below-threshold``, and ``HttpTaxonomyStore.audit_collection``) with
``nexus.mcp_infra.get_collection_row`` faked, so each fails on the name-prefix
reading and passes on the row reading. None needs a tenant.
"""
from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from nexus.commands.taxonomy_cmd import taxonomy

# A code__-named collection whose row says knowledge, and the mirror image.
CODE_NAMED_KNOWLEDGE_ROW = "code__1-1__bge-base-en-v15-768__v1"
KNOWLEDGE_NAMED_CODE_ROW = "knowledge__1-2__bge-base-en-v15-768__v1"
DOCS_NAMED_RDR_ROW = "docs__1-3__bge-base-en-v15-768__v1"
UNREGISTERED_LEGACY = "docs__legacy-no-row"

_ROWS: dict[str, dict[str, str]] = {
    CODE_NAMED_KNOWLEDGE_ROW: {"content_type": "knowledge"},
    KNOWLEDGE_NAMED_CODE_ROW: {"content_type": "code"},
    DOCS_NAMED_RDR_ROW: {"content_type": "rdr"},
}


@pytest.fixture(autouse=True)
def _rows(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fake the row seam; returns the list of names it was asked about."""
    asked: list[str] = []

    def _get_collection_row(name: str, *, refresh: bool = True, strict: bool = False):
        asked.append(name)
        return _ROWS.get(name)

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _get_collection_row)
    monkeypatch.setattr("nexus.mcp_infra.get_live_collection_names", lambda: sorted(_ROWS))
    return asked


# ── the function ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name, expected",
    [
        (CODE_NAMED_KNOWLEDGE_ROW, 0.50),
        (KNOWLEDGE_NAMED_CODE_ROW, 0.70),
        (DOCS_NAMED_RDR_ROW, 0.55),
    ],
)
def test_row_content_type_beats_the_name_prefix(name: str, expected: float) -> None:
    from nexus.projection_threshold import projection_threshold

    assert projection_threshold(name) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        (UNREGISTERED_LEGACY, 0.55),
        ("knowledge__legacy-no-row", 0.50),
        ("code__legacy-no-row", 0.70),
        ("other__weird", 0.70),
        ("noprefix", 0.70),
    ],
)
def test_no_row_falls_back_to_the_name_derivation(name: str, expected: float) -> None:
    from nexus.projection_threshold import projection_threshold

    assert projection_threshold(name) == expected


def test_unreadable_listing_falls_back_and_logs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.projection_threshold as pt

    def _boom(name: str, *, refresh: bool = True, strict: bool = False):
        raise RuntimeError("catalog listing unreadable")

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _boom)
    monkeypatch.setattr(pt, "_unreadable_logged", False, raising=False)
    events: list[str] = []
    monkeypatch.setattr(pt._log, "warning", lambda event, **_kw: events.append(event))

    assert pt.projection_threshold(CODE_NAMED_KNOWLEDGE_ROW) == 0.70  # the name says code
    assert pt.projection_threshold(UNREGISTERED_LEGACY) == 0.55
    assert events == ["projection_threshold_row_unreadable"], "logged once per process, not per call"


# ── the callers ─────────────────────────────────────────────────────────────


class _Taxonomy:
    """Records what the commands hand the store; the store itself is not under test."""

    def __init__(self, distinct: list[str]) -> None:
        self.distinct = distinct
        self.projected: list[tuple[str, float]] = []
        self.pruned: list[tuple[str, float]] = []

    def get_distinct_collections(self) -> list[str]:
        return list(self.distinct)

    def compute_icf_map(self, *, use_cache: bool = True) -> dict:
        return {}

    def project_against(self, source, targets, handle, *, threshold, **_kw) -> dict[str, Any]:
        self.projected.append((source, round(threshold, 2)))
        return {"matched_topics": [], "novel_chunks": [], "total_chunks": 0, "chunk_assignments": []}

    def prune_projection_below(self, prefix: str, thr: float) -> int:
        self.pruned.append((prefix, round(thr, 2)))
        return 1


def _wire(monkeypatch: pytest.MonkeyPatch, distinct: list[str]) -> _Taxonomy:
    tax = _Taxonomy(distinct)

    class _Db:
        taxonomy = tax

        def close(self) -> None:
            pass

    monkeypatch.setattr("nexus.commands.taxonomy_cmd._T2Database", lambda *_a, **_k: _Db())
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._command_shared_t2_client", lambda: None)
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._without_superseded", lambda cols: cols)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    return tax


def test_single_source_default_reads_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    tax = _wire(monkeypatch, [])
    result = CliRunner().invoke(
        taxonomy, ["project", CODE_NAMED_KNOWLEDGE_ROW, "--against", "knowledge__other"],
    )
    assert result.exit_code == 0, result.output
    assert tax.projected == [(CODE_NAMED_KNOWLEDGE_ROW, 0.50)]


def test_single_source_explicit_threshold_short_circuits_the_row(
    monkeypatch: pytest.MonkeyPatch, _rows: list[str],
) -> None:
    tax = _wire(monkeypatch, [])
    result = CliRunner().invoke(
        taxonomy,
        ["project", CODE_NAMED_KNOWLEDGE_ROW, "--against", "knowledge__other", "--threshold", "0.81"],
    )
    assert result.exit_code == 0, result.output
    assert tax.projected == [(CODE_NAMED_KNOWLEDGE_ROW, 0.81)]
    assert _rows == [], "an explicit --threshold must not read a row"


def test_backfill_default_reads_each_sources_row(monkeypatch: pytest.MonkeyPatch) -> None:
    tax = _wire(monkeypatch, [CODE_NAMED_KNOWLEDGE_ROW, KNOWLEDGE_NAMED_CODE_ROW, UNREGISTERED_LEGACY])
    result = CliRunner().invoke(taxonomy, ["project", "--backfill"])
    assert result.exit_code == 0, result.output
    assert dict(tax.projected) == {
        CODE_NAMED_KNOWLEDGE_ROW: 0.50,
        KNOWLEDGE_NAMED_CODE_ROW: 0.70,
        UNREGISTERED_LEGACY: 0.55,
    }


def test_prune_with_backfill_prunes_each_collection_at_its_rows_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tax = _wire(monkeypatch, [UNREGISTERED_LEGACY])
    result = CliRunner().invoke(taxonomy, ["project", "--backfill", "--prune-below-threshold"])
    assert result.exit_code == 0, result.output
    # One prune per full collection name (live listing plus the collections with topics),
    # never one per name prefix.
    assert dict(tax.pruned) == {
        CODE_NAMED_KNOWLEDGE_ROW: 0.50,
        KNOWLEDGE_NAMED_CODE_ROW: 0.70,
        DOCS_NAMED_RDR_ROW: 0.55,
        UNREGISTERED_LEGACY: 0.55,
    }
    assert len(tax.pruned) == 4
    assert "Pruned 4 projection assignment(s)." in result.output


def test_audit_collection_default_threshold_reads_the_row() -> None:
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
    store._get = lambda path, params=None, **_kw: (  # type: ignore[method-assign]
        {"similarities": [0.10, 0.45, 0.52, 0.80], "hub_rows": []} if path == "/audit" else {}
    )
    store.compute_icf_map = lambda *a, **k: {}  # type: ignore[method-assign]
    report = store.audit_collection(CODE_NAMED_KNOWLEDGE_ROW)
    assert report.threshold == 0.50
    assert report.below_threshold_count == 2
