# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tb2yj (RDR-169 Gap 6 leg 3): ``nx doctor --check-references``.

stat_source/staleness_signal (nexus.aspect_readers) had no production
caller until this check. Covers:
  is_reference_only()       — pure scheme predicate
  sample_candidates()       — seeded, never pads/repeats
  format_report()           — fresh/stale/dangling/unknown/error counts,
                              per-scheme breakdown, never gated on unknown
  the wiring test           — fails if the flag stops calling stat_source
                              (the RDR-169 post-mortem's own lesson), plus
                              stale/dangling classification through the
                              real run_check_references/staleness_signal
                              path via an injected stat_source result
  real engine substrate     — a real catalog round-trip: all_documents(),
                              source_uri, source_mtime and meta genuinely
                              reach stat_one() the way the real engine
                              serves them, and a chroma:// reference (zero
                              network, always fresh) proves the walk +
                              report end to end.

The real HTTP mechanics of the https:// stat (real HEAD, real
Last-Modified/ETag header parsing, the bounded retry) are already covered
by ``tests/test_aspect_readers_staleness.py`` against a real local server
via the injectable ``http_client`` seam; this file does not re-prove them,
it proves the CATALOG WALK and ORCHESTRATION around them.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from click.testing import CliRunner

from nexus import doctor_references
from nexus.aspect_readers import HTTPS_ETAG_META_KEY, StatFail, StatOk
from nexus.cli import main
from nexus.doctor_references import (
    MAX_CONCURRENCY,
    ReferenceCheckResult,
    check_references,
    estimated_worst_case_s,
    format_report,
    is_reference_only,
    sample_candidates,
)
from tests._catalog_fixture_ops import ActiveCatalog


# ── is_reference_only ────────────────────────────────────────────────────────


class TestIsReferenceOnly:
    def test_https_is_reference_only(self) -> None:
        assert is_reference_only("https://example.com/doc") is True

    def test_obsidian_is_reference_only(self) -> None:
        assert is_reference_only("obsidian://open?vault=v&file=f.md") is True

    def test_x_devonthink_item_is_reference_only(self) -> None:
        assert is_reference_only("x-devonthink-item://UUID") is True

    def test_nx_scratch_is_reference_only(self) -> None:
        assert is_reference_only("nx-scratch://session/s/e") is True

    def test_chroma_is_reference_only(self) -> None:
        assert is_reference_only("chroma://col/doc") is True

    def test_file_is_not_reference_only(self) -> None:
        assert is_reference_only("file:///abs/path.md") is False

    def test_empty_is_not_reference_only(self) -> None:
        assert is_reference_only("") is False

    def test_no_scheme_is_not_reference_only(self) -> None:
        assert is_reference_only("not-a-uri") is False


# ── sample_candidates ─────────────────────────────────────────────────────────


class TestSampleCandidates:
    def test_returns_all_when_fewer_than_sample(self) -> None:
        candidates = list(range(5))
        assert sample_candidates(candidates, 20, seed=1) == candidates

    def test_never_exceeds_sample_size(self) -> None:
        candidates = list(range(500))
        result = sample_candidates(candidates, 50, seed=1)
        assert len(result) == 50

    def test_deterministic_per_seed(self) -> None:
        candidates = list(range(500))
        assert sample_candidates(candidates, 50, seed=7) == sample_candidates(candidates, 50, seed=7)

    def test_never_pads_or_repeats(self) -> None:
        candidates = list(range(500))
        result = sample_candidates(candidates, 50, seed=3)
        assert len(result) == len(set(result))


# ── sizing / concurrency (nexus-0ne1m critique, significant #1) ──────────────


def test_default_sample_is_sized_so_the_check_is_actually_runnable() -> None:
    """50 (the original default) made the worst case ~51 minutes serial;
    10 keeps the default estimate in the low minutes even before
    --references-sample is narrowed for a specific need."""
    assert doctor_references.DEFAULT_SAMPLE == 10


