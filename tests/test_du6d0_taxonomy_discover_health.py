# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-du6d0: qgc4b's self-heal guard (``_taxonomy_incomplete`` /
``_collections_without_topics`` in ``nexus.commands.index``) only re-runs
taxonomy discovery on a no-change ``nx index repo`` run while a collection
has ZERO topics. Once a collection has produced >=1 topic, a LATER discover
failure (credential expiry, quota, schema drift) has no operator-visible
signal on a repo whose files stopped changing:
``run_collection_postprocessing`` only invokes discover when
``files_changed>0`` or the zero-topic self-heal fires, and the "no files
changed — skipping discovery" line read as reassurance regardless.

Covers, per the bead's TDD plan:

1. A successful discover records the success and clears a prior failure
   (``nexus.mcp_infra.record_taxonomy_discover_attempt`` /
   ``parse_taxonomy_discover_health``).
2. A failed discover records attempt time and error class without
   touching ``taxonomy_meta.last_discover_at`` (structurally true: the
   recording function never touches the taxonomy store at all).
3. The ``nx doctor`` row (``nexus.health._check_taxonomy_discover_health``)
   is n/a on no recorded attempts, ok when the most recent attempt
   succeeded, warn when it failed.
4. The "no files changed — skipping discovery" line names a collection
   whose last discover attempt failed, instead of reading as reassurance.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.mcp_infra import (
    TAXONOMY_DISCOVER_HEALTH_PROJECT,
    parse_taxonomy_discover_health,
    record_taxonomy_discover_attempt,
    taxonomy_discover_health_title,
)


