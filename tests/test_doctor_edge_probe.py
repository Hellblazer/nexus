# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-t0ms7: ``nx doctor --probe-edge`` proves ``Idempotency-Key`` reaches the engine.

The client talks to a REAL local HTTP server (not a patched ``_post``) that plays the engine behind an
edge: ``echo`` keeps the key, ``drop`` is an edge whose origin request policy strips it (the
RDR-007 A5 shape), and the other modes break one later step. Each failure must name its hop. Only the
collection listing is stubbed; the keyed POST and the ledger GET go over the wire.
"""
from __future__ import annotations

import http.server
import json
import threading

import pytest
from click.testing import CliRunner

from nexus import doctor_edge_probe as probe
from nexus.cli import main
from nexus.commands import doctor
from nexus.db import http_vector_client as hvc
from nexus.db import service_endpoint

_EXPIRE = "/v1/vectors/gc/expire-quarantine"
_COLLECTIONS = [
    {"name": "code__1-27__bge-base__v1", "lifecycle_state": "live"},
    {"name": "quarantine-code__1-27__bge-base__v1", "lifecycle_state": "live"},
]


class _Engine:
    """A fake engine behind a fake edge. ``mode`` picks the defect; ``seen`` records what arrived."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.keys_received: list[str | None] = []
        self.ledger: dict[str, dict] = {}
        self.posts = 0
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code: int, body: dict, headers: dict[str, str] | None = None) -> None:
                payload = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:  # noqa: N802 — stdlib callback name
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.posts += 1
                # `drop` models the edge stripping the header before the origin: the engine sees no key.
                key = None if outer.mode == "drop" else self.headers.get("Idempotency-Key")
                outer.keys_received.append(key)
                if self.path != _EXPIRE:
                    return self._send(404, {"error": "no route"})
                if outer.mode == "unsupported":
                    return self._send(400, {"error": "idempotency_key_unsupported"})
                if outer.mode == "edge_503":
                    return self._send(502, {"error": "bad gateway"}, {"Server": "awselb/2.0"})
                expired = 3 if outer.mode == "expired_rows" else 0
                echo = {"Idempotency-Key": key} if key and outer.mode != "no_echo" else {}
                if key and outer.mode != "ledger_empty":
                    outer.ledger[key] = {"state": "committed", "response_status": 200, "response": {"expired": expired}}
                self._send(200, {"expired": expired, "refused": None, "more": False,
                                 "row_limit": 300, "floor_refused": False}, echo)

            def do_GET(self) -> None:  # noqa: N802 — stdlib callback name
                if outer.mode == "no_status_route":
                    return self._send(404, {"error": "no route"})
                if self.path.startswith("/v1/requests/"):
                    key = self.path.rsplit("/", 1)[1]
                    return self._send(200, outer.ledger.get(key, {"state": "unknown"}))
                self._send(404, {"error": "no route"})

            def log_message(self, *_a: object) -> None:
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
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


def test_key_echoed_and_committed_passes(engine_for) -> None:
    eng = engine_for("echo")
    result = probe.run_edge_probe()
    assert result.ok, result.detail
    assert result.hop is None
    # The server really received the key the probe reports, on the wire.
    assert eng.keys_received == [result.key]
    assert eng.ledger[result.key]["state"] == "committed"
    assert "echoed and committed" in probe.render(result)


def test_hop_that_drops_the_key_fails_naming_the_hop(engine_for) -> None:
    eng = engine_for("drop")
    result = probe.run_edge_probe()
    assert not result.ok
    assert result.hop == probe.HOP_KEY_DROPPED
    assert "did not echo Idempotency-Key" in result.detail
    assert eng.keys_received == [None]  # the engine really saw no key
    out = probe.render(result)
    assert "FAIL" in out and "hop: a hop between client and engine" in out


def test_engine_that_does_not_echo_fails_even_with_a_ledger_row(engine_for) -> None:
    engine_for("no_echo")
    result = probe.run_edge_probe()
    assert not result.ok
    assert result.hop == probe.HOP_KEY_DROPPED


def test_status_route_not_served_fails(engine_for) -> None:
    engine_for("no_status_route")
    result = probe.run_edge_probe()
    assert not result.ok
    assert result.hop == probe.HOP_STATUS_ROUTE


def test_ledger_with_no_committed_row_fails(engine_for) -> None:
    engine_for("ledger_empty")
    result = probe.run_edge_probe()
    assert not result.ok
    assert result.hop == probe.HOP_LEDGER
    assert "'unknown'" in result.detail


def test_engine_without_keyed_form_fails_as_engine(engine_for) -> None:
    engine_for("unsupported")
    result = probe.run_edge_probe()
    assert not result.ok
    assert result.hop == probe.HOP_ENGINE
    assert "not a key lost in transit" in result.detail