class TestEstimatedWorstCaseS:
    def test_matches_serial_bound_at_concurrency_one(self) -> None:
        serial = estimated_worst_case_s(10, concurrency=1)
        per_call = doctor_references._https_worst_case_per_call_s()
        assert serial == pytest.approx(10 * per_call)

    def test_concurrency_divides_the_bound(self) -> None:
        per_call = doctor_references._https_worst_case_per_call_s()
        assert estimated_worst_case_s(8, concurrency=8) == pytest.approx(per_call)
        assert estimated_worst_case_s(16, concurrency=8) == pytest.approx(2 * per_call)

    def test_concurrency_never_exceeds_the_sample_size(self) -> None:
        """8 workers for a sample of 3 is nonsensical -- workers is capped
        at the sample size, so this reads the same as concurrency=3."""
        per_call = doctor_references._https_worst_case_per_call_s()
        assert estimated_worst_case_s(3, concurrency=8) == pytest.approx(per_call)

    def test_zero_sample_is_zero(self) -> None:
        assert estimated_worst_case_s(0) == 0.0

    def test_default_concurrency_is_max_concurrency(self) -> None:
        assert estimated_worst_case_s(16) == pytest.approx(estimated_worst_case_s(16, MAX_CONCURRENCY))


class TestCheckReferencesConcurrency:
    """A deterministic fake stat_source proves check_references actually
    runs candidates concurrently, bounded to MAX_CONCURRENCY, rather than
    serially (the exact 'no mitigation' shape the critique names)."""

    def test_runs_with_bounded_concurrency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lock = threading.Lock()
        state = {"current": 0, "max_seen": 0}

        def _fake_stat_one(entry: Any, *, http_client: Any) -> ReferenceCheckResult:
            with lock:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1
            return ReferenceCheckResult(
                scheme="https", tumbler=str(entry), title="t",
                source_uri="https://example.invalid/doc", signal="fresh",
            )

        monkeypatch.setattr(doctor_references, "stat_one", _fake_stat_one)

        candidates = list(range(20))
        start = time.monotonic()
        results = check_references(candidates, http_client=None)
        elapsed = time.monotonic() - start

        assert len(results) == 20
        assert [r.tumbler for r in results] == [str(c) for c in candidates], (
            "ThreadPoolExecutor.map preserves input order in its results"
        )
        assert state["max_seen"] > 1, "never actually ran concurrently"
        assert state["max_seen"] <= MAX_CONCURRENCY
        # Serial would be 20 * 0.05s = 1.0s; bounded concurrency of up to 8
        # should finish well under half that.
        assert elapsed < 0.5, f"took {elapsed:.2f}s -- looks serial, not concurrent"

    def test_empty_candidates_returns_empty_without_a_pool(self) -> None:
        assert check_references([], http_client=None) == []


# ── format_report ─────────────────────────────────────────────────────────────


def _row(scheme: str, signal: str | None = None, error: str | None = None, tumbler: str = "1.1.1") -> ReferenceCheckResult:
    return ReferenceCheckResult(
        scheme=scheme, tumbler=tumbler, title="T", source_uri=f"{scheme}://x", signal=signal, error=error,
    )


class TestFormatReport:
    def test_not_applicable_case_is_handled_by_the_caller_not_format_report(self) -> None:
        """format_report always assumes a non-empty results list — the
        empty-candidates 'not applicable' short-circuit lives in
        run_check_references, before format_report is ever called."""
        lines, ok = format_report([], total_candidates=0, sample=50, seed=1)
        assert ok  # zero results -> no stale/dangling/error found
        assert "0 reference-only document(s) sampled" in lines[0]

    def test_all_fresh_is_ok(self) -> None:
        results = [_row("https", "fresh"), _row("obsidian", "fresh")]
        lines, ok = format_report(results, total_candidates=2, sample=50, seed=1)
        assert ok
        assert "2 fresh, 0 stale, 0 dangling, 0 unknown, 0 not stattable" in lines[0]

    def test_stale_fails_and_is_named(self) -> None:
        results = [_row("https", "stale", tumbler="1.1.5")]
        lines, ok = format_report(results, total_candidates=1, sample=50, seed=1)
        assert not ok
        assert any("1.1.5" in line for line in lines)
        assert any("Stale (1)" in line for line in lines)

    def test_dangling_fails_and_is_named(self) -> None:
        results = [_row("obsidian", "dangling", tumbler="1.1.9")]
        lines, ok = format_report(results, total_candidates=1, sample=50, seed=1)
        assert not ok
        assert any("Dangling (1)" in line for line in lines)
        assert any("1.1.9" in line for line in lines)

    def test_unknown_alone_never_fails(self) -> None:
        """'unknown' never fails this check on its own — an indeterminate
        check is not evidence of a real problem."""
        results = [_row("https", "unknown"), _row("https", "unknown")]
        lines, ok = format_report(results, total_candidates=2, sample=50, seed=1)
        assert ok
        assert "2 unknown" in lines[0]

    def test_error_fails(self) -> None:
        results = [_row("https", error="RuntimeError: boom")]
        lines, ok = format_report(results, total_candidates=1, sample=50, seed=1)
        assert not ok
        assert any("Not stattable (1)" in line for line in lines)

    def test_per_scheme_breakdown(self) -> None:
        results = [
            _row("https", "fresh"), _row("https", "stale"),
            _row("obsidian", "fresh"),
        ]
        lines, _ = format_report(results, total_candidates=3, sample=50, seed=1)
        scheme_lines = [line for line in lines if "https:" in line or "obsidian:" in line]
        assert any("https: fresh=1 stale=1 dangling=0 unknown=0 error=0" in line for line in scheme_lines)
        assert any("obsidian: fresh=1 stale=0 dangling=0 unknown=0 error=0" in line for line in scheme_lines)

    def test_stale_list_is_capped(self) -> None:
        results = [_row("https", "stale", tumbler=f"1.1.{i}") for i in range(15)]
        lines, ok = format_report(results, total_candidates=15, sample=50, seed=1)
        assert not ok
        assert any("... and 5 more" in line for line in lines)