class _FakeMemory:
    """Minimal in-memory stand-in for ``HttpMemoryStore`` (only ``.put`` /
    ``.get`` / ``.get_all`` are used by the code under test)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], str] = {}

    def put(self, project: str, title: str, content: str, tags: str = "", ttl=None) -> int:
        self._rows[(project, title)] = content
        return len(self._rows)

    def get(self, project: str, title: str) -> dict | None:
        content = self._rows.get((project, title))
        if content is None:
            return None
        return {"project": project, "title": title, "content": content}

    def get_all(self, project: str) -> list[dict]:
        return [
            {"project": p, "title": t, "content": c}
            for (p, t), c in self._rows.items() if p == project
        ]


# ── 1 & 2: record_taxonomy_discover_attempt / parse_taxonomy_discover_health ─


class TestRecordTaxonomyDiscoverAttempt:
    def test_success_records_success_and_last_attempt_at(self) -> None:
        mem = _FakeMemory()
        record_taxonomy_discover_attempt(
            mem, "code__nexus", success=True, at="2026-09-27T00:00:00Z",
        )
        entry = mem.get(TAXONOMY_DISCOVER_HEALTH_PROJECT, taxonomy_discover_health_title("code__nexus"))
        assert entry is not None
        record = parse_taxonomy_discover_health(entry["content"])
        assert record["last_outcome"] == "success"
        assert record["last_attempt_at"] == "2026-09-27T00:00:00Z"
        assert record["error_class"] == ""

    def test_success_clears_a_prior_failure(self) -> None:
        mem = _FakeMemory()
        record_taxonomy_discover_attempt(
            mem, "code__nexus", success=False, error_class="ConnectionError",
            at="2026-09-01T00:00:00Z",
        )
        record_taxonomy_discover_attempt(
            mem, "code__nexus", success=True, at="2026-09-27T00:00:00Z",
        )
        entry = mem.get(TAXONOMY_DISCOVER_HEALTH_PROJECT, "code__nexus")
        record = parse_taxonomy_discover_health(entry["content"])
        assert record["last_outcome"] == "success"
        assert record["error_class"] == ""
        assert record["last_attempt_at"] == "2026-09-27T00:00:00Z"

    def test_failure_records_attempt_time_and_error_class(self) -> None:
        mem = _FakeMemory()
        record_taxonomy_discover_attempt(
            mem, "docs__nexus", success=False, error_class="TimeoutError",
            at="2026-09-15T12:00:00Z",
        )
        entry = mem.get(TAXONOMY_DISCOVER_HEALTH_PROJECT, "docs__nexus")
        record = parse_taxonomy_discover_health(entry["content"])
        assert record["last_outcome"] == "failure"
        assert record["error_class"] == "TimeoutError"
        assert record["last_attempt_at"] == "2026-09-15T12:00:00Z"

    def test_failure_never_touches_the_taxonomy_store(self) -> None:
        """Structural proof of requirement 2's 'without touching
        last_discover_at': the function's only collaborator is *memory* —
        it has no taxonomy parameter at all, so it cannot reach
        taxonomy_meta.last_discover_at (the engine column stamped by
        record_discover_count) even in a failure branch."""
        import inspect
        params = inspect.signature(record_taxonomy_discover_attempt).parameters
        assert "taxonomy" not in params
        assert list(params)[:2] == ["memory", "collection"]

    def test_failure_after_success_keeps_writing_only_via_memory_put(self) -> None:
        mem = _FakeMemory()
        record_taxonomy_discover_attempt(mem, "code__nexus", success=True, at="t1")
        record_taxonomy_discover_attempt(
            mem, "code__nexus", success=False, error_class="QuotaExceeded", at="t2",
        )
        entry = mem.get(TAXONOMY_DISCOVER_HEALTH_PROJECT, "code__nexus")
        record = parse_taxonomy_discover_health(entry["content"])
        assert record["last_outcome"] == "failure"
        assert record["error_class"] == "QuotaExceeded"
        assert record["last_attempt_at"] == "t2"

    def test_write_failure_is_swallowed(self) -> None:
        class _BoomMemory:
            def put(self, *a, **k):
                raise RuntimeError("t2 unavailable")

        # Must not raise -- best-effort diagnostic write.
        record_taxonomy_discover_attempt(_BoomMemory(), "code__nexus", success=True)


class TestParseTaxonomyDiscoverHealth:
    def test_malformed_json_parses_to_empty_dict(self) -> None:
        assert parse_taxonomy_discover_health("not json{{{") == {}

    def test_empty_content_parses_to_empty_dict(self) -> None:
        assert parse_taxonomy_discover_health("") == {}

    def test_non_object_json_parses_to_empty_dict(self) -> None:
        assert parse_taxonomy_discover_health("[1, 2, 3]") == {}

    def test_well_formed_content_round_trips(self) -> None:
        mem = _FakeMemory()
        record_taxonomy_discover_attempt(mem, "code__x", success=True, at="2026-01-01T00:00:00Z")
        entry = mem.get(TAXONOMY_DISCOVER_HEALTH_PROJECT, "code__x")
        assert parse_taxonomy_discover_health(entry["content"])["last_outcome"] == "success"


# ── 3: nx doctor row — nexus.health._check_taxonomy_discover_health ──────────


class TestCheckTaxonomyDiscoverHealth:
    def _store(self, entries: list[dict], *, get_all_exc: Exception | None = None):
        calls = {"get_all": 0}

        class _Store:
            closed = False

            def get_all(self, project: str) -> list[dict]:
                calls["get_all"] += 1
                if get_all_exc is not None:
                    raise get_all_exc
                return list(entries)

            def close(self) -> None:
                self.closed = True

        store = _Store()
        store.calls = calls  # type: ignore[attr-defined]
        store._entries = entries  # type: ignore[attr-defined] -- _run's default live-set derives from this
        return store

    def _run(
        self, monkeypatch, store, *,
        live_collections: set[str] | None = None,
        list_collections_exc: Exception | None = None,
        tax_store: object | None = None,
    ):
        """*live_collections*, when omitted, defaults to every title in
        *store*'s own entries -- i.e. "everything the fixture names is
        live" -- so every PRE-EXISTING test (written before the
        review-round-2 stale-row filter) keeps its exact prior semantics
        without having to know the filter exists. Tests exercising
        staleness pass an explicit, narrower set.

        *tax_store* (nexus-l3dg2): the engine-reconcile collaborator.
        Omitted, it defaults to a fake that RAISES -- "engine reconcile
        unavailable" -- so every PRE-EXISTING test (written before the
        engine-reconcile step existed) keeps its exact prior semantics
        (every recorded failure still warns) deterministically, regardless
        of whether a real service happens to be reachable in this test
        environment. Tests exercising reconciliation pass a fake
        ``get_last_discover_stamps`` double instead."""
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.db.t2.http_memory_store.HttpMemoryStore",
            lambda *a, **k: store, raising=False,
        )
        names = (
            live_collections if live_collections is not None
            else {str(e.get("title", "")) for e in getattr(store, "_entries", [])}
        )

        class _T3:
            def list_collections(self) -> list[dict]:
                if list_collections_exc is not None:
                    raise list_collections_exc
                return [{"name": n} for n in names]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _T3(), raising=False)

        if tax_store is None:
            def _raise(*a, **k):
                raise RuntimeError("no service registered")
            monkeypatch.setattr(
                "nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore", _raise, raising=False,
            )
        else:
            monkeypatch.setattr(
                "nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore",
                lambda *a, **k: tax_store, raising=False,
            )
        return h._check_taxonomy_discover_health()[0]

    def test_no_entries_is_not_applicable(self, monkeypatch) -> None:
        store = self._store([])
        r = self._run(monkeypatch, store)
        assert r.ok is True
        assert "not applicable" in r.detail
        assert store.closed is True

    def test_all_succeeded_is_ok(self, monkeypatch) -> None:
        content = '{"last_outcome": "success", "last_attempt_at": "t1", "error_class": ""}'
        store = self._store([{"title": "code__nexus", "content": content}])
        r = self._run(monkeypatch, store)
        assert r.ok is True
        assert r.warn is False
        assert "1 collection(s)" in r.detail

    def test_a_failed_collection_is_a_soft_warning(self, monkeypatch) -> None:
        content = (
            '{"last_outcome": "failure", "last_attempt_at": "2026-09-20T00:00:00Z", '
            '"error_class": "ConnectionError"}'
        )
        store = self._store([{"title": "code__nexus", "content": content}])
        r = self._run(monkeypatch, store)
        assert r.ok is False and r.warn is True
        assert "code__nexus" in r.detail
        assert "2026-09-20T00:00:00Z" in r.detail
        assert "ConnectionError" in r.detail
        assert "nx taxonomy discover --collection <name>" in r.detail
        assert any("nx taxonomy discover" in s for s in r.fix_suggestions)

    def test_mixed_collections_only_names_the_failed_one(self, monkeypatch) -> None:
        ok_content = '{"last_outcome": "success", "last_attempt_at": "t1", "error_class": ""}'
        bad_content = '{"last_outcome": "failure", "last_attempt_at": "t2", "error_class": "Boom"}'
        store = self._store([
            {"title": "docs__ok", "content": ok_content},
            {"title": "code__bad", "content": bad_content},
        ])
        r = self._run(monkeypatch, store)
        assert r.ok is False and r.warn is True
        assert "code__bad" in r.detail
        assert "docs__ok" not in r.detail

    def test_success_after_failure_reads_ok_not_warn(self, monkeypatch) -> None:
        """Upsert semantics: the stored record is always the MOST RECENT
        attempt, so a success recorded after an earlier failure reads
        clean -- 'ok when last success is newer than last failure'."""
        mem = _FakeMemory()
        record_taxonomy_discover_attempt(mem, "code__nexus", success=False, error_class="X", at="t1")
        record_taxonomy_discover_attempt(mem, "code__nexus", success=True, at="t2")
        entries = mem.get_all(TAXONOMY_DISCOVER_HEALTH_PROJECT)
        store = self._store(entries)
        r = self._run(monkeypatch, store)
        assert r.ok is True
        assert r.warn is False

    def test_connect_failure_degrades_to_not_applicable(self, monkeypatch) -> None:
        import nexus.health as h

        def _raise(*a, **k):
            raise RuntimeError("no service registered")

        monkeypatch.setattr(
            "nexus.db.t2.http_memory_store.HttpMemoryStore", _raise, raising=False,
        )
        r = h._check_taxonomy_discover_health()[0]
        assert r.ok is True
        assert "not applicable" in r.detail

    def test_get_all_failure_skips(self, monkeypatch) -> None:
        store = self._store([], get_all_exc=RuntimeError("transport failure"))
        r = self._run(monkeypatch, store)
        assert r.ok is True
        assert "skipped" in r.detail
        assert store.closed is True

    def test_multiple_failed_collections_truncated_at_ten(self, monkeypatch) -> None:
        entries = [
            {
                "title": f"code__{i}",
                "content": f'{{"last_outcome": "failure", "last_attempt_at": "t{i}", "error_class": "E{i}"}}',
            }
            for i in range(12)
        ]
        store = self._store(entries)
        r = self._run(monkeypatch, store)
        assert r.ok is False and r.warn is True
        assert "12 collection(s)" in r.detail
        assert "… 2 more" in r.detail

    def test_stale_failed_entry_is_ignored_and_counted(self, monkeypatch) -> None:
        """Item 2: a collection deleted or renamed after a failed attempt
        must not warn forever -- its entry is dropped and the drop is
        named, not silently absorbed into a clean pass."""
        content = '{"last_outcome": "failure", "last_attempt_at": "t1", "error_class": "Boom"}'
        store = self._store([{"title": "code__deleted", "content": content}])
        r = self._run(monkeypatch, store, live_collections=set())
        assert r.ok is True
        assert r.warn is False
        assert "not applicable" in r.detail
        assert "1 recorded attempt" in r.detail

    def test_stale_and_live_failed_mixed_only_warns_on_the_live_one(self, monkeypatch) -> None:
        bad = '{"last_outcome": "failure", "last_attempt_at": "t1", "error_class": "Boom"}'
        store = self._store([
            {"title": "code__live_bad", "content": bad},
            {"title": "code__deleted", "content": bad},
        ])
        r = self._run(monkeypatch, store, live_collections={"code__live_bad"})
        assert r.ok is False and r.warn is True
        assert "code__live_bad" in r.detail
        assert "code__deleted" not in r.detail
        assert "1 stale entry" in r.detail

    def test_stale_note_pluralizes_correctly(self, monkeypatch) -> None:
        ok = '{"last_outcome": "success", "last_attempt_at": "t1", "error_class": ""}'
        store = self._store([
            {"title": "code__live", "content": ok},
            {"title": "code__deleted_1", "content": ok},
            {"title": "code__deleted_2", "content": ok},
        ])
        r = self._run(monkeypatch, store, live_collections={"code__live"})
        assert r.ok is True
        assert "2 stale entries" in r.detail

    def test_list_collections_failure_fails_open_not_toward_silence(self, monkeypatch) -> None:
        """A failure to check liveness must never silently drop a
        genuine, still-live warning -- it fails OPEN (no filtering),
        never toward treating everything as stale."""
        content = (
            '{"last_outcome": "failure", "last_attempt_at": "2026-09-20T00:00:00Z", '
            '"error_class": "ConnectionError"}'
        )
        store = self._store([{"title": "code__nexus", "content": content}])
        r = self._run(monkeypatch, store, list_collections_exc=RuntimeError("t3 unreachable"))
        assert r.ok is False and r.warn is True
        assert "code__nexus" in r.detail
        assert "stale" not in r.detail

    def test_registered_in_run_health_checks(self) -> None:
        import inspect
        import nexus.health as h
        source = inspect.getsource(h.run_health_checks)
        assert "_check_taxonomy_discover_health()" in source

    def test_row_absent_from_fresh_install_mvv_allowlist(self) -> None:
        """nexus-7zhag doctrine: a NEW doctor row resolves not-applicable
        on a virgin box (no discover attempts recorded yet) and must
        never gain an entry in the fresh-install MVV's doctor warnings
        allowlist."""
        import re as _re

        mvv_path = Path(__file__).resolve().parent / "e2e" / "fresh-install-mvv.sh"
        source = mvv_path.read_text(encoding="utf-8")
        match = _re.search(r"ALLOWLIST_REGEX='([^']*)'", source)
        assert match is not None, "fresh-install-mvv.sh must still define ALLOWLIST_REGEX"
        allowlist_regex = match.group(1)
        assert "taxonomy.discover" not in allowlist_regex
        assert "taxonomy_discover_health" not in allowlist_regex


# ── nexus-l3dg2: engine reconcile against taxonomy_meta.last_discover_at ─────


class _FakeTaxonomyStore:
    """Minimal stand-in for ``HttpTaxonomyStore`` (only
    ``get_last_discover_stamps``/``close`` are used by the reconcile step)."""

    def __init__(self, stamps: dict[str, dict] | None = None, *, exc: Exception | None = None) -> None:
        self._stamps = stamps or {}
        self._exc = exc
        self.closed = False
        self.calls: list[list[str]] = []

    def get_last_discover_stamps(self, collections: list[str]) -> dict[str, dict]:
        self.calls.append(list(collections))
        if self._exc is not None:
            raise self._exc
        return {c: self._stamps[c] for c in collections if c in self._stamps}

    def close(self) -> None:
        self.closed = True


class TestEngineReconcile:
    """nexus-l3dg2 (du6d0 residual, item 3): a T2-write failure right after
    a REAL engine-side discover success must not keep this row warning
    forever — the engine's own ``last_discover_at`` reconciles it."""

    def _content(self, *, outcome: str, at: str, error: str = "Boom") -> str:
        return (
            f'{{"last_outcome": "{outcome}", "last_attempt_at": "{at}", '
            f'"error_class": "{error if outcome == "failure" else ""}"}}'
        )

    def _store(self, entries: list[dict]):
        return TestCheckTaxonomyDiscoverHealth()._store(entries)

    def _run(self, monkeypatch, store, tax_store):
        return TestCheckTaxonomyDiscoverHealth()._run(monkeypatch, store, tax_store=tax_store)

    def test_engine_stamp_newer_than_failure_reads_ok(self, monkeypatch) -> None:
        content = self._content(outcome="failure", at="2026-09-20T00:00:00Z")
        store = self._store([{"title": "code__nexus", "content": content}])
        tax = _FakeTaxonomyStore({
            "code__nexus": {"last_discover_at": "2026-09-21T00:00:00Z", "last_discover_doc_count": 5},
        })
        r = self._run(monkeypatch, store, tax)
        assert r.ok is True
        assert r.warn is False
        assert "reconciled" in r.detail
        assert tax.closed is True

    def test_engine_stamp_older_than_failure_still_warns(self, monkeypatch) -> None:
        content = self._content(outcome="failure", at="2026-09-20T00:00:00Z")
        store = self._store([{"title": "code__nexus", "content": content}])
        tax = _FakeTaxonomyStore({
            "code__nexus": {"last_discover_at": "2026-09-19T00:00:00Z", "last_discover_doc_count": 5},
        })
        r = self._run(monkeypatch, store, tax)
        assert r.ok is False
        assert r.warn is True
        assert "code__nexus" in r.detail

    def test_no_engine_stamp_at_all_still_warns(self, monkeypatch) -> None:
        """A collection the engine has never discovered (absent from the
        batch response) is unreconciled -- absence is not evidence of a
        newer success."""
        content = self._content(outcome="failure", at="2026-09-20T00:00:00Z")
        store = self._store([{"title": "code__nexus", "content": content}])
        tax = _FakeTaxonomyStore({})  # nothing known to the engine
        r = self._run(monkeypatch, store, tax)
        assert r.ok is False and r.warn is True
        assert "code__nexus" in r.detail

    def test_one_engine_call_covers_every_failed_collection(self, monkeypatch) -> None:
        entries = [
            {"title": f"code__{i}", "content": self._content(outcome="failure", at=f"2026-09-{10+i:02d}T00:00:00Z")}
            for i in range(5)
        ]
        store = self._store(entries)
        tax = _FakeTaxonomyStore({})
        self._run(monkeypatch, store, tax)
        assert len(tax.calls) == 1
        assert sorted(tax.calls[0]) == sorted(f"code__{i}" for i in range(5))

    def test_reconcile_skips_succeeded_collections_entirely(self, monkeypatch) -> None:
        """Only FAILED collections are sent to the engine -- reconciling a
        collection that already reads ok would be wasted work."""
        ok = self._content(outcome="success", at="t1")
        bad = self._content(outcome="failure", at="2026-09-20T00:00:00Z")
        store = self._store([
            {"title": "docs__ok", "content": ok},
            {"title": "code__bad", "content": bad},
        ])
        tax = _FakeTaxonomyStore({})
        self._run(monkeypatch, store, tax)
        assert tax.calls == [["code__bad"]]

    def test_engine_404_falls_back_to_prior_behavior_with_a_note(self, monkeypatch) -> None:
        """An engine that predates the route answers 404 (TaxonomyHandler's
        generic route-miss response) -- the row degrades to the
        pre-nexus-l3dg2 behavior (every recorded failure still warns) and
        names the reconcile as unavailable, once."""
        import httpx

        content = self._content(outcome="failure", at="2026-09-20T00:00:00Z")
        store = self._store([{"title": "code__nexus", "content": content}])
        request = httpx.Request("POST", "http://svc/v1/taxonomy/meta/last_discover_batch")
        response = httpx.Response(404, request=request)
        tax = _FakeTaxonomyStore(exc=httpx.HTTPStatusError("not found", request=request, response=response))
        r = self._run(monkeypatch, store, tax)
        assert r.ok is False and r.warn is True
        assert "code__nexus" in r.detail
        assert "engine reconcile unavailable" in r.detail

    def test_default_fake_construction_failure_also_notes_unavailable(self, monkeypatch) -> None:
        """The default ``_run`` fixture (no ``tax_store`` passed) simulates
        a construction-time failure (no service registered at all) --
        every PRE-EXISTING test relies on this degrading exactly like the
        404 case."""
        content = self._content(outcome="failure", at="2026-09-20T00:00:00Z")
        store = self._store([{"title": "code__nexus", "content": content}])
        r = TestCheckTaxonomyDiscoverHealth()._run(monkeypatch, store)
        assert r.ok is False and r.warn is True
        assert "engine reconcile unavailable" in r.detail

    def test_mixed_reconciled_and_still_failed_names_only_the_latter(self, monkeypatch) -> None:
        bad_recent = self._content(outcome="failure", at="2026-09-20T00:00:00Z")
        bad_older_stamp = self._content(outcome="failure", at="2026-09-20T00:00:00Z")
        store = self._store([
            {"title": "code__reconciled", "content": bad_recent},
            {"title": "code__still_bad", "content": bad_older_stamp},
        ])
        tax = _FakeTaxonomyStore({
            "code__reconciled": {"last_discover_at": "2026-09-21T00:00:00Z", "last_discover_doc_count": 1},
            "code__still_bad": {"last_discover_at": "2026-09-19T00:00:00Z", "last_discover_doc_count": 1},
        })
        r = self._run(monkeypatch, store, tax)
        assert r.ok is False and r.warn is True
        assert "code__still_bad" in r.detail
        assert "code__reconciled" not in r.detail
        assert "1 other collection(s) reconciled" in r.detail


