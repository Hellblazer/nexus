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
from datetime import datetime, timedelta, timezone
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
    ReplyNotWrittenError,
    ReplySpec,
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

        rows = store.rd(f"mailbox/{addr}", {"to": addr}, n=2)
        assert len(rows) == 1, f"expected one row, got {len(rows)}"
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
        rows = store.rd(f"ledger/{session}", {"agent_id": "a1", "kind": "start"}, n=2)
        assert len(rows) == 1, f"expected one row, got {len(rows)}"
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
        rows = store.rdp(f"mailbox/{addr}", {"to": addr}, n=2)
        assert len(rows) == 1, f"expected one row, got {len(rows)}"

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

        rows = store.rd(f"mailbox/{addr}", {"to": addr}, n=2)
        assert len(rows) == 1, f"expected one row, got {len(rows)}"
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


# ── renew (nexus-h61dl.8, RDR-206 Phase 2) ───────────────────────────────


class TestRenew:
    """``renew`` extends a live claim's lease without touching attempts.

    TWO CEILINGS, TWO BEHAVIOURS, and they do not collapse into one rule
    (confirmed against TupleRepository by the engine author, 2026-09-12):
    a requested duration ABOVE the template's ``max_lease_seconds`` is
    REFUSED with ``LeaseTooLong``, while a duration inside that cap on a
    tuple whose own expiry is nearer is silently CLIPPED to the expiry
    (``DSL.least(candidate, TUPLES.EXPIRES_AT)``). A test that treats them
    as one ceiling passes for the wrong reason. The RDR said "capped at"
    in prose and ``LeaseTooLong`` in the same item's error list; the
    prose was amended, but the ambiguity is why these are separate tests.
    """

    def _claimed(self, store: HttpTupleStore, lease_s: int = 30):
        addr = _uniq("addr")
        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
            nonce=_uniq("nonce"),
        )
        row, claim_id = store.in_(
            f"mailbox/{addr}", {"to": addr}, claimant="c1", lease_s=lease_s,
        )
        return addr, row, claim_id

    def test_renew_moves_the_lease_forward(self, t2_service_env) -> None:
        store = HttpTupleStore()
        _addr, row, claim_id = self._claimed(store, lease_s=30)
        before = datetime.fromisoformat(row.lease_until)

        after = store.renew(claim_id, "c1", 300)

        assert isinstance(after, datetime)
        assert after.tzinfo is not None, (
            "an aware datetime, or a naive one silently mis-compares against "
            "the engine's UTC (the nexus-rph82 JVM-local-vs-GMT class)"
        )
        assert after > before

    def test_renew_returns_the_engines_lease_until_not_a_local_computation(
        self, t2_service_env,
    ) -> None:
        """The grant is clipped in SQL against the LIVE row, so ``lease_until``
        can come back EARLIER than ``now + lease_s``. Recomputing it
        client-side would look like a harmless local optimisation and would
        disagree with the engine in exactly the window RDR-206 Phase 1's
        whole-phase finding was about, so the client surfaces what the engine
        said. Driven with a tuple whose TTL is shorter than the lease asked
        for: the clip is observable only because the two differ."""
        store = HttpTupleStore()
        addr = _uniq("addr")
        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hi",
            nonce=_uniq("nonce"), ttl_seconds=60,
        )
        _row, claim_id = store.in_(
            f"mailbox/{addr}", {"to": addr}, claimant="c1", lease_s=30,
        )
        asked_for = 600
        before = datetime.now(timezone.utc)
        granted = store.renew(claim_id, "c1", asked_for)
        delta_s = (granted - before).total_seconds()

        # NOT `granted < before + asked_for`: the client's clock is read
        # AFTER the engine's, so that comparison holds even when nothing was
        # clipped, and the test would pass with the clip deleted. Bound it
        # against the TUPLE's 60 s ttl instead, well clear of the 600 s that
        # an unclipped grant would return. Measured 2026-09-12: 60.0 s.
        assert delta_s <= 120, (
            f"expected the grant clipped to the tuple's ~60 s expiry, got "
            f"{delta_s:.1f}s -- an unclipped engine returns ~{asked_for}s and "
            "a client that recomputed now+lease_s would report the same wrong "
            "instant"
        )
        assert delta_s > 0, "a renew that grants nothing is not a renew"

    def test_renew_by_the_wrong_claimant_is_claim_ownership(self, t2_service_env) -> None:
        store = HttpTupleStore()
        _addr, _row, claim_id = self._claimed(store)
        with pytest.raises(ClaimOwnershipError) as exc_info:
            store.renew(claim_id, "someone-else", 60)
        assert exc_info.value.code == "ClaimOwnership"

    def test_renew_of_an_unknown_claim_is_claim_not_found(self, t2_service_env) -> None:
        store = HttpTupleStore()
        self._claimed(store)
        with pytest.raises(ClaimNotFoundError) as exc_info:
            store.renew(uuid.uuid4().hex, "c1", 60)
        assert exc_info.value.code == "ClaimNotFound"

    def test_renew_above_max_lease_seconds_is_refused_not_capped(
        self, t2_service_env,
    ) -> None:
        """The FIRST ceiling. Compared against ``max_lease_seconds`` alone,
        with no remaining-TTL term, and it REFUSES."""
        store = HttpTupleStore()
        _addr, _row, claim_id = self._claimed(store)
        with pytest.raises(LeaseTooLongError) as exc_info:
            store.renew(claim_id, "c1", 900 + 1)
        assert exc_info.value.code == "LeaseTooLong"

    def test_renew_rejects_empty_arguments_before_sending(self, t2_service_env) -> None:
        store = HttpTupleStore()
        for bad in ({"claim_id": "", "claimant": "c1"}, {"claim_id": "x", "claimant": ""}):
            with pytest.raises(ValueError):
                store.renew(bad["claim_id"], bad["claimant"], 60)


