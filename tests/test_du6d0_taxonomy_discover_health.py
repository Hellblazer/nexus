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
        return store

    def _run(self, monkeypatch, store):
        import nexus.health as h
        monkeypatch.setattr(
            "nexus.db.t2.http_memory_store.HttpMemoryStore",
            lambda *a, **k: store, raising=False,
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


# ── 4: the "no files changed — skipping discovery" line ─────────────────────


class _FakeMemoryStore:
    def __init__(self, failed: set[str]) -> None:
        self._failed = failed

    def get(self, project: str, title: str) -> dict | None:
        if title in self._failed:
            return {
                "project": project, "title": title,
                "content": '{"last_outcome": "failure", "last_attempt_at": "t1", "error_class": "Boom"}',
            }
        return {
            "project": project, "title": title,
            "content": '{"last_outcome": "success", "last_attempt_at": "t1", "error_class": ""}',
        }


class _FakeDB:
    def __init__(self, memory) -> None:
        self.memory = memory

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def _patch_t2_memory(monkeypatch, failed: set[str]) -> None:
    import nexus.db.t2 as t2mod
    monkeypatch.setattr(
        t2mod, "T2Database", lambda *_a, **_k: _FakeDB(_FakeMemoryStore(failed)),
    )


class TestCollectionsWithFailedDiscover:
    def test_empty_collections_short_circuits(self) -> None:
        from nexus.commands.index import _collections_with_failed_discover
        assert _collections_with_failed_discover([]) == set()

    def test_names_the_failed_collection(self, monkeypatch) -> None:
        from nexus.commands.index import _collections_with_failed_discover
        _patch_t2_memory(monkeypatch, {"code__a"})
        result = _collections_with_failed_discover(["code__a", "docs__b"])
        assert result == {"code__a"}

    def test_all_succeeded_returns_empty(self, monkeypatch) -> None:
        from nexus.commands.index import _collections_with_failed_discover
        _patch_t2_memory(monkeypatch, set())
        result = _collections_with_failed_discover(["code__a", "docs__b"])
        assert result == set()

    def test_probe_error_fails_safe_to_empty(self, monkeypatch) -> None:
        from nexus.commands.index import _collections_with_failed_discover
        import nexus.db.t2 as t2mod

        def _raise(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(t2mod, "T2Database", _raise)
        assert _collections_with_failed_discover(["code__a"]) == set()


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
