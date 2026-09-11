# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpTupleStore (bead nexus-em75s.9, RDR-205 Phase 2 Step 1) against the
real engine substrate (``t2_service_env`` — ``tests/_engine_substrate.py``),
plus a few pure-client-side behaviors (the 8 KB pre-send guard, the
timeout-ordering arithmetic, the parked-502 gateway retry) that do not need
a live tuple row and are cheaper and more deterministic as mock-transport /
monkeypatch tests than as engine round trips.

Two v1 templates are loaded at engine boot (``service/src/main/resources/
tuples/templates/{ledger,mailbox}.yaml``):

  - ``ledger/<session_id>``: keys ``[agent_id, kind]``, ``id_from=keys``,
    ``take.enabled=false`` (never claimable).
  - ``mailbox/<address>``: keys ``[to]``, dims ``{from (required), kind,
    correlation_id, address_kind}``, ``id_from=keys+nonce``,
    ``id_dims=[from]``, ``take.enabled=true``, ``max_attempts=3``,
    ``max_lease_seconds=900``, ``retention_seconds=604800``.

``t2_service_env`` mints a FRESH tenant per test function and the engine's
tenant tables carry forced RLS, so tests reusing the SAME literal subspace
name never collide with each other. The one exception is the tuple-space's
park-cap counters (``TupleWaitRegistry``), which are per-JVM-process, NOT
per-tenant — the park-cap test below uses a per-run-unique claimant string
for exactly that reason.
"""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

import httpx
import pytest

import nexus.db.t2._refreshable_client as refreshable
from nexus.db.t2.http_tuple_store import (
    ClaimNotFoundError,
    ClaimOwnershipError,
    HttpTupleStore,
    LeaseTooLongError,
    ParkCapExceededError,
    RequestTooLargeError,
    SchemaViolationError,
    TakeDisabledError,
    TimeoutTooLongError,
    TtlTooLongError,
    TupleError,
    UnknownSubspaceError,
    _ERROR_CLASSES_BY_CODE,
    _MAX_REQUEST_BODY_BYTES,
    _PARK_TIMEOUT_MARGIN_S,
    _check_request_size,
    _raise_typed,
)
from nexus.db.t2.records import TupleRow


def _uniq(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:10]}"


# ── out ──────────────────────────────────────────────────────────────────


class TestOut:
    def test_out_then_rd_returns_the_written_tuple(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        tuple_id = store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hello",
            nonce=_uniq("nonce"),
        )
        assert isinstance(tuple_id, str) and len(tuple_id) == 64
        int(tuple_id, 16)  # lowercase hex, RDR-086 convention

        rows = store.rd(f"mailbox/{addr}", {"to": addr})
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, TupleRow)
        assert row.id == tuple_id
        assert row.subspace == f"mailbox/{addr}"
        assert row.template == "mailbox/<address>"
        assert row.keys == {"to": addr}
        assert row.dims == {"from": "sender-a"}
        assert row.body == "hello"
        assert row.claim_state is None
        assert row.claimant is None
        assert not hasattr(row, "claim_id")

    def test_missing_nonce_on_keys_plus_nonce_template_is_schema_violation(
        self, t2_service_env,
    ) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        with pytest.raises(SchemaViolationError) as exc_info:
            store.out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi")
        assert exc_info.value.code == "SchemaViolation"

    def test_ttl_seconds_above_retention_is_ttl_too_long(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        with pytest.raises(TtlTooLongError) as exc_info:
            store.out(
                f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
                nonce=_uniq("nonce"), ttl_seconds=604800 + 1,
            )
        assert exc_info.value.code == "TtlTooLong"


# ── rd / rdp ─────────────────────────────────────────────────────────────


class TestRd:
    def test_rd_finds_nothing_before_out_and_the_row_after(self, t2_service_env) -> None:
        store = HttpTupleStore()
        session = _uniq("sess")
        assert store.rd(f"ledger/{session}", {"agent_id": "a1", "kind": "start"}) == []

        store.out(f"ledger/{session}", {"agent_id": "a1", "kind": "start"}, None, None)
        rows = store.rd(f"ledger/{session}", {"agent_id": "a1", "kind": "start"})
        assert len(rows) == 1
        assert rows[0].keys == {"agent_id": "a1", "kind": "start"}

    def test_unknown_subspace_is_unknown_subspace_error(self, t2_service_env) -> None:
        store = HttpTupleStore()
        with pytest.raises(UnknownSubspaceError) as exc_info:
            store.rd(_uniq("bogus/nothing"))
        assert exc_info.value.code == "UnknownSubspace"

    def test_timeout_s_above_the_engine_cap_is_timeout_too_long(self, t2_service_env) -> None:
        store = HttpTupleStore()
        session = _uniq("sess")
        with pytest.raises(TimeoutTooLongError) as exc_info:
            store.rd(f"ledger/{session}", {"agent_id": "a1", "kind": "start"}, timeout_s=26)
        assert exc_info.value.code == "TimeoutTooLong"


class TestRdp:
    def test_rdp_is_a_non_blocking_probe(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        assert store.rdp(f"mailbox/{addr}", {"to": addr}) == []

        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
            nonce=_uniq("nonce"),
        )
        rows = store.rdp(f"mailbox/{addr}", {"to": addr})
        assert len(rows) == 1

    def test_unknown_subspace_is_unknown_subspace_error(self, t2_service_env) -> None:
        store = HttpTupleStore()
        with pytest.raises(UnknownSubspaceError):
            store.rdp(_uniq("bogus/nothing"))


# ── in_ / inp ────────────────────────────────────────────────────────────


class TestIn:
    def test_out_then_in_claims_the_tuple(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
            nonce=_uniq("nonce"),
        )
        result = store.in_(f"mailbox/{addr}", {"to": addr}, claimant="c1", lease_s=30)
        assert result is not None
        row, claim_id = result
        assert isinstance(row, TupleRow)
        assert row.claim_state == "claimed"
        assert row.claimant == "c1"
        assert isinstance(claim_id, str) and claim_id

        # Claimed rows are invisible to a second in_() by a different claimant.
        assert store.in_(f"mailbox/{addr}", {"to": addr}, claimant="c2", lease_s=30) is None

    def test_in_against_take_disabled_template_is_take_disabled(self, t2_service_env) -> None:
        store = HttpTupleStore()
        session = _uniq("sess")
        store.out(f"ledger/{session}", {"agent_id": "a1", "kind": "start"}, None, None)
        with pytest.raises(TakeDisabledError) as exc_info:
            store.in_(f"ledger/{session}", {"agent_id": "a1", "kind": "start"}, claimant="c1", lease_s=30)
        assert exc_info.value.code == "TakeDisabled"


class TestInp:
    def test_inp_claims_without_blocking(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
            nonce=_uniq("nonce"),
        )
        result = store.inp(f"mailbox/{addr}", {"to": addr}, claimant="c1", lease_s=30)
        assert result is not None
        row, claim_id = result
        assert row.claim_state == "claimed"
        assert claim_id

    def test_inp_probe_miss_returns_none(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        assert store.inp(f"mailbox/{addr}", {"to": addr}, claimant="c1", lease_s=30) is None

    def test_lease_s_above_max_lease_seconds_is_lease_too_long(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
            nonce=_uniq("nonce"),
        )
        with pytest.raises(LeaseTooLongError) as exc_info:
            store.inp(f"mailbox/{addr}", {"to": addr}, claimant="c1", lease_s=900 + 1)
        assert exc_info.value.code == "LeaseTooLong"


# ── ack / nack ───────────────────────────────────────────────────────────


class TestAck:
    def test_ack_consumes_the_row(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
            nonce=_uniq("nonce"),
        )
        row, claim_id = store.in_(f"mailbox/{addr}", {"to": addr}, claimant="c1", lease_s=30)

        store.ack(claim_id, "c1")

        # Acked rows are never returned by rd (RDR-205 §Technical Design
        # "Operations": "Acked rows are never returned").
        assert store.rd(f"mailbox/{addr}", {"to": addr}) == []
        census = store.subspace_stats(f"mailbox/{addr}")
        assert census.consumed == 1
        assert census.available == 0
        assert census.claimed == 0

    def test_ack_with_bogus_claim_id_is_claim_not_found(self, t2_service_env) -> None:
        store = HttpTupleStore()
        with pytest.raises(ClaimNotFoundError) as exc_info:
            store.ack(str(uuid.uuid4()), "c1")
        assert exc_info.value.code == "ClaimNotFound"


class TestNack:
    def test_nack_releases_the_claim(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
            nonce=_uniq("nonce"),
        )
        row, claim_id = store.in_(f"mailbox/{addr}", {"to": addr}, claimant="c1", lease_s=30)

        store.nack(claim_id, "c1")

        rows = store.rd(f"mailbox/{addr}", {"to": addr})
        assert len(rows) == 1
        assert rows[0].claim_state is None
        assert rows[0].claimant is None

    def test_nack_by_the_wrong_claimant_is_claim_ownership(self, t2_service_env) -> None:
        store = HttpTupleStore()
        addr = _uniq("addr")
        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
            nonce=_uniq("nonce"),
        )
        row, claim_id = store.in_(f"mailbox/{addr}", {"to": addr}, claimant="c1", lease_s=30)

        with pytest.raises(ClaimOwnershipError) as exc_info:
            store.nack(claim_id, "not-c1")
        assert exc_info.value.code == "ClaimOwnership"


# ── registry / census ────────────────────────────────────────────────────


class TestRegistry:
    def test_registry_carries_both_v1_templates(self, t2_service_env) -> None:
        store = HttpTupleStore()
        reg = store.registry()
        assert reg["digest"]
        names = {t["name"] for t in reg["templates"]}
        assert {"ledger/<session_id>", "mailbox/<address>"} <= names


class TestSubspaceList:
    def test_subspace_list_filters_by_prefix(self, t2_service_env) -> None:
        store = HttpTupleStore()
        session = _uniq("sess")
        subspace = f"ledger/{session}"
        store.out(subspace, {"agent_id": "a1", "kind": "start"}, None, None)

        censuses = store.subspace_list(prefix=subspace)
        assert len(censuses) == 1
        assert censuses[0].subspace == subspace
        assert censuses[0].total == 1
        assert censuses[0].available == 1


class TestSubspaceStats:
    def test_subspace_stats_matches_a_written_row(self, t2_service_env) -> None:
        store = HttpTupleStore()
        session = _uniq("sess")
        subspace = f"ledger/{session}"
        store.out(subspace, {"agent_id": "a1", "kind": "start"}, None, None)

        census = store.subspace_stats(subspace)
        assert census.subspace == subspace
        assert census.total == 1
        assert census.available == 1
        assert census.claimed == 0
        assert census.dead == 0
        assert census.consumed == 0

    def test_empty_subspace_is_refused_client_side(self, t2_service_env) -> None:
        store = HttpTupleStore()
        with pytest.raises(ValueError):
            store.subspace_stats("")


# ── ParkCapExceeded (RDR-205 Test Plan: "a claimant's fifth") ─────────────


class TestParkCapExceeded:
    def test_a_claimants_fifth_concurrent_park_is_refused(self, t2_service_env) -> None:
        """Per-claimant park cap defaults to 4 (``NX_TUPLE_PARK_CAP_
        PER_CLAIMANT``, ``TupleWaitRegistry.DEFAULT_PARK_CAP_PER_CLAIMANT``).
        ``claimant`` is process-JVM-global (not tenant-scoped) so this test
        uses a run-unique claimant string to stay isolated from any other
        concurrently-running test under xdist.
        """
        store = HttpTupleStore()
        addr = _uniq("addr")
        subspace = f"mailbox/{addr}"
        claimant = _uniq("parkcap-claimant")
        # A valid, never-matching pattern -- the template's required "to"
        # key is present, but no out() ever wrote this address, so every
        # call finds nothing on its first (non-blocking) probe and moves
        # to park.
        pattern = {"to": addr}

        def _park(timeout_s: int) -> object:
            return store.in_(subspace, pattern, claimant=claimant, lease_s=30, timeout_s=timeout_s)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_park, 8) for _ in range(4)]
            # Give the 4 background calls time to reach the actual park
            # (past their own non-blocking first probe) before this thread
            # issues the 5th.
            time.sleep(2.0)

            with pytest.raises(ParkCapExceededError) as exc_info:
                store.in_(subspace, pattern, claimant=claimant, lease_s=30, timeout_s=8)
            assert exc_info.value.code == "ParkCapExceeded"

            done, not_done = futures_wait(futures, timeout=15)
            assert not not_done, "background parked calls did not finish in time"
            for f in done:
                assert f.result() is None, "no tuple was ever written for this address"


# ── 8 KB pre-send guard (RDR-205 Test Plan: "over 8 KB with a body under it") ──


def _httpx_json_body_len(payload: dict) -> int:
    """The exact byte length httpx puts on the wire for *payload* as a
    JSON request body -- via httpx's own public ``json=`` request-building
    path (``httpx.Request``), not a hand-copied ``json.dumps`` literal
    (nexus-em75s.42 review fix: a literal restates ``_check_request_size``'s
    own implementation, so a drift between that literal and httpx's real
    encoder would go unnoticed by both the guard and a test built the same
    way; this measures against whatever httpx version ``>=0.27,<1.0`` is
    actually installed, not one snapshot of its separators/ensure_ascii/
    allow_nan choice)."""
    return len(httpx.Request("POST", "http://example.invalid", json=payload).content)


class TestPreSendGuard:
    def test_a_payload_at_the_cap_is_accepted(self) -> None:
        payload = {"subspace": "x", "keys": {"to": "y" * 10}}
        assert _httpx_json_body_len(payload) < _MAX_REQUEST_BODY_BYTES
        _check_request_size(payload)  # must not raise

    def test_a_payload_exactly_at_the_cap_boundary_is_accepted(self) -> None:
        """Pads a payload until httpx's REAL wire encoding is exactly
        ``_MAX_REQUEST_BODY_BYTES`` (sized via ``_httpx_json_body_len``,
        never a recomputed ``json.dumps`` literal), pinning the guard's
        inclusive boundary against the installed httpx's own byte count
        (nexus-em75s.42 review fix, item 6)."""
        payload: dict = {"subspace": "x", "keys": {"to": ""}}
        base = _httpx_json_body_len(payload)
        payload["keys"]["to"] = "y" * (_MAX_REQUEST_BODY_BYTES - base)
        assert _httpx_json_body_len(payload) == _MAX_REQUEST_BODY_BYTES
        _check_request_size(payload)  # exactly at the cap -- must not raise

    def test_one_byte_over_the_cap_via_httpx_encoding_is_refused(self) -> None:
        """The mirror of the boundary-accepted test above: one byte past
        ``_MAX_REQUEST_BODY_BYTES``, measured the same way (real httpx
        encoding, not a literal), is refused."""
        payload: dict = {"subspace": "x", "keys": {"to": ""}}
        base = _httpx_json_body_len(payload)
        payload["keys"]["to"] = "y" * (_MAX_REQUEST_BODY_BYTES - base + 1)
        assert _httpx_json_body_len(payload) == _MAX_REQUEST_BODY_BYTES + 1
        with pytest.raises(RequestTooLargeError):
            _check_request_size(payload)

    def test_over_8kb_serialised_request_is_refused_before_any_send(self) -> None:
        store = HttpTupleStore.__new__(HttpTupleStore)

        def _must_not_be_called(*_a, **_k):  # pragma: no cover - only fires on failure
            raise AssertionError("super()._post was called -- the pre-send guard did not fire")

        original = refreshable.RefreshableHttpStoreMixin._post
        refreshable.RefreshableHttpStoreMixin._post = _must_not_be_called
        try:
            # The tuple BODY itself is small ("a"), but "keys" carries a
            # value long enough to push the SERIALISED request over 8 KB --
            # this is the exact scenario RDR-205's Test Plan names: "a
            # serialised request over 8 KB with a body under it".
            oversized_keys = {"to": "x" * (_MAX_REQUEST_BODY_BYTES + 200)}
            with pytest.raises(RequestTooLargeError):
                store._post("/out", {"subspace": "mailbox/x", "keys": oversized_keys, "body": "a"})
        finally:
            refreshable.RefreshableHttpStoreMixin._post = original


# ── HTTP timeout ordering (above timeout_s so the server's cap fires first) ──


class TestTimeoutOrdering:
    def test_rd_park_timeout_is_above_timeout_s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = HttpTupleStore.__new__(HttpTupleStore)
        captured: dict = {}

        def fake_post(self, path, payload, **kwargs):
            captured.update(kwargs)
            return {"tuples": []}

        monkeypatch.setattr(HttpTupleStore, "_post", fake_post)
        store.rd("mailbox/x", timeout_s=10)
        assert captured["timeout"] == 10 + _PARK_TIMEOUT_MARGIN_S

    def test_rd_with_no_timeout_s_passes_no_timeout_override(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = HttpTupleStore.__new__(HttpTupleStore)
        captured: dict = {}

        def fake_post(self, path, payload, **kwargs):
            captured.update(kwargs)
            return {"tuples": []}

        monkeypatch.setattr(HttpTupleStore, "_post", fake_post)
        store.rd("mailbox/x")
        assert captured["timeout"] is None

    def test_in_park_timeout_is_above_timeout_s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = HttpTupleStore.__new__(HttpTupleStore)
        captured: dict = {}

        def fake_post(self, path, payload, **kwargs):
            captured.update(kwargs)
            return {"tuple": None, "claim_id": None}

        monkeypatch.setattr(HttpTupleStore, "_post", fake_post)
        store.in_("mailbox/x", {"to": "x"}, claimant="c1", lease_s=30, timeout_s=7)
        assert captured["timeout"] == 7 + _PARK_TIMEOUT_MARGIN_S


# ── Parked 502 retries within the bounded gateway policy ──────────────────


class TestGatewayRetry:
    def test_a_parked_rd_receiving_a_502_retries_and_returns_the_tuple(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
        store = HttpTupleStore(base_url="http://mock.test")

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if len(calls) == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json={"tuples": [{
                "id": "a" * 64, "subspace": "mailbox/x", "template": "mailbox/<address>",
                "keys": {"to": "x"}, "dims": {"from": "y"}, "body": "hi",
                "claim_state": None, "claimant": None, "lease_until": None,
                "attempts": 0, "consumed_at": None, "consumed_by": None,
                "expires_at": None, "created_at": None,
            }]})

        store._client = httpx.Client(
            base_url="http://mock.test", transport=httpx.MockTransport(handler),
        )

        rows = store.rd("mailbox/x", {"to": "x"}, timeout_s=5)

        assert calls == ["/v1/tuples/rd", "/v1/tuples/rd"]
        assert len(rows) == 1
        assert rows[0].id == "a" * 64


# ── Typed-error mapping (pure function -- no network, no engine) ──────────


def _status_error(status: int, error_code: str | None, detail: str | None) -> httpx.HTTPStatusError:
    body: dict = {}
    if error_code is not None:
        body["error"] = error_code
    if detail is not None:
        body["detail"] = detail
    request = httpx.Request("POST", "http://x.test/v1/tuples/out")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class TestTypedErrorMapping:
    """Every op's client-side error handling ultimately runs through
    ``_raise_typed`` — this is the shared "does classification actually
    classify" proof, including the operations (``registry``,
    ``subspace_list``) that have no engine-side typed-error path of their
    own to exercise against the real substrate."""

    @pytest.mark.parametrize("code,cls", sorted(_ERROR_CLASSES_BY_CODE.items()))
    def test_each_of_the_nine_codes_maps_to_its_class(
        self, code: str, cls: type[TupleError],
    ) -> None:
        # The status code itself is irrelevant to _raise_typed's
        # classification (it reads the "error" field, not the bare
        # status -- see test_status_code_alone_cannot_disambiguate_
        # shared_statuses below), so any status works here.
        exc = _status_error(400, code, "the detail")
        with pytest.raises(cls) as exc_info:
            _raise_typed(exc)
        assert exc_info.value.code == code
        assert str(exc_info.value) == "the detail"
        assert exc_info.value.__cause__ is exc

    def test_unrecognised_error_code_falls_through_unchanged(self) -> None:
        exc = _status_error(404, "SomeFutureCode", "not one of the nine")
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            _raise_typed(exc)
        assert exc_info.value is exc

    def test_non_json_body_falls_through_unchanged(self) -> None:
        request = httpx.Request("GET", "http://x.test/v1/tuples/registry")
        response = httpx.Response(401, text="unauthorized", request=request)
        exc = httpx.HTTPStatusError("HTTP 401", request=request, response=response)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            _raise_typed(exc)
        assert exc_info.value is exc

    def test_status_code_alone_cannot_disambiguate_shared_statuses(self) -> None:
        """``UnknownSubspace`` and ``ClaimNotFound`` are both 404 —
        classification MUST read the ``error`` field, not the bare status."""
        unknown = _status_error(404, "UnknownSubspace", "no template for x")
        not_found = _status_error(404, "ClaimNotFound", "no live claim")
        with pytest.raises(UnknownSubspaceError):
            _raise_typed(unknown)
        with pytest.raises(ClaimNotFoundError):
            _raise_typed(not_found)
