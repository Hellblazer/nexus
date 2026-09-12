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

import json
import os
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
         kind: str = "note", claim_state=None) -> dict:
    return {
        "id": tuple_id,
        "subspace": f"mailbox/{SESSION_ID}",
        "template": "mailbox",
        "keys": {"to": SESSION_ID},
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
                    pattern = (body.get("keys_pattern") or {}).get("to")
                    rows = [
                        r for r in engine.rows
                        if pattern is None or r["keys"].get("to") == pattern
                    ]
                    self._json(200, {"tuples": rows})
                elif self.path == "/v1/tuples/in":
                    if not engine.claimable:
                        self._json(200, {})
                        return
                    pattern = (body.get("keys_pattern") or {}).get("to")
                    live = [
                        r for r in engine.rows
                        if r["claim_state"] != "dead"
                        and (pattern is None or r["keys"].get("to") == pattern)
                    ]
                    if not live:
                        self._json(200, {})
                        return
                    row = live[0]
                    self._json(200, {"tuple": row, "claim_id": "claim-" + row["id"]})
                elif self.path == "/v1/tuples/ack":
                    if not engine.ack_ok:
                        self._json(404, {"error": "ClaimNotFound"})
                        return
                    cid = body.get("claim_id", "")
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
    floor, which the bead says out loud."""

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
        reg = tmp_path / "config" / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("nexus-19\n", encoding="utf-8")
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
        reg = tmp_path / "config" / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(f"\n  nexus-19  \n\n# a comment\nnexus-19\n{SESSION_ID}\n",
                       encoding="utf-8")
        _wired(tmp_path, eng)
        res = _run(tmp_path=tmp_path)
        assert res.returncode == 0, res.stderr
        subspaces = [b.get("subspace") for p, b in eng.calls if p == "/v1/tuples/rd"]
        assert subspaces.count("mailbox/nexus-19") == 1
        assert subspaces.count(f"mailbox/{SESSION_ID}") == 1
        assert not any(s and "#" in s for s in subspaces)


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
        assert "SKIP" in res.stderr
        assert "deadline" in res.stderr, res.stderr
        assert "the prompt is not waiting for it" in res.stderr

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
        reg = tmp_path / "config" / "tuple-watch" / "addresses"
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
        reg = tmp_path / "config" / "tuple-watch" / "addresses"
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
