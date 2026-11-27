# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o step 3: the mint credential and the data-token lease keep to their origin.

``DataTokenManager.bearer_for`` takes the saved ``mint_token`` from the account's config and
POSTs it to ``base_url`` + ``/v1/data-tokens/mint``. The URL half is repository-choosable, so
the credential goes only to the origin it was issued for (``bound_mint_credential``), and never
over cleartext off this machine. The cross-process data-token lease, already keyed by netloc,
also records the scheme it was minted over and is read back only for that scheme.

Real port-0 ``http.server`` decoys record what arrives; no poster fake stands between the
manager and the socket.
"""
from __future__ import annotations

import ast
import contextlib
import http.server
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from nexus.db import data_token as dt
from nexus.db.data_token import DataTokenManager, DataTokenMintError, _lease_key

MANAGED = "https://api.conexus-nexus.com"
_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"


class _Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.seen.append((self.path, self.headers.get("Authorization", "")))  # type: ignore[attr-defined]
        body = json.dumps({"data_token": "minted-" + self.server.name, "expires_in_seconds": 600}).encode()  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # noqa: D102
        pass


@contextlib.contextmanager
def _server(name: str) -> Iterator[http.server.ThreadingHTTPServer]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    srv.seen = []  # type: ignore[attr-defined]
    srv.name = name  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _url(srv: http.server.ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_MINT_TOKEN", "NX_MINT_TENANT"):
        monkeypatch.delenv(k, raising=False)
    # the default poster is urllib; a stray proxy variable must not reroute a loopback decoy
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def _save(credentials: dict[str, str]) -> None:
    d = Path(os.environ["NEXUS_CONFIG_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yml").write_text(yaml.safe_dump({"credentials": credentials}), encoding="utf-8")


# ── the mint credential ──────────────────────────────────────────────────────

def test_saved_mint_credential_is_not_posted_to_another_origin():
    with _server("decoy") as decoy:
        _save({"mint_token": "MINT-SECRET", "service_url": MANAGED})
        mgr = DataTokenManager(config_dir=Path(os.environ["NEXUS_CONFIG_DIR"]))
        assert mgr.bearer_for(_url(decoy), "t1") is None
        assert decoy.seen == []  # type: ignore[attr-defined]


def test_saved_mint_credential_with_no_saved_url_is_bound_to_the_managed_default():
    with _server("decoy") as decoy:
        _save({"mint_token": "MINT-SECRET"})
        mgr = DataTokenManager(config_dir=Path(os.environ["NEXUS_CONFIG_DIR"]))
        assert mgr.bearer_for(_url(decoy), "t1") is None
        assert decoy.seen == []  # type: ignore[attr-defined]


def test_saved_mint_credential_mints_at_the_origin_it_was_saved_with():
    with _server("real") as real:
        _save({"mint_token": "MINT-SECRET", "service_url": _url(real)})
        mgr = DataTokenManager(config_dir=Path(os.environ["NEXUS_CONFIG_DIR"]))
        assert mgr.bearer_for(_url(real), "t1") == "minted-real"
        assert real.seen == [("/v1/data-tokens/mint", "Bearer MINT-SECRET")]  # type: ignore[attr-defined]


def test_decoy_and_real_in_one_process_only_the_bound_origin_sees_the_credential():
    with _server("real") as real, _server("decoy") as decoy:
        _save({"mint_token": "MINT-SECRET", "service_url": _url(real)})
        mgr = DataTokenManager(config_dir=Path(os.environ["NEXUS_CONFIG_DIR"]))
        assert mgr.bearer_for(_url(decoy), "t1") is None
        assert mgr.bearer_for(_url(real), "t1") == "minted-real"
        assert decoy.seen == []  # type: ignore[attr-defined]
        assert len(real.seen) == 1  # type: ignore[attr-defined]


def test_env_mint_credential_is_unbound_and_reaches_any_origin(monkeypatch):
    """The accepted residual: a user-exported NX_MINT_TOKEN is not bound (Sam, 2026-11-26)."""
    with _server("anywhere") as srv:
        monkeypatch.setenv("NX_MINT_TOKEN", "ENV-MINT")
        mgr = DataTokenManager(config_dir=Path(os.environ["NEXUS_CONFIG_DIR"]))
        assert mgr.bearer_for(_url(srv), "t1") == "minted-anywhere"
        assert srv.seen == [("/v1/data-tokens/mint", "Bearer ENV-MINT")]  # type: ignore[attr-defined]


def test_injected_mint_credential_is_the_callers_and_is_not_origin_checked():
    with _server("anywhere") as srv:
        mgr = DataTokenManager(mint_credential=lambda: "INJECTED", config_dir=Path(os.environ["NEXUS_CONFIG_DIR"]))
        assert mgr.bearer_for(_url(srv), "t1") == "minted-anywhere"
        assert srv.seen == [("/v1/data-tokens/mint", "Bearer INJECTED")]  # type: ignore[attr-defined]


def test_is_configured_is_presence_only():
    _save({"mint_token": "MINT-SECRET", "service_url": MANAGED})
    assert DataTokenManager(config_dir=Path(os.environ["NEXUS_CONFIG_DIR"])).is_configured() is True
    assert DataTokenManager(config_dir=Path(os.environ["NEXUS_CONFIG_DIR"]), mint_credential=lambda: "").is_configured() is False


@pytest.mark.expects_unresolvable_endpoint
@pytest.mark.parametrize("injected", [None, lambda: "INJECTED"])
def test_no_mint_credential_over_cleartext_off_machine(monkeypatch, injected):
    """http to a host that is not this machine: no POST is made, whoever supplied the credential."""
    monkeypatch.setenv("NX_MINT_TOKEN", "ENV-MINT")
    sent: list[str] = []

    def poster(url, headers, body):  # pragma: no cover - must not run
        sent.append(url)
        return 200, {"data_token": "x"}, {}

    mgr = DataTokenManager(mint_credential=injected, poster=poster, config_dir=Path(os.environ["NEXUS_CONFIG_DIR"]))
    with pytest.raises(DataTokenMintError) as err:
        mgr.bearer_for("http://203.0.113.9:8080", "t1")
    assert sent == []
    assert "203.0.113.9" not in str(err.value)


def test_https_off_machine_still_mints(monkeypatch):
    monkeypatch.setenv("NX_MINT_TOKEN", "ENV-MINT")
    seen: list[tuple[str, str]] = []

    def poster(url, headers, body):
        seen.append((url, headers["Authorization"]))
        return 200, {"data_token": "tok", "expires_in_seconds": 600}, {}

    mgr = DataTokenManager(poster=poster, config_dir=Path(os.environ["NEXUS_CONFIG_DIR"]))
    assert mgr.bearer_for("https://engine.example", "t1") == "tok"
    assert seen == [("https://engine.example/v1/data-tokens/mint", "Bearer ENV-MINT")]


# ── the data-token lease ─────────────────────────────────────────────────────

def _write_lease_file(config_dir: Path, base_url: str, *, scheme: str | None | object = ..., tenant: str = "t1") -> Path:
    payload: dict[str, object] = {
        "format_version": dt._LEASE_FORMAT_VERSION,
        "token": "LEASED",
        "tenant": tenant,
        "base_url_digest": _lease_key(base_url, tenant),
        "expires_at": time.time() + 600,
        "ttl_seconds": 600.0,
    }
    if scheme is not ...:
        payload["scheme"] = scheme
    config_dir.mkdir(parents=True, exist_ok=True)
    path = dt._data_token_lease_path(base_url, tenant, config_dir)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("lease_scheme", "url", "ok"),
    [
        ("https", "https://engine.example", True),
        ("http", "http://127.0.0.1:9", True),
        ("https", "http://engine.example", False),   # same host, other scheme
        ("http", "https://engine.example", False),
        (None, "https://engine.example", True),      # an old lease: https is fine
        (None, "http://127.0.0.1:9", True),          # an old lease: loopback is fine
        (None, "http://localhost:9", True),
        (None, "http://[::1]:9", True),
        (None, "http://engine.example", False),      # an old lease never serves cleartext off this machine
        (None, "http://203.0.113.9:80", False),
        ("", "https://engine.example", False),       # a present but empty field is not "absent"
        (7, "https://engine.example", False),
        ("https", "http://user:pw@127.0.0.1:9", False),
        (None, "http://user:pw@127.0.0.1:9", True),   # judged on urlsplit, as the netloc key is
        (None, "http://127.0.0.1\\@evil.example", False),
        ("http", "http://[::1", False),
    ],
)
def test_lease_scheme_rule(lease_scheme, url, ok):
    assert dt._lease_scheme_ok(lease_scheme, url) is ok


def test_written_lease_records_its_scheme(tmp_path):
    with _server("real") as real:
        cfg = tmp_path / "cfg"
        mgr = DataTokenManager(mint_credential=lambda: "M", config_dir=cfg)
        assert mgr.bearer_for(_url(real), "t1") == "minted-real"
        path = dt._data_token_lease_path(_url(real), "t1", cfg)
        assert json.loads(path.read_text())["scheme"] == "http"


def test_cli_reader_refuses_a_lease_minted_over_the_other_scheme(tmp_path):
    cfg = tmp_path / "cfg"
    # a lease for the netloc, minted over https; the same netloc over http must not borrow it
    _write_lease_file(cfg, "http://127.0.0.1:9", scheme="https")
    mgr = DataTokenManager(mint_credential=lambda: "M", config_dir=cfg)
    assert mgr.fresh_lease_token("http://127.0.0.1:9", "t1") is None
    assert mgr.has_fresh_lease("http://127.0.0.1:9", "t1") is False


def test_cli_reader_accepts_a_matching_scheme_and_an_old_loopback_lease(tmp_path):
    cfg = tmp_path / "cfg"
    _write_lease_file(cfg, "http://127.0.0.1:9", scheme="http")
    _write_lease_file(cfg, "http://127.0.0.1:10")  # no scheme field: written before this change
    mgr = DataTokenManager(mint_credential=lambda: "M", config_dir=cfg)
    assert mgr.fresh_lease_token("http://127.0.0.1:9", "t1") == "LEASED"
    assert mgr.fresh_lease_token("http://127.0.0.1:10", "t1") == "LEASED"


def test_cli_reader_old_lease_serves_https_but_not_cleartext_off_machine(tmp_path):
    cfg = tmp_path / "cfg"
    _write_lease_file(cfg, "https://engine.example")
    _write_lease_file(cfg, "http://engine.example")
    mgr = DataTokenManager(mint_credential=lambda: "M", config_dir=cfg)
    assert mgr.fresh_lease_token("https://engine.example", "t1") == "LEASED"
    assert mgr.fresh_lease_token("http://engine.example", "t1") is None


def test_old_lease_is_not_rewritten_by_a_read_and_gains_the_field_when_next_written(tmp_path):
    with _server("real") as real:
        cfg = tmp_path / "cfg"
        base = _url(real)
        path = _write_lease_file(cfg, base)
        before = path.read_bytes()
        mgr = DataTokenManager(mint_credential=lambda: "M", config_dir=cfg)
        assert mgr.fresh_lease_token(base, "t1") == "LEASED"
        assert path.read_bytes() == before  # no boot-time rewrite, no read-time rewrite
        assert real.seen == []  # type: ignore[attr-defined]
        # an expired-by-threshold read forces the next mint, which writes the new shape
        mgr2 = DataTokenManager(mint_credential=lambda: "M", config_dir=cfg, wall_clock=lambda: time.time() + 590)
        assert mgr2.bearer_for(base, "t1") == "minted-real"
        assert json.loads(path.read_text())["scheme"] == "http"


# ── the plugin's stdlib reader ───────────────────────────────────────────────

def _plugin_reader(tmp_path: Path, cfg: Path, url: str) -> str:
    """Run read_data_token_lease under bare python (no nexus import), as a hook would."""
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(_SCRIPTS)!r})\n"
        "import _endpoint_resolve as e\n"
        "from pathlib import Path\n"
        f"r = e.read_data_token_lease(Path({str(cfg)!r}), {url!r}, tenant='t1')\n"
        "assert 'nexus' not in sys.modules\n"
        "print(json.dumps(r))\n"
    )
    out = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, check=True, cwd=tmp_path)
    return json.loads(out.stdout.strip())


@pytest.mark.parametrize(
    ("lease_scheme", "url", "token"),
    [
        ("http", "http://127.0.0.1:9", "LEASED"),
        ("https", "http://127.0.0.1:9", None),
        ("https", "https://engine.example", "LEASED"),
        ("http", "https://engine.example", None),
        (..., "https://engine.example", "LEASED"),
        (..., "http://127.0.0.1:9", "LEASED"),
        (..., "http://localhost:9", "LEASED"),
        (..., "http://engine.example", None),
        (..., "http://203.0.113.9", None),
        ("http", "http://user:pw@127.0.0.1:9", "LEASED"),
    ],
)
def test_plugin_reader_applies_the_same_scheme_rule(tmp_path, lease_scheme, url, token):
    cfg = tmp_path / "cfg"
    _write_lease_file(cfg, url, scheme=lease_scheme)
    assert _plugin_reader(tmp_path, cfg, url) == token


def test_plugin_and_cli_scheme_helpers_are_the_same_text():
    sys.path.insert(0, str(_SCRIPTS))
    try:
        import _endpoint_resolve as hook  # noqa: PLC0415
    finally:
        sys.path.remove(str(_SCRIPTS))

    def body(fn) -> str:
        node = ast.parse(inspect.getsource(fn).lstrip()).body[0]
        assert isinstance(node, ast.FunctionDef)
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            node.body = node.body[1:]
        node.name = "f"
        return ast.dump(node)

    assert body(dt._lease_scheme_ok) == body(hook.lease_scheme_ok)
