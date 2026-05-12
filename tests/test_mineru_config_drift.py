# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-oa7r: PID-file-as-source-of-truth for MinerU server URL.

Pre-fix: ``_restart_mineru_server`` and ``nx mineru start`` wrote
the live port to ``~/.config/nexus/config.yml``'s
``pdf.mineru_server_url``. The port was ephemeral but the config
was persistent; when the server later died, the URL drifted to a
dead port and every subsequent session silently fell through to
the OOM-prone in-process subprocess.

Post-fix: PID file at ``~/.config/nexus/mineru.pid`` is canonical.
``get_mineru_server_url`` reads it at call time. Neither startup
path writes the live port to config any more.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ── _read_live_mineru_port ───────────────────────────────────────────


def test_live_port_returns_port_when_pid_alive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    pid_path = tmp_path / "mineru.pid"
    pid_path.write_text(json.dumps({
        "pid": os.getpid(),  # this test process is alive
        "port": 49353,
    }))
    from nexus.config import _read_live_mineru_port
    assert _read_live_mineru_port() == 49353


def test_live_port_returns_none_when_pid_dead(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    pid_path = tmp_path / "mineru.pid"
    # PID 1 is init on macOS/Linux; safe to use a clearly-not-our-pid
    # value that we then patch _is_process_alive to claim is dead.
    pid_path.write_text(json.dumps({"pid": 999999, "port": 49353}))
    with patch(
        "nexus.commands.mineru._is_process_alive", return_value=False,
    ):
        from nexus.config import _read_live_mineru_port
        assert _read_live_mineru_port() is None


def test_live_port_returns_none_when_no_pid_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    from nexus.config import _read_live_mineru_port
    assert _read_live_mineru_port() is None


def test_live_port_returns_none_when_malformed_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    (tmp_path / "mineru.pid").write_text("{not-json}")
    from nexus.config import _read_live_mineru_port
    assert _read_live_mineru_port() is None


# ── get_mineru_server_url ────────────────────────────────────────────


def test_url_prefers_live_pid_file_over_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    # Config says dead port; PID file says live port — PID wins.
    pid_path = tmp_path / "mineru.pid"
    pid_path.write_text(json.dumps({
        "pid": os.getpid(),
        "port": 49353,
    }))
    with patch(
        "nexus.config.get_pdf_config",
        return_value=type("X", (), {
            "mineru_server_url": "http://127.0.0.1:9999",
        })(),
    ):
        from nexus.config import get_mineru_server_url
        assert get_mineru_server_url() == "http://127.0.0.1:49353"


def test_url_falls_back_to_config_when_no_live_pid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    with patch(
        "nexus.config.get_pdf_config",
        return_value=type("X", (), {
            "mineru_server_url": "http://127.0.0.1:8010",
        })(),
    ):
        from nexus.config import get_mineru_server_url
        assert get_mineru_server_url() == "http://127.0.0.1:8010"


def test_url_falls_back_to_config_when_pid_stale(tmp_path: Path, monkeypatch) -> None:
    """Stale PID file (server died without cleanup) — fall back to config."""
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    pid_path = tmp_path / "mineru.pid"
    pid_path.write_text(json.dumps({"pid": 999999, "port": 49353}))
    with patch(
        "nexus.commands.mineru._is_process_alive", return_value=False,
    ), patch(
        "nexus.config.get_pdf_config",
        return_value=type("X", (), {
            "mineru_server_url": "http://127.0.0.1:8010",
        })(),
    ):
        from nexus.config import get_mineru_server_url
        assert get_mineru_server_url() == "http://127.0.0.1:8010"


# ── Startup paths must not write config ──────────────────────────────


def test_nx_mineru_start_does_not_set_config(monkeypatch) -> None:
    """``nx mineru start`` must not write ``pdf.mineru_server_url``.
    Reading the module source is the cheap regression for this
    contract — exercising the full start flow needs a real
    mineru-api binary."""
    import nexus.commands.mineru as mineru_mod
    src = Path(mineru_mod.__file__).read_text()
    assert "set_config_value(\"pdf.mineru_server_url\"" not in src, (
        "nx mineru start re-introduced the config write; the PID file "
        "is the canonical source of truth — see nexus-oa7r."
    )


def test_restart_mineru_server_does_not_set_config(monkeypatch) -> None:
    """``_restart_mineru_server`` in pdf_extractor must not write
    ``pdf.mineru_server_url`` either."""
    import nexus.pdf_extractor as ext_mod
    src = Path(ext_mod.__file__).read_text()
    assert "set_config_value(\"pdf.mineru_server_url\"" not in src, (
        "_restart_mineru_server re-introduced the config write; "
        "the PID file is canonical (nexus-oa7r)."
    )