class TestRenewAgainstAnOldEngine:
    """An engine predating ``/renew`` must produce a LOUD failure, never a
    silent no-op that would let a caller believe its lease was extended
    while the claim quietly lapses underneath it.

    The first version of this test posted a bare ``text="Not Found"`` body,
    which is NOT what an old engine sends. It passed, but through the
    json-parse-failed branch of ``_raise_typed`` rather than the branch the
    real response takes — the same defect class as an assertion satisfied by
    clock skew: right answer, wrong reason, and no coverage of the path that
    actually runs. ``engine-service-v0.1.116`` answers the unknown route
    from its switch default with a real JSON body carrying a real ``error``
    field whose VALUE is unrecognised, so the miss happens at the code
    lookup, not at the parse. Both shapes are now driven."""

    @staticmethod
    def _engine_404(monkeypatch, **response_kw) -> None:
        def _404(*_a, **_k):
            request = httpx.Request("POST", "http://engine/v1/tuples/renew")
            response = httpx.Response(404, request=request, **response_kw)
            raise httpx.HTTPStatusError("404", request=request, response=response)

        monkeypatch.setattr(refreshable.RefreshableHttpStoreMixin, "_post", _404)

    def test_the_real_v0_1_116_body_stays_a_bare_http_error(self, monkeypatch) -> None:
        """The shape an old engine actually sends: valid JSON, an ``error``
        field, an unrecognised code. Exercises the code-lookup miss."""
        self._engine_404(monkeypatch, json={"error": "unknown tuples op: /renew"})
        store = HttpTupleStore()
        with pytest.raises(httpx.HTTPStatusError):
            store.renew("claim-1", "c1", 60)

    def test_an_unparseable_404_body_also_stays_a_bare_http_error(
        self, monkeypatch,
    ) -> None:
        """The other branch: a proxy or edge answering with non-JSON."""
        self._engine_404(monkeypatch, text="Not Found")
        store = HttpTupleStore()
        with pytest.raises(httpx.HTTPStatusError):
            store.renew("claim-1", "c1", 60)

    def test_a_recognised_code_in_a_404_still_maps_to_its_typed_error(
        self, monkeypatch,
    ) -> None:
        """Guards the boundary from the other side: the fall-through must be
        driven by the code being UNRECOGNISED, not by the status being 404.
        ClaimNotFound is a 404 too, and it must still map."""
        self._engine_404(monkeypatch, json={"error": "ClaimNotFound", "detail": "gone"})
        store = HttpTupleStore()
        with pytest.raises(ClaimNotFoundError):
            store.renew("claim-1", "c1", 60)