# ── wiring test (RDR-169 post-mortem's own lesson) ───────────────────────────


class _WiringFakeEntry:
    tumbler = "1.1.1"
    title = "Reference doc"
    alias_of = ""
    source_uri = "https://example.invalid/doc"
    source_mtime = 0.0
    meta: dict = {}


class _WiringFakeCat:
    def all_documents(self, limit: int = 0) -> list[Any]:
        return [_WiringFakeEntry()]


def test_check_references_calls_stat_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wiring test that fails if the --check-references flag stops
    calling stat_source — the exact 'built and not wired' shape the
    RDR-169 post-mortem names as its own Gap 3 and fix."""
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _WiringFakeCat(),
    )
    calls: list[str] = []

    def _fake_stat_source(uri: str, **_kw: Any) -> StatOk:
        calls.append(uri)
        return StatOk(current_mtime=0.0)

    monkeypatch.setattr(doctor_references, "stat_source", _fake_stat_source)

    doctor_references.run_check_references(sample=50, seed=1)

    assert calls == ["https://example.invalid/doc"], (
        "run_check_references no longer calls stat_source over its "
        "reference-only candidates — this is the exact silent-disconnect "
        "shape the RDR-169 post-mortem's Gap 3 names"
    )


def test_no_reference_only_documents_is_not_applicable(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class _EmptyCat:
        def all_documents(self, limit: int = 0) -> list[Any]:
            return []

    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _EmptyCat(),
    )
    doctor_references.run_check_references(sample=50, seed=1)
    out = capsys.readouterr().out
    assert "not applicable" in out
    assert "✗" not in out


def test_unreadable_catalog_is_a_hard_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise RuntimeError("engine unreachable")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", _boom)
    with pytest.raises(SystemExit):
        doctor_references.run_check_references(sample=50, seed=1)


# ── classification through the real run_check_references path ──────────────
#
# Same fake-catalog seam the wiring test above uses, but with a scripted
# stat_source result — proves stale/dangling propagate through the REAL
# run_check_references -> stat_one -> staleness_signal -> format_report
# chain and set the CLI exit code, without needing a real network call
# (that mechanic is aspect_readers' own, covered in its test file).


class _OneEntryCat:
    def __init__(self, entry: Any) -> None:
        self._entry = entry

    def all_documents(self, limit: int = 0) -> list[Any]:
        return [self._entry]


class _FakeEntry:
    def __init__(self, source_uri: str, *, source_mtime: float = 0.0, meta: dict | None = None) -> None:
        self.tumbler = "1.1.1"
        self.title = "Reference doc"
        self.alias_of = ""
        self.source_uri = source_uri
        self.source_mtime = source_mtime
        self.meta = meta or {}


def test_stale_stat_result_fails_the_real_check(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _FakeEntry("https://example.invalid/doc", source_mtime=0.0)
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _OneEntryCat(entry),
    )
    monkeypatch.setattr(
        doctor_references, "stat_source",
        lambda uri, **_kw: StatOk(current_mtime=1_000_000.0),  # recorded 0.0 < current -> stale
    )
    with pytest.raises(SystemExit) as exc_info:
        doctor_references.run_check_references(sample=50, seed=1)
    assert exc_info.value.code == 1


def test_absent_stat_result_is_dangling_and_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _FakeEntry("obsidian://open?vault=v&file=f.md")
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _OneEntryCat(entry),
    )
    monkeypatch.setattr(
        doctor_references, "stat_source",
        lambda uri, **_kw: StatFail(reason="absent", detail="gone"),
    )
    with pytest.raises(SystemExit) as exc_info:
        doctor_references.run_check_references(sample=50, seed=1)
    assert exc_info.value.code == 1


def test_recorded_etag_is_forwarded_from_entry_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """stat_one reads the recorded ETag out of the catalog entry's own
    meta (HTTPS_ETAG_META_KEY) and forwards it to stat_source — the half
    of nexus-0ne1m's contract this check is responsible for."""
    entry = _FakeEntry(
        "https://example.invalid/doc", meta={HTTPS_ETAG_META_KEY: '"abc123"'},
    )
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _OneEntryCat(entry),
    )
    captured: dict[str, Any] = {}

    def _fake_stat_source(uri: str, *, recorded_etag: str | None = None, **_kw: Any) -> StatOk:
        captured["recorded_etag"] = recorded_etag
        return StatOk(current_mtime=None, etag_stale=False)

    monkeypatch.setattr(doctor_references, "stat_source", _fake_stat_source)

    doctor_references.run_check_references(sample=50, seed=1)

    assert captured["recorded_etag"] == '"abc123"'