# ── 4: the "no files changed — skipping discovery" line ─────────────────────


class _FakeHttpMemoryStore:
    """Minimal stand-in for ``HttpMemoryStore`` (review round 2, nexus-du6d0):
    ``_collections_with_failed_discover`` now takes ONE ``get_all(project)``
    round trip, the same shape ``nx doctor``'s ``taxonomy.discover health``
    row uses, rather than a per-collection ``T2Database`` open."""

    closed = False

    def __init__(self, failed: set[str], all_collections: set[str] | None = None, *, get_all_exc=None) -> None:
        self._failed = failed
        self._all = all_collections if all_collections is not None else failed
        self._get_all_exc = get_all_exc
        self.get_all_calls = 0

    def get_all(self, project: str) -> list[dict]:
        self.get_all_calls += 1
        if self._get_all_exc is not None:
            raise self._get_all_exc
        rows = []
        for col in sorted(self._all):
            outcome = "failure" if col in self._failed else "success"
            rows.append({
                "project": project, "title": col,
                "content": f'{{"last_outcome": "{outcome}", "last_attempt_at": "t1", "error_class": "Boom"}}',
            })
        return rows

    def close(self) -> None:
        self.closed = True


def _patch_http_memory_store(monkeypatch, store) -> None:
    monkeypatch.setattr(
        "nexus.db.t2.http_memory_store.HttpMemoryStore", lambda *_a, **_k: store, raising=False,
    )