# ── ack with a reply (nexus-h61dl.8) ─────────────────────────────────────


class TestReplySpec:
    """The shape is mirrored by the MCP tool and the CLI, so it is pinned
    here rather than left to whichever call site is written next."""

    def test_a_caller_supplied_nonce_is_refused_at_construction(self) -> None:
        """The engine REFUSES a nonce in the reply object rather than
        ignoring it -- it sets the nonce itself to hex(request tuple id).
        A frozen dataclass with no such field refuses it one layer earlier
        and without a round trip. Accepting-and-stripping would make the
        client the only layer that tolerates a nonce, which is the exact
        divergence the engine's refusal exists to prevent."""
        with pytest.raises(TypeError):
            ReplySpec(subspace="mailbox/a", keys={"to": "a"}, nonce="deadbeef")

    def test_payload_omits_absent_optionals(self) -> None:
        spec = ReplySpec(subspace="mailbox/a", keys={"to": "a"})
        assert spec.to_payload() == {"subspace": "mailbox/a", "keys": {"to": "a"}}

    def test_payload_carries_every_field_when_present(self) -> None:
        spec = ReplySpec(
            subspace="mailbox/a", keys={"to": "a"}, dims={"from": "b"},
            body="hi", ttl_seconds=60,
        )
        assert spec.to_payload() == {
            "subspace": "mailbox/a", "keys": {"to": "a"},
            "dims": {"from": "b"}, "body": "hi", "ttl_seconds": 60,
        }

    def test_an_empty_body_is_sent_and_empty_dims_are_omitted(self) -> None:
        """The out()-correspondence edge. ``out`` distinguishes ``body=None``
        (omit) from ``body=""`` (send -- an empty body is meaningful), and
        drops a falsy ``dims``. Writing ``if body:`` here instead of
        ``if body is not None:`` silently diverges from ``out`` for exactly
        one input, which is the kind of difference nobody notices."""
        spec = ReplySpec(subspace="mailbox/a", keys={"to": "a"}, dims={}, body="")
        payload = spec.to_payload()
        assert payload["body"] == ""
        assert "dims" not in payload

    def test_the_payload_matches_what_out_would_send_for_the_same_arguments(
        self, monkeypatch,
    ) -> None:
        """The invariant is held by two separate pieces of code -- ``out``'s
        inline payload construction and ``ReplySpec.to_payload`` -- with
        nothing tying them together, so it is asserted rather than trusted.
        The engine treats a reply object AS an out; a divergence here is a
        reply that cannot be written for arguments ``out`` accepts."""
        captured: dict[str, object] = {}

        def _capture(_self, path, payload, **_kw):
            captured["path"] = path
            captured["payload"] = payload
            return {"id": "0" * 64}

        monkeypatch.setattr(HttpTupleStore, "_post", _capture)
        store = HttpTupleStore()
        store.out("mailbox/a", {"to": "a"}, {"from": "b"}, "hi", ttl_seconds=60)

        out_payload = dict(captured["payload"])
        out_payload.pop("nonce", None)
        assert out_payload == ReplySpec(
            subspace="mailbox/a", keys={"to": "a"}, dims={"from": "b"},
            body="hi", ttl_seconds=60,
        ).to_payload()


