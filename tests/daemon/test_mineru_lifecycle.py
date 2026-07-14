# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1qdb9: MinerU on-demand spawn guard on the RDR-149 election.

Fixture-driven: no real mineru-api is ever launched; the spawn core,
health probe and pid file are all patched. The election flock is REAL
(ServiceRegistry against a tmp config dir) — that is the substrate
membership under test.
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

from nexus.daemon.mineru_lifecycle import ensure_mineru_running, spawn_policy_allows


def _pdf_cfg(autostart=True):
    cfg = MagicMock()
    cfg.mineru_autostart = autostart
    return cfg


class TestEnsureShortCircuits:
    def test_healthy_server_returns_immediately(self):
        with patch("nexus.config.get_mineru_server_url",
                   return_value="http://127.0.0.1:8010"), \
                patch("nexus.daemon.mineru_lifecycle._healthy",
                      return_value=True), \
                patch("nexus._mineru_spawn.spawn_server_process") as spawn:
            assert ensure_mineru_running() == "http://127.0.0.1:8010"
        spawn.assert_not_called()

    def test_autostart_disabled_by_config_returns_none(self, monkeypatch):
        monkeypatch.delenv("NX_MINERU_AUTOSTART", raising=False)  # exercise the CONFIG gate
        with patch("nexus.config.get_mineru_server_url",
                   return_value="http://127.0.0.1:8010"), \
                patch("nexus.daemon.mineru_lifecycle._healthy",
                      return_value=False), \
                patch("nexus.config.get_pdf_config",
                      return_value=_pdf_cfg(autostart=False)), \
                patch("nexus._mineru_spawn.spawn_server_process") as spawn:
            assert ensure_mineru_running() is None
        spawn.assert_not_called()

    def test_env_falsy_spellings_disable_even_with_config_true(self, monkeypatch):
        """Review H1: a stray NX_MINERU_AUTOSTART=false in a shell must
        DISABLE (aspect-worker allow-list precedent), never force-enable
        past an explicit config."""
        for spelling in ("0", "false", "False", "FALSE", "no", "No", "off", "OFF"):
            monkeypatch.setenv("NX_MINERU_AUTOSTART", spelling)
            with patch("nexus.config.get_mineru_server_url",
                       return_value="http://127.0.0.1:8010"), \
                    patch("nexus.config.get_pdf_config",
                          return_value=_pdf_cfg(autostart=True)):
                assert spawn_policy_allows() is False, spelling

    def test_env_enable_overrides_config_false(self, monkeypatch):
        monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
        with patch("nexus.config.get_mineru_server_url",
                   return_value="http://127.0.0.1:8010"), \
                patch("nexus.config.get_pdf_config",
                      return_value=_pdf_cfg(autostart=False)):
            assert spawn_policy_allows() is True

    def test_remote_url_is_never_shadowed_by_a_local_spawn(self, monkeypatch):
        """RDR-148 Gap 1 applied to the spawn side: explicit remote
        operator intent wins; we never spawn a local server behind it."""
        monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")  # past the env gate — the REMOTE guard is under test
        with patch("nexus.config.get_mineru_server_url",
                   return_value="http://mineru.example.com:8010"), \
                patch("nexus.daemon.mineru_lifecycle._healthy",
                      return_value=False), \
                patch("nexus.config.get_pdf_config",
                      return_value=_pdf_cfg()), \
                patch("nexus._mineru_spawn.spawn_server_process") as spawn:
            assert ensure_mineru_running() is None
        spawn.assert_not_called()


