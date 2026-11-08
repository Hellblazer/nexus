# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release-asset download: retry a 429, 5xx, timeout or short body, then fall
back to the REST asset endpoint (nexus-h29w1: the write-seam gate was red at
336d1cf37 and e60499516).

GitHub's ``/releases/download/<tag>/<name>`` redirect returned 504 for some
assets of engine-service-v0.1.182 while
``GET /repos/<repo>/releases/assets/<id>`` (``Accept: application/octet-stream``)
served the same bytes, so one bad edge path failed every install. These tests
drive ``binary_install._download`` and the bge downloader against real HTTP
servers on port 0: a fake GitHub, and a second origin standing in for the
storage host the asset endpoint redirects to.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from nexus.daemon import binary_install as b
from nexus.db import service_bge_model as sbm

TAG = "engine-service-v9.9.9"
NAME = "nexus-pg-linux-amd64.txz.sha256"
ASSET_ID = 4242
BODY = b"0123abcd  nexus-pg-linux-amd64.txz\n"
TOKEN = "t0ken-for-tests"


@dataclass
class _Plan:
    """What the fake GitHub answers, and what each path saw."""

    primary: list[int] = field(default_factory=list)  # per-attempt status; 0 = short body
    after: int = 504  # status once ``primary`` is used up
    primary_sleep_s: float = 0.0
    sleep_on_attempt: dict[int, float] = field(default_factory=dict)
    listing_grows: bool = False  # the first listing lacks NAME, later ones carry it
    api_status: int = 200
    tags_status: int = 200
    reject_token: bool = False  # 401 for any request carrying Authorization
    asset_listed: bool = True
    asset_on_storage_host: bool = False  # the listing names a non-API host
    hits: dict[str, int] = field(default_factory=dict)
    auth_seen: dict[str, list[str | None]] = field(default_factory=dict)
    accept_seen: dict[str, list[str | None]] = field(default_factory=dict)


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _record(plan: _Plan, key: str, h: BaseHTTPRequestHandler) -> int:
    plan.hits[key] = plan.hits.get(key, 0) + 1
    plan.auth_seen.setdefault(key, []).append(h.headers.get("Authorization"))
    plan.accept_seen.setdefault(key, []).append(h.headers.get("Accept"))
    return plan.hits[key]


def _storage_handler(plan: _Plan) -> type[BaseHTTPRequestHandler]:
    class Storage(_Quiet):
        def do_GET(self) -> None:  # noqa: N802
            key = "storage" if self.path.startswith("/blob/") else "listed-elsewhere"
            _record(plan, key, self)
            self._send(200, BODY)

    return Storage


def _github_handler(plan: _Plan, storage: list[str]) -> type[BaseHTTPRequestHandler]:
    class GitHub(_Quiet):
        def do_GET(self) -> None:  # noqa: N802
            host = f"http://{self.headers['Host']}"
            path = self.path
            if path in (f"/dl/{TAG}/{NAME}", f"/dl/{TAG}/model.onnx"):
                n = _record(plan, "primary", self)
                if plan.primary_sleep_s and n == 1:
                    time.sleep(plan.primary_sleep_s)
                if n in plan.sleep_on_attempt:
                    time.sleep(plan.sleep_on_attempt[n])
                status = plan.primary[n - 1] if n <= len(plan.primary) else plan.after
                if status == 1:  # RFC-noncompliant: Content-Length and chunked both
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(BODY) * 4))
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(f"{len(BODY):x}\r\n".encode() + BODY + b"\r\n0\r\n\r\n")
                    return
                if status == 0:  # headers promise more than the body carries
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(BODY) * 4))
                    self.end_headers()
                    self.wfile.write(BODY[:7])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self._send(status, BODY if status == 200 else b"upstream says no")
                return
            if path == f"/elsewhere/{TAG}/{NAME}":
                _record(plan, "elsewhere", self)
                self._send(504, b"gateway timeout")
                return
            if path == f"/repos/o/r/releases/tags/{TAG}":
                n_tags = _record(plan, "tags", self)
                if plan.reject_token and self.headers.get("Authorization"):
                    self._send(401, b'{"message": "Bad credentials"}', "application/json")
                    return
                if plan.tags_status != 200:
                    self._send(plan.tags_status, b'{"message": "API rate limit exceeded"}', "application/json")
                    return
                url = (
                    f"{storage[0]}/listed/{ASSET_ID}"
                    if plan.asset_on_storage_host
                    else f"{host}/repos/o/r/releases/assets/{ASSET_ID}"
                )
                names = [NAME, "model.onnx"] if plan.asset_listed else []
                if plan.listing_grows and n_tags == 1:
                    names = ["model.onnx"]
                assets = [{"name": nm, "id": ASSET_ID, "url": url} for nm in names]
                self._send(200, json.dumps({"tag_name": TAG, "assets": assets}).encode(), "application/json")
                return
            if path == f"/repos/o/r/releases/assets/{ASSET_ID}":
                _record(plan, "asset", self)
                if plan.reject_token and self.headers.get("Authorization"):
                    self._send(401, b'{"message": "Bad credentials"}', "application/json")
                elif plan.api_status != 200:
                    self._send(plan.api_status, b"bad gateway")
                elif self.headers.get("Accept") != "application/octet-stream":
                    self._send(200, b'{"json": "metadata, not bytes"}', "application/json")
                else:  # the real endpoint redirects to a signed storage URL
                    self.send_response(302)
                    self.send_header("Location", f"{storage[0]}/blob/{ASSET_ID}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                return
            self._send(404, b"not found")

    return GitHub


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Plan, str]]:
    plan = _Plan()
    storage_url: list[str] = []
    storage, s_base = _serve(_storage_handler(plan))
    storage_url.append(s_base)
    server, base = _serve(_github_handler(plan, storage_url))
    monkeypatch.setattr(b, "_RELEASE_DOWNLOAD_BASE", f"{base}/dl")
    monkeypatch.setattr(b, "_RELEASE_API_BASE", f"{base}/repos/o/r")
    monkeypatch.setattr(b, "_RETRY_DELAYS_S", (0.0, 0.0))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    b._release_assets_cache.clear()
    b._rejected_api_tokens.clear()
    try:
        yield plan, base
    finally:
        for srv in (server, storage):
            srv.shutdown()
            srv.server_close()
        b._release_assets_cache.clear()
        b._rejected_api_tokens.clear()


