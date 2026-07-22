# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ou4tb caller walk — behavioral VectorServiceError injection.

Part b (f138b3ef) made the service vector client RAISE on a degraded
service. The exhaustive three-scope caller walk (2026-07-22) then found
every site where an enclosing broad ``except`` re-silenced that raise —
a degraded service reading as empty/success. These tests INJECT a
VectorServiceError through each fixed site and assert the failure is
now visible (named WARNING event, distinct report bucket, or skip echo)
— the behavioral harness the bead's part-b close note called for,
replacing grep-level pins for the walked sites.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

import nexus.catalog.catalog_spans as spans
import nexus.db as _db  # noqa: F401 — monkeypatch target for function-local make_t3 imports
from nexus.catalog.catalog_spans import resolve_span_text_for_entry
from nexus.collection_audit import sample_live_distances
from nexus.commands.catalog import _backfill_papers
from nexus.commands.catalog_cmds.doctor import _run_name_vs_embed_dim
from nexus.commands.index import _project_cross_collections, _run_projection_pass
from nexus.db.http_vector_client import VectorServiceError
from nexus.commands.taxonomy_cmd import _run_discover_projection
from nexus.indexer import _prune_misclassified_in_collection

_BOOM = VectorServiceError("GET /v1/vector/get failed: connection refused")


class _RaisingCol:
    """A collection handle whose reads/deletes see a degraded service."""

    def get(self, *a, **k):
        raise _BOOM

    def delete(self, *a, **k):
        raise _BOOM

    def count(self) -> int:
        return 1


class _RaisingT3:
    def get_collection(self, name):
        return _RaisingCol()

    def get_or_create_collection(self, name):
        return _RaisingCol()

    def list_collections(self):
        return [{"name": "docs__papers__bge-base-en-v15-768__v1", "count": 3}]

    def get_embeddings(self, name, ids):  # pragma: no cover — get raises first
        raise _BOOM


def _events(logs: list[dict]) -> list[str]:
    return [entry["event"] for entry in logs]


class TestProjectionIncompleteFetchCounted:
    def test_incomplete_fetch_is_counted_not_dropped(self, monkeypatch) -> None:
        """The index auto-pass computed ``incomplete_fetch`` and dropped it
        (walker-ou4tb-db finding); the helper now returns the count."""
        class _Tax:
            def project_against(self, col, others, client, threshold=0.85):
                return {"chunk_assignments": [], "incomplete_fetch": True}

        total, incomplete = _project_cross_collections(
            _Tax(), ["code__a", "code__b"], None,
        )
        assert total == 0
        assert incomplete == 2


