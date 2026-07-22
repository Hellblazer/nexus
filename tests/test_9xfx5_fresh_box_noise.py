# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-9xfx5: a virgin install's first ``nx doctor`` must not look broken.

MinerU half (the ladder half is pinned in
``tests/test_init_cmd.py::TestLadderConvergence``): the doctor leg used to
probe the built-in default URL (http://127.0.0.1:8010) on boxes where no
MinerU server was ever provisioned, rendering a red ✗ ("unreachable ...
OOM-risk") for every fresh install. Provisioned-ness is now an explicit
predicate (``nexus.config.mineru_server_provisioned``): an explicit
non-default ``pdf.mineru_server_url`` OR a live pid-file server. Doctor
probes only when provisioned; a ✗ now means a provisioned server is
actually unreachable.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(d))
    return d


def test_unprovisioned_on_fresh_config(cfg_dir: Path, monkeypatch):
    import nexus.config as config

    monkeypatch.setattr(config, "_read_live_mineru_port", lambda: None)
    assert config.mineru_server_provisioned() is False


def test_provisioned_via_explicit_config_url(cfg_dir: Path, monkeypatch):
    import nexus.config as config

    (cfg_dir / "config.yml").write_text(
        "pdf:\n  mineru_server_url: http://127.0.0.1:9999\n"
    )
    monkeypatch.setattr(config, "_read_live_mineru_port", lambda: None)
    assert config.mineru_server_provisioned() is True


def test_provisioned_via_live_pid_file(cfg_dir: Path, monkeypatch):
    import nexus.config as config

    monkeypatch.setattr(config, "_read_live_mineru_port", lambda: 5555)
    assert config.mineru_server_provisioned() is True


def test_doctor_renders_unprovisioned_as_skip_not_red_x(cfg_dir: Path, monkeypatch, capsys):
    """The doctor leg must take the graceful not-configured branch on an
    unprovisioned box — no probe of the built-in default URL, no ✗."""
    import nexus.config as config
    from nexus.commands.doctor import _run_check_mineru

    monkeypatch.setattr(config, "_read_live_mineru_port", lambda: None)

    probed: list[str] = []

    class _NoProbe:
        def __init__(self, *a, **kw):
            probed.append("constructed")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            probed.append(url)
            raise AssertionError("must not probe an unprovisioned URL")

    monkeypatch.setattr("httpx.Client", _NoProbe)
    _run_check_mineru()
    out = capsys.readouterr().out
    assert "no mineru-api server configured" in out
    assert "unreachable" not in out
    assert probed == [], "unprovisioned box must not probe the default URL"


def test_doctor_still_probes_when_provisioned(cfg_dir: Path, monkeypatch, capsys):
    """A provisioned-but-dead server must STILL render the red ✗ — the fix
    must not blind doctor to genuine failures."""
    import nexus.config as config
    from nexus.commands.doctor import _run_check_mineru

    (cfg_dir / "config.yml").write_text(
        "pdf:\n  mineru_server_url: http://127.0.0.1:9999\n"
    )
    monkeypatch.setattr(config, "_read_live_mineru_port", lambda: None)

    class _Dead:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise ConnectionError("dead")

    monkeypatch.setattr("httpx.Client", _Dead)
    _run_check_mineru()
    out = capsys.readouterr().out
    assert "unreachable" in out
