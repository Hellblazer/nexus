# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-6flt7: native-smoke.sh has no /v1/tuples probe. Every OTHER jOOQ-backed
# endpoint family this script exercises (T1 scratch, memory/plans/taxonomy/chash)
# has a real-Python-client probe living next to this file (nexus-cm5km's
# t1_real_client.py / t2_real_client.py), each closing the same seam: routing
# (nexus.db.t2.http_tuple_store.HttpTupleStore) proven together with backend
# (this compiled native artifact), not separately. The tuple space (RDR-205,
# RDR-206) had NO probe of either kind here -- every tuple test ran on the JVM
# jar (the Java suite, tests/test_rdr206_mvv_*.py), so a native-image-only
# reflection or serialization gap in TupleHandler/TupleRepository (the same
# defect CLASS nexus-opr9m found in the T1 schema, and the class the T1 probe
# above was written to catch) had no native gate at all.
#
# The real Python client, not curl (nexus-6flt7 scope): every raw-curl
# assertion in native-smoke.sh checks only an HTTP status code via `assert()`.
# HttpTupleStore's response shapes are exactly what the RDR-206 review flagged
# as easy to get quietly wrong across a JVM/native boundary -- renew's
# ISO-8601 lease_until (MAPPER's JavaTimeModule rendering an OffsetDateTime),
# ack's reply_id (a 64-hex tuple id or null), and the nine typed refusals
# (ClaimNotFound, SchemaViolation, ...) that share HTTP status codes and so
# cannot be told apart by status alone (_raise_typed reads the JSON "error"
# field). The real client already parses all three; a curl probe would have
# to hand-roll typed-error classification this file gets for free, and would
# still only prove the JSON shape, never that HttpTupleStore's own request/
# response handling works against this SAME compiled artifact -- the identical
# gap nexus-97oz3 (T1) and nexus-rxqqd (memory/plans/taxonomy/chash) closed
# for their surfaces.
#
# See native-smoke.sh for the env contract (NEXUS_CONFIG_DIR,
# NX_SERVICE_HOST/PORT/TOKEN) this script expects to already be set in its
# environment. Unlike t1_real_client.py / t2_real_client.py this probe does
# not read NATIVE_SMOKE_CLEANUP_ROWS: every row it writes goes to a
# uuid4-derived, per-run-unique mailbox address (below), so nothing here ever
# collides with or accumulates alongside a prior run's rows even against an
# external, non-throwaway NX_DB_URL -- the mailbox template's own
# retention_seconds (604800, a week) is what eventually reclaims them, the
# same as any other mailbox traffic.
import os
import uuid
from datetime import datetime

import httpx

from nexus.db.t2._refreshable_client import DEFAULT_TENANT
from nexus.db.t2.http_tuple_store import ClaimNotFoundError, HttpTupleStore, TooLargeError
from nexus.db.t2.records import ReplySpec

tuples = HttpTupleStore()

# Unique per run (uuid4-derived, mirrors TupleAckWithReplyTest.addr()): out()
# is idempotent-by-construction on (keys, nonce), so a fixed address across
# reruns would either collide with a still-claimed prior row (the mailbox
# template's take.max_attempts=3, retention_seconds=604800 -- a week) or make
# the "exactly that row" rdp assertion below depend on what a PRIOR run left
# behind rather than on this run's own write.
request_to = f"native-smoke-tuples-req-{uuid.uuid4().hex[:12]}"
reply_to = f"native-smoke-tuples-reply-{uuid.uuid4().hex[:12]}"
claimant = "native-smoke-tuples-py"

# ── out: write a request into mailbox/<request_to> ──────────────────────────
request_nonce = f"nonce-{uuid.uuid4().hex}"
tuple_id = tuples.out(
    f"mailbox/{request_to}",
    {"to": request_to},
    {"from": "native-smoke-asker"},
    "native smoke tuple request",
    nonce=request_nonce,
)
assert tuple_id and len(tuple_id) == 64, f"out() did not return a 64-hex id: {tuple_id!r}"

# ── inp: claim it (destructive, non-blocking) ───────────────────────────────
claimed = tuples.inp(
    f"mailbox/{request_to}", {"to": request_to}, claimant=claimant, lease_s=60,
)
assert claimed is not None, "inp() did not claim the tuple just written"
row, claim_id = claimed
assert row.id == tuple_id, f"inp() claimed the wrong row: {row.id} != {tuple_id}"
assert claim_id, "inp() returned no claim_id"
assert row.lease_until, "inp() claimed row carries no lease_until"

