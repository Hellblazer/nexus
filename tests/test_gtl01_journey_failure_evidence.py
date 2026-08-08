# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gtl01: unit coverage for the two pure helpers behind
``tests/test_scenario_journeys.py``'s ``_engine_log_on_failure`` autouse
fixture — widened failure-tail selection (missing-chash + upsert-event
lines, not just the bare refusal WARN) and the invocation-environment
snapshot (NEXUS_* env, xdist worker id, load average).

These two functions are pure (no pytest-node/fixture plumbing), so they're
tested directly rather than through the fixture's ``rep_call``-gated
integration path — the fixture itself was already empirically verified to
render a report section on failure (T2 diagnostics-completion-refusal-
2026-08-08 FIX-UP ROUND, Item 3), and this file adds the missing unit
coverage for what changed underneath it: the SELECTION logic that decides
which lines make it into that section.
"""
from __future__ import annotations

import os

from tests.test_scenario_journeys import (
    _invocation_env_snapshot,
    _missing_chashes_from,
    _select_failure_evidence_lines,
)

_REFUSAL_LINE = (
    "2026-08-08T20:33:00.000Z WARN event=complete_index_run_refused tenant=t1 "
    "doc_id=1.2.3 collection=docs__x__bge-base-en-v15-768__v1 referenced=1 "
    "present=0 missing=1 claimed_chunk_count=1 "
    "missing_chash_sample=[606712dfaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]\n"
)
_ZERO_MISSING_REFUSAL_LINE = (
    "2026-08-08T20:33:00.000Z WARN event=complete_index_run_refused tenant=t1 "
    "doc_id=1.2.3 collection=docs__x__bge-base-en-v15-768__v1 referenced=2 "
    "present=1 missing=0 claimed_chunk_count=1 missing_chash_sample=[]\n"
)
_UNRELATED_LINE = "2026-08-08T20:33:00.000Z INFO event=egress_proxy_configured source=x\n"


class TestMissingChashesFrom:
    def test_single_chash_sample_extracted(self):
        assert _missing_chashes_from([_REFUSAL_LINE]) == [
            "606712dfaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ]

    def test_multi_chash_sample_split_and_stripped(self):
        line = (
            "WARN event=complete_index_run_refused ... "
            "missing_chash_sample=[aaaa, bbbb, cccc]\n"
        )
        assert _missing_chashes_from([line]) == ["aaaa", "bbbb", "cccc"]

    def test_empty_sample_yields_nothing(self):
        """The zero-content refusal shape (missing=0, referenced!=chunkCount)
        legitimately logs an empty sample — must not be mistaken for a
        parse failure or contribute spurious substring matches."""
        assert _missing_chashes_from([_ZERO_MISSING_REFUSAL_LINE]) == []

    def test_no_refusal_lines_yields_nothing(self):
        assert _missing_chashes_from([]) == []

    def test_line_without_the_field_is_skipped_not_erroring(self):
        assert _missing_chashes_from([_UNRELATED_LINE]) == []


_UPSERT_DEDUP_LINE = "2026-08-08T20:33:00.010Z INFO event=upsert_dedup_collapsed collection=docs__x__bge-base-en-v15-768__v1 received=2 kept=1 collapsed=1\n"
_UPSERT_EMBED_SKIPPED_LINE = "2026-08-08T20:33:00.020Z INFO event=upsert_embed_skipped collection=docs__x__bge-base-en-v15-768__v1 skipped=1 embedded=0\n"
_CHASH_MENTIONED_ELSEWHERE_LINE = (
    "2026-08-08T20:33:00.030Z DEBUG some other line mentioning chash "
    "606712dfaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa in passing\n"
)


class TestSelectFailureEvidenceLines:
    def test_refusal_line_alone_selected_as_refusal(self):
        refusal, upsert = _select_failure_evidence_lines([_REFUSAL_LINE, _UNRELATED_LINE])
        assert refusal == [_REFUSAL_LINE]
        assert upsert == []

    def test_known_upsert_events_selected_even_without_a_refusal(self):
        """The widening applies independent of whether a refusal line was
        found at all — an upsert-event-only log (no refusal this run) still
        surfaces its upsert trace rather than falling back to the blind
        tail, since the caller treats 'refusal_lines or upsert_lines' as
        the non-fallback condition."""
        refusal, upsert = _select_failure_evidence_lines(
            [_UNRELATED_LINE, _UPSERT_DEDUP_LINE, _UPSERT_EMBED_SKIPPED_LINE],
        )
        assert refusal == []
        assert upsert == [_UPSERT_DEDUP_LINE, _UPSERT_EMBED_SKIPPED_LINE]

    def test_line_mentioning_a_missing_chash_is_pulled_in(self):
        refusal, upsert = _select_failure_evidence_lines(
            [_REFUSAL_LINE, _CHASH_MENTIONED_ELSEWHERE_LINE, _UNRELATED_LINE],
        )
        assert refusal == [_REFUSAL_LINE]
        assert upsert == [_CHASH_MENTIONED_ELSEWHERE_LINE]

    def test_refusal_lines_never_duplicated_into_upsert_lines(self):
        """A refusal line naming an upsert-event keyword or its own chash
        (self-reference) must not double-count into both lists."""
        line = _REFUSAL_LINE.rstrip("\n") + " event=upsert_dedup_collapsed\n"
        refusal, upsert = _select_failure_evidence_lines([line])
        assert refusal == [line]
        assert upsert == []

    def test_zero_missing_refusal_pulls_in_no_spurious_chash_lines(self):
        """An empty missing_chash_sample must not become a substring that
        matches everything (empty-string 'in' every line is True) — the
        helper explicitly guards against that."""
        refusal, upsert = _select_failure_evidence_lines(
            [_ZERO_MISSING_REFUSAL_LINE, _UNRELATED_LINE],
        )
        assert refusal == [_ZERO_MISSING_REFUSAL_LINE]
        assert upsert == []

    def test_neither_present_yields_two_empty_lists(self):
        refusal, upsert = _select_failure_evidence_lines([_UNRELATED_LINE])
        assert refusal == []
        assert upsert == []

    def test_upsert_lines_capped_to_last_200_matches(self):
        """Critique round 3, item A2: engine.log is session-scoped, so a
        full-run log can carry thousands of upsert-event lines from
        unrelated tests by the time one journey fails. Over 200 matches
        must cap to exactly 200 — the LAST ones, not the first."""
        many = [
            f"2026-08-08T20:33:{i:02d}.000Z INFO event=upsert_dedup_collapsed "
            f"collection=docs__x__bge-base-en-v15-768__v1 received={i} kept={i} collapsed=0\n"
            for i in range(250)
        ]
        refusal, upsert = _select_failure_evidence_lines(many)
        assert refusal == []
        assert len(upsert) == 200
        assert upsert == many[-200:]
        assert upsert[0] != many[0]


class TestInvocationEnvSnapshot:
    def test_includes_only_nexus_and_nx_prefixed_vars(self, monkeypatch):
        """Widened from NEXUS_*-only (critique round 3, item S2/A3): this
        repo's operative invocation env is predominantly NX_* (NX_AGENT,
        NX_SESSION_ID, NX_SERVICE_HOST/PORT), which the original
        NEXUS_*-only filter missed entirely."""
        monkeypatch.setenv("NEXUS_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("NEXUS_SERVICE_URL", "http://localhost:1")
        monkeypatch.setenv("NX_SESSION_ID", "abc123")
        monkeypatch.setenv("NX_AGENT", "developer")
        monkeypatch.setenv("SOME_OTHER_VAR", "should-not-appear")
        snapshot = _invocation_env_snapshot()
        assert "NEXUS_LOG_LEVEL=DEBUG" in snapshot
        assert "NEXUS_SERVICE_URL=http://localhost:1" in snapshot
        assert "NX_SESSION_ID=abc123" in snapshot
        assert "NX_AGENT=developer" in snapshot
        assert "SOME_OTHER_VAR" not in snapshot

    def test_includes_claude_code_session_id(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-42")
        assert "CLAUDE_CODE_SESSION_ID=sess-42" in _invocation_env_snapshot()

    def test_token_shaped_names_redact_the_value_but_show_the_name(self, monkeypatch):
        """NX_SERVICE_TOKEN is live in the scenario journeys' env
        (``t2_service_env`` sets it) — it must never reach pytest failure
        output or CI logs. Name-matching is case-insensitive substring
        (TOKEN/KEY/SECRET/PASSWORD); the name is still listed so the
        redaction itself is visible, only the value is withheld."""
        monkeypatch.setenv("NX_SERVICE_TOKEN", "super-secret-value")
        monkeypatch.setenv("NX_FAKE_TOKEN", "another-secret")
        monkeypatch.setenv("NEXUS_API_KEY", "key-value")
        monkeypatch.setenv("NX_DB_PASSWORD", "pw-value")
        snapshot = _invocation_env_snapshot()
        assert "NX_SERVICE_TOKEN=<redacted>" in snapshot
        assert "NX_FAKE_TOKEN=<redacted>" in snapshot
        assert "NEXUS_API_KEY=<redacted>" in snapshot
        assert "NX_DB_PASSWORD=<redacted>" in snapshot
        assert "super-secret-value" not in snapshot
        assert "another-secret" not in snapshot
        assert "key-value" not in snapshot
        assert "pw-value" not in snapshot

    def test_worker_defaults_to_master_outside_xdist(self, monkeypatch):
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        assert "xdist_worker=master" in _invocation_env_snapshot()

    def test_worker_id_reflects_xdist_env(self, monkeypatch):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
        assert "xdist_worker=gw3" in _invocation_env_snapshot()

    def test_includes_a_load_average(self):
        snapshot = _invocation_env_snapshot()
        assert "load_avg(1,5,15)=" in snapshot
        # getloadavg() is POSIX; this suite only runs on POSIX platforms
        # (darwin/linux per CLAUDE.md), so assert the real numeric form
        # rather than merely tolerating the "unavailable" fallback.
        if hasattr(os, "getloadavg"):
            assert "unavailable" not in snapshot
