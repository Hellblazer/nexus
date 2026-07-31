# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7f7gb (GH #1419 Issue 3b): a 503 must not restart the service.

Steve Harris watched the supervisor cycle ``nexus-service`` after 3 consecutive
failed health checks caused purely by CPU-bound local-embedding load, not by
any failure. A client mid-batch loses its connection when that happens.

MECHANISM (verified on both sides of the wire):

* ``HealthHandler.java`` implements ``GET /health`` as
  ``dataSource.getConnection()`` + ``SELECT 1`` — it takes a HikariCP pool
  connection.
* The supervisor probes it with ``_HEALTH_TIMEOUT`` and exits for an OS
  restart after ``_MAX_UNHEALTHY_HEARTBEATS`` consecutive failures.

So the probe that decides whether to KILL the service competes for the very
resource that saturation exhausts. Under indexing load the pool is contended,
``/health`` blocks past the probe timeout, the counter fills, and the service
is restarted — which severs in-flight clients, which makes them retry, which
adds load. Self-amplifying.

THE DEEPER DEFECT the fix addresses: ``_service_healthy`` returned a bare bool,
collapsing "answered 503" and "did not answer at all" into one False. Those are
opposite pieces of evidence:

* 503  — the process ANSWERED. It is demonstrably alive; the DB is unhappy.
         Restarting cannot fix a down database and does sever clients.
* timeout / refused — the process may genuinely be wedged. This is the only
         evidence that justifies a restart.

