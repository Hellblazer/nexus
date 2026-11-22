# SPDX-License-Identifier: AGPL-3.0-or-later
"""F35 (nexus-7t3w1): a MinerU server already running is cycled when its recorded weights source differs from a new server's."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus import _mineru_spawn, mineru_weights as mw
from tests._f35_weights_bed import (  # noqa: F401 — env is a fixture
    _CONTENT, _complete_root, _root, env,
)

#: tests/conftest.py stubs the fetch and the child env for every other module; this one drives them.
REAL_MINERU_WEIGHTS_FETCH = True

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── a running server on the wrong source is cycled (review S4) ───────────────────────────────────────────


def test_weights_state_names_the_root_only_for_an_env_that_points_at_it(env):
    assert mw.weights_state({}) is None
    _complete_root(env)
    assert mw.weights_state() == str(_root())
    assert mw.weights_state({"MINERU_MODEL_SOURCE": "local", "MINERU_TOOLS_CONFIG_JSON": "/their/mineru.json"}) is None


def test_a_server_started_before_the_root_was_complete_is_stale_once_it_is(env):
    from nexus._mineru_health import server_weights_stale  # noqa: PLC0415

    info = {"pid": 4242, "port": 8123, "weights_root": None}
    url = "http://127.0.0.1:8123"
    assert not server_weights_stale(url, info), "no root yet: a server on MinerU's own source is current"
    _complete_root(env)  # the upgrade pass spawned it under NX_MINERU_WEIGHTS_FETCH_SKIP=1; a later run filled the root
    assert server_weights_stale(url, info)
    assert server_weights_stale(url, {k: v for k, v in info.items() if k != "weights_root"}), "an older pid file"
    assert not server_weights_stale(url, {**info, "weights_root": str(_root())})
    assert not server_weights_stale("http://127.0.0.1:9999", info), "another port is not the registered server"
    assert not server_weights_stale("http://gpu.lan:8123", info), "a remote server is not ours to cycle"
    assert not server_weights_stale(url, None)


def test_a_server_on_the_nexus_root_is_stale_when_the_user_then_chooses_local(env, monkeypatch):
    from nexus._mineru_health import server_weights_stale  # noqa: PLC0415

    _complete_root(env)
    info = {"pid": 4242, "port": 8123, "weights_root": str(_root())}
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "local")
    assert server_weights_stale("http://127.0.0.1:8123", info)


def test_the_extractor_cycles_a_stale_server_once_and_leaves_a_current_one(env, monkeypatch, tmp_path):
    from nexus import _mineru_pid  # noqa: PLC0415
    from nexus.pdf_extractor import PDFExtractor  # noqa: PLC0415

    _complete_root(env)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    info = {"pid": 4242, "port": 8123, "weights_root": None}
    monkeypatch.setattr(_mineru_pid, "read_pid_file", lambda: info)
    extractor = PDFExtractor()
    restarts: list[dict] = []
    monkeypatch.setattr(extractor, "_restart_broken_mineru_server", lambda i: restarts.append(i) or True)

    extractor._replace_server_with_stale_weights("http://127.0.0.1:8123")
    assert restarts == [info]

    restarts.clear()
    info["weights_root"] = str(_root())
    extractor._replace_server_with_stale_weights("http://127.0.0.1:8123")
    assert restarts == []


def test_the_availability_check_runs_the_stale_weights_check_on_a_healthy_server(env, monkeypatch):
    """The wiring: a healthy registered server goes through the replacement check before pages are sent to it."""
    from nexus.pdf_extractor import PDFExtractor  # noqa: PLC0415

    extractor = PDFExtractor()
    seen: list[str] = []
    monkeypatch.setattr(extractor, "_probe_mineru_health", lambda url: (True, ""))
    monkeypatch.setattr(extractor, "_replace_server_with_deleted_cwd", lambda url: None)
    monkeypatch.setattr(extractor, "_replace_server_with_stale_weights", seen.append)
    import nexus.config as nexus_config  # noqa: PLC0415

    monkeypatch.setattr(nexus_config, "get_mineru_server_url", lambda *a, **k: "http://127.0.0.1:8123")
    assert extractor._mineru_server_available() is True
    assert seen == ["http://127.0.0.1:8123"]


def test_spawn_server_process_gives_the_child_the_server_env_and_records_its_weights(env, monkeypatch, tmp_path):
    """The child gets exactly ``_server_env``'s result (the F35 variables ride in it), and the pid file records
    which weights that environment named."""
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    from tests._module_seam import patch_in  # noqa: PLC0415

    _complete_root(env)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    proc = MagicMock(pid=4321)
    proc.poll.return_value = None
    pid_path = tmp_path / "mineru.pid"
    with patch("nexus._mineru_spawn._resolve_mineru_api_bin", return_value="/fake/bin/mineru-api"), patch(
        "nexus._mineru_pid._pid_file_path", return_value=pid_path
    ), patch("nexus._mineru_spawn._mineru_output_root", return_value=tmp_path), patch(
        "nexus.logging_setup.open_child_log_or_devnull", return_value=MagicMock()
    ), patch_in("nexus._mineru_spawn", "subprocess.Popen", return_value=proc) as popen:
        assert _mineru_spawn.spawn_server_process(8123) is proc
    child_env = popen.call_args.kwargs["env"]
    assert child_env["MINERU_MODEL_SOURCE"] == "local"
    assert child_env["MINERU_TOOLS_CONFIG_JSON"] == str(mw.mineru_json_path())
    assert child_env["HF_HUB_OFFLINE"] == "1"
    assert child_env["MINERU_API_OUTPUT_ROOT"] == str(tmp_path)
    assert json.loads(pid_path.read_text())["weights_root"] == str(_root())
