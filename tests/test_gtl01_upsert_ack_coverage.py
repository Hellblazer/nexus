# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gtl01: upsert-chunks ACK coverage — the client-side half of the
2026-08-08 self-diagnosis arc's final named residue.

T2 ``debug-scenario-journeys-parallel-red-2026-08-08`` (recurrence comment,
2026-08-08 ~13:31): a scenario journey took probe present=0 -> branch=
full_upsert_no_existing count=1 -> no exception -> ~26ms later the engine
refused completion with the chunk genuinely absent. Undecidable at the
time because ``HttpVectorClient.upsert_chunks_with_embeddings`` (the method
that branch calls) logged NOTHING about its own request or the engine's
response — T2 ``critique-diagnostics-refusal-2026-08-08`` round 2 named this
exact gap as the one residue neither prior round closed.

This file pins the new ``http_vector_upsert_chunks_request`` /
``http_vector_upsert_chunks_response`` events on ``HttpVectorClient.
upsert_chunks`` (and, by delegation, ``upsert_chunks_with_embeddings`` —
the method every production indexer path actually calls). Both fire at
INFO, not DEBUG: unlike the doc_indexer.py decision-trail events (all
DEBUG, captured only because the scenario journeys now pass
``NEXUS_LOG_LEVEL=DEBUG``), this is the client's only account of whether a
write was attempted and what the engine acknowledged. INFO does NOT survive
the CLI's untouched WARNING default — it is visible under
``NEXUS_LOG_LEVEL=INFO`` (a realistic troubleshooting setting) and
trivially under the scenario journeys' DEBUG env; daemon-family modes
(mcp/watchdog/t3_daemon/storage_service) default to INFO, so a background
flush emits ~2 lines per page into their rotating logs.