class TestCollectionsWithFailedDiscover:
    """Review round 2, nexus-du6d0 item 1: one HttpMemoryStore().get_all(...)
    round trip, filtered to the caller's *collections*, instead of a
    per-collection T2Database probe."""

    def test_empty_collections_short_circuits(self) -> None:
        from nexus.commands.index import _collections_with_failed_discover
        assert _collections_with_failed_discover([]) == set()

    def test_names_the_failed_collection(self, monkeypatch) -> None:
        from nexus.commands.index import _collections_with_failed_discover
        store = _FakeHttpMemoryStore({"code__a"}, {"code__a", "docs__b"})
        _patch_http_memory_store(monkeypatch, store)
        result = _collections_with_failed_discover(["code__a", "docs__b"])
        assert result == {"code__a"}

    def test_all_succeeded_returns_empty(self, monkeypatch) -> None:
        from nexus.commands.index import _collections_with_failed_discover
        store = _FakeHttpMemoryStore(set(), {"code__a", "docs__b"})
        _patch_http_memory_store(monkeypatch, store)
        result = _collections_with_failed_discover(["code__a", "docs__b"])
        assert result == set()

    def test_probe_error_fails_safe_to_empty(self, monkeypatch) -> None:
        from nexus.commands.index import _collections_with_failed_discover
        store = _FakeHttpMemoryStore(set(), get_all_exc=RuntimeError("boom"))
        _patch_http_memory_store(monkeypatch, store)
        assert _collections_with_failed_discover(["code__a"]) == set()

    def test_connect_failure_fails_safe_to_empty(self, monkeypatch) -> None:
        from nexus.commands.index import _collections_with_failed_discover

        def _raise(*a, **k):
            raise RuntimeError("no service registered")

        monkeypatch.setattr(
            "nexus.db.t2.http_memory_store.HttpMemoryStore", _raise, raising=False,
        )
        assert _collections_with_failed_discover(["code__a"]) == set()

    def test_a_failed_entry_outside_collections_is_ignored(self, monkeypatch) -> None:
        """A collection this repo doesn't have is never even considered --
        the filter to *collections* is item 1's own staleness guard for
        the skip line (item 2's report note)."""
        from nexus.commands.index import _collections_with_failed_discover
        store = _FakeHttpMemoryStore(
            {"code__a", "code__deleted_elsewhere"}, {"code__a", "code__deleted_elsewhere"},
        )
        _patch_http_memory_store(monkeypatch, store)
        result = _collections_with_failed_discover(["code__a"])
        assert result == {"code__a"}

    def test_one_round_trip_regardless_of_collection_count(self, monkeypatch) -> None:
        cols = [f"code__{i}" for i in range(20)]
        store = _FakeHttpMemoryStore(set(cols[:3]), set(cols))
        _patch_http_memory_store(monkeypatch, store)
        from nexus.commands.index import _collections_with_failed_discover
        result = _collections_with_failed_discover(cols)
        assert result == set(cols[:3])
        assert store.get_all_calls == 1
        assert store.closed is True