class TestAckWithReply:
    def _request_claimed_by(self, store: HttpTupleStore, reply_addr: str):
        addr = _uniq("addr")
        store.out(
            f"mailbox/{addr}", {"to": addr}, {"from": reply_addr}, "request",
            nonce=_uniq("nonce"),
        )
        _row, claim_id = store.in_(
            f"mailbox/{addr}", {"to": addr}, claimant="worker", lease_s=60,
        )
        return addr, claim_id

    def test_plain_ack_still_returns_none(self, t2_service_env) -> None:
        """The additive half: every existing caller posts /ack and discards
        the response, and must keep seeing exactly what it saw."""
        store = HttpTupleStore()
        _addr, claim_id = self._request_claimed_by(store, _uniq("replyto"))
        assert store.ack(claim_id, "worker") is None

    def test_ack_with_a_reply_returns_the_reply_id(self, t2_service_env) -> None:
        store = HttpTupleStore()
        reply_addr = _uniq("replyto")
        _addr, claim_id = self._request_claimed_by(store, reply_addr)

        reply_id = store.ack(
            claim_id, "worker",
            reply=ReplySpec(
                subspace=f"mailbox/{reply_addr}", keys={"to": reply_addr},
                dims={"from": "worker"}, body="done",
            ),
        )

        assert isinstance(reply_id, str) and len(reply_id) == 64
        int(reply_id, 16)

        # n=2, not the default n=1 (nexus-dd, RDR-206 Phase 2 review). With
        # the default the read can return at most one row, so "exactly one
        # reply exists" is satisfied by the READ'S LIMIT rather than by the
        # store's state, and a duplicate reply write passes unnoticed. Ask
        # for more than the expected count whenever the assertion IS the
        # count.
        rows = store.rd(f"mailbox/{reply_addr}", {"to": reply_addr}, n=2)
        assert [r.body for r in rows] == ["done"], (
            f"expected exactly one reply row, got {[r.body for r in rows]}"
        )
        assert rows[0].id == reply_id

    def test_a_reply_to_an_unresolvable_subspace_leaves_the_request_claimed(
        self, t2_service_env,
    ) -> None:
        """The refusal happens BEFORE the ack's transaction opens, so it is
        not a rollback -- the request is still claimed and still ackable by
        the same claimant. Mirrors the engine's own assertion in
        TupleAckWithReplyTest; without it a caller could reasonably assume a
        failed ack consumed the row anyway."""
        store = HttpTupleStore()
        _addr, claim_id = self._request_claimed_by(store, _uniq("replyto"))

        with pytest.raises(UnknownSubspaceError):
            store.ack(
                claim_id, "worker",
                reply=ReplySpec(subspace=_uniq("bogus/nowhere"), keys={"to": "x"}),
            )

        assert store.ack(claim_id, "worker") is None, (
            "the claim did not survive a refused reply"
        )

    def test_a_reply_to_a_keys_only_template_is_a_schema_violation(
        self, t2_service_env,
    ) -> None:
        """A reply target must resolve to a keys+nonce template; the ledger
        is keys-only, so it is refused rather than silently given a nonce."""
        store = HttpTupleStore()
        session = _uniq("sess")
        store.out(f"ledger/{session}", {"agent_id": "a1", "kind": "start"}, None, None)
        _addr, claim_id = self._request_claimed_by(store, _uniq("replyto"))

        with pytest.raises(SchemaViolationError):
            store.ack(
                claim_id, "worker",
                reply=ReplySpec(
                    subspace=f"ledger/{session}",
                    keys={"agent_id": "a1", "kind": "done"},
                ),
            )

    def test_an_oversized_reply_body_trips_the_guard_before_sending(
        self, monkeypatch,
    ) -> None:
        """The 8 KB guard measures the SERIALISED request, so a reply body
        pushes an ack over a cap a bare ack could never reach. Asserts
        nothing was sent, not merely that it raised -- a guard that fires
        after the write has already left is not a guard."""
        sent: list[str] = []

        def _record(_self, path, *_a, **_kw):
            sent.append(path)
            return {}

        monkeypatch.setattr(refreshable.RefreshableHttpStoreMixin, "_post", _record)
        store = HttpTupleStore()

        with pytest.raises(RequestTooLargeError):
            store.ack(
                "claim-1", "worker",
                reply=ReplySpec(
                    subspace="mailbox/a", keys={"to": "a"},
                    body="x" * (_MAX_REQUEST_BODY_BYTES + 1),
                ),
            )
        assert sent == [], "the oversized ack reached the transport"