class TestEnsureSpawns:
    def _run(self, tmp_path, monkeypatch, *, healthy_after_spawn=True,
             workers=1, calls=1, wait_healthy_s=3.0, proc_poll=None):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")  # spawn path under test (conftest pins 0)
        spawned = []
        state = {"up": False}

        def fake_spawn(port):
            spawned.append(port)
            state["up"] = True  # server "starts" instantly
            proc = MagicMock(pid=4242)
            proc.poll.return_value = proc_poll
            proc.returncode = proc_poll
            return proc

        def fake_healthy(url):
            return state["up"] and state.get("spawn_visible", True)

        def fake_pid():
            return {"pid": 4242, "port": 8010} if state["up"] else None

        results = []
        durations = []
        with patch("nexus.config.get_mineru_server_url",
                   return_value="http://127.0.0.1:8010"), \
                patch("nexus.daemon.mineru_lifecycle._healthy",
                      side_effect=lambda u: fake_healthy(u) if healthy_after_spawn else False), \
                patch("nexus.config.get_pdf_config",
                      return_value=_pdf_cfg()), \
                patch("nexus.config.get_mineru_configured_fixed_port",
                      return_value=None), \
                patch("nexus._mineru_spawn._find_free_port",
                      return_value=8010), \
                patch("nexus._mineru_pid.read_pid_file",
                      side_effect=fake_pid), \
                patch("nexus._mineru_pid.is_process_alive",
                      return_value=True), \
                patch("nexus._mineru_spawn.spawn_server_process",
                      side_effect=fake_spawn):
            if workers == 1:
                for _ in range(calls):
                    t0 = time.monotonic()
                    results.append(
                        ensure_mineru_running(wait_healthy_s=wait_healthy_s))
                    durations.append(time.monotonic() - t0)
            else:
                threads = [
                    threading.Thread(target=lambda: results.append(
                        ensure_mineru_running(wait_healthy_s=wait_healthy_s)))
                    for _ in range(workers)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        return spawned, results, durations

    def test_down_server_is_spawned_and_url_returned(self, tmp_path, monkeypatch):
        spawned, results, _ = self._run(tmp_path, monkeypatch)
        assert spawned == [8010]
        assert results == ["http://127.0.0.1:8010"]

    def test_concurrent_ensures_elect_exactly_one_spawner(self, tmp_path, monkeypatch):
        """The substrate election: N concurrent PDF ops, ONE spawn — the
        losers see the pid claim inside the critical section and wait."""
        spawned, results, _ = self._run(tmp_path, monkeypatch, workers=4)
        assert spawned == [8010]  # exactly one spawn
        assert results == ["http://127.0.0.1:8010"] * 4

    def test_health_timeout_returns_none_never_raises(self, tmp_path, monkeypatch):
        """First-start model download can outlast any bound: the document
        falls back (None), the warming server serves the NEXT one."""
        spawned, results, _ = self._run(
            tmp_path, monkeypatch, healthy_after_spawn=False,
        )
        assert spawned == [8010]
        assert results == [None]

    def test_dead_child_fails_fast_not_full_budget(self, tmp_path, monkeypatch):
        """Review H2: a fast-crashing child (port in use) must fall back in
        one poll tick, not burn the full wait budget per document."""
        spawned, results, durations = self._run(
            tmp_path, monkeypatch, healthy_after_spawn=False,
            wait_healthy_s=30.0, proc_poll=1,
        )
        assert spawned == [8010]
        assert results == [None]
        assert durations[0] < 5.0, f"took {durations[0]:.1f}s — burned the budget"

    def test_warmup_budget_is_shared_across_documents(self, tmp_path, monkeypatch):
        """nexus-m45o6: a second document arriving during (or after) the
        warm-up window must NOT re-wait a fresh full budget — the marker
        caps the batch's total stall at one budget."""
        spawned, results, durations = self._run(
            tmp_path, monkeypatch, healthy_after_spawn=False,
            calls=2, wait_healthy_s=2.0,
        )
        assert spawned == [8010]  # second call sees the live pid, no respawn
        assert results == [None, None]
        assert durations[0] >= 2.0  # the spawner pays the budget once
        assert durations[1] < 1.0, (
            f"second document re-waited {durations[1]:.1f}s — the warm-up "
            f"stall multiplied across documents"
        )


    def test_stale_marker_from_replaced_attempt_never_caps_a_fresh_wait(
        self, tmp_path, monkeypatch,
    ):
        """Review M1 (60ed904e): a warming marker left by a DEAD/replaced
        attempt must be discarded when the live pid file names a different
        server (e.g. an operator's manual `nx mineru start`, which stamps
        no marker) — not shrink that server's wait to an expired budget."""
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
        # Stale marker: pid 1111, stamped far outside any budget.
        (tmp_path / "mineru_warming.json").write_text(
            json.dumps({"pid": 1111, "ts": time.time() - 10_000}),
        )
        # Live pid file names a DIFFERENT server (9999) that turns healthy
        # on the second probe — inside the fresh budget, far outside the
        # stale marker's.
        probes = {"n": 0}

        def flaky_healthy(url):
            probes["n"] += 1
            return probes["n"] >= 2

        with patch("nexus.config.get_mineru_server_url",
                   return_value="http://127.0.0.1:8010"), \
                patch("nexus.daemon.mineru_lifecycle._healthy",
                      side_effect=flaky_healthy), \
                patch("nexus.daemon.mineru_lifecycle._HEALTH_POLL_S", 0.05), \
                patch("nexus.config.get_pdf_config",
                      return_value=_pdf_cfg()), \
                patch("nexus._mineru_pid.read_pid_file",
                      return_value={"pid": 9999, "port": 8010}), \
                patch("nexus._mineru_pid.is_process_alive",
                      return_value=True), \
                patch("nexus._mineru_spawn.spawn_server_process") as spawn:
            result = ensure_mineru_running(wait_healthy_s=5.0)
        assert result == "http://127.0.0.1:8010"
        spawn.assert_not_called()  # live pid claimed inside the election
        # The stale marker was discarded, not honored.
        assert not (tmp_path / "mineru_warming.json").exists()
