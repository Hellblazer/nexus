# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ldf2s round 1 (L1): ``--max-rps`` is a hard limit on every HTTP request the verb sends.

The first round bounded the verb by pauses between rounds, which cannot hold a rate: a round sends several
requests, a gateway retry sends one again, and a failed ``update_many`` page falls back to one request per
document. These pins run the real ``HttpCatalogClient`` (httpx) and the real vector client (the pooled urllib
transport) against an in-process engine on a real socket, and hold the verb to the limit at the point where
requests leave the process: the number of requests the ENGINE saw equals the number the gate granted, and no
two grants are closer than ``1 / R`` on a fake clock that retry sleeps advance too.
"""
from __future__ import annotations

import http.client
import http.server
import json
import threading
import time
from collections import Counter
from urllib.parse import urlparse

import pytest

import nexus.db.http_vector_client as hvc
import nexus.db.t2._refreshable_client as rc
from nexus.catalog import meta_backfill as mb
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db import gateway_hold, request_gate
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.pooled_http import build_opener
from tests._module_seam import module_time

DOCS = "docs__gate__minilm__v1"
NOTES = "knowledge__gate__minilm__v1"
N_DOCS = 25
BIG_NOTE_PIECES = 650    # three store-get pages of 300, 300 and 50


def _chash(tag: int) -> str:
    return format(tag, "064x")


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Engine:
    """The engine routes the verb uses, with faults to inject: ``faults[path]`` is a list of statuses, one
    consumed per request to that path (0: answer normally)."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.faults: dict[str, list[int]] = {}
        self.docs = [
            {"tumbler": f"1.{i}", "title": f"P{i}", "content_type": "paper", "file_path": f"p{i}.pdf",
             "physical_collection": DOCS, "meta": {}, "chunk_count": 1, "indexed_at": "2026-01-01T00:00:00+00:00"}
            for i in range(1, N_DOCS + 1)
        ]
        self.docs.append({
            "tumbler": "1.900", "title": "big", "content_type": "knowledge", "file_path": "", "physical_collection": NOTES,
            "meta": {}, "chunk_count": BIG_NOTE_PIECES, "indexed_at": "2026-01-01T00:00:00+00:00",
        })
        self.manifests = {f"1.{i}": [_chash(i)] for i in range(1, N_DOCS + 1)}
        self.manifests["1.900"] = [_chash(10_000 + k) for k in range(BIG_NOTE_PIECES)]
        self.owner = {c: d for d, cs in self.manifests.items() for c in cs}
        self.rows = {
            (DOCS, _chash(i)): {"extraction_method": "mineru", "indexed_at": "2025-01-01T00:00:00+00:00"}
            for i in range(1, N_DOCS + 1)
        }
        for k in range(BIG_NOTE_PIECES):
            self.rows[(NOTES, _chash(10_000 + k))] = {"indexed_at": "2025-01-01T00:00:00+00:00"}

    def paths(self) -> Counter:
        return Counter(path for _method, path in self.log)

    def answer(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        self.log.append((method, path))
        queued = self.faults.get(path)
        if queued and (status := queued.pop(0)):
            return status, {"error": "injected"}
        if path == "/v1/catalog/list":
            return 200, {"documents": self.docs}
        if path == "/v1/catalog/manifest/get_many":
            found = {d: [{"position": i, "chash": c} for i, c in enumerate(self.manifests[d])] for d in body["doc_ids"] if d in self.manifests}
            return 200, {"manifests": found, "count": len(found)}
        if path == "/v1/catalog/manifest/chash_positions":
            rows = [{"chash": c, "doc_id": self.owner[c], "position": 0, "chunk_count": 1} for c in body["chashes"] if c in self.owner]
            return 200, {"rows": rows, "count": len(rows)}
        if path == "/v1/vectors/store-get":
            ids = [i for i in body["ids"] if (body["collection"], i) in self.rows]
            return 200, {"ids": ids, "documents": ["t"] * len(ids), "count": len(ids),
                         "metadatas": [self.rows[(body["collection"], i)] for i in ids]}
        if path == "/v1/catalog/update_many":
            return 200, {"updated": [1] * len(body["updates"])}
        if path == "/v1/catalog/update":
            return 200, {"updated": 1}
        return 404, {"error": f"no route {path}"}


@pytest.fixture
def engine(monkeypatch):
    state = _Engine()

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            status, payload = state.answer(self.command, urlparse(self.path).path, body)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _serve

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (state.url, "tok"))
    # the cross-process 503 hold lives in a file; the test keeps its retries to its own fake clock
    monkeypatch.setattr(gateway_hold, "wait_before_attempt", lambda **_k: 0.0)
    monkeypatch.setattr(gateway_hold, "record_503", lambda sleep_s, **_k: sleep_s)
    monkeypatch.setattr(gateway_hold, "record_success", lambda **_k: None)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    module_time(monkeypatch, [hvc, rc]).sleep = fake.sleep    # the gateway retry ladders sleep on the same clock
    return fake


