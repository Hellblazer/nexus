# SPDX-License-Identifier: AGPL-3.0-or-later
"""The .mcpb server's launch-time update notice points at the plugin (nexus-qkb9v).

The .mcpb never updates itself, so the notice an out-of-date user sees must
name the recommended route (the conexus plugin from the Hellblazer/nexus
marketplace, which runs the installed nx) and how to switch, say how the
plugin copy itself is refreshed (remove and re-add the marketplace), and keep
the release URL fallback and the opt-out variable.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

from tests._module_seam import setattr_in

_SERVER = Path(__file__).resolve().parent.parent / "mcpb" / "src" / "server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("mcpb_server_under_test", _SERVER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_check(monkeypatch, capsys, *, installed: str, latest: str) -> str:
    mod = _load_server()
    monkeypatch.delenv("NX_MCPB_SKIP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(mod, "_pkg_version", lambda _name: installed)
    body = json.dumps({"info": {"version": latest}}).encode()
    setattr_in(monkeypatch, mod, "urllib.request.urlopen", lambda *a, **k: _Resp(body))
    mod._check_stale_install()
    return capsys.readouterr().err


def test_stale_notice_names_the_plugin_route(monkeypatch, capsys) -> None:
    err = _run_check(monkeypatch, capsys, installed="7.60.0", latest="7.82.0")
    assert "installed conexus=7.60.0" in err
    assert "latest on PyPI=7.82.0" in err
    assert "does not update itself" in err
    assert "Customize -> Plugins" in err
    assert "Hellblazer/nexus" in err
    assert "install conexus" in err
    assert "remove the Conexus extension" in err
    assert "two servers" in err
    assert "remove it and add it again" in err
    assert "nx self install" in err
    assert "releases/latest" in err
    assert "NX_MCPB_SKIP_UPDATE_CHECK=1" in err


def test_current_install_prints_nothing(monkeypatch, capsys) -> None:
    assert _run_check(monkeypatch, capsys, installed="7.82.0", latest="7.82.0") == ""


def test_opt_out_variable_silences_the_notice(monkeypatch, capsys) -> None:
    mod = _load_server()
    monkeypatch.setenv("NX_MCPB_SKIP_UPDATE_CHECK", "1")
    monkeypatch.setattr(mod, "_pkg_version", lambda _name: "7.60.0")

    def _boom(*a, **k):
        raise AssertionError("opt-out must not touch the network")

    setattr_in(monkeypatch, mod, "urllib.request.urlopen", _boom)
    mod._check_stale_install()
    assert capsys.readouterr().err == ""