Only UNKNOWN advances the restart counter. Hal decision 2026-07-24. The
correct long-term fix is a dependency-free engine liveness endpoint, queued
for the v0.1.55 engine batch.
"""
from __future__ import annotations

import http.server
import threading

import pytest


@pytest.fixture
def health_server():
    """Real loopback server on port 0 whose /health status is switchable."""
    state = {"status": 200, "delay": 0.0}

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib callback name
            if state["delay"]:
                import time

                time.sleep(state["delay"])
            body = b'{"status":"ok"}'
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a: object) -> None:
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, state
    httpd.shutdown()
    httpd.server_close()


def _probe(port: int):
    from nexus.daemon.storage_service_daemon import StorageServiceSupervisor

    sup = StorageServiceSupervisor.__new__(StorageServiceSupervisor)
    sup._service_port = port
    return sup._probe_service_health()


class TestProbeClassification:
    def test_200_is_ok(self, health_server) -> None:
        from nexus.daemon.storage_service_daemon import HealthProbe

        httpd, _state = health_server
        assert _probe(httpd.server_address[1]) is HealthProbe.OK

    def test_503_is_unready_not_unknown(self, health_server) -> None:
        """THE fix. The service answered — it is alive. Classifying this as
        UNKNOWN is what let a DB blip and a load spike both kill a healthy
        process."""
        from nexus.daemon.storage_service_daemon import HealthProbe

        httpd, state = health_server
        state["status"] = 503
        assert _probe(httpd.server_address[1]) is HealthProbe.UNREADY

    def test_timeout_is_unknown(self, health_server, monkeypatch) -> None:
        """A probe that never came back is the ONLY evidence of a wedge."""
        import nexus.daemon.storage_service_daemon as ssd
        from nexus.daemon.storage_service_daemon import HealthProbe

        httpd, state = health_server
        state["delay"] = 1.5
        monkeypatch.setattr(ssd, "_HEALTH_TIMEOUT", 0.25)
        assert _probe(httpd.server_address[1]) is HealthProbe.UNKNOWN

    def test_refused_connection_is_unknown(self) -> None:
        import socket

        from nexus.daemon.storage_service_daemon import HealthProbe

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listening now
        assert _probe(port) is HealthProbe.UNKNOWN

    def test_unset_port_is_unknown(self) -> None:
        from nexus.daemon.storage_service_daemon import HealthProbe

        assert _probe(0) is HealthProbe.UNKNOWN


class TestRestartAccounting:
    """The counter must advance ONLY on UNKNOWN."""

    def _sup(self, probe_result):
        from unittest.mock import MagicMock

        import nexus.daemon.storage_service_daemon as ssd

        sup = ssd.StorageServiceSupervisor.__new__(ssd.StorageServiceSupervisor)
        sup._consecutive_unhealthy_heartbeats = 0
        sup._service_port = 1
        sup._pg_port = 2
        sup._proc = MagicMock()
        sup._proc.poll.return_value = None
        sup._proc.pid = 4242
        sup._supervisor = MagicMock()
        sup._supervisor.fenced = False   # healthy path re-stamps and checks this
        sup._scope = "test-scope"        # only read when fenced, but keep it real
        sup._probe_service_health = lambda: probe_result
        sup._pg_reachable = lambda: True
        return sup

    def test_repeated_503_never_reaches_the_restart_threshold(
        self, monkeypatch,
    ) -> None:
        """Steve's shape. Twenty consecutive 503s must not kill a process that
        answered twenty times."""
        import nexus.daemon.storage_service_daemon as ssd
        from nexus.daemon.storage_service_daemon import HealthProbe

        monkeypatch.setattr(ssd, "_pid_is_alive", lambda _pid: True)
        sup = self._sup(HealthProbe.UNREADY)

        for _ in range(20):
            keep_going, _pg = sup.heartbeat_once()
            assert keep_going, "a service answering 503 must never be killed"
        assert sup._consecutive_unhealthy_heartbeats == 0

    def test_sustained_unknown_still_triggers_the_restart(
        self, monkeypatch,
    ) -> None:
        """The stuck-but-alive class the threshold exists for must still be
        caught — this fix must not disarm wedge detection."""
        import nexus.daemon.storage_service_daemon as ssd
        from nexus.daemon.storage_service_daemon import HealthProbe

        monkeypatch.setattr(ssd, "_pid_is_alive", lambda _pid: True)
        sup = self._sup(HealthProbe.UNKNOWN)

        outcomes = [
            sup.heartbeat_once()[0]
            for _ in range(ssd._MAX_UNHEALTHY_HEARTBEATS)
        ]
        assert outcomes[-1] is False, "sustained no-answer must still restart"

    def test_an_ok_beat_resets_the_counter(self, monkeypatch) -> None:
        import nexus.daemon.storage_service_daemon as ssd
        from nexus.daemon.storage_service_daemon import HealthProbe

        monkeypatch.setattr(ssd, "_pid_is_alive", lambda _pid: True)
        sup = self._sup(HealthProbe.UNKNOWN)
        sup.heartbeat_once()
        assert sup._consecutive_unhealthy_heartbeats == 1

        sup._probe_service_health = lambda: HealthProbe.OK
        sup.heartbeat_once()
        assert sup._consecutive_unhealthy_heartbeats == 0


def test_probe_timeout_respects_the_lease_ttl_invariant() -> None:
    """The probe budget is CAPPED, not free.

    Originally this asserted ``_HEALTH_TIMEOUT >= 10.0`` on the reasoning that
    the probe must outlast pool contention. That was wrong and the existing
    invariant test caught it: the probe BLOCKS the heartbeat thread, so a tick
    costs ``_HEALTH_TIMEOUT + heartbeat_interval`` and the lease TTL must be at
    least 3x that (test_ttl_exceeds_worst_case_heartbeat_tick). A 20s probe
    made the supervisor's OWN lease age out mid-probe — trading a spurious
    restart for a vanished endpoint, which is the same outage wearing a
    different hat.

    That reasoning held while /health was the restart authority. nexus-hubc0
    CLOSED the gap the last paragraph of this docstring used to describe as
    tracked-but-open: the dependency-free ``/livez`` endpoint now holds the
    restart decision, so /health no longer has to outlast a saturated pool to
    avoid being mistaken for death — and its budget came back DOWN (4.0 -> 2.0)
    as a result, which is what restored the TTL margin.

    The budget is still capped, and the cap now spans BOTH probes, because a
    beat whose /health goes silent consults /livez before deciding anything.
    """
    import nexus.daemon.storage_service_daemon as ssd
    from nexus.daemon.service_registry import DEFAULT_HEARTBEAT_INTERVAL, ttl_for_tier

    worst_tick = ssd._HEALTH_TIMEOUT + ssd._LIVEZ_TIMEOUT + DEFAULT_HEARTBEAT_INTERVAL
    assert ttl_for_tier("storage_service") >= 3 * worst_tick, (
        "probe budget raised past what the lease TTL can absorb — the "
        "supervisor would lose its own lease while probing"
    )
    # The DETECTION window is unchanged in wall-clock terms: still ~4 beats of
    # total silence before an exit-for-restart, still far wider than the 2s /
    # 3-beat window that cycled Steve's service. What changed is WHAT has to be
    # silent — both endpoints, not just the pool-bound one — so a busy service
    # no longer spends this window on its way to a needless kill.
    assert ssd._MAX_UNHEALTHY_HEARTBEATS * worst_tick >= 16.0