class TestCatalogSpans:
    def test_chunk_char_span_degraded_service_warns(self, monkeypatch) -> None:
        """catalog_spans chunk:char branch: previously ``except Exception:
        return None`` with NO log — user-facing transclusion text silently
        rendered 'not found' on a degraded service."""
        monkeypatch.setattr(_db, "make_t3", lambda: _RaisingT3())
        entry = SimpleNamespace(
            physical_collection="docs__papers__bge-base-en-v15-768__v1",
            file_path="",
            meta={"doc_id": "d1"},
            tumbler="1.2.3",
        )
        # nexus-ib6uy: degraded service now RAISES (unreachable ≠ empty) —
        # the API boundary renders the distinction, never a silent None.
        with capture_logs() as logs, pytest.raises(VectorServiceError):
            resolve_span_text_for_entry(entry, "3:10-40", catalog=None)
        assert "resolve_span_chunk_char_service_degraded" in _events(logs)

    def test_chunk_char_span_generic_failure_stays_debug(self, monkeypatch) -> None:
        class _TypeErrCol(_RaisingCol):
            def get(self, *a, **k):
                raise TypeError("not service-shaped")

        class _T3(_RaisingT3):
            def get_or_create_collection(self, name):
                return _TypeErrCol()

        monkeypatch.setattr(_db, "make_t3", lambda: _T3())
        entry = SimpleNamespace(
            physical_collection="docs__papers__bge-base-en-v15-768__v1",
            file_path="",
            meta={},
            tumbler="1.2.3",
        )
        with capture_logs() as logs:
            out = resolve_span_text_for_entry(entry, "3:10-40", catalog=None)
        assert out is None
        assert "resolve_span_chunk_char_service_degraded" not in _events(logs)

    def test_fallback_scan_reports_incomplete_when_service_degraded(
        self, monkeypatch,
    ) -> None:
        """fallback_chash_scan: every probe failing on a degraded service
        must not read as a clean not-found — the aggregate WARNING carries
        the unreadable-collection count."""
        monkeypatch.setattr(
            spans, "_t3_collection_names", lambda t3: ["code__a", "code__b"],
        )
        def _raise(*a, **k):
            raise _BOOM
        monkeypatch.setattr(spans, "resolve_span_in_t3", _raise)
        spans.reset_chash_fallback_warning_for_tests()
        # nexus-ib6uy: an all-unreadable scan RAISES — not-found cannot be
        # concluded over collections that couldn't be read.
        with capture_logs() as logs, pytest.raises(VectorServiceError):
            spans.fallback_chash_scan(
                span="chash:" + "ab" * 32,
                hex_chash="ab" * 32,
                t3=_RaisingT3(),
                build_ref=lambda **kw: kw,
            )
        assert "resolve_chash_fallback_incomplete_service_degraded" in _events(logs)
        row = next(
            e for e in logs
            if e["event"] == "resolve_chash_fallback_incomplete_service_degraded"
        )
        assert row["unreadable_collections"] == 2

    def test_composite_prior_unreadable_added_to_scan_failures(
        self, monkeypatch,
    ) -> None:
        """fallback_chash_scan: unreadable collections observed BEFORE the
        scan (prior_unreadable) compose with scan-time failures — the
        raise carries the sum."""
        monkeypatch.setattr(
            spans, "_t3_collection_names", lambda t3: ["code__a"],
        )
        def _raise(*a, **k):
            raise _BOOM
        monkeypatch.setattr(spans, "resolve_span_in_t3", _raise)
        spans.reset_chash_fallback_warning_for_tests()
        with capture_logs() as logs, pytest.raises(VectorServiceError):
            spans.fallback_chash_scan(
                span="chash:" + "ab" * 32,
                hex_chash="ab" * 32,
                t3=_RaisingT3(),
                build_ref=lambda **kw: kw,
                prior_unreadable=1,
            )
        row = next(
            e for e in logs
            if e["event"] == "resolve_chash_fallback_incomplete_service_degraded"
        )
        assert row["unreadable_collections"] == 2  # 1 scan + 1 prior

    def test_timeout_after_service_failures_still_raises(
        self, monkeypatch,
    ) -> None:
        """The deadline-timeout exits must NOT read as clean not-found when
        service failures were already observed (review 2026-07-22): a
        degraded service slowing every probe toward the deadline is the
        same incomplete-scan condition as an exhausted scan."""
        import time as _time

        monkeypatch.setattr(
            spans, "_t3_collection_names", lambda t3: ["code__a", "code__b"],
        )

        def _probe(span, coll, t3):
            if coll == "code__a":
                raise _BOOM  # fast service failure, counted before timeout
            _time.sleep(2.0)  # stalls past the shrunk deadline
            return None

        monkeypatch.setattr(spans, "resolve_span_in_t3", _probe)
        monkeypatch.setattr(spans, "_CHASH_FALLBACK_DEADLINE_S", 0.3)
        spans.reset_chash_fallback_warning_for_tests()
        with capture_logs() as logs, pytest.raises(VectorServiceError):
            spans.fallback_chash_scan(
                span="chash:" + "ab" * 32,
                hex_chash="ab" * 32,
                t3=_RaisingT3(),
                build_ref=lambda **kw: kw,
            )
        events = _events(logs)
        assert "resolve_chash_fallback_timeout" in events
        assert "resolve_chash_fallback_incomplete_service_degraded" in events

    def test_timeout_with_only_prior_unreadable_raises(
        self, monkeypatch,
    ) -> None:
        """An immediately-expired deadline (deadline 0) with prior
        unreadable collections raises through the FIRST timeout exit."""
        monkeypatch.setattr(
            spans, "_t3_collection_names", lambda t3: ["code__a"],
        )
        import time as _time
        monkeypatch.setattr(
            spans, "resolve_span_in_t3",
            lambda *a, **k: _time.sleep(2.0),
        )
        monkeypatch.setattr(spans, "_CHASH_FALLBACK_DEADLINE_S", 0.0)
        spans.reset_chash_fallback_warning_for_tests()
        with pytest.raises(VectorServiceError):
            spans.fallback_chash_scan(
                span="chash:" + "ab" * 32,
                hex_chash="ab" * 32,
                t3=_RaisingT3(),
                build_ref=lambda **kw: kw,
                prior_unreadable=1,
            )

    def test_timeout_clean_misses_still_returns_none(
        self, monkeypatch,
    ) -> None:
        """Timeout with ZERO failures keeps the existing contract: plain
        None (slow-but-healthy service, span genuinely may not exist)."""
        import time as _time

        monkeypatch.setattr(
            spans, "_t3_collection_names", lambda t3: ["code__a"],
        )
        monkeypatch.setattr(
            spans, "resolve_span_in_t3",
            lambda *a, **k: _time.sleep(2.0),
        )
        monkeypatch.setattr(spans, "_CHASH_FALLBACK_DEADLINE_S", 0.3)
        spans.reset_chash_fallback_warning_for_tests()
        assert spans.fallback_chash_scan(
            span="chash:" + "ab" * 32,
            hex_chash="ab" * 32,
            t3=_RaisingT3(),
            build_ref=lambda **kw: kw,
        ) is None