# ── real engine substrate: the catalog round-trip itself ────────────────────
#
# The real HTTPS network mechanics are aspect_readers' own (already covered
# against a real local server in tests/test_aspect_readers_staleness.py);
# what needs a REAL engine is proving all_documents()/source_uri/
# source_mtime/meta genuinely round-trip the way stat_one expects. chroma://
# is content-addressed (always fresh, zero network), so it exercises that
# round-trip without needing a fake TLS stack for https://.


class TestRealCatalogRoundTrip:
    def test_chroma_reference_reads_fresh_file_reference_excluded(self, t2_service_env) -> None:
        cat = ActiveCatalog()
        cat.register_owner("test-repo", "repo", repo_hash="abcd1234")
        from nexus.catalog.tumbler import Tumbler

        ref_tumbler = cat.register(
            Tumbler.parse("1.1"), "Chroma reference doc",
            content_type="knowledge", source_uri="chroma://knowledge__thing/some-doc",
        )
        cat.register(
            Tumbler.parse("1.1"), "File-backed doc",
            content_type="knowledge", source_uri="file:///abs/notes/thing.md",
        )

        result = CliRunner().invoke(main, ["doctor", "--check-references"])

        assert result.exit_code == 0, result.output
        assert "1 reference-only document(s) sampled (of 1 candidate(s))" in result.output
        assert "1 fresh, 0 stale" in result.output
        assert str(ref_tumbler) not in result.output  # only stale/dangling rows are named

    def test_record_https_etag_merges_with_preexisting_meta(self, t2_service_env) -> None:
        """nexus-0ne1m critique (verified-correct-but-undertested note):
        record_https_etag's docstring claims the ENGINE's meta write is a
        MERGE (jsonb_concat), never a bare replace -- confirmed by reading
        CatalogRepository.java, but every prior test exercised this via a
        fake writer, so a future engine-side regression (replace instead
        of merge) would go undetected. This registers with a real,
        unrelated meta key through the REAL engine, calls
        record_https_etag for real, and reads the row back for real."""
        from nexus.aspect_readers import HTTPS_ETAG_META_KEY, record_https_etag
        from nexus.catalog.tumbler import Tumbler

        class _FakeHttpsResponse:
            status_code = 200
            headers = {"etag": '"merge-test-etag"'}

        class _FakeHttpsClient:
            def head(self, uri: str) -> _FakeHttpsResponse:
                return _FakeHttpsResponse()

        cat = ActiveCatalog()
        cat.register_owner("test-repo", "repo", repo_hash="abcd1234")

        tumbler = cat.register(
            Tumbler.parse("1.1"), "Merge-test doc",
            content_type="knowledge", source_uri="https://example.invalid/merge-doc",
            meta={"pre_existing_key": "pre_existing_value"},
        )

        record_https_etag(
            cat, tumbler, "https://example.invalid/merge-doc",
            http_client=_FakeHttpsClient(),
        )

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.meta.get("pre_existing_key") == "pre_existing_value", (
            "the pre-existing meta key must survive the ETag write -- a "
            "bare replace instead of jsonb_concat would silently drop it"
        )
        assert entry.meta.get(HTTPS_ETAG_META_KEY) == '"merge-test-etag"'

    def test_no_reference_only_documents_on_a_real_fresh_tenant(self, t2_service_env) -> None:
        """A virgin tenant with only file-backed documents (or none at
        all) is not applicable — the nexus-7zhag doctrine."""
        cat = ActiveCatalog()
        cat.register_owner("test-repo", "repo", repo_hash="abcd1234")
        from nexus.catalog.tumbler import Tumbler

        cat.register(
            Tumbler.parse("1.1"), "File-backed doc",
            content_type="knowledge", source_uri="file:///abs/notes/thing.md",
        )

        result = CliRunner().invoke(main, ["doctor", "--check-references"])

        assert result.exit_code == 0, result.output
        assert "not applicable" in result.output


