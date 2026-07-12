# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pg-source verify-fill entry point (nexus-te885.8.2).

``verify_fill_pg_source`` is the reconcile entry point for rows written
directly to LOCAL pgvector post-cutover that exist in no Chroma store at
all — the substrate behind the 2026-07-01 nexus-te885.1 incident. This
module proves three things:

1. ``verify_fill_pg_source`` wires a :class:`~nexus.migration.pg_read.PgReadClient`
   (opened via :func:`~nexus.migration.vector_etl.resolve_local_service_endpoint`)
   into the UNCHANGED :func:`~nexus.migration.vector_etl.verify_fill_collections`
   with ``leg="pg"`` — mirroring ``verify_fill_local``/``verify_fill_cloud``'s
   own wiring tests (``TestVerifyFillLegRouting`` in ``test_vector_etl.py``).
2. ``resolve_local_service_endpoint`` prioritizes an explicit override,
   falls back to ``discover_lease()`` per-field, and fails loud (never
   silently) when neither resolves.
3. A non-vacuous integration-shaped run: the REAL ``PgReadClient`` adapter
   (mocked only at its HTTP boundary, mirroring ``test_pg_read.py``'s own
   convention) as the SOURCE, a stateful ``FakeVectorClient`` as the
   TARGET — a chunk present ONLY in the pg source gets FILLED, a chunk
   already present in the target is SKIPPED (not re-sent). This proves the
   WIRING correctly threads a real pg-source adapter into
   ``_verify_fill_one``'s existing diff/fill logic; it does not re-test
   that logic itself (already covered by
   ``tests/migration/test_verify_fill_regression.py``).

