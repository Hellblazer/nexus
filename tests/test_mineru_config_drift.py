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

import inspect
from importlib.metadata import version as _pkg_version

import pytest

from nexus import _mineru_spawn
from nexus.config import _read_live_mineru_port as _live_port


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
        "nexus._mineru_pid.is_process_alive", return_value=False,
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


def test_url_explicit_nondefault_config_wins_over_live_pid(
    tmp_path: Path, monkeypatch,
) -> None:
    """RDR-148 Gap 1: an explicit, non-default operator override wins
    over a live local pid file. The operator manages the server
    out-of-band (remote host / fixed URL); a live pid must not hijack
    that intent (CA-2 precedence inversion fix)."""
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    # PID file says a live local server is up...
    pid_path = tmp_path / "mineru.pid"
    pid_path.write_text(json.dumps({
        "pid": os.getpid(),
        "port": 49353,
    }))
    # ...but the operator explicitly pointed config elsewhere.
    with patch(
        "nexus.config.get_pdf_config",
        return_value=type("X", (), {
            "mineru_server_url": "http://mineru.internal:9999",
        })(),
    ):
        from nexus.config import get_mineru_server_url
        assert get_mineru_server_url() == "http://mineru.internal:9999"


def test_url_prefers_live_pid_when_config_is_default(
    tmp_path: Path, monkeypatch,
) -> None:
    """When config is left at the built-in default, a live pid file
    (ephemeral port from ``nx mineru start``) wins."""
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    pid_path = tmp_path / "mineru.pid"
    pid_path.write_text(json.dumps({
        "pid": os.getpid(),
        "port": 49353,
    }))
    with patch(
        "nexus.config.get_pdf_config",
        return_value=type("X", (), {
            "mineru_server_url": "http://127.0.0.1:8010",
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
        "nexus._mineru_pid.is_process_alive", return_value=False,
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


# ── get_mineru_configured_fixed_port (start-path/read-path seam) ──────


def test_configured_fixed_port_returns_port_for_nondefault_local_url(monkeypatch) -> None:
    with patch(
        "nexus.config.get_pdf_config",
        return_value=type("X", (), {
            "mineru_server_url": "http://127.0.0.1:53947",
        })(),
    ):
        from nexus.config import get_mineru_configured_fixed_port
        assert get_mineru_configured_fixed_port() == 53947


def test_configured_fixed_port_none_for_default_url(monkeypatch) -> None:
    with patch(
        "nexus.config.get_pdf_config",
        return_value=type("X", (), {
            "mineru_server_url": "http://127.0.0.1:8010",
        })(),
    ):
        from nexus.config import get_mineru_configured_fixed_port
        assert get_mineru_configured_fixed_port() is None


def test_configured_fixed_port_none_for_remote_host(monkeypatch) -> None:
    """A remote/out-of-band URL (e.g. launchctl, another host) has
    nothing local to bind — must not be treated as a local port."""
    with patch(
        "nexus.config.get_pdf_config",
        return_value=type("X", (), {
            "mineru_server_url": "http://mineru.internal:9999",
        })(),
    ):
        from nexus.config import get_mineru_configured_fixed_port
        assert get_mineru_configured_fixed_port() is None


def test_configured_fixed_port_accepts_localhost_hostname(monkeypatch) -> None:
    with patch(
        "nexus.config.get_pdf_config",
        return_value=type("X", (), {
            "mineru_server_url": "http://localhost:53947",
        })(),
    ):
        from nexus.config import get_mineru_configured_fixed_port
        assert get_mineru_configured_fixed_port() == 53947


def test_mineru_start_binds_configured_port_not_random_free_port(monkeypatch) -> None:
    """The actual regression (nexus incident 2026-07-01): a bare `nx
    mineru start` (--port 0, i.e. auto-assign) with a non-default fixed
    port in config must bind THAT port, not a random one — otherwise
    the live server and get_mineru_server_url() disagree, and the
    server is invisible to `nx doctor` / the PDF pipeline despite
    reporting a successful start.

    Exercises start()'s actual port-selection branch (not just the
    config helper in isolation) by monkeypatching the subprocess spawn
    and health poll so no real mineru-api binary is needed.
    """
    import nexus.commands.mineru as mineru_mod
    from click.testing import CliRunner

    # start()'s import of get_mineru_configured_fixed_port is deferred
    # (inside the function body), so patch the source in nexus.config —
    # matching the module's own deferred-import style, not a
    # module-level attribute on mineru.py that doesn't exist pre-call.
    monkeypatch.setattr(
        "nexus.config.get_mineru_configured_fixed_port",
        lambda: 53947,
    )
    monkeypatch.setattr(mineru_mod, "_read_pid_file", lambda: None)
    monkeypatch.setattr(mineru_mod, "_resolve_mineru_api_bin", lambda: "/bin/true")
    monkeypatch.setattr(mineru_mod, "_mineru_output_root", lambda: Path("/tmp"))
    monkeypatch.setattr(mineru_mod, "_server_env", lambda output_root: {})

    captured_cmd = {}

    class _FakeProc:
        pid = 12345
        def poll(self):
            return None

    def _fake_popen(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(mineru_mod.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(mineru_mod, "_write_pid_file", lambda *a, **kw: None)

    class _FakeResp:
        status_code = 200

    monkeypatch.setattr(mineru_mod.httpx, "get", lambda *a, **kw: _FakeResp())

    runner = CliRunner()
    result = runner.invoke(mineru_mod.start, [])

    assert result.exit_code == 0, result.output
    assert "port 53947" in result.output
    assert "53947" in captured_cmd["cmd"], (
        f"start() must bind the configured fixed port, not a random one; got argv {captured_cmd['cmd']}"
    )


# ── nexus-yq3vk: the pid file must prove IDENTITY, not just liveness ─────────
#
# get_mineru_server_url step 2 adopts whatever server the shared config dir has
# registered, and the only check was is_process_alive() — existence. Measured on
# a real box: ~/.config/nexus/mineru.pid pointed at a server the INSTALLED tool
# started under mineru 3.4.4, while the develop checkout pinned 3.1.11. Every
# `uv run` from develop therefore extracted through 3.4.4, so uv.lock did not
# control the code that produced the text, and one PDF yielded three different
# outputs on one machine. Different extraction is a data-correctness difference.


def _pidfile(tmp_path: Path, monkeypatch, **extra) -> None:
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    (tmp_path / "mineru.pid").write_text(json.dumps({
        "pid": os.getpid(), "port": 49353, **extra,
    }))


def test_matching_version_is_adopted(tmp_path: Path, monkeypatch) -> None:
    _pidfile(tmp_path, monkeypatch,
             mineru_version=_pkg_version("mineru"), python="/some/python")
    assert _live_port() == 49353


def test_skewed_version_is_REFUSED(tmp_path: Path, monkeypatch) -> None:
    """The whole point: a server running a different mineru must not be used.

    Refusing returns None, so resolution falls through to the default and a
    matching server gets spawned — rather than silently extracting through a
    version this environment does not pin.
    """
    _pidfile(tmp_path, monkeypatch,
             mineru_version="0.0.0-not-ours", python="/other/python")
    assert _live_port() is None


def test_legacy_pidfile_without_the_field_still_works(
    tmp_path: Path, monkeypatch,
) -> None:
    """Backward compatibility, deliberately.

    A pid file written before this change records no version. Refusing those
    would break every existing install until the user restarted the server, for
    a mismatch we have no evidence of. It is adopted, with a debug breadcrumb —
    absent identity is not matching identity, and the log says so.
    """
    _pidfile(tmp_path, monkeypatch)
    assert _live_port() == 49353


def test_spawn_records_version_and_interpreter(tmp_path: Path) -> None:
    """The write side: without these fields the read side can never compare."""
    src = inspect.getsource(_mineru_spawn)
    assert '"mineru_version"' in src, "spawn must stamp the mineru version"
    assert '"python": sys.executable' in src, "spawn must stamp the interpreter"


def test_virtual_vram_unit_is_gb_and_sane() -> None:
    """MINERU_VIRTUAL_VRAM_SIZE is in GB — mineru's get_vram() returns it raw
    and compares against a GB batch-ratio ladder (>=8 -> 4, >=16 -> 8, ...).

    nexus-xyk9o (2026-08-02): the value "8192" (an MB-looking figure) read as
    8192 GB, selected the maximum MFR batch ratio, and every server generation
    was silently memory-killed mid formula batch — the whole election/
    rediscovery ladder then worked as designed while every server it spawned
    died on the first formula-dense window. Pin the value to a figure that is
    only plausible as GB so a unit regression cannot re-ship quietly.
    """
    from nexus._mineru_spawn import _server_env

    vram = int(_server_env(Path("/tmp"))["MINERU_VIRTUAL_VRAM_SIZE"])
    assert 1 <= vram <= 64, (
        f"MINERU_VIRTUAL_VRAM_SIZE={vram}: not plausible as a GB budget — "
        "mineru reads this raw in GB; an MB-unit value maxes the MFR batch "
        "ratio and gets the server OOM-killed (nexus-xyk9o)"
    )