Uses the same ``capture_logs()`` + ``structlog.reset_defaults()`` pattern
as ``tests/test_gtl01_upsert_skip_reembed_diagnostics.py`` and
``tests/test_5xn3k_update_chunks_missing.py`` — ``capture_logs()`` only
swaps structlog's *processors*, not ``wrapper_class``, and this repo's
default config (``nexus.logging_setup``) filters below WARNING.
"""
from __future__ import annotations

import structlog
from structlog.testing import capture_logs

from nexus.db.http_vector_client import HttpVectorClient

_COLL = "code__x__stub-code-1024__v1"


def _capture_logs():
    structlog.reset_defaults()
    return capture_logs()


def _client() -> HttpVectorClient:
    return HttpVectorClient()


def _events(logs: list[dict], event: str) -> list[dict]:
    return [e for e in logs if e["event"] == event]


def _fake_post(response):
    def _post(path, body, *, tenant="default", timeout=120):
        assert path == "/v1/vectors/upsert-chunks"
        return response

    return _post


class TestRequestLoggedAtInfo:
    """The outgoing request is logged BEFORE the POST, unconditionally —
    the only client-side evidence a write was even attempted."""

    def test_single_page_logs_collection_and_counts(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _fake_post({"upserted": 2}),
        )
        with _capture_logs() as logs:
            _client().upsert_chunks(
                _COLL,
                ids=["a" * 64, "b" * 64],
                documents=["doc a", "doc b"],
                metadatas=[{}, {}],
            )

        reqs = _events(logs, "http_vector_upsert_chunks_request")
        assert len(reqs) == 1, f"expected exactly one, got: {logs}"
        assert reqs[0]["collection"] == _COLL
        assert reqs[0]["count"] == 2
        assert reqs[0]["distinct_chash_count"] == 2
        assert reqs[0]["page"] == 1
        assert reqs[0]["pages"] == 1
        assert reqs[0]["log_level"] == "info", (
            "visible under NEXUS_LOG_LEVEL=INFO and daemon-family default "
            "INFO modes — DEBUG would not be"
        )

    def test_duplicate_ids_in_batch_diverge_count_from_distinct_chash_count(
        self, monkeypatch,
    ):
        """A batch containing a duplicate id collapses server-side
        (``PgVectorRepository.upsertChunksInternal``'s in-batch dedup,
        engine event ``upsert_dedup_collapsed``) — the client can't see
        that collapse directly, but a count/distinct_chash_count divergence
        on the OUTGOING request is itself a signal worth having logged."""
        dup = "a" * 64
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _fake_post({"upserted": 2}),
        )
        with _capture_logs() as logs:
            _client().upsert_chunks(
                _COLL,
                ids=[dup, dup],
                documents=["doc a", "doc a again"],
                metadatas=[{}, {}],
            )

        reqs = _events(logs, "http_vector_upsert_chunks_request")
        assert len(reqs) == 1
        assert reqs[0]["count"] == 2
        assert reqs[0]["distinct_chash_count"] == 1

    def test_force_re_embed_forwarded_into_the_request_log(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _fake_post({"upserted": 1}),
        )
        with _capture_logs() as logs:
            _client().upsert_chunks(
                _COLL, ids=["a" * 64], documents=["doc"], metadatas=[{}],
                force_re_embed=True,
            )

        reqs = _events(logs, "http_vector_upsert_chunks_request")
        assert reqs[0]["force_re_embed"] is True


class TestResponseVerdictLoggedAtInfo:
    """The response verdict — acked count, whether the ``upserted`` field
    was present at all, and whether it matches what was sent — logged
    unconditionally, BEFORE the ack-mismatch raise (so it survives even
    when the caller only sees the exception, not the log stream)."""

    def test_intact_ack_logs_match_true(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _fake_post({"upserted": 2}),
        )
        with _capture_logs() as logs:
            _client().upsert_chunks(
                _COLL, ids=["a" * 64, "b" * 64], documents=["d1", "d2"],
                metadatas=[{}, {}],
            )

        resp = _events(logs, "http_vector_upsert_chunks_response")
        assert len(resp) == 1
        assert resp[0]["collection"] == _COLL
        assert resp[0]["sent"] == 2
        assert resp[0]["acked"] == 2
        assert resp[0]["ack_present"] is True
        assert resp[0]["match"] is True
        assert resp[0]["log_level"] == "info"

    def test_missing_ack_field_logs_ack_present_false_before_raising(
        self, monkeypatch,
    ):
        """No ``upserted`` key at all — the engine gave no usable verdict.
        This is the finding itself (see the source comment): the raise
        still fires (None != sent), but ``ack_present=False`` distinguishes
        'no signal given' from 'a signal given and it disagreed'."""
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post", _fake_post({}),
        )
        with _capture_logs() as logs:
            try:
                _client().upsert_chunks(
                    _COLL, ids=["a" * 64], documents=["d1"], metadatas=[{}],
                )
                raised = False
            except RuntimeError:
                raised = True

        assert raised, "a missing ack field must still raise (unchanged house pattern)"
        resp = _events(logs, "http_vector_upsert_chunks_response")
        assert len(resp) == 1, "the verdict must be logged BEFORE the raise, not skipped by it"
        assert resp[0]["acked"] is None
        assert resp[0]["ack_present"] is False
        assert resp[0]["match"] is False

    def test_mismatched_but_present_ack_logs_ack_present_true_match_false(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _fake_post({"upserted": 1}),
        )
        with _capture_logs() as logs:
            try:
                _client().upsert_chunks(
                    _COLL, ids=["a" * 64, "b" * 64], documents=["d1", "d2"],
                    metadatas=[{}, {}],
                )
                raised = False
            except RuntimeError:
                raised = True

        assert raised
        resp = _events(logs, "http_vector_upsert_chunks_response")
        assert len(resp) == 1
        assert resp[0]["sent"] == 2
        assert resp[0]["acked"] == 1
        assert resp[0]["ack_present"] is True
        assert resp[0]["match"] is False


class TestPagingLogsOnePairPerPage:
    def test_two_pages_log_two_request_and_response_pairs(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.http_vector_client.per_collection_chunk_cap",
            lambda collection: 1,
        )
        posts: list[dict] = []

        def _post(path, body, *, tenant="default", timeout=120):
            posts.append(body)
            return {"upserted": len(body["ids"])}

        monkeypatch.setattr("nexus.db.http_vector_client._post", _post)
        with _capture_logs() as logs:
            _client().upsert_chunks(
                _COLL, ids=["a" * 64, "b" * 64], documents=["d1", "d2"],
                metadatas=[{}, {}],
            )

        assert len(posts) == 2, "cap=1 over 2 ids must page into 2 POSTs"
        reqs = _events(logs, "http_vector_upsert_chunks_request")
        resps = _events(logs, "http_vector_upsert_chunks_response")
        assert len(reqs) == 2
        assert len(resps) == 2
        assert [r["page"] for r in reqs] == [1, 2]
        assert [r["pages"] for r in reqs] == [2, 2]
        assert all(r["count"] == 1 for r in reqs)
        assert all(r["match"] is True for r in resps)


class TestUpsertChunksWithEmbeddingsInheritsCoverage:
    """``upsert_chunks_with_embeddings`` is the method every production
    indexer path actually calls (doc_indexer.py's ``_upsert_skip_reembed``,
    code_indexer.py, prose_indexer.py, exporter.py) — it delegates straight
    to ``upsert_chunks``, so it must inherit both new events without any
    separate wiring."""

    def test_request_and_response_events_fire_through_the_embeddings_shim(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _fake_post({"upserted": 1}),
        )
        with _capture_logs() as logs:
            _client().upsert_chunks_with_embeddings(
                _COLL,
                ids=["a" * 64],
                documents=["doc a"],
                embeddings=[[0.1, 0.2]],
                metadatas=[{}],
            )

        reqs = _events(logs, "http_vector_upsert_chunks_request")
        resps = _events(logs, "http_vector_upsert_chunks_response")
        assert len(reqs) == 1
        assert len(resps) == 1
        assert resps[0]["match"] is True
