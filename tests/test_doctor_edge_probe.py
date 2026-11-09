# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-t0ms7: ``nx doctor --probe-edge`` proves ``Idempotency-Key`` reaches the engine.

The client talks to a REAL local HTTP server (not a patched ``_post``) that plays the engine behind an
edge: ``echo`` keeps the key, ``drop`` is an edge whose origin request policy strips it from the request
(the RDR-007 A5 shape), ``strip_response`` strips it only from the response, and the other modes break one
later step. Each failure must name its hop (asserted by equality, not substring). Only the collection
listing is stubbed; the keyed POST and the ledger GET go over the wire.

The fake engine listens on loopback, which the probe refuses as "not a managed edge" unless the host is
pinned, so the tests pin it with ``expect_host`` (which is also how a scheduled step pins the edge).
"""
from __future__ import annotations

import http.server
import json
import threading
import uuid

import pytest
from click.testing import CliRunner

from nexus import doctor_edge_probe as probe
from nexus.cli import main
from nexus.commands import doctor
from nexus.db import http_vector_client as hvc
from nexus.db import service_endpoint

_EXPIRE = "/v1/vectors/gc/expire-quarantine"
_HOST = "127.0.0.1"
_COLLECTIONS = [
    {"name": "code__1-27__bge-base__v1", "lifecycle_state": "live"},
    {"name": "quarantine-code__1-27__bge-base__v1", "lifecycle_state": "live"},
]


class _Engine:
    """A fake engine behind a fake edge. ``mode`` picks the defect; the lists record what arrived."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.keys_received: list[str | None] = []
        self.bodies: list[dict] = []
        self.ledger: dict[str, dict] = {}
        self.posts = 0
        self.gets = 0
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code: int, body: dict, headers: dict[str, str] | None = None) -> None:
                payload = json.dumps(body).encode()
                # send_response_only: send_response would add its own Server header ahead of the edge's.
                self.send_response_only(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:  # noqa: N802 — stdlib callback name
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.posts += 1
                outer.bodies.append(json.loads(raw or b"{}"))
                # `drop` models the edge stripping the header before the origin: the engine sees no key.
                key = None if outer.mode == "drop" else self.headers.get("Idempotency-Key")
                outer.keys_received.append(key)
                if self.path != _EXPIRE:
                    return self._send(404, {"error": "no route"})
                if outer.mode == "unsupported":
                    return self._send(400, {"error": "idempotency_key_unsupported"})
                if outer.mode == "edge_waf_403":
                    # An AWS WAF/ALB refusal: the edge answers, the application never sees the request.
                    return self._send(403, {"error": "forbidden"}, {"Server": "awselb/2.0"})
                if outer.mode == "auth_401":
                    return self._send(401, {"error": "unauthorized"})
                if outer.mode == "gate_busy":
                    return self._send(503, {"error": "sweep_gate_busy"}, {"Retry-After": "1"})
                expired = 3 if outer.mode == "expired_rows" else 0
                # The engine reads the key as a canonical UUID; echoing the parsed form, not the raw header,
                # keeps request text out of a response header (CodeQL py/http-response-splitting).
                echo = {"Idempotency-Key": str(uuid.UUID(key))} if key and outer.mode != "strip_response" else {}
                if key and outer.mode != "ledger_empty":
                    outer.ledger[key] = {"state": "committed", "response_status": 200, "response": {"expired": expired}}
                self._send(200, {"expired": expired, "refused": None, "more": False,
                                 "row_limit": 1, "floor_refused": False}, echo)

            def do_GET(self) -> None:  # noqa: N802 — stdlib callback name
                outer.gets += 1
                if outer.mode == "no_status_route":
                    return self._send(404, {"error": "no route"})
                if self.path.startswith("/v1/requests/"):
                    key = self.path.rsplit("/", 1)[1]
                    return self._send(200, outer.ledger.get(key, {"state": "unknown"}))
                self._send(404, {"error": "no route"})

            def log_message(self, *_a: object) -> None:
                pass

        self.httpd = http.server.HTTPServer((_HOST, 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def engine_for(monkeypatch):
    started: list[_Engine] = []

    def _start(mode: str) -> _Engine:
        eng = _Engine(mode)
        started.append(eng)
        monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (eng.url, "tok"))
        monkeypatch.setattr(probe, "_list_collections", lambda: list(_COLLECTIONS))
        # The production-write guard presumes a dev-checkout write targets the operator's store; this
        # server is on loopback and is the whole point of the test.
        monkeypatch.setenv("NX_ALLOW_PROD_WRITE", "test: loopback fake engine for nexus-t0ms7")
        return eng

    yield _start
    for eng in started:
        eng.close()


def _run() -> probe.EdgeProbeResult:
    return probe.run_edge_probe(expect_host=_HOST)


def test_key_echoed_and_committed_passes(engine_for) -> None:
    eng = engine_for("echo")
    result = _run()
    assert result.ok, result.detail
    assert result.hop is None and result.exit_code == 0
    # The server really received the key the probe reports, on the wire.
    assert eng.keys_received == [result.key]
    assert eng.ledger[result.key]["state"] == "committed"
    assert eng.gets == 1
    assert "echoed and committed" in probe.render(result)


def test_the_page_is_a_noop_by_construction(engine_for) -> None:
    """The body on the wire, not the constants: far-past cutoff, force off, one row, a dedicated sibling
    that no real drain names, and a registered origin that is not a quarantine collection."""
    eng = engine_for("echo")
    assert _run().ok
    (body,) = eng.bodies
    assert body["cutoff"] == probe.PROBE_CUTOFF == "2000-01-01T00:00:00Z"
    assert body["force"] is False
    assert body["row_limit"] == 1
    assert body["quarantine_collection"] == probe.PROBE_QUARANTINE_COLLECTION == "quarantine-nx-doctor-edge-probe"
    assert body["quarantine_collection"] not in {c["name"] for c in _COLLECTIONS}
    assert body["origin_collection"] == "code__1-27__bge-base__v1"


def test_request_path_drop_fails_naming_the_request_path(engine_for) -> None:
    eng = engine_for("drop")
    result = _run()
    assert not result.ok and result.exit_code == 1
    assert result.hop == probe.HOP_KEY_DROPPED
    assert eng.keys_received == [None]  # the engine really saw no key
    out = probe.render(result)
    assert "FAIL" in out and "request path" in out
    assert "RDR-007 A5" in out  # the pointer shows on a key-loss hop


def test_response_header_stripped_is_a_distinct_hop(engine_for) -> None:
    """Committed with no echo: the key reached the engine, the response header was lost on the way back."""
    eng = engine_for("strip_response")
    result = _run()
    assert not result.ok and result.exit_code == 1
    assert result.hop == probe.HOP_RESPONSE_HEADER
    assert result.hop != probe.HOP_KEY_DROPPED
    assert eng.keys_received == [result.key]
    assert eng.gets == 1  # the ledger is read whatever the header said
    out = probe.render(result)
    assert "response path" in out and "reached the engine" in result.detail
    assert "conexus-c4i2" in out  # a key-loss hop carries the origin-policy pointer


def test_status_route_not_served_fails(engine_for) -> None:
    engine_for("no_status_route")
    result = _run()
    assert not result.ok
    assert result.hop == probe.HOP_STATUS_ROUTE
    assert "RDR-007 A5" not in probe.render(result)


def test_ledger_with_no_committed_row_fails_when_the_echo_was_fine(engine_for) -> None:
    engine_for("ledger_empty")
    # ledger_empty still echoes, so only the ledger is wrong
    result = _run()
    assert not result.ok
    assert result.hop == probe.HOP_LEDGER
    assert "'unknown'" in result.detail
    assert "RDR-007 A5" not in probe.render(result)


def test_engine_without_keyed_form_fails_as_engine(engine_for) -> None:
    engine_for("unsupported")
    result = _run()
    assert not result.ok
    assert result.hop == probe.HOP_ENGINE
    assert "not a key lost in transit" in result.detail


def test_gateway_error_names_the_edge(engine_for) -> None:
    engine_for("edge_waf_403")
    result = _run()
    assert not result.ok and result.exit_code == 1
    assert result.hop == probe.HOP_EDGE
    assert not result.inconclusive  # an edge refusal is a finding, not a credentials problem
    assert "RDR-007 A5" not in probe.render(result)


def test_rows_expired_by_the_noop_page_fails_loudly(engine_for) -> None:
    engine_for("expired_rows")
    result = _run()
    assert not result.ok
    assert "did not answer expired=0" in result.detail


def test_credentials_that_cannot_write_are_inconclusive_exit_two(engine_for) -> None:
    engine_for("auth_401")
    result = _run()
    assert not result.ok and result.inconclusive and result.exit_code == 2
    assert result.hop == probe.HOP_CREDENTIALS
    out = probe.render(result)
    assert "NOT PROBED" in out and "credentials cannot send this write" in out
    assert "RDR-007 A5" not in out


def test_busy_sweep_gate_is_inconclusive_exit_two(engine_for) -> None:
    engine_for("gate_busy")
    result = _run()
    assert not result.ok and result.inconclusive and result.exit_code == 2
    assert result.hop == probe.HOP_GATE_BUSY
    assert "retry" in result.detail


def test_unreachable_endpoint_names_the_network(monkeypatch) -> None:
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://127.0.0.1:1", "tok"))
    monkeypatch.setattr(probe, "_list_collections", lambda: list(_COLLECTIONS))
    monkeypatch.setenv("NX_ALLOW_PROD_WRITE", "test: refused-connection probe for nexus-t0ms7")
    result = _run()
    assert not result.ok
    assert result.hop == probe.HOP_NETWORK


def test_no_collection_is_not_a_pass(engine_for, monkeypatch) -> None:
    eng = engine_for("echo")
    monkeypatch.setattr(probe, "_list_collections", lambda: [])
    result = _run()
    assert not result.ok and result.exit_code == 1
    assert result.hop == probe.HOP_SETUP
    assert "NOT PROBED" in result.detail
    assert eng.posts == 0


def test_unlistable_tenant_is_not_probed_never_a_traceback(engine_for, monkeypatch) -> None:
    def boom() -> list:
        raise RuntimeError("no service")

    engine_for("echo")
    monkeypatch.setattr(probe, "_list_collections", boom)
    result = _run()
    assert not result.ok and result.hop == probe.HOP_SETUP


def test_sends_nothing_when_the_production_write_guard_refuses(engine_for, monkeypatch) -> None:
    # The suite's autouse fixture opts every test in, so the guard is made to refuse as it does for a
    # dev-checkout process aimed at a real store: the probe reports it and sends no request.
    eng = engine_for("echo")

    def refuse(base_url: str) -> None:
        raise service_endpoint.ProductionWriteGuardError("STOP: refusing a WRITE")

    monkeypatch.setattr(service_endpoint, "guard_production_write", refuse)
    result = _run()
    assert not result.ok
    assert "production-write guard" in result.detail
    assert eng.posts == 0


class TestEndpointHost:
    def test_loopback_endpoint_is_not_a_managed_edge_and_sends_nothing(self, engine_for) -> None:
        eng = engine_for("echo")
        result = probe.run_edge_probe()  # no --expect-host
        assert not result.ok and not result.inconclusive and result.exit_code == 1
        assert result.hop == probe.HOP_SETUP
        assert "not a managed edge" in result.detail
        assert result.host == _HOST
        assert eng.posts == 0 and eng.gets == 0

    def test_host_mismatch_is_not_probed_and_sends_nothing(self, engine_for) -> None:
        eng = engine_for("echo")
        result = probe.run_edge_probe(expect_host="api.conexus-nexus.com")
        assert not result.ok and result.exit_code == 1
        assert "not the expected api.conexus-nexus.com" in result.detail
        assert eng.posts == 0

    def test_expect_host_is_case_insensitive(self, engine_for) -> None:
        engine_for("echo")
        assert probe.run_edge_probe(expect_host=_HOST.upper()).ok

    def test_host_is_printed_on_pass_and_on_fail(self, engine_for) -> None:
        engine_for("echo")
        assert f"(host {_HOST})" in probe.render(_run())
        engine_for("drop")
        assert f"(host {_HOST})" in probe.render(_run())

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.9.9.9", "::1", "0.0.0.0", "x.localhost"])
    def test_local_hosts_are_recognised(self, host: str) -> None:
        assert probe._is_local_host(host)

    @pytest.mark.parametrize("host", ["api.conexus-nexus.com", "10.0.0.5", "203.0.113.7"])
    def test_managed_hosts_are_not_local(self, host: str) -> None:
        assert not probe._is_local_host(host)


class TestPickProbeOrigin:
    def test_first_live_non_quarantine_collection_by_name(self) -> None:
        rows = [
            {"name": "quarantine-aaa__1-1__m__v1"},
            {"name": "docs__1-1__m__v1"},
            {"name": "code__1-1__m__v1"},
        ]
        assert probe.pick_probe_origin(rows) == "code__1-1__m__v1"

    def test_only_quarantine_or_retired_collections_gives_none(self) -> None:
        rows = [{"name": "quarantine-x__1-1__m__v1"}, {"name": "code__1-1__m__v1", "lifecycle_state": "retired"}]
        assert probe.pick_probe_origin(rows) is None


class TestCli:
    def test_pass_exits_zero(self, monkeypatch) -> None:
        monkeypatch.setattr(
            probe, "run_edge_probe", lambda **_: probe.EdgeProbeResult(True, "key k echoed and committed", host="h")
        )
        result = CliRunner().invoke(main, ["doctor", "--probe-edge"])
        assert result.exit_code == 0
        assert "Edge idempotency probe (host h)" in result.output

    def test_failure_exits_one_and_names_the_hop(self, monkeypatch) -> None:
        monkeypatch.setattr(
            probe, "run_edge_probe",
            lambda **_: probe.EdgeProbeResult(False, "the 200 did not echo", probe.HOP_KEY_DROPPED, key="k", host="h"),
        )
        result = CliRunner().invoke(main, ["doctor", "--probe-edge"])
        assert result.exit_code == 1
        assert "hop: request path" in result.output

    def test_inconclusive_exits_two(self, monkeypatch) -> None:
        monkeypatch.setattr(
            probe, "run_edge_probe",
            lambda **_: probe.EdgeProbeResult(
                False, "NOT PROBED: gate busy", probe.HOP_GATE_BUSY, host="h", inconclusive=True
            ),
        )
        result = CliRunner().invoke(main, ["doctor", "--probe-edge"])
        assert result.exit_code == 2
        assert "NOT PROBED" in result.output

    def test_expect_host_reaches_the_probe(self, monkeypatch) -> None:
        seen: dict = {}

        def fake(**kw):
            seen.update(kw)
            return probe.EdgeProbeResult(True, "ok", host="h")

        monkeypatch.setattr(probe, "run_edge_probe", fake)
        result = CliRunner().invoke(main, ["doctor", "--probe-edge", "--expect-host", "api.example.com"])
        assert result.exit_code == 0
        assert seen == {"expect_host": "api.example.com"}

    def test_expect_host_without_the_probe_is_refused(self) -> None:
        result = CliRunner().invoke(main, ["doctor", "--expect-host", "x"])
        assert result.exit_code != 0
        assert "only applies to --probe-edge" in result.output

    def test_one_mode_per_invocation(self) -> None:
        result = CliRunner().invoke(main, ["doctor", "--probe-edge", "--check-t1"])
        assert result.exit_code != 0
        assert "one mode per invocation" in result.output

    def test_json_is_refused(self) -> None:
        result = CliRunner().invoke(main, ["doctor", "--probe-edge", "--json"])
        assert result.exit_code != 0
        assert "--probe-edge" in result.output

    def test_not_named_in_the_opt_in_check_lists(self) -> None:
        assert "--probe-edge" not in doctor._OPT_IN_ONLY_CHECKS
        assert not any("edge" in name for name in doctor._SUPPLEMENTARY_CHECK_NAMES)
