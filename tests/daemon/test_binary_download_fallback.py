# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release-asset download: retry a 5xx or timeout, then fall back to the REST
asset endpoint (nexus-h29w1 write-seam red at 336d1cf37 and e60499516).

GitHub's ``/releases/download/<tag>/<name>`` redirect returned 504 for some
assets of engine-service-v0.1.182 while
``GET /repos/<repo>/releases/assets/<id>`` (``Accept: application/octet-stream``)
served the same bytes, so one bad edge path failed every install. These tests
drive ``binary_install._download`` against a real HTTP server on port 0.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nexus.daemon import binary_install as b

TAG = "engine-service-v9.9.9"
NAME = "nexus-pg-linux-amd64.txz.sha256"
ASSET_ID = 4242
BODY = b"0123abcd  nexus-pg-linux-amd64.txz\n"


@dataclass
class _Plan:
    """What the fake GitHub answers, and what it saw."""

    primary: list[int] = field(default_factory=list)
    after: int = 504
    primary_sleep_s: float = 0.0
    api_status: int = 200
    asset_listed: bool = True
    hits: dict[str, int] = field(default_factory=dict)
    auth_seen: dict[str, str | None] = field(default_factory=dict)
    accept_seen: list[str | None] = field(default_factory=list)


def _handler(plan: _Plan) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002  quiet
            pass

        def _count(self, key: str) -> int:
            plan.hits[key] = plan.hits.get(key, 0) + 1
            plan.auth_seen[key] = self.headers.get("Authorization")
            return plan.hits[key]

        def _send(
            self, status: int, body: bytes, ctype: str = "application/octet-stream"
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            host = f"http://{self.headers['Host']}"
            if self.path == f"/dl/{TAG}/{NAME}":
                n = self._count("primary")
                if plan.primary_sleep_s and n == 1:
                    time.sleep(plan.primary_sleep_s)
                status = plan.primary[n - 1] if n <= len(plan.primary) else plan.after
                self._send(status, BODY if status == 200 else b"gateway timeout")
                return
            if self.path == f"/dl/{TAG}/other.txt":
                self._count("other")
                self._send(504, b"gateway timeout")
                return
            if self.path == f"/repos/o/r/releases/tags/{TAG}":
                self._count("tags")
                assets = (
                    [
                        {
                            "name": NAME,
                            "id": ASSET_ID,
                            "url": f"{host}/repos/o/r/releases/assets/{ASSET_ID}",
                        }
                    ]
                    if plan.asset_listed
                    else []
                )
                self._send(
                    200,
                    json.dumps({"tag_name": TAG, "assets": assets}).encode(),
                    "application/json",
                )
                return
            if self.path == f"/repos/o/r/releases/assets/{ASSET_ID}":
                self._count("asset")
                plan.accept_seen.append(self.headers.get("Accept"))
                if plan.api_status != 200:
                    self._send(plan.api_status, b"bad gateway")
                elif self.headers.get("Accept") != "application/octet-stream":
                    self._send(
                        200, b'{"json": "metadata, not bytes"}', "application/json"
                    )
                else:
                    self._send(200, BODY)
                return
            self._send(404, b"not found")

    return H


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Plan, str]]:
    plan = _Plan()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(plan))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(b, "_RELEASE_DOWNLOAD_BASE", f"{base}/dl")
    monkeypatch.setattr(b, "_RELEASE_API_BASE", f"{base}/repos/o/r")
    monkeypatch.setattr(b, "_RETRY_DELAYS_S", (0.0, 0.0))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    b._release_assets_cache.clear()
    try:
        yield plan, base
    finally:
        server.shutdown()
        server.server_close()
        b._release_assets_cache.clear()


def _url() -> str:
    return b.release_asset_url(TAG, NAME)


def test_persistent_504_falls_back_to_the_rest_asset_endpoint(
    github, tmp_path: Path
) -> None:
    plan, _ = github
    dest = tmp_path / NAME
    b._download(_url(), dest)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 3  # first try plus two retries
    assert plan.hits["tags"] == 1
    assert plan.hits["asset"] == 1
    assert plan.accept_seen == ["application/octet-stream"]


def test_a_504_that_clears_on_retry_never_reaches_the_api(
    github, tmp_path: Path
) -> None:
    plan, _ = github
    plan.primary = [504]
    plan.after = 200
    dest = tmp_path / NAME
    b._download(_url(), dest)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 2
    assert "tags" not in plan.hits and "asset" not in plan.hits


def test_a_timeout_is_retried(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.after = 200
    plan.primary_sleep_s = 1.5
    dest = tmp_path / NAME
    b._download(_url(), dest, timeout=0.5)
    assert dest.read_bytes() == BODY
    assert plan.hits["primary"] == 2


def test_404_is_absent_at_once_with_no_retry_or_fallback(
    github, tmp_path: Path
) -> None:
    plan, _ = github
    plan.primary = [404]
    with pytest.raises(b.BinaryAssetAbsentError):
        b._download(_url(), tmp_path / NAME)
    assert plan.hits["primary"] == 1
    assert "tags" not in plan.hits


def test_both_paths_failing_is_a_download_error_naming_both(
    github, tmp_path: Path
) -> None:
    plan, _ = github
    plan.api_status = 502
    with pytest.raises(b.BinaryDownloadError) as ei:
        b._download(_url(), tmp_path / NAME)
    assert not isinstance(ei.value, b.BinaryAssetAbsentError)
    msg = str(ei.value)
    assert "504" in msg and "502" in msg
    assert plan.hits["primary"] == 3 and plan.hits["asset"] == 1


def test_an_asset_the_release_does_not_list_is_absent(github, tmp_path: Path) -> None:
    plan, _ = github
    plan.asset_listed = False
    with pytest.raises(b.BinaryAssetAbsentError):
        b._download(_url(), tmp_path / NAME)
    assert plan.hits["tags"] == 1 and "asset" not in plan.hits


def test_a_url_outside_the_release_base_retries_but_has_no_fallback(
    github, tmp_path: Path
) -> None:
    plan, base = github
    with pytest.raises(b.BinaryDownloadError):
        b._download(f"{base}/dl-elsewhere/x", tmp_path / "x")
    assert "tags" not in plan.hits


def test_the_release_lookup_is_made_once_per_tag(github, tmp_path: Path) -> None:
    plan, _ = github
    b._download(_url(), tmp_path / "a")
    b._download(_url(), tmp_path / "b")
    assert plan.hits["tags"] == 1
    assert plan.hits["asset"] == 2


def test_a_token_goes_to_the_api_only(
    github, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = github
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    b._download(_url(), tmp_path / NAME)
    assert plan.auth_seen["primary"] is None
    assert plan.auth_seen["tags"] == "Bearer t0ken"
    assert plan.auth_seen["asset"] == "Bearer t0ken"


def test_a_partial_first_attempt_leaves_no_stale_bytes(github, tmp_path: Path) -> None:
    plan, _ = github
    dest = tmp_path / NAME
    dest.write_bytes(
        b"stale bytes from an earlier attempt that were longer than the body"
    )
    plan.primary = [504]
    plan.after = 200
    b._download(_url(), dest)
    assert dest.read_bytes() == BODY