# ── item 4 (review round 2): _record_discover_health survives a facade ──────
# with no `.memory` attribute at all, and logs the swallowed failure at
# WARNING (not DEBUG) so a real facade missing `.memory` is visible.


class _NoMemoryDB:
    """Minimal double standing in for ``db``: no ``.memory`` at all --
    the exact shape that escaped the discover loop's own exception
    handling in review round 1 (evaluating ``db.memory`` as an argument
    expression raises ``AttributeError`` before any try/except inside
    the wrapped function ever runs)."""

    taxonomy = object()


class TestRecordDiscoverHealthWrapper:
    def test_success_branch_survives_missing_memory_attribute(self) -> None:
        from nexus.commands.index import _record_discover_health
        # Must not raise -- the whole point of the wrapper.
        _record_discover_health(_NoMemoryDB(), "code__x", success=True)

    def test_failure_branch_survives_missing_memory_attribute(self) -> None:
        from nexus.commands.index import _record_discover_health
        _record_discover_health(_NoMemoryDB(), "code__x", success=False, error_class="Boom")

    def test_swallowed_failure_logs_at_warning_not_debug(self) -> None:
        from structlog.testing import capture_logs

        from nexus.commands.index import _record_discover_health

        with capture_logs() as cap:
            _record_discover_health(_NoMemoryDB(), "code__x", success=True)

        matches = [e for e in cap if e.get("event") == "taxonomy_discover_health_record_call_failed"]
        assert matches, f"expected a taxonomy_discover_health_record_call_failed log entry, got: {cap}"
        assert matches[0].get("log_level") == "warning", (
            f"a real facade missing .memory must be visible at WARNING, not swallowed at "
            f"DEBUG: got {matches[0].get('log_level')!r}"
        )
        assert matches[0].get("collection") == "code__x"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def index_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _mock_reg() -> MagicMock:
    mock = MagicMock()
    mock.get.return_value = {"code_collection": "code__myrepo__emb__v1"}
    return mock