Per the bead's own constraint: ``_verify_fill_one``/``_iter_id_pages`` and
``verify_fill_local``/``verify_fill_cloud`` receive ZERO changes here — this
suite only exercises the NEW pg-source seam plus the widened ``leg``
Literal.
"""
from __future__ import annotations

import hashlib

import pytest

from nexus.migration import pg_read
from nexus.migration.vector_etl import (
    MigrationReport,
    resolve_local_service_endpoint,
    verify_fill_collections,
    verify_fill_pg_source,
)

# Reuse the locked ETL test fakes + naming helper (single source of truth;
# mirrors test_verify_fill_regression.py's own precedent for cross-file
# test-fake reuse — a drift in FakeVectorClient's surface trips both suites).
from tests.migration.test_vector_etl import (  # noqa: PLC2701 — shared test fakes
    FakeVectorClient,
    _coll,
)


def _chash(text: str) -> str:
    """Chunk natural ID: sha256(text)[:32] (the repo-wide chash convention)."""
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# ═══════════════════════════════════════════════════════════════════════════
# 1. verify_fill_pg_source wiring
# ═══════════════════════════════════════════════════════════════════════════


class TestVerifyFillPgSourceWiring:
    def test_constructs_pg_read_client_and_delegates_with_pg_leg(
        self, monkeypatch
    ) -> None:
        """Mock both the adapter construction seam and
        verify_fill_collections — assert verify_fill_pg_source opens a
        PgReadClient from the resolved endpoint and delegates with
        leg='pg', mirroring TestVerifyFillLegRouting's own pattern for
        the local/cloud legs."""
        constructed: list[tuple[str, str]] = []

        class FakePgReadClient:
            def __init__(self, base_url: str, token: str) -> None:
                constructed.append((base_url, token))

        monkeypatch.setattr(
            "nexus.migration.vector_etl.PgReadClient", FakePgReadClient
        )

        captured: dict[str, object] = {}

        def fake_verify_fill_collections(read_client, vector_client, *, leg, **kwargs):
            captured["read_client"] = read_client
            captured["vector_client"] = vector_client
            captured["leg"] = leg
            captured["kwargs"] = kwargs
            return MigrationReport(leg=leg, results=())

        monkeypatch.setattr(
            "nexus.migration.vector_etl.verify_fill_collections",
            fake_verify_fill_collections,
        )

        fake_vc = object()
        report = verify_fill_pg_source(
            "http://localhost:9999", fake_vc, local_token="tok"
        )

        assert constructed == [("http://localhost:9999", "tok")]
        assert isinstance(captured["read_client"], FakePgReadClient)
        assert captured["vector_client"] is fake_vc
        assert captured["leg"] == "pg"
        assert report.leg == "pg"

    def test_resolver_is_consulted_for_endpoint(self, monkeypatch) -> None:
        """verify_fill_pg_source must route endpoint resolution through
        resolve_local_service_endpoint (never construct the PgReadClient
        from an unresolved/raw pair) — proves the resolver seam is wired,
        not bypassed."""
        resolved_args: list[tuple[object, object]] = []

        def fake_resolver(explicit_url=None, explicit_token=None):
            resolved_args.append((explicit_url, explicit_token))
            return "http://resolved:1234", "resolved-tok"

        monkeypatch.setattr(
            "nexus.migration.vector_etl.resolve_local_service_endpoint",
            fake_resolver,
        )

        constructed: list[tuple[str, str]] = []

        class FakePgReadClient:
            def __init__(self, base_url: str, token: str) -> None:
                constructed.append((base_url, token))

        monkeypatch.setattr(
            "nexus.migration.vector_etl.PgReadClient", FakePgReadClient
        )
        monkeypatch.setattr(
            "nexus.migration.vector_etl.verify_fill_collections",
            lambda read_client, vector_client, *, leg, **kwargs: MigrationReport(
                leg=leg, results=()
            ),
        )

        verify_fill_pg_source(None, object(), local_token=None)

        assert resolved_args == [(None, None)]
        assert constructed == [("http://resolved:1234", "resolved-tok")]


# ═══════════════════════════════════════════════════════════════════════════
# 2. resolve_local_service_endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveLocalServiceEndpoint:
    def test_explicit_override_wins_over_lease(self, monkeypatch) -> None:
        def fail_if_called():
            raise AssertionError("discover_lease must not be called when both explicit args are given")

        monkeypatch.setattr(
            "nexus.db.service_endpoint.discover_lease",
            lambda: fail_if_called(),
        )
        url, token = resolve_local_service_endpoint(
            "http://explicit:9999", "explicit-tok"
        )
        assert url == "http://explicit:9999"
        assert token == "explicit-tok"

    def test_falls_back_to_lease_when_no_override(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "nexus.db.service_endpoint.discover_lease",
            lambda: ("http://leased:4321", "leased-tok"),
        )
        url, token = resolve_local_service_endpoint()
        assert url == "http://leased:4321"
        assert token == "leased-tok"

    def test_partial_override_fills_remaining_half_from_lease(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "nexus.db.service_endpoint.discover_lease",
            lambda: ("http://leased:4321", "leased-tok"),
        )
        url, token = resolve_local_service_endpoint(explicit_url="http://explicit:9999")
        assert url == "http://explicit:9999"
        assert token == "leased-tok"

    def test_raises_clear_actionable_error_when_nothing_resolves(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "nexus.db.service_endpoint.discover_lease",
            lambda: (None, None),
        )
        with pytest.raises(RuntimeError, match="not resolvable"):
            resolve_local_service_endpoint()

    def test_strips_trailing_slash_from_explicit_url(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "nexus.db.service_endpoint.discover_lease",
            lambda: (None, None),
        )
        url, _token = resolve_local_service_endpoint("http://explicit:9999/", "tok")
        assert url == "http://explicit:9999"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Non-vacuous integration-shaped test: real PgReadClient + real
#    verify_fill_collections diff/fill logic, mocked only at the HTTP
#    boundary (mirrors test_pg_read.py's own mocking convention).
# ═══════════════════════════════════════════════════════════════════════════


class TestPgSourceFillVsSkipWiring:
    def test_missing_chunk_filled_present_chunk_skipped(self, monkeypatch) -> None:
        name = _coll("pgsrc-1-1")  # knowledge__pgsrc-1-1__minilm-l6-v2-384__v1
        id_present = _chash("chunk already in target")
        id_missing = _chash("chunk only in pg source")

        # ── Mock the HTTP boundary pg_read.py itself already tests against
        # (test_pg_read.py's convention) — this is the SOURCE.
        def fake_get(base_url, token, path, *, tenant="default", timeout=30):
            raise AssertionError(f"unexpected GET {path}")

        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            assert path == "/v1/vectors/get"
            assert body["collection"] == name
            if body["offset"] == 0:
                return {
                    "ids": [id_present, id_missing],
                    "documents": ["doc-present", "doc-missing"],
                    "metadatas": [{}, {}],
                }
            return {"ids": [], "documents": [], "metadatas": []}

        monkeypatch.setattr(pg_read, "_get", fake_get)
        monkeypatch.setattr(pg_read, "_post", fake_post)

        # ── The TARGET already holds id_present (nothing to send for it).
        target = FakeVectorClient()
        target.upsert_chunks(name, [id_present], ["doc-present"], [{}])

        # ── Bypass endpoint resolution (already covered above) with an
        # explicit pair — proves verify_fill_pg_source threads a REAL
        # PgReadClient (not a mock of the class) into the real
        # verify_fill_collections/_verify_fill_one diff/fill path.
        report = verify_fill_pg_source(
            "http://localhost:9999",
            target,
            local_token="tok",
            collections=[name],
        )

        assert report.leg == "pg"
        assert len(report.results) == 1
        result = report.results[0]
        assert result.collection == name
        assert result.source_count == 2
        assert result.missing_count == 1
        assert result.filled_count == 1
        assert result.status == "filled"

        # Non-vacuous: exactly ONE upsert call from THIS run (the seed call
        # above is the pre-existing state, not part of what verify-fill
        # sent), and it names exactly the missing id — the present id was
        # never re-sent.
        assert target.upsert_calls[1:] == [(name, [id_missing])]
        assert target.store[name][id_missing][0] == "doc-missing"
        assert id_present in target.store[name]

    def test_second_pass_is_a_true_noop(self, monkeypatch) -> None:
        """Stateful re-run: after the fill above, a second pass against the
        SAME (now-converged) target sends zero further rows — proves the
        wiring doesn't blindly re-send, matching the non-tautology bar
        test_verify_fill_regression.py sets for every verify-fill surface."""
        name = _coll("pgsrc-1-2")
        id_a = _chash("second-pass chunk a")
        id_b = _chash("second-pass chunk b")

        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            if body["offset"] == 0:
                return {
                    "ids": [id_a, id_b],
                    "documents": ["doc-a", "doc-b"],
                    "metadatas": [{}, {}],
                }
            return {"ids": [], "documents": [], "metadatas": []}

        monkeypatch.setattr(pg_read, "_post", fake_post)

        target = FakeVectorClient()
        first = verify_fill_pg_source(
            "http://localhost:9999", target, local_token="tok", collections=[name]
        )
        assert first.results[0].filled_count == 2
        assert len(target.upsert_calls) == 1

        second = verify_fill_pg_source(
            "http://localhost:9999", target, local_token="tok", collections=[name]
        )
        assert second.results[0].filled_count == 0
        assert second.results[0].missing_count == 0
        assert second.results[0].status == "verified"
        # No further upsert calls beyond the one from the first pass.
        assert len(target.upsert_calls) == 1

    def test_passthrough_model_end_to_end_stitch_reaches_target(
        self, monkeypatch
    ) -> None:
        """SUBSTANTIVE-CRITIC SIGNIFICANT FINDING (review round 1): every
        other test in this class uses the default _MODEL_384 fixture, which
        is NOT in _PASSTHROUGH_MODELS -- so _is_same_model_passthrough never
        fires and _iter_id_pages never requests include_embeddings=True. The
        embeddings stitch (PgReadClient._fetch_embeddings, the two-endpoint
        /v1/vectors/get + /v1/vectors/get-embeddings call) was previously
        proven correct ONLY in isolation (test_pg_read.py, calling
        iter_collection_chunks directly) and the wiring proven correct ONLY
        without embeddings (the two tests above). Nothing proved the two
        compose through the REAL verify_fill_pg_source call chain for a
        passthrough-eligible collection -- exactly the billed-Voyage-
        re-embed-avoidance scenario this whole bead exists to guarantee.

        Uses model="bge-base-en-v15-768" (in _PASSTHROUGH_MODELS) with the
        target defaulting to the SAME name as source (no target_names
        override) -- both _is_same_model_passthrough conditions. Mocks BOTH
        /v1/vectors/get (ids/documents/metadatas) AND /v1/vectors/get-
        embeddings (the stitch's second call) on the same _post monkeypatch,
        branching on path -- proving both genuinely fire for one fill pass,
        and that the resulting embeddings (not None) reach the target's
        upsert_chunks call, not a reembed-fallback path."""
        name = _coll("pgsrc-1-4", model="bge-base-en-v15-768")
        id_missing = _chash("passthrough-eligible chunk")
        vector = [0.1, 0.2, 0.3]

        get_calls: list[dict] = []
        embeddings_calls: list[dict] = []

        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            if path == "/v1/vectors/get":
                get_calls.append(body)
                if body["offset"] == 0:
                    return {
                        "ids": [id_missing],
                        "documents": ["doc-passthrough"],
                        "metadatas": [{}],
                    }
                return {"ids": [], "documents": [], "metadatas": []}
            if path == "/v1/vectors/get-embeddings":
                embeddings_calls.append(body)
                assert body["ids"] == [id_missing]
                return {"ids": [id_missing], "embeddings": [vector]}
            raise AssertionError(f"unexpected POST {path}")

        monkeypatch.setattr(pg_read, "_post", fake_post)

        target = FakeVectorClient()
        report = verify_fill_pg_source(
            "http://localhost:9999", target, local_token="tok", collections=[name]
        )

        assert report.results[0].filled_count == 1
        assert report.results[0].status == "filled"

        # Both endpoints genuinely fired -- not just the plain get() path.
        assert len(get_calls) == 1
        assert len(embeddings_calls) == 1

        # The stitched embedding reached the target's upsert call as a real
        # vector, not None (which would mean _verify_fill_one fell back to
        # a re-embed rather than the free verbatim passthrough).
        assert target.upsert_calls[-1] == (name, [id_missing])
        assert target.upsert_embeddings[-1] == [vector]


# ═══════════════════════════════════════════════════════════════════════════
# 4. Regression guard: verify_fill_collections still accepts "pg" in its
#    widened Literal (a type-level assertion the runtime doesn't otherwise
#    exercise, but worth pinning since Literal widening was the whole point
#    of touching that signature).
# ═══════════════════════════════════════════════════════════════════════════


class TestLegLiteralWidened:
    def test_verify_fill_collections_accepts_pg_leg_directly(self) -> None:
        # collections=[] means the enumeration loop never touches read_client
        # (explicit=True, names=[]) — None suffices to prove the Literal
        # accepts "pg" without needing a real/fake adapter here.
        report = verify_fill_collections(
            None,
            FakeVectorClient(),
            leg="pg",
            collections=[],
        )
        assert report.leg == "pg"
        assert report.results == ()