class TestIndexerLegacyPrune:
    def test_legacy_in_prune_service_degraded_warns_and_continues(
        self, monkeypatch,
    ) -> None:
        """indexer legacy ``$in`` prune arm: was ``except Exception:
        continue`` with no log (walker-ou4tb-core #3)."""
        with capture_logs() as logs:
            pruned = _prune_misclassified_in_collection(
                _RaisingCol(),
                {Path("/tmp/x.md")},
                {Path("/tmp/x.md"): "doc-1"},
                kind="DOCS",
                catalog=None,
            )
        assert pruned == 0
        assert "legacy_prune_in_query_failed" in _events(logs)


class TestCatalogDoctorNameVsEmbedDim:
    def test_unreadable_store_fails_instead_of_green(self, monkeypatch) -> None:
        """walker-ou4tb-catalog MEDIUM: a degraded store rendered
        ``name-vs-embed-dim: PASS`` (checked=0) because read errors were
        buried in the unknown_token bucket and ``pass = not mismatches``."""
        monkeypatch.setattr(_db, "make_t3", lambda: _RaisingT3())
        report = _run_name_vs_embed_dim()
        assert report["pass"] is False
        assert report["checked"] == 0
        assert len(report["read_errors"]) == 1
        assert report["read_errors"][0]["collection"] == (
            "docs__papers__bge-base-en-v15-768__v1"
        )
        # Read errors are NOT mislabeled as unrecognized model tokens.
        assert report["unknown_token"] == []


class TestBackfillPapers:
    def test_degraded_read_skips_registration(self, monkeypatch, capsys) -> None:
        """_backfill_papers registered a paper with DEFAULT metadata (title
        = collection name, no author, year 0) on a degraded read — DEBUG
        only. Now: visible skip, no registration."""
        registered: list = []

        class _Writer:
            def register(self, **kw):  # pragma: no cover — must not be called
                registered.append(kw)

        class _Cat:
            def by_owner(self, owner):  # pragma: no cover — unreached on skip
                return []

        out = _backfill_papers(
            _Cat(), _RaisingT3(), dry_run=False, repo_collections=set(),
            writer=_Writer(),
        )
        assert out == 0
        assert registered == []
        assert "vector service degraded" in capsys.readouterr().out

    def test_generic_failure_also_skips_not_registers_defaults(
        self, monkeypatch, capsys,
    ) -> None:
        """ou4tb critique: unified with _backfill_rdrs — a non-service
        failure also skips-and-reports instead of registering junk default
        metadata that the idempotency guard would freeze forever."""
        registered: list = []

        class _Writer:
            def register(self, **kw):  # pragma: no cover — must not be called
                registered.append(kw)

        class _Cat:
            def by_owner(self, owner):  # pragma: no cover — unreached on skip
                return []

        class _TypeErrCol(_RaisingCol):
            def get(self, *a, **k):
                raise TypeError("bib_year coercion boom")

        class _T3(_RaisingT3):
            def get_or_create_collection(self, name):
                return _TypeErrCol()

        out = _backfill_papers(
            _Cat(), _T3(), dry_run=False, repo_collections=set(),
            writer=_Writer(),
        )
        assert out == 0
        assert registered == []
        assert "metadata unreadable" in capsys.readouterr().out