class TestSkipLineNamesFailedDiscover:
    """Requirement 4: the maintenance-mode skip line says when the last
    discover attempt failed, instead of reading as reassurance."""

    def _run(self, runner: CliRunner, repo: Path, *, failed: set[str]):
        with (
            patch("nexus.commands.index._registry", return_value=_mock_reg()),
            patch("nexus.indexer.index_repository", return_value={"files_changed": 0}),
            patch("nexus.commands.index._collections_without_topics", return_value=set()),
            patch("nexus.commands.index._collections_with_failed_discover", return_value=failed),
            patch("nexus.commands.index.run_collection_postprocessing"),
        ):
            result = runner.invoke(main, ["index", "repo", str(repo)])
        return result

    def _repo(self, index_home: Path) -> Path:
        repo = index_home / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        return repo

    def test_no_failure_keeps_the_plain_skip_line(self, runner: CliRunner, index_home: Path) -> None:
        repo = self._repo(index_home)
        result = self._run(runner, repo, failed=set())
        assert result.exit_code == 0, result.output
        assert "Taxonomy: no files changed — skipping discovery" in result.output
        assert "FAILED" not in result.output

    def test_a_failure_names_the_collection_and_remedy(self, runner: CliRunner, index_home: Path) -> None:
        repo = self._repo(index_home)
        result = self._run(runner, repo, failed={"code__myrepo__emb__v1"})
        assert result.exit_code == 0, result.output
        assert "no files changed — skipping discovery" in result.output
        assert "last discover attempt FAILED" in result.output
        assert "code__myrepo__emb__v1" in result.output
        assert "nx taxonomy discover --collection <name>" in result.output