def test_gateway_error_names_the_edge(engine_for) -> None:
    engine_for("edge_503")
    result = probe.run_edge_probe()
    assert not result.ok
    assert "edge" in (result.hop or "")


def test_rows_expired_by_the_noop_page_fails_loudly(engine_for) -> None:
    engine_for("expired_rows")
    result = probe.run_edge_probe()
    assert not result.ok
    assert "did not answer expired=0" in result.detail


def test_unreachable_endpoint_names_the_network(monkeypatch) -> None:
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://127.0.0.1:1", "tok"))
    monkeypatch.setattr(probe, "_list_collections", lambda: list(_COLLECTIONS))
    monkeypatch.setenv("NX_ALLOW_PROD_WRITE", "test: refused-connection probe for nexus-t0ms7")
    result = probe.run_edge_probe()
    assert not result.ok
    assert result.hop in (probe.HOP_NETWORK, probe.HOP_EDGE_OR_ENGINE)


def test_no_collection_is_not_a_pass(monkeypatch) -> None:
    monkeypatch.setattr(probe, "_list_collections", lambda: [])
    result = probe.run_edge_probe()
    assert not result.ok
    assert result.hop == probe.HOP_SETUP
    assert "NOT PROBED" in result.detail


def test_unlistable_tenant_is_not_probed_never_a_traceback(monkeypatch) -> None:
    def boom() -> list:
        raise RuntimeError("no service")

    monkeypatch.setattr(probe, "_list_collections", boom)
    result = probe.run_edge_probe()
    assert not result.ok and result.hop == probe.HOP_SETUP


def test_sends_nothing_when_the_production_write_guard_refuses(engine_for, monkeypatch) -> None:
    # The suite's autouse fixture opts every test in, so the guard is made to refuse as it does for a
    # dev-checkout process aimed at a real store: the probe reports it and sends no request.
    eng = engine_for("echo")

    def refuse(base_url: str) -> None:
        raise service_endpoint.ProductionWriteGuardError("STOP: refusing a WRITE")

    monkeypatch.setattr(service_endpoint, "guard_production_write", refuse)
    result = probe.run_edge_probe()
    assert not result.ok
    assert "production-write guard" in result.detail
    assert eng.posts == 0


class TestPickProbeCollections:
    def test_prefers_an_origin_whose_quarantine_sibling_exists(self) -> None:
        rows = [
            {"name": "aaa__1-1__m__v1"},
            {"name": "code__1-27__bge-base__v1"},
            {"name": "quarantine-code__1-27__bge-base__v1"},
        ]
        assert probe.pick_probe_collections(rows) == (
            "code__1-27__bge-base__v1", "quarantine-code__1-27__bge-base__v1",
        )

    def test_falls_back_to_first_origin_with_derived_sibling(self) -> None:
        origin, sibling = probe.pick_probe_collections([{"name": "code__1-1__m__v1"}])  # type: ignore[misc]
        assert origin == "code__1-1__m__v1" and sibling == "quarantine-code__1-1__m__v1"

    def test_only_quarantine_or_retired_collections_gives_none(self) -> None:
        rows = [{"name": "quarantine-x__1-1__m__v1"}, {"name": "code__1-1__m__v1", "lifecycle_state": "retired"}]
        assert probe.pick_probe_collections(rows) is None


class TestCli:
    def test_flag_runs_the_probe_and_exits_zero_on_pass(self, monkeypatch) -> None:
        monkeypatch.setattr(probe, "run_edge_probe", lambda: probe.EdgeProbeResult(True, "key k echoed and committed"))
        result = CliRunner().invoke(main, ["doctor", "--probe-edge"])
        assert result.exit_code == 0
        assert "Edge idempotency probe" in result.output

    def test_flag_exits_one_and_names_the_hop_on_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(
            probe, "run_edge_probe",
            lambda: probe.EdgeProbeResult(False, "the 200 did not echo", probe.HOP_KEY_DROPPED, key="k"),
        )
        result = CliRunner().invoke(main, ["doctor", "--probe-edge"])
        assert result.exit_code == 1
        assert "hop: a hop between client and engine" in result.output

    def test_one_mode_per_invocation(self) -> None:
        result = CliRunner().invoke(main, ["doctor", "--probe-edge", "--check-t1"])
        assert result.exit_code != 0
        assert "one mode per invocation" in result.output

    def test_json_is_refused(self) -> None:
        result = CliRunner().invoke(main, ["doctor", "--probe-edge", "--json"])
        assert result.exit_code != 0
        assert "--probe-edge" in result.output

    def test_never_part_of_the_default_sweep(self) -> None:
        assert "--probe-edge" not in doctor._OPT_IN_ONLY_CHECKS
        assert not any("edge" in name for name in doctor._SUPPLEMENTARY_CHECK_NAMES)