# ── writer-side wiring (nexus-0ne1m): register/update call record_https_etag ─


class TestWriterWiring:
    """record_https_etag is called, unconditionally, right after every
    register/update — its own internal scheme check is what makes it a
    no-op for non-https, not the call site. This pins that the call sites
    themselves never stop making the call (the same 'built and not wired'
    failure shape nexus-tb2yj's own wiring test guards)."""

    def test_cli_register_calls_record_https_etag(
        self, t2_service_env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cat = ActiveCatalog()
        cat.register_owner("test-repo", "repo", repo_hash="abcd1234")

        calls: list[tuple[Any, str]] = []
        monkeypatch.setattr(
            "nexus.aspect_readers.record_https_etag",
            lambda writer, tumbler, source_uri, **_kw: calls.append((tumbler, source_uri)),
        )

        result = CliRunner().invoke(
            main,
            [
                "catalog", "register", "--title", "T", "--owner", "1.1",
                "--source-uri", "https://example.invalid/doc",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert calls[0][1] == "https://example.invalid/doc"

    def test_cli_update_calls_record_https_etag_only_with_source_uri(
        self, t2_service_env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cat = ActiveCatalog()
        cat.register_owner("test-repo", "repo", repo_hash="abcd1234")
        from nexus.catalog.tumbler import Tumbler

        tumbler = cat.register(Tumbler.parse("1.1"), "T", content_type="knowledge")

        calls: list[tuple[Any, str]] = []
        monkeypatch.setattr(
            "nexus.aspect_readers.record_https_etag",
            lambda writer, t, source_uri, **_kw: calls.append((t, source_uri)),
        )

        no_uri = CliRunner().invoke(main, ["catalog", "update", str(tumbler), "--title", "T2"])
        assert no_uri.exit_code == 0, no_uri.output
        assert calls == []

        with_uri = CliRunner().invoke(
            main, ["catalog", "update", str(tumbler), "--source-uri", "https://example.invalid/doc"],
        )
        assert with_uri.exit_code == 0, with_uri.output
        assert len(calls) == 1
        assert str(calls[0][0]) == str(tumbler)
        assert calls[0][1] == "https://example.invalid/doc"

    def test_mcp_register_calls_record_https_etag(
        self, t2_service_env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cat = ActiveCatalog()
        cat.register_owner("test-repo", "repo", repo_hash="abcd1234")

        calls: list[tuple[Any, str]] = []
        monkeypatch.setattr(
            "nexus.aspect_readers.record_https_etag",
            lambda writer, tumbler, source_uri, **_kw: calls.append((tumbler, source_uri)),
        )

        from nexus.mcp.catalog import catalog_register

        out = catalog_register(
            title="T", owner="1.1", source_uri="https://example.invalid/doc",
        )
        assert "error" not in out, out
        assert len(calls) == 1
        assert calls[0][1] == "https://example.invalid/doc"
