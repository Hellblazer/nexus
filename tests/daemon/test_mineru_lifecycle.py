# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1qdb9: MinerU on-demand lifecycle on the RDR-149 substrate.

Fixture-driven: no real mineru-api is ever launched; the spawn core,
health probe and pid file are all patched. The election flock is REAL
(ServiceRegistry against a tmp config dir) — that is the substrate
membership under test.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from nexus.daemon.mineru_lifecycle import ensure_mineru_running


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
                patch("nexus.commands.mineru.spawn_server_process") as spawn:
            assert ensure_mineru_running() == "http://127.0.0.1:8010"
        spawn.assert_not_called()

    def test_autostart_disabled_returns_none(self):
        with patch("nexus.config.get_mineru_server_url",
                   return_value="http://127.0.0.1:8010"), \
                patch("nexus.daemon.mineru_lifecycle._healthy",
                      return_value=False), \
                patch("nexus.config.get_pdf_config",
                      return_value=_pdf_cfg(autostart=False)), \
                patch("nexus.commands.mineru.spawn_server_process") as spawn:
            assert ensure_mineru_running() is None
        spawn.assert_not_called()

    def test_remote_url_is_never_shadowed_by_a_local_spawn(self):
        """RDR-148 Gap 1 applied to the spawn side: explicit remote
        operator intent wins; we never spawn a local server behind it."""
        with patch("nexus.config.get_mineru_server_url",
                   return_value="http://mineru.example.com:8010"), \
                patch("nexus.daemon.mineru_lifecycle._healthy",
                      return_value=False), \
                patch("nexus.config.get_pdf_config",
                      return_value=_pdf_cfg()), \
                patch("nexus.commands.mineru.spawn_server_process") as spawn:
            assert ensure_mineru_running() is None
        spawn.assert_not_called()


class TestEnsureSpawns:
    def _run(self, tmp_path, monkeypatch, *, healthy_after_spawn=True,
             workers=1):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")  # spawn path under test (conftest pins 0)
        spawned = []
        state = {"up": False}

        def fake_spawn(port):
            spawned.append(port)
            state["up"] = True  # server "starts" instantly
            return MagicMock(pid=4242)

        def fake_healthy(url):
            return state["up"] and state.get("spawn_visible", True)

        def fake_pid():
            return {"pid": 4242, "port": 8010} if state["up"] else None

        results = []
        with patch("nexus.config.get_mineru_server_url",
                   return_value="http://127.0.0.1:8010"), \
                patch("nexus.daemon.mineru_lifecycle._healthy",
                      side_effect=lambda u: fake_healthy(u) if healthy_after_spawn else False), \
                patch("nexus.config.get_pdf_config",
                      return_value=_pdf_cfg()), \
                patch("nexus.config.get_mineru_configured_fixed_port",
                      return_value=None), \
                patch("nexus.commands.mineru._find_free_port",
                      return_value=8010), \
                patch("nexus.commands.mineru._read_pid_file",
                      side_effect=fake_pid), \
                patch("nexus.commands.mineru._is_process_alive",
                      return_value=True), \
                patch("nexus.commands.mineru.spawn_server_process",
                      side_effect=fake_spawn):
            if workers == 1:
                results.append(ensure_mineru_running(wait_healthy_s=3))
            else:
                threads = [
                    threading.Thread(target=lambda: results.append(
                        ensure_mineru_running(wait_healthy_s=3)))
                    for _ in range(workers)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        return spawned, results

    def test_down_server_is_spawned_and_url_returned(self, tmp_path, monkeypatch):
        spawned, results = self._run(tmp_path, monkeypatch)
        assert spawned == [8010]
        assert results == ["http://127.0.0.1:8010"]

    def test_concurrent_ensures_elect_exactly_one_spawner(self, tmp_path, monkeypatch):
        """The substrate election: N concurrent PDF ops, ONE spawn — the
        losers see the pid claim inside the critical section and wait."""
        spawned, results = self._run(tmp_path, monkeypatch, workers=4)
        assert spawned == [8010]  # exactly one spawn
        assert results == ["http://127.0.0.1:8010"] * 4

    def test_health_timeout_returns_none_never_raises(self, tmp_path, monkeypatch):
        """First-start model download can outlast any bound: the document
        falls back (None), the warming server serves the NEXT one."""
        spawned, results = self._run(
            tmp_path, monkeypatch, healthy_after_spawn=False,
        )
        assert spawned == [8010]
        assert results == [None]