def _t3() -> HttpVectorClient:
    client = HttpVectorClient.__new__(HttpVectorClient)
    client._tenant = "default"
    return client


def _run(engine, clock, rps, **kw):
    gate = request_gate.RequestGate(rps, clock=clock, sleep=clock.sleep)
    with HttpCatalogClient(base_url=engine.url, _token="tok") as cat, request_gate.paced(gate):
        report = mb.backfill_meta(cat, _t3(), cat, apply=True, batch=5, requests=lambda: gate.count, clock=clock, **kw)
    return report, gate


def _gaps(gate: request_gate.RequestGate) -> list[float]:
    return [b - a for a, b in zip(gate.stamps, gate.stamps[1:])]


# ── the limiter ────────────────────────────────────────────────────────────────────────────────────

def test_the_gate_spaces_grants_by_the_interval_and_counts_them():
    clock = _FakeClock()
    gate = request_gate.RequestGate(4.0, clock=clock, sleep=clock.sleep)
    for _ in range(9):
        gate.acquire()
    assert gate.count == 9 and clock.now == pytest.approx(9 * 0.25), "nine requests take nine intervals: the first waits too"
    assert all(g == pytest.approx(0.25) for g in _gaps(gate))
    clock.now += 10.0    # an idle spell does not bank a burst
    gate.acquire()
    gate.acquire()
    assert gate.stamps[-1] - gate.stamps[-2] == pytest.approx(0.25)