class TestCollectionAudit:
    def test_sample_live_distances_degraded_warns(self) -> None:
        with capture_logs() as logs:
            out = sample_live_distances("docs__papers__bge-base-en-v15-768__v1", _RaisingT3(), n=5)
        assert out == []
        entry = next(e for e in logs if e["event"] == "sample_live_distances_failed")
        assert entry["log_level"] == "warning"


class _DegradedTaxonomy:
    def project_against(self, *a, **k):
        raise _BOOM


class TestProjectionPassBehavior:
    """The index auto-projection pass, extracted to _run_projection_pass
    (ou4tb critique: the inline arms were pinned only by source greps)."""

    def test_degraded_service_visible_skip(self) -> None:
        said: list[str] = []
        with capture_logs() as logs:
            _run_projection_pass(_DegradedTaxonomy(), ["a", "b"], None, said.append)
        assert any("SKIPPED — vector service degraded" in s for s in said)
        assert "taxonomy_projection_skipped_service_degraded" in _events(logs)

    def test_incomplete_fetch_surfaced(self) -> None:
        class _Tax:
            def project_against(self, *a, **k):
                return {"chunk_assignments": [], "incomplete_fetch": True}

        said: list[str] = []
        with capture_logs() as logs:
            _run_projection_pass(_Tax(), ["a", "b"], None, said.append)
        assert any("incomplete source fetch" in s for s in said)
        assert "taxonomy_projection_incomplete_fetch" in _events(logs)

    def test_generic_failure_warns_without_crashing(self) -> None:
        class _Tax:
            def project_against(self, *a, **k):
                raise TypeError("boom")

        with capture_logs() as logs:
            _run_projection_pass(_Tax(), ["a", "b"], None, lambda s: None)
        assert "taxonomy_projection_failed" in _events(logs)

    def test_command_body_calls_the_helper(self) -> None:
        src = (Path(__file__).resolve().parent.parent / "src/nexus/commands/index.py").read_text()
        assert "_run_projection_pass(" in src


class TestDiscoverProjectionBehavior:
    """The discover auto-projection pass, extracted to
    _run_discover_projection."""

    def test_degraded_service_visible_skip(self, capsys) -> None:
        with capture_logs() as logs:
            _run_discover_projection(_DegradedTaxonomy(), ["a", "b"], None)
        assert "SKIPPED — vector service degraded" in capsys.readouterr().err
        assert "discover_projection_skipped_service_degraded" in _events(logs)

    def test_incomplete_fetch_echoed_and_skipped(self, capsys) -> None:
        class _Tax:
            def project_against(self, *a, **k):
                return {"chunk_assignments": [], "incomplete_fetch": True}

        _run_discover_projection(_Tax(), ["a", "b"], None)
        assert "incomplete" in capsys.readouterr().out

    def test_command_body_calls_the_helper(self) -> None:
        src = (Path(__file__).resolve().parent.parent / "src/nexus/commands/taxonomy_cmd.py").read_text()
        assert "_run_discover_projection(db.taxonomy" in src


class TestDoctorRetry:
    def test_transient_blip_does_not_flap_the_check(self, monkeypatch) -> None:
        """ou4tb critique: one bounded retry — a single transient read
        failure must not FAIL the whole name-vs-embed-dim check; a
        persistently unreadable store still does (test above)."""
        class _FlakyCol(_RaisingCol):
            def __init__(self) -> None:
                self.calls = 0

            def get(self, *a, **k):
                self.calls += 1
                if self.calls == 1:
                    raise _BOOM
                return {"ids": ["c1"]}

        flaky = _FlakyCol()

        class _T3(_RaisingT3):
            def get_collection(self, name):
                return flaky

            def get_embeddings(self, name, ids):
                return [[0.0] * 768]

        monkeypatch.setattr(_db, "make_t3", lambda: _T3())
        report = _run_name_vs_embed_dim()
        assert report["pass"] is True
        assert report["read_errors"] == []
        assert report["checked"] == 1
