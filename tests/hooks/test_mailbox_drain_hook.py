# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The UserPromptSubmit mailbox drain hook (bead nexus-6konb.7, MM-2.2).

The deterministic CONSUMER OF RECORD for RDR-205 mailbox delivery. The
Phase 1 watcher (``nx tuple watch``) pings and never claims; this hook
claims, acks and renders. They are never two renderers of one row --
see bead nexus-73vnw's DISJOINTNESS paragraph.

These tests drive the real script as a subprocess against a mock engine,
the same shape ``test_tuple_ledger_project.py`` uses for the sibling
tuple hook, because the thing under test is a stdlib-only script with no
``nexus`` import and its failure modes are transport-shaped.
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "mailbox_drain.py"
)

SESSION_ID = "sess-mailbox-drain"


def _payload(**overrides: str) -> str:
    base = {
        "session_id": SESSION_ID,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "what is the status",
        "cwd": "/tmp",
    }
    base.update(overrides)
    return json.dumps(base)


def _run(
    *,
    tmp_path: Path,
    stdin: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    env = {k: v for k, v in os.environ.items() if not k.startswith("NX_SERVICE_")}
    env["NEXUS_CONFIG_DIR"] = str(config_dir)
    env["XDG_STATE_HOME"] = str(state_dir)
    # nexus-6konb.19: the hook may spawn `nx hook mailbox-arm`. A PATH with no
    # nx keeps every test off the live install; re-arm tests pass a fake nx.
    env["PATH"] = "/usr/bin:/bin"
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin if stdin is not None else _payload(),
        capture_output=True,
        text=True,
        env=env,
        # A bound on a HANG, not on performance. The hook budgets itself at 6s
        # internally; this only stops a wedged subprocess from hanging the suite.
        # It was 20s, which is the load-sensitive shape fixed under nexus-61vos:
        # interpreter startup plus a busy box can eat that without anything being
        # wrong, and the red then names the hook rather than the contention.
        timeout=300,
    )