# ── renew: extend the lease, assert lease_until moved forward ───────────────
# RDR-206 (nexus-h61dl.4). Against an engine predating /renew, the unknown
# route falls through TupleHandler's switch default branch and answers 404
# with {"error":"unknown tuples op: /renew"} -- a code _raise_typed does not
# recognise (not one of the nine RDR-205 TupleException subtypes), so it
# re-raises the bare httpx.HTTPStatusError uncaught here (verified against
# engine-service-v0.1.116, see HttpTupleStore.renew's own docstring). This
# call is deliberately NOT wrapped in try/except: on such an engine it raises,
# the script exits non-zero without printing OK, and native-smoke.sh's
# `grep -q "^OK$"` reports FAIL -- the non-vacuity property nexus-6flt7 asks
# for, demonstrated by the exact 404 path rather than a live stub engine.
# `before` is the wire ISO string from inp()'s TupleRow; new_lease_until is
# the parsed, aware datetime renew() returns. Re-parse `before` so both sides
# compare as datetimes, not a string-vs-datetime mismatch.
before_dt = datetime.fromisoformat(row.lease_until)
new_lease_until = tuples.renew(claim_id, claimant, 120)
assert new_lease_until > before_dt, (
    f"renew() must move lease_until FORWARD: {new_lease_until} is not after {before_dt}"
)

# ── ack with reply: consume the request, write a reply to mailbox/<reply_to> ─
reply_id = tuples.ack(
    claim_id, claimant,
    reply=ReplySpec(f"mailbox/{reply_to}", {"to": reply_to}, {"from": "native-smoke-answerer"}, "native smoke reply"),
)
assert reply_id and len(reply_id) == 64 and all(c in "0123456789abcdef" for c in reply_id), (
    f"ack(reply=...) did not return a 64-hex reply_id: {reply_id!r}"
)

# ── rdp: read the reply address, exactly the one row ────────────────────────
# n=2 (nexus-6flt7 scope: "rdp the reply address at n=2 (exactly that row)")
# -- asks for up to 2 rows to prove the ack wrote EXACTLY one, not that a
# smaller n happened to cap the count at 1.
replies = tuples.rdp(f"mailbox/{reply_to}", {"to": reply_to}, n=2)
assert len(replies) == 1, f"rdp(n=2) on the reply address must return exactly one row: {replies}"
assert replies[0].id == reply_id, f"rdp() found the wrong row: {replies[0].id} != {reply_id}"
assert replies[0].body == "native smoke reply", replies[0].body

# ── typed refusal: a stale ack on the already-consumed claim -> ClaimNotFound ─
try:
    tuples.ack(claim_id, claimant)
    raise AssertionError("a second ack on an already-consumed claim_id must raise ClaimNotFoundError")
except ClaimNotFoundError:
    pass
except httpx.HTTPStatusError as exc:  # pragma: no cover -- only if _raise_typed's mapping regresses
    raise AssertionError(
        f"stale ack raised an unmapped HTTPStatusError instead of ClaimNotFoundError: {exc}"
    ) from exc

# ── typed refusal: the ENGINE's own per-template cap, through the real client ─
# nexus-r7xao: proves the ENGINE'S OWN size check (a native-image reflection/
# serialization gap in TooLargeException would show up here exactly as it
# would for any other TupleException subtype) through HttpTupleStore's typed-
# error mapping. A single oversized GLOBAL-cap field cannot reach the engine
# for this: the client mirrors the same 4096-byte body cap and would refuse
# it locally first (RequestTooLargeError/TooLargeError raised client-side,
# never sent). The genuine engine-only gap is a template's LOWER
# max_body_bytes, which the client has no copy of (RDR-205 §Technical
# Design: "no client carries a copy" of the registry) -- ledger/<session_id>
# declares max_body_bytes: 0, so even a 1-byte body passes every client-side
# check and is refused only when the engine itself applies the template's cap.
oversize_session = f"native-smoke-tuples-oversize-{uuid.uuid4().hex[:12]}"
try:
    tuples.out(
        f"ledger/{oversize_session}",
        {"agent_id": "native-smoke-oversize-agent", "kind": "start"},
        {"agent_type": "developer"},
        "x",
    )
    raise AssertionError(
        "an out() to ledger/<session_id> (max_body_bytes: 0) with a 1-byte "
        "body must raise TooLargeError from the ENGINE"
    )
except TooLargeError:
    pass

# ── raw over-8KB request: proves the ENGINE'S OWN whole-request cap ─────────
# Bypasses HttpTupleStore's identical 8 KB client-side pre-check entirely (a
# raw httpx POST, not the client) so this leg actually reaches the compiled
# native engine rather than being refused locally before any bytes are sent.
_raw_base = f"http://{os.environ['NX_SERVICE_HOST']}:{os.environ['NX_SERVICE_PORT']}"
raw_resp = httpx.post(
    f"{_raw_base}/v1/tuples/out",
    content=b'{"subspace":"mailbox/' + oversize_addr.encode() + b'","keys":{"to":"'
    + (b"x" * 9000) + b'"}}',
    headers={
        "Authorization": f"Bearer {os.environ['NX_SERVICE_TOKEN']}",
        "X-Nexus-Tenant": os.environ.get("NX_SERVICE_TENANT", DEFAULT_TENANT),
        "Content-Type": "application/json",
    },
)
assert raw_resp.status_code == 413, (
    f"a raw over-8KB request must be refused 413, got {raw_resp.status_code}: {raw_resp.text!r}"
)
raw_body = raw_resp.json()
assert raw_body.get("error") == "TooLarge", f"expected error=TooLarge, got {raw_body!r}"

print("OK")