def test_the_gate_holds_concurrent_callers_to_the_interval():
    gate = request_gate.RequestGate(200.0)    # real clock, 5 ms apart
    threads = [threading.Thread(target=lambda: [gate.acquire() for _ in range(10)]) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stamps = sorted(gate.stamps)
    assert gate.count == 40 and min(b - a for a, b in zip(stamps, stamps[1:])) >= 0.005 - 1e-6


def test_an_unlimited_gate_counts_and_never_sleeps():
    clock = _FakeClock()
    gate = request_gate.RequestGate(None, clock=clock, sleep=clock.sleep)
    for _ in range(50):
        gate.acquire()
    assert gate.count == 50 and clock.now == 0.0


@pytest.mark.parametrize("bad", [0, -1.0, float("nan")])
def test_a_rate_that_is_not_above_zero_is_refused(bad):
    with pytest.raises(ValueError):
        request_gate.RequestGate(bad)


def test_paced_installs_and_restores_and_costs_nothing_without_a_gate():
    assert request_gate.requests_so_far() == 0
    request_gate.before_request()    # no gate: a no-op
    outer, inner = request_gate.RequestGate(None), request_gate.RequestGate(None)
    with request_gate.paced(outer):
        request_gate.before_request()
        with request_gate.paced(inner):
            request_gate.before_request()
            request_gate.before_request()
        request_gate.before_request()
    assert (outer.count, inner.count) == (2, 2)
    assert request_gate.requests_so_far() == 0


# ── every request of a whole run ───────────────────────────────────────────────────────────────────

def test_every_request_of_a_run_is_paced_retries_and_fallbacks_included(engine, clock):
    """Paging (a 650-piece note: three store-get pages), a 503 on a store-get that the vector client retries
    after its 2 s backoff, and an ``update_many`` page the engine refuses (the client falls back to one
    ``/update`` per document). R = 0.2/s, so the gate's 5 s interval is longer than the 2 s retry backoff: a
    retried request is spaced by the gate or not at all."""
    engine.faults["/v1/vectors/store-get"] = [0, 503]
    engine.faults["/v1/catalog/update_many"] = [500]
    report, gate = _run(engine, clock, 0.2)

    seen = engine.paths()
    assert report.documents_examined == N_DOCS + 1 and report.writes_failed == 0 and not report.interrupted
    assert seen["/v1/vectors/store-get"] >= 5 + 3 + 1, "the rows of the five-document rounds, the note's three pages, and the retried 503"
    assert seen["/v1/catalog/update"] >= 5, "the refused update_many page fell back to one request per document"
    assert seen["/v1/catalog/update_many"] >= 2, "and the later rounds' pages went through"
    assert len(engine.log) == gate.count, f"the engine saw {len(engine.log)} requests, the gate granted {gate.count}: some request left unpaced"
    assert min(_gaps(gate)) >= 5.0 - 1e-9, f"two requests {min(_gaps(gate)):.3f}s apart at --max-rps 0.2"
    assert clock.now >= gate.count * 5.0 and gate.count > 30


def test_the_run_has_no_unpaced_request_without_faults_either(engine, clock):
    report, gate = _run(engine, clock, 2.0)
    assert report.writes_failed == 0 and len(engine.log) == gate.count and gate.count > 20
    assert min(_gaps(gate)) >= 0.5 - 1e-9
    assert gate.count / clock.now <= 2.0 + 1e-9, "the mean rate over the whole run is at most the limit too"


def test_progress_counts_the_requests_the_engine_saw_and_both_rates(engine, clock):
    seen: list[mb.Progress] = []
    gate = request_gate.RequestGate(2.0, clock=clock, sleep=clock.sleep)
    with HttpCatalogClient(base_url=engine.url, _token="tok") as cat, request_gate.paced(gate):
        mb.backfill_meta(cat, _t3(), cat, apply=True, batch=5, requests=lambda: gate.count, clock=clock, progress=seen.append)
    last = seen[-1]
    assert last.requests == len(engine.log) == gate.count, "the figure is requests on the wire, listing pages included"
    assert last.rate_since_start == pytest.approx(last.requests / last.elapsed) and last.rate_since_start <= 2.0 + 1e-9
    assert last.recent_rounds == min(mb.RECENT_ROUNDS, last.batches) and 0 < last.rate_recent <= 2.0 + 1e-9
    assert f"{last.requests} requests (" in last.line() and "/s since start" in last.line() and "/s over the last 6 rounds" in last.line()


def test_without_the_hooks_the_gate_would_see_nothing(engine, clock, monkeypatch):
    """Control: the same run with the catalog transport's hook removed. The engine sees requests the gate never
    granted, which is exactly what the equality in the test above catches."""
    monkeypatch.setattr(rc, "before_request", lambda: None)
    _report, gate = _run(engine, clock, 2.0)
    assert len(engine.log) > gate.count


# ── the pooled transport's own replay ──────────────────────────────────────────────────────────────

def test_a_stale_connection_replay_is_a_second_request_and_passes_the_gate(engine, monkeypatch):
    """``pooled_http`` replays a request once on a fresh connection when a pooled one turns out stale. That is a
    second request on the wire, so it is paced and counted like any other."""
    opener = build_opener(lambda _sock: None)
    calls = {"n": 0}
    real = http.client.HTTPConnection.getresponse

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:     # the second response read, on the pooled connection of the first request
            raise ConnectionResetError("peer closed the pooled connection")
        return real(self, *a, **kw)

    monkeypatch.setattr(http.client.HTTPConnection, "getresponse", flaky)
    gate = request_gate.RequestGate(None)
    with request_gate.paced(gate):
        for _ in range(2):
            with opener.open(f"{engine.url}/v1/catalog/list", timeout=5) as resp:
                assert resp.status == 200
    # The reset is faked on the client's read, so the client never waits for the server to finish the second
    # request: its handler thread can log it after the client has moved on (develop red, job 126331517009).
    # Wait, bounded, for the server's log to catch up before comparing.
    deadline = time.monotonic() + 2.0
    while len(engine.log) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gate.count == 3 and len(engine.log) == 3, "two logical requests, one replayed after the reset: three on the wire, all granted"


# ── the two one-shot transports: the data-token mint and the /version probe (nexus-dr0da) ──────────

class _OneShotServer:
    """A real local HTTP server (port 0) that answers a POST mint and GET /version and counts requests."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        outer = self

        class _H(http.server.BaseHTTPRequestHandler):
            def _reply(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 — http.server's hook name
                outer.seen.append(f"POST {self.path}")
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._reply({"token": "t", "expires_in": 60})

            def do_GET(self) -> None:  # noqa: N802 — http.server's hook name
                outer.seen.append(f"GET {self.path}")
                self._reply({"release_version": "999.0.0", "app_version": "999.0.0"})

            def log_message(self, *_a) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def one_shot():
    srv = _OneShotServer()
    yield srv
    srv.close()


def _mint(one_shot: _OneShotServer) -> None:
    from nexus.db.data_token import _default_poster
    _default_poster(f"{one_shot.url}/v1/data-token", {}, {"tenant": "t"})


def _probe(one_shot: _OneShotServer) -> None:
    import contextlib
    from nexus.db.managed_endpoint import probe_managed_service
    with contextlib.suppress(Exception):  # only the request matters here, not the verdict on the version
        probe_managed_service(base_url=one_shot.url, timeout=5.0)


@pytest.mark.parametrize("send", [_mint, _probe], ids=["data-token-mint", "version-probe"])
def test_the_one_shot_transports_pass_the_gate(one_shot, send):
    """Each request the mint and the probe put on the wire is one the gate granted: removing either
    hook makes the gate's count fall short of what the server saw."""
    with request_gate.paced(request_gate.RequestGate()) as gate:
        send(one_shot)
    assert len(one_shot.seen) == 1, one_shot.seen
    assert gate.count == len(one_shot.seen)


@pytest.mark.parametrize(("send", "module"), [(_mint, "nexus.db.data_token"), (_probe, "nexus.db.managed_endpoint")],
                         ids=["data-token-mint", "version-probe"])
def test_without_a_one_shot_hook_the_gate_would_see_nothing(one_shot, monkeypatch, send, module):
    """Control for the test above: with the call site's hook removed the server still sees the request
    and the gate does not, which the equality above catches."""
    monkeypatch.setattr(f"{module}.before_request", lambda: None)
    with request_gate.paced(request_gate.RequestGate()) as gate:
        send(one_shot)
    assert len(one_shot.seen) == 1 and gate.count == 0


def test_a_long_lived_gate_keeps_a_bounded_tail_of_stamps():
    """The count is exact however long the gate lives; the diagnostic stamps keep only the newest."""
    gate = request_gate.RequestGate()
    for _ in range(request_gate.MAX_STAMPS + 5):
        gate.acquire()
    assert gate.count == request_gate.MAX_STAMPS + 5
    assert len(gate.stamps) == request_gate.MAX_STAMPS