def _write_storage_lease(config_dir: Path, *, host: str, port: int) -> None:
    record = {
        "scope_key": str(os.getuid()),
        "generation": 1,
        "owner_token": "test-owner",
        "heartbeat_epoch": time.time(),
        "ttl": 30.0,
        "endpoint": {"host": host, "port": port, "token": "local-supervisor-token"},
        "version": "test",
        "payload": {},
        "status": "live",
        "format_version": 1,
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"storage_service_addr.{os.getuid()}"
    path.write_text(json.dumps(record))
    path.chmod(0o600)


def _row(tuple_id: str, *, sender: str = "peer-a", body: str = "hello",
         kind: str = "note", claim_state=None, to: str = SESSION_ID) -> dict:
    return {
        "id": tuple_id,
        "subspace": f"mailbox/{to}",
        "template": "mailbox",
        "keys": {"to": to},
        "dims": {"from": sender, "kind": kind, "correlation_id": "c-1"},
        "body": body,
        "claim_state": claim_state,
        "claimant": None,
        "lease_until": None,
        "attempts": 0,
        "consumed_at": None,
        "consumed_by": None,
        "expires_at": None,
        "created_at": "2026-09-12T00:00:00Z",
    }


class _MockEngine:
    """Serves /v1/tuples/{rd,in,ack}. ``rows`` is what rd returns; ``claimable``
    controls whether in succeeds, so a row taken by a peer between rd and in can
    be constructed."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.claimable: bool = True
        self.ack_ok: bool = True
        self.calls: list[tuple[str, dict]] = []
        self.rd_delay_s: float = 0.0
        #: Delay applied only to a paginated follow-up ``rd`` (one that carries
        #: a ``since`` cursor), so a full first page can answer instantly while
        #: the page needed to confirm a pending id's true absence never
        #: returns in time (nexus-1kvk3's budget-exhausted path).
        self.rd_delay_since_s: float = 0.0
        #: Drop the connection on the Nth call to this route (1-based), AFTER
        #: applying its effect. Models the killing case: the engine consumed the
        #: row and the client never learned it.
        self.drop_after_effect_on: tuple[str, int] | None = None
        #: Kill the connection on an rd for this address, so the hook raises
        #: _Skip on that mailbox and no other.
        self.fail_rd_for: str | None = None
        #: Answer this route with this HTTP status instead of its normal reply.
        #: Only 404 is a confirmed negative; every other status leaves the
        #: outcome UNKNOWN, which is the distinction these model.
        self.status_for: dict[str, int] = {}
        #: Apply ack's effect (consume the row) and THEN answer 500. The engine
        #: committed and failed on the way out -- the window where treating a
        #: non-2xx as a clean refusal loses the message for good.
        self.ack_500_after_effect: bool = False
        #: rd answers 200 with a body that is not an object at all.
        self.malformed_rd: bool = False
        #: Same, but only for this address, so an unexpected failure can be aimed
        #: at ONE mailbox and the others watched for collateral damage.
        self.malformed_rd_for: str | None = None
        self._route_counts: dict[str, int] = {}
        #: Serializes the claim decision (read-eligible, then mutate) and the
        #: ack decision (read-matched, then mutate) across concurrent handler
        #: threads -- ThreadingHTTPServer runs one thread per connection, and
        #: two real drain subprocesses hitting this engine at once (RDR-208
        #: Phase 2 Step 3's concurrent-drain test) need the SAME atomicity a
        #: real engine's own claim/ack provides, or two threads can each read
        #: "eligible" before either mutates, and both "claim" the same row.
        self._claim_lock = threading.Lock()
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:  # noqa: N802, C901
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    body = {}
                engine.calls.append((self.path, body))
                engine._route_counts[self.path] = engine._route_counts.get(self.path, 0) + 1
                if self.path in engine.status_for:
                    self._json(engine.status_for[self.path], {"error": "forced"})
                    return
                if self.path == "/v1/tuples/rd" and engine.malformed_rd:
                    self._json(200, ["not", "an", "object"])
                    return
                if self.path == "/v1/tuples/rd" and engine.malformed_rd_for is not None \
                        and (body.get("keys_pattern") or {}).get("to") == engine.malformed_rd_for:
                    self._json(200, ["not", "an", "object"])
                    return
                if self.path == "/v1/tuples/rd":
                    if engine.fail_rd_for and (body.get("keys_pattern") or {}).get(
                        "to",
                    ) == engine.fail_rd_for:
                        self.close_connection = True
                        return
                    if engine.rd_delay_s:
                        time.sleep(engine.rd_delay_s)
                    if engine.rd_delay_since_s and body.get("since"):
                        time.sleep(engine.rd_delay_since_s)
                    pattern = (body.get("keys_pattern") or {}).get("to")
                    rows = [
                        r for r in engine.rows
                        if pattern is None or r["keys"].get("to") == pattern
                    ]
                    # Sorted + paginated (nexus-galkv.6 fix 1 harness): the
                    # real engine orders `rd` by (created_at, id) ascending and
                    # caps a page at the requested `n`. The mock used to
                    # ignore both and return every matching row in one shot,
                    # which made a >_PROBE_N-row backlog undetectable by any
                    # test -- exactly the shape the confirming-probe
                    # pagination bug needed to hide behind.
                    rows = sorted(
                        rows, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")),
                    )
                    since = body.get("since")
                    if since:
                        since_key = (str(since.get("created_at") or ""), str(since.get("id") or ""))
                        rows = [
                            r for r in rows
                            if (str(r.get("created_at") or ""), str(r.get("id") or "")) > since_key
                        ]
                    n = body.get("n")
                    if isinstance(n, int) and n > 0:
                        rows = rows[:n]
                    self._json(200, {"tuples": rows})
                elif self.path == "/v1/tuples/in":
                    if not engine.claimable:
                        self._json(200, {})
                        return
                    pattern = (body.get("keys_pattern") or {}).get("to")
                    claimant_in = body.get("claimant")
                    now = time.time()
                    # Lease semantics (nexus-galkv.6 harness fix): a row already
                    # claimed with a live lease is NOT eligible -- otherwise two
                    # concurrent `in` calls on one mailbox both "claim" the same
                    # row, which a real engine's exactly-once contract forbids
                    # and which made the concurrent-drain test vacuous. The read
                    # (eligible) and the write (mutate the chosen row) happen
                    # under one lock, matching the atomicity a real engine's
                    # claim provides -- two ThreadingHTTPServer handler threads
                    # racing here otherwise both read "eligible" before either
                    # mutates, and both claim the same row.
                    with engine._claim_lock:
                        # Same-claimant idempotent retake (RDR-205 Technical
                        # Design "Claim"; TupleRepository.claimOnce,
                        # service/src/main/java/dev/nexus/service/db/
                        # TupleRepository.java:694-706): a caller presenting
                        # the SAME claimant as an already-claimed, still-live,
                        # unconsumed row gets that SAME claim back, no new
                        # state change. Modeled here (nexus-galkv.6 fix 2) so
                        # a claimant COLLISION across two concurrent drain
                        # processes -- the exact hazard fix 2's per-invocation
                        # unique claimant closes -- is reproducible by a test,
                        # rather than the mock's own conservative "a claimed
                        # row is never eligible" rule above hiding it by
                        # refusing every re-claim outright, real engine
                        # retake or not.
                        retake = [
                            r for r in engine.rows
                            if r["claim_state"] == "claimed"
                            and r.get("claimant") == claimant_in
                            and (r.get("lease_until") or 0) > now
                            and (pattern is None or r["keys"].get("to") == pattern)
                        ]
                        if retake:
                            row = retake[0]
                            self._json(200, {"tuple": dict(row), "claim_id": "claim-" + row["id"]})
                            return
                        eligible = [
                            r for r in engine.rows
                            if r["claim_state"] != "dead"
                            and (pattern is None or r["keys"].get("to") == pattern)
                            and (r.get("claim_state") != "claimed"
                                 or (r.get("lease_until") or 0) < now)
                        ]
                        if not eligible:
                            self._json(200, {})
                            return
                        row = eligible[0]
                        row["claim_state"] = "claimed"
                        row["claimant"] = claimant_in
                        row["lease_until"] = now + float(body.get("lease_s") or 30)
                        claimed = dict(row)
                    self._json(200, {"tuple": claimed, "claim_id": "claim-" + claimed["id"]})
                elif self.path == "/v1/tuples/ack":
                    if not engine.ack_ok:
                        # This forced refusal is a SIMPLIFICATION of the
                        # engine's real ClaimNotFoundException path -- the
                        # mock does not model every state a real
                        # ClaimNotFound can mean, only the one this test
                        # suite exercises: the claim is invalid (already
                        # expired or never valid), not "still held by
                        # someone else". Per that reading, and per this test
                        # file's own _drain_address call site docstring
                        # ("the lease lapses and the row returns to the
                        # mailbox"), the claim this forced 404 refuses is
                        # released here too -- otherwise a lease
                        # semantics-aware `in` (nexus-galkv.6) would keep the
                        # row unreclaimable for its full 30s and a same-prompt
                        # or next-prompt re-claim test would starve on a lease
                        # nothing actually holds any more.
                        cid = body.get("claim_id", "")
                        with engine._claim_lock:
                            for r in engine.rows:
                                if "claim-" + r["id"] == cid:
                                    r["claim_state"] = None
                                    r["claimant"] = None
                                    r["lease_until"] = None
                        self._json(404, {"error": "ClaimNotFound"})
                        return
                    cid = body.get("claim_id", "")
                    with engine._claim_lock:
                        matched = [r for r in engine.rows if "claim-" + r["id"] == cid]
                        if not matched:
                            # No live claim by this id: already acked, or never
                            # claimed. A real engine answers ClaimNotFound either way.
                            self._json(404, {"error": "ClaimNotFound"})
                            return
                        engine.rows = [r for r in engine.rows if "claim-" + r["id"] != cid]
                    if engine.ack_500_after_effect:
                        self._json(500, {"error": "boom"})
                        return
                    drop = engine.drop_after_effect_on
                    if drop and drop[0] == self.path and \
                            engine._route_counts[self.path] == drop[1]:
                        # effect applied, response never sent
                        self.close_connection = True
                        return
                    self._json(200, {})
                else:
                    self._json(404, {"error": "not found"})

            def _json(self, status: int, payload: object) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def engine():
    made: list[_MockEngine] = []

    def make() -> _MockEngine:
        e = _MockEngine()
        made.append(e)
        return e

    yield make
    for e in made:
        e.close()


def _wired(tmp_path: Path, eng: _MockEngine) -> None:
    _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=eng.port)


class TestDrainDelivers:
    def test_empty_mailbox_injects_nothing_and_exits_zero(self, tmp_path, engine) -> None:
        eng = engine()
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == ""

    def test_one_waiting_row_is_rendered_claimed_and_acked(self, tmp_path, engine) -> None:
        eng = engine()
        eng.rows = [_row("aa11", sender="nexus-70", body="web/index.html changed")]
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0, res.stderr
        assert "nexus-70" in res.stdout
        assert "web/index.html changed" in res.stdout
        assert "/v1/tuples/in" in eng.paths()
        assert "/v1/tuples/ack" in eng.paths()

    def test_a_delivered_row_is_not_re_injected_on_the_next_prompt(
        self, tmp_path, engine,
    ) -> None:
        eng = engine()
        eng.rows = [_row("bb22", body="only once")]
        _wired(tmp_path, eng)
        first = _run(tmp_path=tmp_path)
        assert "only once" in first.stdout
        second = _run(tmp_path=tmp_path)
        assert "only once" not in second.stdout

    def test_the_body_is_delivered_here_unlike_the_watcher_ping(
        self, tmp_path, engine,
    ) -> None:
        """The watcher deliberately never carries a body; this hook is the
        consumer of record and delivery without the body would be pointless."""
        eng = engine()
        eng.rows = [_row("cc33", body="the actual message text")]
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert "the actual message text" in res.stdout


class TestDrainNeverRendersWhatItDidNotConsume:
    def test_a_row_claimed_by_a_peer_between_rd_and_in_is_not_rendered(
        self, tmp_path, engine,
    ) -> None:
        """The read-then-claim hazard at the client. rd saw it; in lost the
        race. Rendering it would tell the session it received mail that
        someone else is now holding."""
        eng = engine()
        eng.rows = [_row("dd44", body="someone else got this")]
        eng.claimable = False
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0, res.stderr
        assert "someone else got this" not in res.stdout

    def test_a_row_whose_ack_fails_is_not_rendered(self, tmp_path, engine) -> None:
        """Claimed but not consumed: the lease will lapse and the row returns
        to the mailbox, so rendering it would deliver it twice."""
        eng = engine()
        eng.rows = [_row("ee55", body="ack will fail")]
        eng.ack_ok = False
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0, res.stderr
        assert "ack will fail" not in res.stdout


class TestDeadLetteredRows:
    """A dead-lettered row is unclaimable by construction, so claim-and-ack
    cannot be its dedup and the hook is not a floor under it. It is still
    surfaced once, because the alternative is that a session with no watcher
    armed never learns the message existed (MM-1.4 phase review)."""

    def test_a_dead_row_is_surfaced_once_and_never_claimed(self, tmp_path, engine) -> None:
        eng = engine()
        eng.rows = [_row("ff66", body="poison", claim_state="dead")]
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0, res.stderr
        assert "ff66" in res.stdout
        assert "never be claimed" in res.stdout or "undeliverable" in res.stdout
        # it must not have been claimed: no in/ack for a dead row
        assert "/v1/tuples/ack" not in eng.paths()

    def test_a_dead_row_is_not_surfaced_again_on_the_next_prompt(
        self, tmp_path, engine,
    ) -> None:
        eng = engine()
        eng.rows = [_row("gg77", body="poison", claim_state="dead")]
        _wired(tmp_path, eng)
        first = _run(tmp_path=tmp_path)
        assert "gg77" in first.stdout
        second = _run(tmp_path=tmp_path)
        assert "gg77" not in second.stdout

    def test_a_dead_row_does_not_hide_live_mail_behind_it(self, tmp_path, engine) -> None:
        eng = engine()
        eng.rows = [
            _row("hh88", body="poison", claim_state="dead"),
            _row("ii99", body="real mail"),
        ]
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert "real mail" in res.stdout
        assert "hh88" in res.stdout


class TestAddressRegistry:
    """The session id resolves from the hook payload. The INSTANCE NAME is in
    no environment variable anywhere (MM-1.3), so it can only be drained once
    something has registered it. Until then instance-addressed mail has no
    floor, which the bead says out loud.

    nexus-6konb.9 defect fix: the registry is PER-SESSION
    (``<config>/tuple-watch/addresses.d/<session id>``), corrected from an
    earlier machine-wide ``<config>/tuple-watch/addresses`` file that let
    whichever session prompted first drain every other session's
    instance-addressed mail too."""

    def _reg(self, tmp_path, session_id: str = SESSION_ID):
        reg = tmp_path / "config" / "tuple-watch" / "addresses.d" / session_id
        reg.parent.mkdir(parents=True, exist_ok=True)
        return reg

    def test_the_session_id_address_is_always_drained(self, tmp_path, engine) -> None:
        eng = engine()
        eng.rows = [_row("jj10", body="to my session id")]
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert "to my session id" in res.stdout
        rd_bodies = [b for p, b in eng.calls if p == "/v1/tuples/rd"]
        assert any(b.get("subspace") == f"mailbox/{SESSION_ID}" for b in rd_bodies)

    def test_a_registered_address_is_drained_too(self, tmp_path, engine) -> None:
        eng = engine()
        self._reg(tmp_path).write_text("nexus-19\n", encoding="utf-8")
        _wired(tmp_path, eng)
        _run(tmp_path=tmp_path)
        rd_bodies = [b for p, b in eng.calls if p == "/v1/tuples/rd"]
        assert any(b.get("subspace") == "mailbox/nexus-19" for b in rd_bodies)

    def test_an_unregistered_instance_address_is_not_drained(
        self, tmp_path, engine,
    ) -> None:
        """The honest negative: with nothing registered, the hook cannot know
        this session is also called nexus-19, so that mailbox has no floor."""
        eng = engine()
        _wired(tmp_path, eng)
        _run(tmp_path=tmp_path)
        rd_bodies = [b for p, b in eng.calls if p == "/v1/tuples/rd"]
        assert not any(b.get("subspace") == "mailbox/nexus-19" for b in rd_bodies)

    def test_registry_junk_and_duplicates_are_tolerated(self, tmp_path, engine) -> None:
        eng = engine()
        self._reg(tmp_path).write_text(
            f"\n  nexus-19  \n\n# a comment\nnexus-19\n{SESSION_ID}\n", encoding="utf-8",
        )
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0, res.stderr
        subspaces = [b.get("subspace") for p, b in eng.calls if p == "/v1/tuples/rd"]
        assert subspaces.count("mailbox/nexus-19") == 1
        assert subspaces.count(f"mailbox/{SESSION_ID}") == 1
        assert not any(s and "#" in s for s in subspaces)

    def test_a_machine_wide_flat_registry_file_is_ignored(self, tmp_path, engine) -> None:
        """The old design's flat ``<config>/tuple-watch/addresses`` file, if
        one happens to exist on disk (a relic, or a human who followed the
        stale doc), must never be read by this hook any more -- only the
        per-session ``addresses.d/<session id>`` file counts."""
        eng = engine()
        flat = tmp_path / "config" / "tuple-watch" / "addresses"
        flat.parent.mkdir(parents=True, exist_ok=True)
        flat.write_text("nexus-flat-relic\n", encoding="utf-8")
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0, res.stderr
        rd_bodies = [b for p, b in eng.calls if p == "/v1/tuples/rd"]
        assert not any(b.get("subspace") == "mailbox/nexus-flat-relic" for b in rd_bodies)

    def test_cross_session_drain_never_leaks_a_peer_sessions_instance_mailbox(
        self, tmp_path, engine,
    ) -> None:
        """Two sessions on one box. Session A's watcher registered instance
        NAME_A under A's own session id. Session B's drain (a DIFFERENT
        payload session id) must NOT drain mailbox/NAME_A -- only A's own
        drain may. This crosses session ids on purpose: a same-session test
        would pass even with the retired machine-wide design, which is
        exactly the bug this fix closes."""
        session_a, session_b = "sess-A-owns-instance", "sess-B-different-session"
        instance_a = "nexus-instance-a"
        eng = engine()
        eng.rows = [_row("kk11", sender="peer", body="for instance A")]
        eng.rows[0]["keys"] = {"to": instance_a}
        eng.rows[0]["subspace"] = f"mailbox/{instance_a}"
        _wired(tmp_path, eng)
        self._reg(tmp_path, session_a).write_text(instance_a + "\n", encoding="utf-8")

        res_b = _run(tmp_path=tmp_path, stdin=_payload(session_id=session_b))
        assert res_b.returncode == 0, res_b.stderr
        rd_bodies_b = [b for p, b in eng.calls if p == "/v1/tuples/rd"]
        assert not any(b.get("subspace") == f"mailbox/{instance_a}" for b in rd_bodies_b)

        eng.calls.clear()
        res_a = _run(tmp_path=tmp_path, stdin=_payload(session_id=session_a))
        assert res_a.returncode == 0, res_a.stderr
        rd_bodies_a = [b for p, b in eng.calls if p == "/v1/tuples/rd"]
        assert any(b.get("subspace") == f"mailbox/{instance_a}" for b in rd_bodies_a)


class TestNeverBlocksThePrompt:
    def test_no_engine_reachable_skips_on_stderr_and_injects_nothing(
        self, tmp_path,
    ) -> None:
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0
        assert res.stdout.strip() == ""
        assert "SKIP" in res.stderr

    def test_a_hanging_engine_is_bounded_and_skips(self, tmp_path, engine) -> None:
        eng = engine()
        eng.rd_delay_s = 30.0
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0
        assert res.stdout.strip() == ""
        # Asserted from the hook's OWN statement that its deadline fired, not
        # from wall clock measured out here (nexus-61vos). A wall-clock bound on
        # a shared box measures the box as much as the code, and a red would name
        # the hook while meaning the machine was busy. The subprocess timeout
        # above is the hang guard; this is the behaviour.
        #
        # nexus-scc9t round 2: a peer's combined full run red'd on the exact
        # substrings below. mailbox_drain.py's per-call timeout is a RACE
        # between two clocks both set to call_timeout: the outer
        # thread.join(call_timeout) giving up while the background urlopen
        # call is still blocked reading the response ("{route} exceeded its
        # {call_timeout}s deadline; the prompt is not waiting for it",
        # mailbox_drain.py's _post around line 392) versus urlopen's OWN
        # internal socket timeout completing that thread FIRST with a
        # caught TimeoutError ("transport failure on {route}: ...", same
        # function, line ~395). Both fire from the SAME call_timeout and
        # both are the bounded-skip property this test exists to prove --
        # which one wins is scheduler-dependent under load, not a
        # behavioural difference, so accept either. The transport branch also
        # reports connection refused and malformed replies, so it counts only
        # when it names the timeout.
        assert "SKIP" in res.stderr
        assert (
            (
                "/v1/tuples/rd exceeded its" in res.stderr
                and "the prompt is not waiting for it" in res.stderr
            )
            or (
                "transport failure on /v1/tuples/rd" in res.stderr
                and "timed out" in res.stderr
            )
        ), res.stderr

    def test_malformed_payload_does_not_crash_the_prompt(self, tmp_path, engine) -> None:
        eng = engine()
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path, stdin="not json at all")
        assert res.returncode == 0
        assert res.stdout.strip() == ""

    def test_no_session_id_in_payload_skips(self, tmp_path, engine) -> None:
        eng = engine()
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path, stdin=json.dumps({"hook_event_name": "UserPromptSubmit"}))
        assert res.returncode == 0
        assert res.stdout.strip() == ""


class TestCredentialPolicy:
    """This hook is a synchronous call with a prompt waiting, like
    t2_prefix_scan and routing/_lib, not a fire-and-forget write like the
    sibling ledger hook. So it accepts a static service_token as a last
    resort: refusing one would make the drain silently inert on a managed box
    onboarded with `nx config set service_token` and nothing else, which is a
    documented path. The floor must not vanish where a user did it right."""

    def test_a_managed_box_with_only_a_static_env_token_still_drains(
        self, tmp_path, engine,
    ) -> None:
        eng = engine()
        eng.rows = [_row("kk11", body="managed box mail")]
        res = _run(tmp_path=tmp_path, env_overrides={
            "NX_SERVICE_URL": f"http://127.0.0.1:{eng.port}",
            "NX_SERVICE_TOKEN": "static-managed-token",
        })
        assert res.returncode == 0, res.stderr
        assert "managed box mail" in res.stdout

    def test_a_group_readable_supervisor_lease_is_refused_not_used(
        self, tmp_path, engine,
    ) -> None:
        """The lease token authorizes real engine writes. A lease another local
        account could read must not be trusted, and the module's own accessor is
        what enforces that -- reading the raw dict would skip the audit."""
        eng = engine()
        eng.rows = [_row("ll22", body="should not be delivered")]
        _wired(tmp_path, eng)
        (tmp_path / "config" / f"storage_service_addr.{os.getuid()}").chmod(0o644)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0
        assert "should not be delivered" not in res.stdout
        assert "SKIP" in res.stderr


class TestPartialFailureNeverLosesDeliveredMail:
    """The critical both reviewers reproduced. Every one of these needs MORE
    THAN ONE live row per mailbox, which is exactly the path that had no
    coverage and is why the defect shipped green."""

    def test_a_failure_on_a_later_row_does_not_retract_an_earlier_one(
        self, tmp_path, engine,
    ) -> None:
        eng = engine()
        eng.rows = [_row("r1", body="first message"), _row("r2", body="second message")]
        # row 1 acks cleanly; row 2's ack is applied and the response dropped
        eng.drop_after_effect_on = ("/v1/tuples/ack", 2)
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0
        assert "first message" in res.stdout, (
            "a row that was cleanly claimed, acked and rendered was discarded "
            "because a LATER row failed"
        )

    def test_a_row_consumed_with_a_lost_ack_response_is_recovered_next_prompt(
        self, tmp_path, engine,
    ) -> None:
        """ack reached the engine, the response did not. The row is gone from
        the mailbox, so nothing else will ever show it."""
        eng = engine()
        eng.rows = [_row("r9", body="consumed but never shown")]
        eng.drop_after_effect_on = ("/v1/tuples/ack", 1)
        _wired(tmp_path, eng)
        first = _run(tmp_path=tmp_path)
        assert "consumed but never shown" not in first.stdout
        assert not eng.rows, "the engine should have consumed it"
        second = _run(tmp_path=tmp_path)
        assert "consumed but never shown" in second.stdout, (
            "the row was consumed at the engine and never delivered to anyone"
        )

    def test_recovery_does_not_double_deliver_on_a_third_prompt(
        self, tmp_path, engine,
    ) -> None:
        eng = engine()
        eng.rows = [_row("r8", body="exactly once please")]
        eng.drop_after_effect_on = ("/v1/tuples/ack", 1)
        _wired(tmp_path, eng)
        _run(tmp_path=tmp_path)
        second = _run(tmp_path=tmp_path)
        assert "exactly once please" in second.stdout
        third = _run(tmp_path=tmp_path)
        assert "exactly once please" not in third.stdout

    def test_an_ack_that_never_reached_the_engine_is_not_recovered_twice(
        self, tmp_path, engine,
    ) -> None:
        """The other branch: the row is STILL in the mailbox, so the normal path
        owns it and the pending record must be dropped, not delivered."""
        eng = engine()
        eng.rows = [_row("r7", body="still in the mailbox")]
        eng.ack_ok = False
        _wired(tmp_path, eng)
        first = _run(tmp_path=tmp_path)
        assert "still in the mailbox" not in first.stdout
        assert eng.rows, "the row should still be there"
        # A CLEAN refusal is unambiguous: the engine answered 404, so the ack
        # definitively did not land and the row is definitely still in the
        # mailbox. The pending record is therefore a duplicate and is dropped.
        # That is the opposite of a TRANSPORT failure on ack, where the client
        # cannot tell whether the engine consumed the row, and the record must
        # survive -- the case test_a_row_consumed_with_a_lost_ack_response_is_
        # recovered_next_prompt covers.
        assert not (tmp_path / "config" / "tuple-watch"
                    / f"{SESSION_ID}.pending.json").exists(), (
            "a clean 404 refusal left a duplicate pending record behind"
        )
        eng.ack_ok = True
        second = _run(tmp_path=tmp_path)
        assert second.stdout.count("still in the mailbox") == 1

    def test_several_rows_deliver_in_one_prompt(self, tmp_path, engine) -> None:
        eng = engine()
        eng.rows = [_row(f"m{i}", body=f"message {i}") for i in range(4)]
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        for i in range(4):
            assert f"message {i}" in res.stdout

    def test_one_failing_address_does_not_stop_the_other(self, tmp_path, engine) -> None:
        """_Skip is caught per address, not around the whole loop. The session-id
        mailbox is probed FIRST and made to fail outright; the registered address
        must still be drained afterwards."""
        eng = engine()
        good = _row("z1", body="from the good address")
        good["keys"] = {"to": "other-addr"}
        eng.rows = [good]
        # the FIRST rd is the session-id address: kill its connection so the
        # hook raises _Skip on it before ever reaching the second address
        eng.fail_rd_for = SESSION_ID
        reg = tmp_path / "config" / "tuple-watch" / "addresses.d" / SESSION_ID
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("other-addr\n", encoding="utf-8")
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0
        assert "SKIP" in res.stderr, "the first address must actually have failed"
        assert SESSION_ID in res.stderr
        assert "from the good address" in res.stdout, (
            "a failure on the first address stopped the second"
        )

    def test_an_unexpected_failure_on_one_address_does_not_stop_the_others(
        self, tmp_path, engine,
    ) -> None:
        """The per-address handler for UNEXPECTED exceptions, not just ``_Skip``.

        Written after a mutation showed the sibling malformed-response test could
        not tell the two handlers apart: deleting the per-address ``except
        Exception`` left it green, because ``main``'s outer guard caught the same
        error and still exited 0. The outer guard preserves the exit code and the
        SKIP line; it does NOT preserve "one bad mailbox must not stop the others",
        since it catches outside the loop and every later address is abandoned.
        That is the property only this test pins, and deleting the per-address
        handler fails it.
        """
        eng = engine()
        good = _row("u1", body="from the second address")
        good["keys"] = {"to": "other-addr"}
        eng.rows = [good]
        eng.malformed_rd_for = SESSION_ID
        reg = tmp_path / "config" / "tuple-watch" / "addresses.d" / SESSION_ID
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("other-addr\n", encoding="utf-8")
        _wired(tmp_path, eng)

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0
        assert "SKIP" in res.stderr and SESSION_ID in res.stderr, (
            "the first address must actually have failed"
        )
        assert "unexpected" in res.stderr, (
            "an unanticipated failure must say so, not read as a planned skip"
        )
        assert "from the second address" in res.stdout, (
            "an unexpected failure on the first address abandoned the second"
        )

    def test_a_poison_row_does_not_stop_the_mail_behind_it(self, tmp_path, engine) -> None:
        """A row whose ``dims`` is not a dict must not silence its own address.

        Found by the test-validator at this bead's close gate, reproduced live: both
        renderers called ``dims.get(...)`` on whatever the engine sent, and
        ``row.get("dims") or {}`` rescues None and {} but not a truthy non-dict. The
        per-address guard caught the resulting AttributeError, so the process and every
        other mailbox survived — but that address never drained again, and a live row
        sitting behind the poison row went undelivered across every subsequent prompt.

        Blocked rather than lost, since rendering happens before the ack. Still fatal to
        the floor this hook exists to be, which is why it is fixed here rather than
        deferred: the epic exists to close exactly this class.
        """
        eng = engine()
        poison = _row("poison1", body="unreadable")
        poison["dims"] = "not-a-dict"
        eng.rows = [poison, _row("good1", body="behind the poison row")]
        _wired(tmp_path, eng)

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "Traceback" not in res.stderr
        assert "behind the poison row" in res.stdout, (
            "a row with malformed dims blocked the deliverable row queued behind it"
        )

    def test_a_500_on_ack_keeps_the_pending_record_because_the_outcome_is_unknown(
        self, tmp_path, engine,
    ) -> None:
        """Phase 2 review ship-blocker. Every non-2xx used to collapse to the same
        ``None`` a 404 produces, and ``None`` on ack means "a clean refusal, the
        engine said no", which clears the pending-ack safety record.

        A 500 is not that. ``TupleHandler`` lets an unexpected exception fall
        through to a bare 500, so the ack may already have COMMITTED -- the row is
        consumed and the record is the only trace anyone was ever going to see.
        Clearing it there loses the message permanently, silently, exit 0. This is
        the lost-ack-response window arriving as a status code instead of a dropped
        connection.

        The engine here applies ack's effect and THEN answers 500, which is exactly
        that case rather than a polite refusal.
        """
        eng = engine()
        eng.rows = [_row("s500", body="committed then five hundred")]
        eng.ack_500_after_effect = True
        _wired(tmp_path, eng)

        res = _run(tmp_path=tmp_path)

        # PRECONDITION, not the assertion under test (nexus-mzt40): twice in one
        # full-suite run this hook produced empty stdout AND empty stderr at exit 0,
        # which is the signature of an EMPTY MAILBOX, not of the status handling below.
        # Asserting on stderr first reported "expected SKIP, got ''", which names the
        # wrong thing. If the hook never reached this mock, say so.
        assert eng.calls, (
            f"the hook never reached the mock engine, so this test never exercised "
            f"status handling at all. rc={res.returncode} "
            f"stdout={res.stdout!r} stderr={res.stderr!r}"
        )

        assert res.returncode == 0
        assert "SKIP" in res.stderr and "500" in res.stderr, (
            "an ambiguous status must be reported, not swallowed"
        )
        pending = tmp_path / "config" / "tuple-watch" / f"{SESSION_ID}.pending.json"
        assert pending.exists(), (
            "the pending record was cleared on a 500, so the row the engine already "
            "consumed can never be recovered"
        )
        assert "committed then five hundred" in pending.read_text()

    def test_a_401_on_rd_is_not_read_as_an_empty_mailbox(
        self, tmp_path, engine,
    ) -> None:
        """The same conflation on the read side. ``None`` from rd means the mailbox
        is empty, which drives the pending record's presence check; a 401 says
        nothing whatever about what is in the mailbox, and treating it as emptiness
        delivered stale pending content and cleared it with nothing confirmed.
        """
        eng = engine()
        eng.status_for = {"/v1/tuples/rd": 401}
        _wired(tmp_path, eng)

        res = _run(tmp_path=tmp_path)

        # PRECONDITION, not the assertion under test (nexus-mzt40): twice in one
        # full-suite run this hook produced empty stdout AND empty stderr at exit 0,
        # which is the signature of an EMPTY MAILBOX, not of the status handling below.
        # Asserting on stderr first reported "expected SKIP, got ''", which names the
        # wrong thing. If the hook never reached this mock, say so.
        assert eng.calls, (
            f"the hook never reached the mock engine, so this test never exercised "
            f"status handling at all. rc={res.returncode} "
            f"stdout={res.stdout!r} stderr={res.stderr!r}"
        )

        assert res.returncode == 0
        assert "SKIP" in res.stderr and "401" in res.stderr, (
            "a 401 must surface as a skip, not pass as an empty mailbox"
        )

    def test_a_malformed_engine_response_skips_loudly_instead_of_crashing(
        self, tmp_path, engine,
    ) -> None:
        """Phase 2 review ship-blocker. Nothing but ``_Skip`` was caught, so a
        response the hook did not anticipate -- an rd body that is not an object --
        raised AttributeError, exited 1 and printed a traceback.

        This hook runs on EVERY UserPromptSubmit. That traceback lands in front of
        someone who typed something unrelated, and it contradicts the contract
        stated at the top of the script: one SKIP line, exit 0.
        """
        eng = engine()
        eng.malformed_rd = True
        _wired(tmp_path, eng)

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, (
            f"a malformed engine response must not fail the prompt; stderr:\n{res.stderr}"
        )
        assert "SKIP" in res.stderr
        assert "Traceback" not in res.stderr, "a raw traceback reached the user's prompt"

    def test_budget_exhausted_confirming_a_pending_id_keeps_the_record_and_says_so(
        self, tmp_path, engine,
    ) -> None:
        """nexus-1kvk3's budget-exhausted path. A pending id from an earlier
        ambiguous ack is not resolved by the first probe page -- a FULL page of
        PROBE_N rows, none of them matching it, so its true absence is still
        unknown -- and the page needed to confirm that never returns in time.

        The record must survive untouched (never guessed absent and delivered,
        never guessed present and dropped) and the hook must say why on
        stderr, not silently truncate.
        """
        eng = engine()
        eng.rows = [_row(f"filler{i}") for i in range(20)]  # a full PROBE_N page
        eng.rd_delay_since_s = 30.0  # the paginated follow-up never returns in time
        _wired(tmp_path, eng)

        pending_dir = tmp_path / "config" / "tuple-watch"
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_path = pending_dir / f"{SESSION_ID}.pending.json"
        pending_body = json.dumps({"entries": [
            {"id": "ghost-id", "rendered": "- from=peer-z ... an ambiguous earlier ack"},
        ]})
        pending_path.write_text(pending_body, encoding="utf-8")

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0
        assert res.stdout.strip() == "", (
            "a pending row whose presence could not be confirmed must not be "
            "rendered as either delivered or dropped"
        )
        assert "SKIP" in res.stderr
        assert pending_path.read_text() == pending_body, (
            "the pending record must survive an unconfirmed presence check "
            "untouched, not be cleared on a guess"
        )

    def test_a_pending_record_survives_a_drain_that_never_reclaims_the_row(
        self, tmp_path, engine,
    ) -> None:
        """The critic's round-2 critical. A pending record whose row is still in
        the mailbox must be KEPT, not cleared on sight.

        Clearing on presence assumed the same drain would go on to claim that
        row. A drain whose budget runs out first, or which cannot claim because
        a peer holds it, would leave nothing anywhere to notice if the original
        ambiguous ack later landed at the engine on its own schedule. The row
        would be consumed and shown to nobody, which is the failure class the
        recovery exists to close.
        """
        eng = engine()
        eng.rows = [_row("p1", body="ambiguous then late")]
        # prompt 1: the ack is applied but its response is lost -> pending record,
        # and the row is gone from the engine
        eng.drop_after_effect_on = ("/v1/tuples/ack", 1)
        _wired(tmp_path, eng)
        _run(tmp_path=tmp_path)
        pending = tmp_path / "config" / "tuple-watch" / f"{SESSION_ID}.pending.json"
        assert pending.exists()

        # prompt 2: the row is visible again (as it would be after a lapsed
        # lease) and this drain cannot claim it. The record must survive.
        eng.drop_after_effect_on = None
        eng.rows = [_row("p1", body="ambiguous then late")]
        eng.claimable = False
        second = _run(tmp_path=tmp_path)
        assert "ambiguous then late" not in second.stdout
        assert pending.exists(), (
            "the pending record was cleared merely because the row was visible; "
            "nothing is left to recover it if the original ack lands later"
        )

        # prompt 3: the original ack landed after all, so the row is gone. The
        # record is the only thing that can still deliver it.
        eng.rows = []
        eng.claimable = True
        third = _run(tmp_path=tmp_path)
        assert "ambiguous then late" in third.stdout, (
            "a message consumed at the engine was never shown to anyone"
        )


# ── Cleared-record drain (RDR-208 Phase 2 Step 3, bead nexus-galkv.6) ───────
#
# On ``/clear``, SessionStart records the previous session id in
# ``<config>/tuple-watch/cleared.<new session id>``. This hook reads its OWN
# session's record, after its own mailbox, and empties every mailbox it
# names -- deleting the record only when EVERY named mailbox's claim loop
# ended on an empty claim, a fresh probe shows no live row (dead-lettered
# rows do not count; a row under another process's lease does), and its
# pending file is empty or missing. Any other outcome keeps the record.


def _write_cleared_record(config_dir: Path, session_id: str, ids: list[str]) -> Path:
    path = config_dir / "tuple-watch" / f"cleared.{session_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(ids) + "\n", encoding="utf-8")
    return path


def _cleared_record_path(config_dir: Path, session_id: str) -> Path:
    return config_dir / "tuple-watch" / f"cleared.{session_id}"


class TestClearedRecordDrain:
    def test_mail_at_the_named_mailbox_arrives_once_and_the_record_is_deleted(
        self, tmp_path, engine,
    ) -> None:
        eng = engine()
        eng.rows = [_row("s1-mail", body="stranded by the clear", to="old-sess-id")]
        _wired(tmp_path, eng)
        record = _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "stranded by the clear" in res.stdout
        assert not record.exists()

        # The next prompt sees no record and no message: exactly once.
        second = _run(tmp_path=tmp_path)
        assert second.returncode == 0, second.stderr
        assert "stranded by the clear" not in second.stdout

    def test_the_cap_keeps_the_record_and_the_next_pass_confirms_empty(
        self, tmp_path, engine,
    ) -> None:
        """``_MAX_DELIVER`` (10) ends the claim loop without ever seeing an
        empty claim, so the record must survive that pass -- there may be
        more left. Only the NEXT pass, which claims nothing, deletes it."""
        eng = engine()
        eng.rows = [_row(f"m{i}", body=f"msg{i}", to="old-sess-id") for i in range(10)]
        _wired(tmp_path, eng)
        record = _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])

        first = _run(tmp_path=tmp_path)
        assert first.returncode == 0, first.stderr
        for i in range(10):
            assert f"msg{i}" in first.stdout
        assert record.exists(), "the cap was reached; the record must survive"

        second = _run(tmp_path=tmp_path)
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == ""
        assert not record.exists()

    def test_budget_exhausted_before_the_cleared_mailbox_keeps_the_record(
        self, tmp_path, engine,
    ) -> None:
        eng = engine()
        eng.rows = [_row("s1-mail", body="stranded by the clear", to="old-sess-id")]
        # Consumes the whole drain budget on the session's OWN mailbox probe,
        # so the cleared-record drain never even starts.
        eng.rd_delay_s = 30.0
        _wired(tmp_path, eng)
        record = _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "stranded by the clear" not in res.stdout
        assert record.exists()

    def test_an_ack_refusal_keeps_the_record(self, tmp_path, engine) -> None:
        eng = engine()
        eng.rows = [_row("s1-mail", body="ack will fail", to="old-sess-id")]
        eng.ack_ok = False
        _wired(tmp_path, eng)
        record = _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "ack will fail" not in res.stdout
        assert record.exists()

    def test_a_row_under_another_processs_lease_keeps_the_record(
        self, tmp_path, engine,
    ) -> None:
        """The claim loop ends empty (nothing eligible to claim), but the
        confirming probe afterward still finds the row -- claimed, with a
        live lease held by a process other than this one -- so it counts as
        LIVE and the record must survive. ``delivered == 0`` alone is not
        the delete signal."""
        eng = engine()
        row = _row("s1-mail", body="held by a peer", to="old-sess-id")
        row["claim_state"] = "claimed"
        row["claimant"] = "some-other-process"
        row["lease_until"] = time.time() + 300.0
        eng.rows = [row]
        _wired(tmp_path, eng)
        record = _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "held by a peer" not in res.stdout
        assert record.exists()
        assert "/v1/tuples/in" in eng.paths()

    def test_a_leased_row_past_the_first_page_still_keeps_the_record(
        self, tmp_path, engine,
    ) -> None:
        """RDR-208 Phase 2 Step 3 fix 1 (gate audit round 2). Twenty
        dead-lettered rows fill the confirming probe's first page; a
        twenty-first row -- claimed, under another process's live lease --
        sorts strictly after them and only shows up on a SECOND page. A
        one-page confirming check would miss it entirely and delete the
        record early.
        """
        eng = engine()
        dead_rows = [
            _row(f"d{i:02d}", body="dead filler", claim_state="dead", to="old-sess-id")
            for i in range(20)
        ]
        for row in dead_rows:
            row["created_at"] = "2026-09-12T00:00:00Z"
        leased = _row("leased-row", body="held by a peer, past the first page", to="old-sess-id")
        leased["created_at"] = "2026-09-12T00:00:01Z"  # one second later: page 2
        leased["claim_state"] = "claimed"
        leased["claimant"] = "some-other-process"
        leased["lease_until"] = time.time() + 300.0
        eng.rows = [*dead_rows, leased]
        _wired(tmp_path, eng)
        record = _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "held by a peer, past the first page" not in res.stdout
        assert record.exists(), (
            "a live row on the confirming probe's SECOND page was missed; "
            "the record was deleted while a live claim was still unconfirmed"
        )

    def test_twenty_dead_rows_with_nothing_live_past_them_deletes_the_record(
        self, tmp_path, engine,
    ) -> None:
        """The same shape as the test above, MINUS the leased row: a full
        first page of dead-lettered rows and a genuinely empty second page
        must still delete the record -- pagination must not make an
        actually-empty mailbox unconfirmable."""
        eng = engine()
        dead_rows = [
            _row(f"d{i:02d}", body="dead filler", claim_state="dead", to="old-sess-id")
            for i in range(20)
        ]
        for row in dead_rows:
            row["created_at"] = "2026-09-12T00:00:00Z"
        eng.rows = dead_rows
        _wired(tmp_path, eng)
        record = _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert not record.exists()

    def test_a_nonempty_pending_file_keeps_the_record(self, tmp_path, engine) -> None:
        """A pending entry whose id is STILL PRESENT in the mailbox (here, a
        dead-lettered row of the same id) is kept by ``_recover_pending``
        unconditionally -- it is not "absent, so recovered" -- so the pending
        file stays non-empty after the pass and the cleared record must
        survive on that basis alone, even though the claim loop itself ended
        empty and the only row present is dead-lettered (not live).
        """
        eng = engine()
        eng.rows = [_row("ghost-id", body="dead", claim_state="dead", to="old-sess-id")]
        _wired(tmp_path, eng)
        config_dir = tmp_path / "config"
        record = _write_cleared_record(config_dir, SESSION_ID, ["old-sess-id"])
        pending_dir = config_dir / "tuple-watch"
        pending_dir.mkdir(parents=True, exist_ok=True)
        (pending_dir / "old-sess-id.pending.json").write_text(
            json.dumps({"entries": [
                {"id": "ghost-id", "rendered": "- an ambiguous earlier ack"},
            ]}),
            encoding="utf-8",
        )

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert record.exists()
        assert (pending_dir / "old-sess-id.pending.json").exists(), (
            "the pending entry is present in the mailbox and must be kept, not recovered"
        )

    def test_a_malformed_id_is_skipped_while_the_others_drain(
        self, tmp_path, engine,
    ) -> None:
        eng = engine()
        eng.rows = [_row("s1-mail", body="the good one", to="old-sess-id")]
        _wired(tmp_path, eng)
        config_dir = tmp_path / "config"
        path = _cleared_record_path(config_dir, SESSION_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("../etc/passwd\nold-sess-id\n", encoding="utf-8")

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "the good one" in res.stdout
        assert "SKIP" in res.stderr
        assert "malformed" in res.stderr
        assert not path.exists()

    def test_two_clears_before_a_prompt_drain_both_old_mailboxes(
        self, tmp_path, engine,
    ) -> None:
        """A -> B -> C with no prompt in between: the writer carries both
        ids forward into one record; this hook drains both from it."""
        eng = engine()
        eng.rows = [
            _row("a-mail", body="from the first clear", to="session-a"),
            _row("b-mail", body="from the second clear", to="session-b"),
        ]
        _wired(tmp_path, eng)
        record = _write_cleared_record(
            tmp_path / "config", SESSION_ID, ["session-b", "session-a"],
        )

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "from the first clear" in res.stdout
        assert "from the second clear" in res.stdout
        assert not record.exists()

    def test_a_fork_leaves_the_parents_mailbox_untouched(
        self, tmp_path, engine,
    ) -> None:
        """No cleared record at all (a fork writes none, per RDR-208's Fork
        paragraph and Sam's decision 2): this hook never touches the
        parent's mailbox, and issues no `in` call for it."""
        eng = engine()
        eng.rows = [_row("parent-mail", body="still the parent's", to="parent-sess-id")]
        _wired(tmp_path, eng)

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "still the parent's" not in res.stdout
        in_bodies = [b for p, b in eng.calls if p == "/v1/tuples/in"]
        assert not any(
            (b.get("keys_pattern") or {}).get("to") == "parent-sess-id" for b in in_bodies
        )

    def test_a_record_older_than_seven_days_is_pruned(self, tmp_path, engine) -> None:
        eng = engine()
        eng.rows = [_row("s1-mail", body="expired mailbox", to="old-sess-id")]
        _wired(tmp_path, eng)
        record = _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])
        old_mtime = time.time() - (8 * 24 * 3600)
        os.utime(record, (old_mtime, old_mtime))

        res = _run(tmp_path=tmp_path)

        assert res.returncode == 0, res.stderr
        assert "expired mailbox" not in res.stdout
        assert not record.exists()

    def test_a_held_pending_lock_makes_the_pass_skip_and_keep_the_record(
        self, tmp_path, engine,
    ) -> None:
        """nexus-galkv.6 fix 3 (gate audit round 2). A lock this pass cannot
        acquire within its bound must FAIL CLOSED: no claim attempted at
        all against the mailbox, and the record kept -- never the old
        behaviour of running the pass unlocked past the timeout, which
        could duplicate a delivery.
        """
        import fcntl  # noqa: PLC0415 — deferred: POSIX-only, this test only

        eng = engine()
        eng.rows = [_row("s1-mail", body="stranded by the clear", to="old-sess-id")]
        _wired(tmp_path, eng)
        record = _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])
        lock_path = tmp_path / "config" / "tuple-watch" / "old-sess-id.pending.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            res = _run(tmp_path=tmp_path)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert res.returncode == 0, res.stderr
        assert "stranded by the clear" not in res.stdout
        assert record.exists()
        assert "SKIP" in res.stderr
        assert "old-sess-id" in res.stderr
        in_bodies = [b for p, b in eng.calls if p == "/v1/tuples/in"]
        assert not any(
            (b.get("keys_pattern") or {}).get("to") == "old-sess-id" for b in in_bodies
        ), "the pass claimed from the mailbox despite the unavailable lock"

    def test_two_processes_on_one_session_id_concurrently_deliver_exactly_once(
        self, tmp_path, engine,
    ) -> None:
        """Gate Significant (b). X cleared S1 to S2 (cleared.S2 names S1);
        mail was already sitting at S1. X's drain (through the record) and
        Y's ordinary drain (Y still resumes S1) run CONCURRENTLY as two real
        subprocesses against one engine. Every message is delivered exactly
        once across the two outputs.
        """
        eng = engine()
        eng.rows = [_row(f"m{i}", body=f"concurrent-{i}", to="old-sess-id") for i in range(5)]
        _wired(tmp_path, eng)
        _write_cleared_record(tmp_path / "config", SESSION_ID, ["old-sess-id"])

        results: dict[str, subprocess.CompletedProcess[str]] = {}
        barrier = threading.Barrier(2)

        def _go(key: str, payload_session_id: str) -> None:
            barrier.wait(timeout=10)
            results[key] = _run(tmp_path=tmp_path, stdin=_payload(session_id=payload_session_id))

        tx = threading.Thread(target=_go, args=("x", SESSION_ID))
        ty = threading.Thread(target=_go, args=("y", "old-sess-id"))
        tx.start()
        ty.start()
        tx.join(timeout=30)
        ty.join(timeout=30)

        assert not tx.is_alive() and not ty.is_alive(), "a drain subprocess hung"
        res_x, res_y = results["x"], results["y"]
        assert res_x.returncode == 0, res_x.stderr
        assert res_y.returncode == 0, res_y.stderr

        expected = {f"concurrent-{i}" for i in range(5)}
        delivered_x = {m for m in expected if m in res_x.stdout}
        delivered_y = {m for m in expected if m in res_y.stdout}
        assert delivered_x & delivered_y == set(), (
            f"a message was delivered to both processes: {delivered_x & delivered_y}\n"
            f"x stdout:\n{res_x.stdout}\ny stdout:\n{res_y.stdout}"
        )
        assert delivered_x | delivered_y == expected, (
            f"not every message was delivered: missing "
            f"{expected - (delivered_x | delivered_y)}\n"
            f"x stdout:\n{res_x.stdout}\ny stdout:\n{res_y.stdout}"
        )