class TestAckReplyAgainstAnOldEngine:
    """nexus-u7blf. engine-service-v0.1.116's handleAck reads the body as a
    map with FAIL_ON_UNKNOWN_PROPERTIES false, ignores ``reply``, consumes
    the request and answers ``{"acked":true}`` with no ``reply_id`` key at
    all. Before the guard, ``ack(reply=...)`` returned ``None`` there — the
    same value a plain ack returns — with the request gone and no reply
    written, so the caller could not tell a lost reply from a success.

    The discriminator is exact and needs no version probe: the RDR-206
    engine ALWAYS emits ``reply_id`` on /ack (null on a plain ack, hex when
    a reply was written — TupleHandler.handleAck), and v0.1.116 never emits
    it. Reachability is low (the release pins the engine identity), but the
    failure mode is silent data loss, which is the class this project
    refuses regardless of reachability.
    """

    @staticmethod
    def _engine_answering(monkeypatch, body: dict[str, object]) -> list[dict]:
        sent: list[dict] = []

        def _post(_self, path, payload, **_kw):
            sent.append({"path": path, "payload": payload})
            return body

        monkeypatch.setattr(refreshable.RefreshableHttpStoreMixin, "_post", _post)
        return sent

    def test_old_engine_dropping_a_reply_raises_and_says_the_request_is_gone(
        self, monkeypatch,
    ) -> None:
        self._engine_answering(monkeypatch, {"acked": True})
        store = HttpTupleStore()

        with pytest.raises(ReplyNotWrittenError) as exc_info:
            store.ack(
                "claim-1", "worker",
                reply=ReplySpec(subspace="mailbox/a", keys={"to": "a"}),
            )

        message = str(exc_info.value)
        assert "consumed" in message.lower(), (
            "the message must say the request WAS consumed, or a caller "
            "reasonably retries the ack and gets ClaimNotFound instead: "
            f"{message}"
        )

    def test_a_plain_ack_against_the_same_old_engine_stays_silent(
        self, monkeypatch,
    ) -> None:
        """Nothing was lost, so nothing is raised. A guard that fired here
        would break every existing caller against an older engine."""
        self._engine_answering(monkeypatch, {"acked": True})
        store = HttpTupleStore()
        assert store.ack("claim-1", "worker") is None

    def test_a_new_engine_returning_null_with_a_reply_sent_also_raises(
        self, monkeypatch,
    ) -> None:
        """Would be an engine defect rather than a version skew, but the
        caller's exposure is identical — request consumed, no reply — so it
        is caught by the same guard rather than by a key-presence test that
        would wave the null through."""
        self._engine_answering(monkeypatch, {"acked": True, "reply_id": None})
        store = HttpTupleStore()
        with pytest.raises(ReplyNotWrittenError):
            store.ack(
                "claim-1", "worker",
                reply=ReplySpec(subspace="mailbox/a", keys={"to": "a"}),
            )

    def test_a_plain_ack_against_the_rdr206_engine_returns_none_not_an_error(
        self, monkeypatch,
    ) -> None:
        """The RDR-206 engine sends reply_id: null on a plain ack. That is
        the normal path and must stay quiet."""
        self._engine_answering(monkeypatch, {"acked": True, "reply_id": None})
        store = HttpTupleStore()
        assert store.ack("claim-1", "worker") is None

    def test_the_happy_path_is_untouched(self, monkeypatch) -> None:
        self._engine_answering(monkeypatch, {"acked": True, "reply_id": "ab" * 32})
        store = HttpTupleStore()
        assert store.ack(
            "claim-1", "worker",
            reply=ReplySpec(subspace="mailbox/a", keys={"to": "a"}),
        ) == "ab" * 32