def _url() -> str:
    return b.release_asset_url(TAG, NAME)


# -- recovery ---------------------------------------------------------------


def test_persistent_504_falls_back_to_the_rest_asset_endpoint(github, tmp_path: Path) -> None:
    plan, _ = github
    dest = tmp_path / NAME
    b._download(_url(), dest)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 3  # first try plus two retries
    assert plan.hits["tags"] == 1 and plan.hits["asset"] == 1
    assert plan.hits["storage"] == 1  # the asset endpoint's redirect was followed
    assert plan.accept_seen["asset"] == ["application/octet-stream"]


def test_a_504_that_clears_on_retry_never_reaches_the_api(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.primary = [504]
    plan.after = 200
    dest = tmp_path / NAME
    b._download(_url(), dest)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 2
    assert "tags" not in plan.hits and "asset" not in plan.hits


def test_a_429_is_retried(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.primary = [429]
    plan.after = 200
    b._download(_url(), tmp_path / NAME)
    assert plan.hits["primary"] == 2 and "tags" not in plan.hits


def test_a_timeout_is_retried(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.after = 200
    plan.primary_sleep_s = 1.5
    dest = tmp_path / NAME
    b._download(_url(), dest, timeout=0.5)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 2


def test_a_short_body_is_retried_and_leaves_only_the_full_asset(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.primary = [0]  # Content-Length says more than arrives, then the socket closes
    plan.after = 200
    dest = tmp_path / NAME
    b._download(_url(), dest)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 2 and "tags" not in plan.hits


# -- no retry, no fallback ---------------------------------------------------


def test_404_is_absent_at_once_with_no_retry_or_fallback(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.primary = [404]
    with pytest.raises(b.BinaryAssetAbsentError):
        b._download(_url(), tmp_path / NAME)
    assert plan.hits["primary"] == 1 and "tags" not in plan.hits


def test_403_fails_at_once_with_no_retry_or_fallback(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.primary = [403]
    with pytest.raises(b.BinaryDownloadError) as ei:
        b._download(_url(), tmp_path / NAME)
    assert not isinstance(ei.value, b.BinaryAssetAbsentError)
    assert plan.hits["primary"] == 1 and "tags" not in plan.hits


def test_a_local_write_failure_is_not_retried(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.after = 200
    with pytest.raises(b.BinaryDownloadError, match="failed to write"):
        b._download(_url(), tmp_path / "no-such-dir" / NAME)
    assert plan.hits["primary"] == 1 and "tags" not in plan.hits


def test_a_url_outside_the_release_base_retries_but_has_no_fallback(github, tmp_path: Path) -> None:
    plan, base = github
    with pytest.raises(b.BinaryDownloadError) as ei:
        b._download(f"{base}/elsewhere/{TAG}/{NAME}", tmp_path / NAME)
    assert not isinstance(ei.value, b.BinaryAssetAbsentError)
    assert plan.hits["elsewhere"] == 3
    assert "tags" not in plan.hits


# -- fallback failures -------------------------------------------------------


def test_both_paths_failing_is_a_download_error_naming_both(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.api_status = 502
    with pytest.raises(b.BinaryDownloadError) as ei:
        b._download(_url(), tmp_path / NAME)
    assert not isinstance(ei.value, b.BinaryAssetAbsentError)
    msg = str(ei.value)
    assert "504" in msg and "502" in msg
    assert plan.hits["primary"] == 3 and plan.hits["asset"] == 1


def test_an_api_refusal_names_the_token_remedy(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.tags_status = 403
    with pytest.raises(b.BinaryDownloadError, match="GITHUB_TOKEN"):
        b._download(_url(), tmp_path / NAME)


def test_an_asset_the_release_does_not_list_is_absent(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.asset_listed = False
    with pytest.raises(b.BinaryAssetAbsentError):
        b._download(_url(), tmp_path / NAME)
    assert plan.hits["tags"] == 1 and "asset" not in plan.hits


def test_the_release_lookup_is_made_once_per_tag(github, tmp_path: Path) -> None:
    plan, _ = github
    b._download(_url(), tmp_path / "a")
    b._download(_url(), tmp_path / "b")
    assert plan.hits["tags"] == 1 and plan.hits["asset"] == 2


# -- the token ---------------------------------------------------------------


def test_the_token_reaches_the_api_and_never_the_storage_host(
    github, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = github
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    b._download(_url(), tmp_path / NAME)
    assert plan.auth_seen["primary"] == [None, None, None]
    assert plan.auth_seen["tags"] == [f"Bearer {TOKEN}"]
    assert plan.auth_seen["asset"] == [f"Bearer {TOKEN}"]
    assert plan.auth_seen["storage"] == [None]  # dropped on the 302
    assert plan.accept_seen["storage"] == ["application/octet-stream"]


def test_a_rejected_token_is_retried_anonymously_and_not_sent_again(
    github, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = github
    plan.reject_token = True
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    dest = tmp_path / NAME
    b._download(_url(), dest)
    assert dest.read_bytes() == BODY
    assert plan.auth_seen["tags"] == [f"Bearer {TOKEN}", None]
    assert plan.auth_seen["asset"] == [None]


def test_the_token_is_not_sent_to_a_host_the_listing_names_outside_the_api(
    github, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = github
    plan.asset_on_storage_host = True
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    b._download(_url(), tmp_path / NAME)
    assert plan.auth_seen["listed-elsewhere"] == [None]


# -- the bge model downloader -------------------------------------------------


def test_bge_download_falls_back_and_httpx_drops_the_token_on_redirect(
    github, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, base = github
    monkeypatch.setattr(sbm, "_ASSET_DOWNLOAD_BASE", f"{base}/dl")
    monkeypatch.setattr(sbm, "_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(sbm, "_retry_sleep", lambda _s: None)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    dest = tmp_path / "model.onnx"
    sbm._httpx_stream(f"{base}/dl/{TAG}/model.onnx", dest)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 2 and plan.hits["asset"] == 1
    assert plan.auth_seen["asset"] == [f"Bearer {TOKEN}"]
    assert plan.auth_seen["storage"] == [None]


def test_bge_download_raises_the_original_failure_when_the_fallback_fails(
    github, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, base = github
    plan.api_status = 502
    monkeypatch.setattr(sbm, "_ASSET_DOWNLOAD_BASE", f"{base}/dl")
    monkeypatch.setattr(sbm, "_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(sbm, "_retry_sleep", lambda _s: None)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        sbm._httpx_stream(f"{base}/dl/{TAG}/model.onnx", tmp_path / "model.onnx")
    assert ei.value.response.status_code == 504
    assert plan.hits["asset"] == 1


# -- review r2 pins ------------------------------------------------------------


def test_retries_use_the_short_per_operation_timeout(
    github, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = github
    monkeypatch.setattr(b, "_RETRY_TIMEOUT_S", 0.3)
    plan.primary = [504]
    plan.after = 200
    plan.sleep_on_attempt = {2: 1.0}  # within the first attempt's timeout, past the retries'
    dest = tmp_path / NAME
    b._download(_url(), dest, timeout=5.0)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 3


def test_content_length_with_chunked_encoding_is_accepted(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.primary = [1]
    dest = tmp_path / NAME
    b._download(_url(), dest)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 1


def test_a_listing_without_the_asset_is_fetched_again(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.listing_grows = True
    b._download(f"{b._RELEASE_DOWNLOAD_BASE}/{TAG}/model.onnx", tmp_path / "m")  # caches the short listing
    b._download(_url(), tmp_path / NAME)
    assert (tmp_path / NAME).read_bytes() == BODY
    assert plan.hits["tags"] == 2


def test_bge_failed_fallback_leaves_no_part_and_no_dest(
    github, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, base = github
    plan.api_status = 502
    monkeypatch.setattr(sbm, "_ASSET_DOWNLOAD_BASE", f"{base}/dl")
    monkeypatch.setattr(sbm, "_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(sbm, "_retry_sleep", lambda _s: None)
    dest = tmp_path / "model.onnx"
    with pytest.raises(httpx.HTTPStatusError):
        sbm._httpx_stream(f"{base}/dl/{TAG}/model.onnx", dest)
    assert not dest.exists()
    assert not dest.with_suffix(".onnx.part").exists()