# ── Size pre-check (bead nexus-r7xao) ────────────────────────────────────────


def _load_module():
    import importlib.util  # noqa: PLC0415 -- deliberately deferred

    spec = importlib.util.spec_from_file_location("mailbox_drain_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_drain_address_oversized_address_skips_before_any_post(monkeypatch) -> None:
    """An address long enough to push ``mailbox/<address>`` over the 256-byte
    subspace cap, or the ``to`` pattern value over the 256-byte field cap, is
    refused before any POST — the engine is never even asked."""
    module = _load_module()

    def _forbidden_post(*_a, **_k):  # pragma: no cover — only fires on failure
        raise AssertionError("_drain_address must not POST for an oversized address")

    monkeypatch.setattr(module, "_post", _forbidden_post)

    skips: list[str] = []
    monkeypatch.setattr(module, "_log_skip", skips.append)

    ending = module._drain_address(
        "http://engine.invalid", "token", "a" * 260,
        is_local=True, config_dir=Path("/nonexistent"),
        deadline=time.monotonic() + 5, out=module._Out(),
    )

    assert ending == "skipped"
    assert len(skips) == 1
    assert "oversized" in skips[0]
    assert "260 bytes" in skips[0] or "subspace" in skips[0]


def test_drain_address_address_at_the_cap_reaches_the_probe(monkeypatch, tmp_path) -> None:
    """address feeds TWO checks now (nexus-galkv.6 fix 2, gate audit round
    2): the subspace path segment (``mailbox/`` + address, 256-byte cap)
    and the ``to`` pattern value (256-byte field cap). The claimant no
    longer varies with *address* at all -- it is fixed-length -- so it
    never binds here any more; the subspace check is the tighter of the
    two remaining ones (8-byte ``mailbox/`` prefix leaves 248 for the
    address). An address of exactly 248 chars makes the subspace exactly
    256 bytes. Must reach ``_probe_page``, proving the boundary is
    inclusive. Runs the REAL ``_drain_claimant`` -- no monkeypatch needed
    now that its length is independent of the address.

    ``_pending_lock`` is stubbed here: a 248-char address makes
    ``<address>.pending.lock`` a filename over POSIX ``NAME_MAX`` (255),
    a SEPARATE, filesystem-level constraint this SIZE-CHECK boundary test
    is not about (see ``test_pending_lock_with_*`` and
    ``test_a_held_pending_lock_makes_the_pass_skip_and_keep_the_record``
    for that mechanism's own coverage).
    """
    module = _load_module()
    calls: list[str] = []

    def _fake_probe_page(base_url, token, address, *, is_local, deadline, since):
        calls.append(address)
        return []

    @contextlib.contextmanager
    def _fake_pending_lock(config_dir, address, *, deadline):
        yield True

    monkeypatch.setattr(module, "_probe_page", _fake_probe_page)
    monkeypatch.setattr(module, "_read_pending", lambda config_dir, address: [])
    monkeypatch.setattr(module, "_pending_lock", _fake_pending_lock)

    address = "a" * 248
    assert len(f"mailbox/{address}") == 256
    ending = module._drain_address(
        "http://engine.invalid", "token", address,
        is_local=True, config_dir=tmp_path,
        deadline=time.monotonic() + 5, out=module._Out(),
    )
    assert calls == [address]
    assert ending == "empty"


def test_drain_claimant_is_unique_per_invocation() -> None:
    """nexus-galkv.6 fix 2 (gate audit round 2). Two calls for the SAME
    address must never produce the same claimant, or two concurrent drain
    processes draining the same mailbox could collide on the engine's
    same-claimant retake fast path (TupleRepository.claimOnce,
    service/src/main/java/dev/nexus/service/db/TupleRepository.java:
    694-706) and both walk away believing they hold the identical claim.
    """
    module = _load_module()

    first = module._drain_claimant("some-address")
    second = module._drain_claimant("some-address")

    assert first != second
    assert first.startswith("mailbox-drain-")
    assert second.startswith("mailbox-drain-")


def test_drain_claimant_length_is_independent_of_address_and_pid(monkeypatch) -> None:
    """nexus-galkv.6 fix 2, round 2. A first fix appended ``-{pid}-{suffix}``
    directly to the address, so the claimant's length grew with the address
    AND varied with however many digits the pid happened to have --
    regressing the effective address cap below its old 114-byte headroom
    and making it pid-dependent (gate audit round 2, finding 1). The
    claimant's length must be CONSTANT regardless of either.
    """
    module = _load_module()

    monkeypatch.setattr(module.os, "getpid", lambda: 7)
    short_pid_short_addr = module._drain_claimant("a")
    short_pid_long_addr = module._drain_claimant("a" * 248)

    monkeypatch.setattr(module.os, "getpid", lambda: 2147483647)  # max signed-32-bit pid
    long_pid_short_addr = module._drain_claimant("a")

    lengths = {
        len(short_pid_short_addr.encode("utf-8")),
        len(short_pid_long_addr.encode("utf-8")),
        len(long_pid_short_addr.encode("utf-8")),
    }
    assert len(lengths) == 1, f"claimant length varied: {lengths}"
    for claimant in (short_pid_short_addr, short_pid_long_addr, long_pid_short_addr):
        assert len(claimant.encode("utf-8")) <= module._sz.MAX_CLAIMANT_BYTES


def test_drain_claimant_pid_of_maximum_width_still_fits(monkeypatch) -> None:
    """nexus-galkv.6 fix 2, round 2. A pid wider than the fixed padding
    width (an exotic, wider-than-32-bit pid representation) must not widen
    the claimant past ``MAX_CLAIMANT_BYTES`` -- it is truncated to the
    fixed pid width instead of allowed to grow the string.
    """
    module = _load_module()
    monkeypatch.setattr(module.os, "getpid", lambda: 999999999999999999999)  # 21 digits

    claimant = module._drain_claimant("a" * 248)

    assert len(claimant.encode("utf-8")) <= module._sz.MAX_CLAIMANT_BYTES


def test_pending_lock_with_almost_no_budget_skips_at_once(tmp_path) -> None:
    """nexus-galkv.6 fix 3, round 2. A contended acquire must not spend up
    to ``_PENDING_LOCK_TIMEOUT_S`` when the CALLER's own deadline leaves
    almost nothing -- it must give up at once, not after the lock's own 2s
    ceiling regardless of how little budget the caller actually has left.
    """
    import fcntl  # noqa: PLC0415 — deferred: POSIX-only, this test only

    module = _load_module()
    address = "addr-almost-no-budget"
    lock_path = tmp_path / "tuple-watch" / f"{address}.pending.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        with module._pending_lock(tmp_path, address, deadline=start + 0.05) as acquired:
            elapsed = time.monotonic() - start
            assert acquired is False
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert elapsed < 0.5, f"lock wait took {elapsed:.2f}s despite an almost-exhausted deadline"


def test_pending_lock_with_ample_budget_waits_up_to_its_own_ceiling(tmp_path) -> None:
    """The other direction, so the clamp is pinned both ways: with plenty of
    the caller's own budget left, the lock still waits up to its own
    ``_PENDING_LOCK_TIMEOUT_S`` ceiling, not forever and not zero.
    """
    import fcntl  # noqa: PLC0415 — deferred: POSIX-only, this test only

    module = _load_module()
    address = "addr-ample-budget"
    lock_path = tmp_path / "tuple-watch" / f"{address}.pending.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        with module._pending_lock(tmp_path, address, deadline=start + 60.0) as acquired:
            elapsed = time.monotonic() - start
            assert acquired is False
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert elapsed >= module._PENDING_LOCK_TIMEOUT_S * 0.9
    assert elapsed < module._PENDING_LOCK_TIMEOUT_S * 2


# ── Per-turn re-arm (bead nexus-6konb.19) ────────────────────────────────────
#
# The SessionStart arm instruction can fail to reach the session (measured
# 2026-09-14: nx hook session-start ran at a resume, its output never reached
# the transcript), and nothing re-armed. This hook runs on every prompt: from
# the second prompt it sees for a session, it re-issues the wheel's arm text
# when no live watcher holds the session's own mailbox lock.

_FAKE_ARM = "MAILBOX WATCH: FAKE-ARM-BLOCK"


def _fake_nx(
    tmp_path: Path, *, text: str = _FAKE_ARM, rc: int = 0, delay_s: int = 0,
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "nx-calls.log"
    script = bin_dir / "nx"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {shlex.quote(str(log))}\n'
        + (f"sleep {delay_s}\n" if delay_s else "")
        + f"printf '%s\\n' {shlex.quote(text)}\n"
        f"exit {rc}\n"
    )
    script.chmod(0o755)
    return bin_dir, log


def _with_nx(bin_dir: Path) -> dict[str, str]:
    return {"PATH": f"{bin_dir}:/usr/bin:/bin"}


def _nx_calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _watch_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config" / "tuple-watch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_lock(tmp_path: Path, pid: int) -> None:
    (_watch_dir(tmp_path) / f"{SESSION_ID}.lock").write_text(
        f"pid={pid} session={SESSION_ID} address={SESSION_ID}"
        " started_at=2026-09-14T00:00:00Z"
    )


def _state_path(tmp_path: Path) -> Path:
    return _watch_dir(tmp_path) / f"rearm.{SESSION_ID}"


def _seen(tmp_path: Path, *, last_rearm: float = 0.0, last_attempt: float = 0.0) -> None:
    """The hook has already seen a prompt for this session."""
    _state_path(tmp_path).write_text(
        json.dumps({"last_rearm": last_rearm, "last_attempt": last_attempt})
    )


def _state(tmp_path: Path) -> dict:
    return json.loads(_state_path(tmp_path).read_text())


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def fake_watcher():
    """A live process whose command line carries the watcher's mark."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", "tuple", "watch"],
    )
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


class TestPerTurnRearm:
    def test_the_first_prompt_of_a_session_stays_silent(self, tmp_path, engine) -> None:
        _wired(tmp_path, engine())
        bin_dir, log = _fake_nx(tmp_path)
        res = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == ""
        assert _nx_calls(log) == []
        assert _state_path(tmp_path).is_file()

    def test_the_second_prompt_with_no_watcher_reissues_the_wheel_instruction(
        self, tmp_path, engine,
    ) -> None:
        _wired(tmp_path, engine())
        bin_dir, log = _fake_nx(tmp_path)
        _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        res = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert res.returncode == 0, res.stderr
        assert _FAKE_ARM in res.stdout
        assert "No mailbox watch is running for this session" in res.stdout
        assert _nx_calls(log) == [f"hook mailbox-arm --session-id {SESSION_ID}"]

    def test_nothing_sessionstart_writes_can_suppress_it(self, tmp_path, engine) -> None:
        """SessionStart output is the thing that can be lost, so no file it
        writes may stand in for delivery: a fresh tuple-watch session marker
        must not silence the reminder."""
        _wired(tmp_path, engine())
        bin_dir, _log = _fake_nx(tmp_path)
        _seen(tmp_path)
        (_watch_dir(tmp_path) / f"session.{os.getpid()}").write_text(SESSION_ID)
        res = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert _FAKE_ARM in res.stdout

    def test_a_delivered_instruction_is_not_repeated_inside_the_interval(
        self, tmp_path, engine,
    ) -> None:
        _wired(tmp_path, engine())
        bin_dir, log = _fake_nx(tmp_path)
        _seen(tmp_path)
        _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        second = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert _FAKE_ARM not in second.stdout
        assert len(_nx_calls(log)) == 1

    def test_a_live_watcher_suppresses_it(self, tmp_path, engine, fake_watcher) -> None:
        _wired(tmp_path, engine())
        bin_dir, log = _fake_nx(tmp_path)
        _seen(tmp_path)
        _write_lock(tmp_path, fake_watcher)
        res = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert res.stdout.strip() == ""
        assert _nx_calls(log) == []

    def test_a_reused_pid_that_is_not_a_watcher_counts_as_no_watcher(
        self, tmp_path, engine,
    ) -> None:
        _wired(tmp_path, engine())
        bin_dir, log = _fake_nx(tmp_path)
        _seen(tmp_path)
        _write_lock(tmp_path, os.getpid())
        res = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert _FAKE_ARM in res.stdout
        assert len(_nx_calls(log)) == 1

    def test_a_lock_left_by_a_dead_watcher_counts_as_no_watcher(
        self, tmp_path, engine,
    ) -> None:
        _wired(tmp_path, engine())
        bin_dir, log = _fake_nx(tmp_path)
        _seen(tmp_path)
        _write_lock(tmp_path, _dead_pid())
        res = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert _FAKE_ARM in res.stdout
        assert len(_nx_calls(log)) == 1

    def test_a_failed_attempt_backs_off_briefly_not_for_the_interval(
        self, tmp_path, engine,
    ) -> None:
        _wired(tmp_path, engine())
        bin_dir, log = _fake_nx(tmp_path, rc=1)
        _seen(tmp_path)
        first = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert first.stdout.strip() == ""
        assert len(_nx_calls(log)) == 1
        _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert len(_nx_calls(log)) == 1, "a retry inside the backoff"
        assert _state(tmp_path)["last_rearm"] == 0.0, "a failure started the interval"
        _seen(tmp_path, last_attempt=time.time() - 61.0)
        _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert len(_nx_calls(log)) == 2, "no retry once the backoff passed"

    def test_a_hung_nx_is_cut_off_and_injects_nothing(self, tmp_path, engine) -> None:
        """Without the spawn cap the hook would wait out the fake's sleep and
        then print its text, so the empty stdout is the proof of the cap."""
        _wired(tmp_path, engine())
        bin_dir, log = _fake_nx(tmp_path, delay_s=30)
        _seen(tmp_path)
        res = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == ""
        assert "TimeoutExpired" in res.stderr
        assert len(_nx_calls(log)) == 1

    def test_no_nx_on_path_injects_nothing(self, tmp_path, engine) -> None:
        _wired(tmp_path, engine())
        _seen(tmp_path)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == ""

    def test_an_unreachable_engine_never_spawns_nx(self, tmp_path) -> None:
        bin_dir, log = _fake_nx(tmp_path)
        _seen(tmp_path)
        res = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert res.returncode == 0, res.stderr
        assert _nx_calls(log) == []

    def test_mail_is_still_delivered_alongside_the_rearm(self, tmp_path, engine) -> None:
        eng = engine()
        eng.rows = [_row("rr01", sender="nexus-47", body="rearm with mail")]
        _wired(tmp_path, eng)
        bin_dir, _log = _fake_nx(tmp_path)
        _seen(tmp_path)
        res = _run(tmp_path=tmp_path, env_overrides=_with_nx(bin_dir))
        assert "rearm with mail" in res.stdout
        assert _FAKE_ARM in res.stdout


def test_rearm_naming_matches_the_wheel() -> None:
    """This script cannot import nexus, so it spells the watcher's lock name
    and command mark itself. They must agree with the wheel."""
    from nexus import tuple_watch

    module = _load_module()
    cfg = Path("/cfg")
    for sid in (SESSION_ID, "odd id/with:chars"):
        assert module._watch_lock_path(cfg, sid) == tuple_watch.lock_path(cfg, sid)
    assert module._WATCH_COMMAND_MARK == tuple_watch.WATCH_COMMAND_MARK


def test_cleared_record_naming_matches_the_wheel() -> None:
    """RDR-208 Phase 2 Step 3: this script cannot import nexus, so it spells
    the cleared-record filename itself
    (``nexus.tuple_watch.record_clear_and_write_session_marker`` writes it,
    naming a previous session's mailbox). It must agree with the wheel."""
    from nexus import tuple_watch

    module = _load_module()
    cfg = Path("/cfg")
    for sid in (SESSION_ID, "odd-id-with-dashes.and.dots"):
        assert module._cleared_record_path(cfg, sid) == tuple_watch.cleared_record_path(cfg, sid)


def test_the_lock_body_the_watcher_writes_parses_to_its_pid(tmp_path) -> None:
    from nexus.tuple_watch import acquire_watch_locks, lock_path

    module = _load_module()
    locks = acquire_watch_locks([SESSION_ID], state_dir=tmp_path, emit=lambda _s: None)
    try:
        assert locks.ok
        assert module._lock_pid(lock_path(tmp_path, SESSION_ID).read_text()) == os.getpid()
    finally:
        locks.release()
