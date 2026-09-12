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
        timeout=20,
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
                if self.path == "/v1/tuples/rd":
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
                    self._json(200, {})
                else:
                    self._json(404, {"error": "not found"})

            def _json(self, status: int, payload: dict) -> None:
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
        started = time.monotonic()
        res = _run(tmp_path=tmp_path)
        elapsed = time.monotonic() - started
        assert res.returncode == 0
        assert elapsed < 18, f"the hook blocked the prompt for {elapsed:.1f}s"
        assert res.stdout.strip() == ""
        assert "SKIP" in res.stderr

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
