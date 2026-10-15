# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/launch/nx_setup.py: the credentials page for mode ``cloud`` (RDR-228 Phase 2
Step 5, nexus-sv2wx.18; residuals 5, 10 and 11 of nexus-9i4d4).

One test per requirement of "The credentials page" in the record. Three kinds:

* Page cases drive a :class:`CredentialsPage` in this process: a clock the test owns, an
  "open it" seam the test replaces, a small local service on a port the system picks.
* Run cases start the real script as a subprocess through the file's ``Box`` (the stand-in
  ``uv`` and ``nx`` programs of ``test_nx_setup.py``), act as the browser against the page
  the run serves, and read what the run left behind.
* Cases about the check call :func:`check_pair` against the same small service. The check
  against a REAL engine is in ``tests/test_nx_setup_credentials_engine.py`` (it needs the
  engine substrate; this file runs in jobs that have none).

Nothing here opens a browser: ``Box.env`` sets ``NX_SETUP_OPEN_RECORD``, which makes a run
write the opener file's path to a file instead of asking the system, and the in-process
cases pass their own ``opener``. Test tokens are generated here. Nothing reads or writes
the developer's own config directory.
"""
from __future__ import annotations

import ast
import html.parser
import http.client
import http.server
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.hooks.test_nx_setup import (
    Box,
    _alive,
    _in_process,
    _load_module,
    _retry,
    _terminate,
    _wait_for,
)
from tests._platform import IS_WINDOWS, assert_owner_only

_ROOT = Path(__file__).resolve().parents[2]
_SETUP = _ROOT / "conexus" / "launch" / "nx_setup.py"
_RECORD = _ROOT / "docs" / "rdr" / "rdr-228-plugin-installs-the-cli.md"

_SIX = (
    "waiting for credentials",
    "page not loaded",
    "credentials rejected, waiting for another try",
    "credentials accepted",
    "credentials saved",
    "timed out",
)
_ADDRESS = re.compile(r"http://127\.0\.0\.1:[0-9]+/[A-Za-z0-9_-]+")
_OPENER = "credentials-opener.html"


# -- the small pieces every case uses -----------------------------------------------------------


class Clock:
    """The page's time, owned by the test."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeService:
    """A stand-in Nexus service on a port the system picks: ``/health`` and ``/version`` answer anyone
    (as a local or self-hosted engine does), ``/v1/_whoami`` answers 200 to one bearer and 401 to any
    other. ``seen`` is every request as (path, Authorization header or None)."""

    def __init__(self, good_token: str, *, whoami: str = "ok", edge: bool = False) -> None:
        self.good_token = good_token
        self.whoami = whoami
        self.edge = edge  # a managed edge: /health refuses a missing token (403) and a wrong one (401)
        self.seen: list[tuple[str, str | None]] = []
        self.redirect_to: str | None = None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(body or {}).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                auth = self.headers.get("Authorization")
                outer.seen.append((self.path, auth))
                if self.path == "/health":
                    if outer.edge and auth is None:
                        self._send(403, {"error": "forbidden"})
                    elif outer.edge and auth != f"Bearer {outer.good_token}":
                        self._send(401, {"error": "unauthorized"})
                    else:
                        self._send(200, {"status": "ok"})
                elif self.path == "/version":
                    self._send(200, {"app_version": "1.0-SNAPSHOT", "release_version": "9.9.9"})
                elif self.path == "/v1/_whoami":
                    if outer.redirect_to is not None:
                        self._send(302, {}, {"Location": outer.redirect_to})
                    elif auth == f"Bearer {outer.good_token}":
                        self._send(200 if outer.whoami == "ok" else 403, {"tenant": "t"})
                    else:
                        self._send(401, {"error": "unauthorized"})
                else:
                    self._send(404, {})

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def authorized_paths(self) -> list[str]:
        """The paths a request carrying a bearer token was made to."""
        return [path for path, auth in self.seen if auth is not None]


@pytest.fixture
def service() -> Iterator[Callable[..., FakeService]]:
    made: list[FakeService] = []

    def make(token: str, **kw: Any) -> FakeService:
        svc = FakeService(token, **kw)
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.close()


def _request(
    address: str, method: str = "GET", *, path: str | None = None, fields: dict[str, str] | None = None,
    host: str | None = None, raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP request to the page at *address* (its own path unless *path*)."""
    parts = urllib.parse.urlsplit(address)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=15)
    headers: dict[str, str] = {}
    body = raw_body
    if fields is not None:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if host is not None:
        headers["Host"] = host
    try:
        conn.request(method, parts.path if path is None else path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def _refused(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError:
        return True
    return False


class _Form(html.parser.HTMLParser):
    """The form controls of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[dict[str, str | None]] = []
        self.buttons = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            self.inputs.append(dict(attrs))
        elif tag == "button":
            self.buttons += 1


def _controls(body: bytes) -> _Form:
    form = _Form()
    form.feed(body.decode("utf-8"))
    return form


def _token() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class Opened:
    """What the page was asked to open."""

    paths: list[str] = field(default_factory=list)

    def __call__(self, path: str) -> bool:
        self.paths.append(path)
        return True


@pytest.fixture
def make_page(tmp_path: Path) -> Iterator[Callable[..., Any]]:
    made: list[Any] = []

    def build(clock: Clock | None = None, opener: Opened | None = None, **kw: Any) -> Any:
        mod = _load_module()
        setup = tmp_path / "setup"
        setup.mkdir(mode=0o700, exist_ok=True)
        page = mod.CredentialsPage(
            setup, clock=clock if clock is not None else Clock(), opener=opener if opener is not None else Opened(), **kw,
        )
        made.append(page)
        page.start()
        page.mod = mod
        page.setup_dir = setup
        return page

    yield build
    for page in made:
        page.close()


# -- requirement: the six status values ---------------------------------------------------------


def test_the_status_values_are_exactly_the_six() -> None:
    mod = _load_module()
    assert tuple(mod.CREDENTIAL_STATES) == _SIX
    assert (mod.PAGE_LOAD_SECONDS, mod.PAGE_LISTEN_SECONDS) == (120.0, 600.0)


def test_a_page_only_ever_holds_one_of_the_six_values(make_page: Callable[..., Any], service: Callable[..., FakeService]) -> None:
    good = _token()
    svc = service(good)
    clock = Clock()
    page = make_page(clock)
    seen = [page.state]
    clock.advance(120)
    page.poll()
    seen.append(page.state)
    _request(page.address)
    seen.append(page.state)
    _request(page.address, "POST", fields={"service_url": svc.url, "service_token": _token()})
    seen.append(page.state)
    _request(page.address, "POST", fields={"service_url": svc.url, "service_token": good})
    seen.append(page.state)
    page.mark_saved()
    seen.append(page.state)
    late = make_page(clock)
    clock.advance(600)
    late.poll()
    seen.append(late.state)
    assert seen == [_SIX[0], _SIX[1], _SIX[0], _SIX[2], _SIX[3], _SIX[4], _SIX[5]]


# -- requirement: loopback only, a port the system picks, a request without the path gets nothing -----


def test_the_listener_is_bound_to_loopback_only_on_a_port_the_system_picks(make_page: Callable[..., Any]) -> None:
    a, b = make_page(), make_page()
    assert a.bound_host == "127.0.0.1" and b.bound_host == "127.0.0.1"
    assert a.port > 0 and b.port > 0 and a.port != b.port, "each page asks the system for its own port"
    assert urllib.parse.urlsplit(a.address).hostname == "127.0.0.1"
    # Not reachable on any other address of this machine.
    others = {ip for ip in _local_addresses() if not ip.startswith("127.")}
    for ip in others:
        assert _refused(ip, a.port), f"the page answers on {ip}"


def _local_addresses() -> set[str]:
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(str(info[4][0]))
    except OSError:
        pass
    return found


def test_a_request_without_the_one_time_path_gets_nothing(make_page: Callable[..., Any]) -> None:
    page = make_page()
    path = urllib.parse.urlsplit(page.address).path
    assert len(path) >= 20, "the path is long and random enough to be unguessable"
    wrong = ["/", "/index.html", path + "x", path[:-1], path.upper(), "/" + "a" * (len(path) - 1)]
    for method in ("GET", "POST", "HEAD", "PUT", "DELETE"):
        for bad in wrong:
            status, _headers, body = _request(page.address, method, path=bad, fields={"service_url": "x", "service_token": "y"} if method == "POST" else None)
            assert status in (404, 501), (method, bad, status)
            assert body == b"", (method, bad, "a request without the path gets no content")
    # The right path with a Host that is not the page's own (DNS rebinding) gets nothing either.
    status, _h, body = _request(page.address, host="evil.example")
    assert status == 404 and body == b""
    assert page.state == _SIX[0] and page.loaded is False, "none of that counted as a load"
    assert Path(page.opener_path).exists(), "the opener file stays until the page is really loaded"
    two = make_page()
    assert urllib.parse.urlsplit(two.address).path != path, "the path is new for each page"


# -- requirement: the address is in no file but the opener, on no command line; owner-only opener -----


def test_the_opener_file_is_owner_only_holds_the_address_and_is_all_the_system_is_asked_to_open(make_page: Callable[..., Any]) -> None:
    opened = Opened()
    page = make_page(opener=opened)
    opener = Path(page.opener_path)
    assert opener.name == _OPENER and opener.parent == page.setup_dir
    assert_owner_only(opener)
    text = opener.read_text(encoding="utf-8")
    assert page.address in text
    assert opened.paths == [str(opener)], "the system is asked to open the file, whose path is not the address"
    assert page.address not in " ".join(opened.paths)
    assert [p.name for p in page.setup_dir.iterdir() if page.address in p.read_text(encoding="utf-8", errors="replace")] == [_OPENER]


def test_the_opener_file_is_deleted_when_the_page_is_first_loaded(make_page: Callable[..., Any]) -> None:
    page = make_page()
    opener = Path(page.opener_path)
    assert opener.exists()
    status, headers, body = _request(page.address)
    assert status == 200
    _wait_for(lambda: not opener.exists(), 10, "the opener file to be deleted by the first load")
    assert page.loaded is True
    assert headers.get("cache-control") == "no-store" and headers.get("referrer-policy") == "no-referrer"
    assert "default-src 'none'" in headers.get("content-security-policy", "")
    assert _request(page.address)[0] == 200, "a reload works"
    assert not opener.exists()


# -- requirement: two fields and one button, a password field with autocomplete off -------------------


def test_the_page_has_two_fields_one_button_and_a_password_token_field_with_autocomplete_off(make_page: Callable[..., Any]) -> None:
    page = make_page()
    status, _h, body = _request(page.address)
    assert status == 200
    controls = _controls(body)
    fields = {c.get("name"): c for c in controls.inputs}
    assert set(fields) == {"service_url", "service_token"}
    assert len(controls.inputs) == 2 and controls.buttons == 1
    token = fields["service_token"]
    assert token["type"] == "password" and token["autocomplete"] == "off"
    assert fields["service_url"]["type"] in ("text", "url")


# -- requirement: a pair that fails is not kept, the page says why and stays open ----------------------


def test_a_rejected_pair_is_not_kept_and_the_page_stays_open(make_page: Callable[..., Any], service: Callable[..., FakeService]) -> None:
    good, wrong = _token(), _token()
    svc = service(good)
    page = make_page()
    status, _h, body = _request(page.address, "POST", fields={"service_url": svc.url, "service_token": wrong})
    assert status == 200
    text = body.decode("utf-8")
    assert "refus" in text.lower(), "the page says why"
    assert wrong not in text, "the token is not echoed"
    assert page.state == _SIX[2]
    assert page.accepted_pair is None, "a failed pair is not kept"
    assert page.listening, "the listener is still up"
    assert _request(page.address)[0] == 200, "the page is still served"
    # An unreachable address and an empty field are failures too.
    _request(page.address, "POST", fields={"service_url": "http://127.0.0.1:9", "service_token": wrong})
    assert page.state == _SIX[2] and page.accepted_pair is None
    _request(page.address, "POST", fields={"service_url": "", "service_token": ""})
    assert page.state == _SIX[2] and page.accepted_pair is None
    # And another try works.
    _request(page.address, "POST", fields={"service_url": svc.url, "service_token": good})
    assert page.state == _SIX[3] and page.accepted_pair == (svc.url, good)


# -- requirement: an accepted pair is held, the page says so, the listener closes ----------------------


def test_an_accepted_pair_is_held_in_memory_and_the_listener_closes(make_page: Callable[..., Any], service: Callable[..., FakeService]) -> None:
    good = _token()
    svc = service(good)
    page = make_page()
    port = page.port
    status, _h, body = _request(page.address, "POST", fields={"service_url": svc.url + "/", "service_token": good})
    assert status == 200 and "accepted" in body.decode("utf-8").lower()
    assert good not in body.decode("utf-8")
    assert page.state == _SIX[3]
    assert page.accepted_pair == (svc.url, good), "held in memory, the address without its trailing slash"
    _wait_for(lambda: _refused("127.0.0.1", port), 10, "the listener to close")
    assert page.listening is False
    assert not Path(page.opener_path).exists()
    # Time passing changes nothing once accepted.
    page.poll()
    assert page.state == _SIX[3]
    page.mark_saved()
    assert page.state == _SIX[4]


# -- requirement: the environment notice -----------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, False),
        ({"NX_SERVICE_URL": ""}, False),
        ({"NX_SERVICE_URL": "https://x.example"}, True),
        ({"NX_SERVICE_TOKEN": "abc"}, True),
        ({"NX_SERVICE_URL": "https://x.example", "NX_SERVICE_TOKEN": "abc"}, True),
    ],
)
def test_the_page_says_when_the_environment_takes_precedence(
    make_page: Callable[..., Any], environ: dict[str, str], expected: bool,
) -> None:
    page = make_page(environ=environ)
    _status, _h, body = _request(page.address)
    text = body.decode("utf-8")
    assert ("take precedence" in text) is expected
    for value in environ.values():
        if value:
            assert value not in text, "the notice names the variables and never their values"
    if expected:
        for name in environ:
            if environ[name]:
                assert name in text


def test_the_notice_reads_the_process_environment_by_default(make_page: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    quiet = make_page()
    assert "take precedence" not in _request(quiet.address)[2].decode("utf-8")
    monkeypatch.setenv("NX_SERVICE_TOKEN", _token())
    loud = make_page()
    assert "take precedence" in _request(loud.address)[2].decode("utf-8")


# -- requirement: page not loaded at two minutes, listener closes at ten -----------------------------


def test_page_not_loaded_appears_at_two_minutes_and_the_listener_closes_at_ten(make_page: Callable[..., Any]) -> None:
    clock = Clock()
    page = make_page(clock)
    port = page.port
    opener = Path(page.opener_path)
    clock.advance(119.5)
    page.poll()
    assert page.state == _SIX[0]
    clock.advance(0.5)  # 120 s after the request to open it
    page.poll()
    assert page.state == _SIX[1]
    assert page.listening, "the page is still served at two minutes"
    clock.advance(479.5)  # 599.5 s after listening began
    page.poll()
    assert page.state == _SIX[1] and page.listening
    clock.advance(0.5)
    page.poll()
    assert page.state == _SIX[5]
    _wait_for(lambda: _refused("127.0.0.1", port), 10, "the listener to close at ten minutes")
    assert page.listening is False
    assert not opener.exists(), "the opener file goes with the listener"


def test_ten_minutes_end_the_listener_whatever_happened(make_page: Callable[..., Any], service: Callable[..., FakeService]) -> None:
    good = _token()
    svc = service(good)
    clock = Clock()
    page = make_page(clock)
    _request(page.address)  # loaded
    clock.advance(300)
    _request(page.address, "POST", fields={"service_url": svc.url, "service_token": "no"})
    assert page.state == _SIX[2]
    clock.advance(299.5)
    page.poll()
    assert page.state == _SIX[2] and page.listening
    clock.advance(0.5)
    page.poll()
    assert page.state == _SIX[5] and page.listening is False


def test_a_page_loaded_late_goes_back_to_waiting(make_page: Callable[..., Any]) -> None:
    clock = Clock()
    page = make_page(clock)
    clock.advance(130)
    page.poll()
    assert page.state == _SIX[1]
    _request(page.address)
    assert page.state == _SIX[0]
    clock.advance(10)
    page.poll()
    assert page.state == _SIX[0], "a page that was loaded is never again 'not loaded'"


# -- requirement: the opener file is gone after a close without a load, and after a killed run's next start ---


def test_the_opener_file_is_gone_after_a_close_without_a_load(make_page: Callable[..., Any]) -> None:
    page = make_page()
    opener = Path(page.opener_path)
    assert opener.exists()
    page.close()
    assert not opener.exists()
    page.close()  # idempotent
    assert page.state == _SIX[0], "closing is not a status change"


def test_an_opener_file_a_killed_run_left_is_deleted_when_the_next_run_starts(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.layout()
    box.setup.mkdir(parents=True)
    leftover = box.setup / _OPENER
    leftover.write_text("left by a run that was killed", encoding="utf-8")
    assert box.start("cli-only")["started"] is True
    box.wait_terminal()
    assert not leftover.exists()


def test_a_killed_cloud_run_leaves_an_opener_that_the_next_start_deletes(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.layout()
    box.behave([box.uv_builds_nx()])
    out = box.start("cloud")
    assert out["started"] is True
    opener, _address = _page(box)
    assert opener.exists()
    pid = box.status()["pid"]
    _terminate(pid)
    _wait_for(lambda: not _alive(pid), 15, "the run to die")
    assert opener.exists(), "a killed run cannot clean up: that is the case this covers"
    _age_lock(box.setup)
    out = box.start("cli-only")
    assert out["started"] is True
    box.wait_terminal()
    assert not opener.exists()


# -- the run: cloud serves the page while the build goes on; saves after it ---------------------------


def _page(box: Box, timeout: float = 30) -> tuple[Path, str]:
    """(opener file, page address) once a run has asked the system to open the page."""
    def ready() -> bool:
        try:
            return bool(box.open_record.read_text(encoding="utf-8").strip())
        except OSError:
            return False

    _wait_for(ready, timeout, "the run to ask the system to open the page")
    opener = Path(box.open_record.read_text(encoding="utf-8").splitlines()[0].strip())
    text = _retry(lambda: opener.read_text(encoding="utf-8"))
    match = _ADDRESS.search(text)
    assert match, text
    return opener, match.group(0)


def _age_lock(setup: Path) -> None:
    """Make a dead run's lock stale: its heartbeat has not been written for two minutes."""
    heartbeat = setup / "lock" / "heartbeat"
    old = time.time() - 120.0
    os.utime(heartbeat, (old, old))


def _credentials(box: Box) -> str | None:
    data = box.status_file()
    return None if data is None else data.get("credentials")


@dataclass
class CloudRun:
    """One whole cloud run, observed from the browser's side."""

    box: Box
    token: str
    wrong: str
    service_url: str
    address: str
    secret_path: str
    start_output: dict[str, Any]
    opener_text: str
    opener_problem: str | None
    opener_gone_after_load: bool
    rejected_body: str
    status_when_rejected: dict[str, Any]
    status_when_accepted: dict[str, Any]
    listener_refused_after_accept: bool
    config_calls_when_accepted: list[list[str]]
    run_cmdline: str | None
    credentials_seen: list[str]
    final: dict[str, Any]


def _cmdline(pid: int) -> str | None:
    if IS_WINDOWS:
        return None
    done = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8", check=False)
    return done.stdout.strip() or None


@pytest.fixture(scope="module")
def cloud_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CloudRun]:
    """A cloud run whose build takes a few seconds, driven as a browser would: wrong pair, right
    pair, then the build ends and the run saves. The facts every case below reads."""
    box = Box(tmp_path_factory.mktemp("cloudrun"))
    good, wrong = _token(), _token()
    svc = FakeService(good)
    seen: list[str] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            value = _credentials(box)
            if value is not None and (not seen or seen[-1] != value):
                seen.append(value)
            time.sleep(0.02)

    poller = threading.Thread(target=poll, daemon=True)
    try:
        box.behave([box.uv_builds_nx(sleep=5.0), {"prog": "nx", "match": ["doctor"], "out": "[✓] fine\n"}])
        poller.start()
        out = box.start("cloud")
        assert out["started"] is True, out
        opener, address = _page(box)
        text = _retry(lambda: opener.read_text(encoding="utf-8"))
        try:
            assert_owner_only(opener)
            problem = None
        except AssertionError as exc:
            problem = str(exc)
        status, _h, _b = _request(address)
        assert status == 200
        gone = _wait_gone(opener)
        _s, _h, rejected = _request(address, "POST", fields={"service_url": svc.url, "service_token": wrong})
        _wait_for(lambda: _credentials(box) == _SIX[2], 20, "the rejection to reach the status")
        when_rejected = box.status_file() or {}
        assert _request(address)[0] == 200, "the page stays open after a rejection"
        _s, _h, accepted = _request(address, "POST", fields={"service_url": svc.url, "service_token": good})
        assert b"accepted" in accepted.lower()
        _wait_for(lambda: _credentials(box) == _SIX[3], 20, "the acceptance to reach the status")
        when_accepted = box.status_file() or {}
        port = urllib.parse.urlsplit(address).port
        assert port is not None
        refused = _wait_refused(port)
        early_config = [c["args"] for c in box.calls("nx") if c["args"][:1] == ["config"]]
        cmdline = _cmdline(int(when_accepted["pid"]))
        final = box.wait_terminal(timeout=120)
        stop.set()
        poller.join(5)
        yield CloudRun(
            box=box, token=good, wrong=wrong, service_url=svc.url, address=address,
            secret_path=urllib.parse.urlsplit(address).path.lstrip("/"), start_output=out, opener_text=text,
            opener_problem=problem, opener_gone_after_load=gone, rejected_body=rejected.decode("utf-8"),
            status_when_rejected=when_rejected, status_when_accepted=when_accepted, listener_refused_after_accept=refused,
            config_calls_when_accepted=early_config, run_cmdline=cmdline, credentials_seen=list(seen), final=final,
        )
    finally:
        stop.set()
        svc.close()


def _wait_gone(path: Path) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.02)
    return False


def _wait_refused(port: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _refused("127.0.0.1", port):
            return True
        time.sleep(0.02)
    return False


def _tree_text(root: Path) -> dict[str, str]:
    """Every file under *root* as text (undecodable bytes replaced)."""
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file():
            out[str(path.relative_to(root))] = _retry(lambda p=path: p.read_bytes()).decode("utf-8", errors="replace")
    return out


def test_the_page_is_served_while_the_build_goes_on_and_the_status_shows_it(cloud_run: CloudRun) -> None:
    assert cloud_run.status_when_accepted["stage"] == "build", "acceptance happened while the build was running"
    assert cloud_run.status_when_rejected["stage"] == "build"
    assert cloud_run.status_when_rejected["state"] == "running"


def test_the_run_opener_is_owner_only_holds_the_address_and_is_gone_after_the_first_load(cloud_run: CloudRun) -> None:
    assert cloud_run.opener_problem is None, cloud_run.opener_problem
    assert cloud_run.address in cloud_run.opener_text
    assert cloud_run.opener_gone_after_load


def test_the_address_is_in_no_file_no_log_no_tool_result_and_no_command_line(cloud_run: CloudRun) -> None:
    box = cloud_run.box
    secret = cloud_run.secret_path
    assert len(secret) >= 20
    for name, text in _tree_text(box.config).items():
        assert secret not in text, f"the page's address is in {name}"
    assert secret not in json.dumps(cloud_run.start_output), "the address is in the start tool's result"
    assert secret not in json.dumps(cloud_run.status_when_accepted) and secret not in json.dumps(cloud_run.final)
    assert secret not in json.dumps(box.status())
    assert secret not in " ".join(" ".join(argv) for argv in box.argvs()), "the address is on a child's command line"
    if cloud_run.run_cmdline is not None:
        assert secret not in cloud_run.run_cmdline, "the address is on the setup process's command line"
    assert cloud_run.run_cmdline is not None or IS_WINDOWS


def test_the_token_is_in_neither_the_log_nor_the_status_nor_any_file(cloud_run: CloudRun) -> None:
    box = cloud_run.box
    for name, text in _tree_text(box.config).items():
        assert cloud_run.token not in text, f"the token is in {name}"
        assert cloud_run.wrong not in text, f"a refused token is in {name}"
    assert cloud_run.token not in json.dumps(cloud_run.start_output)
    for snapshot in (cloud_run.status_when_rejected, cloud_run.status_when_accepted, cloud_run.final):
        assert cloud_run.token not in json.dumps(snapshot) and cloud_run.wrong not in json.dumps(snapshot)
    assert cloud_run.token not in cloud_run.rejected_body and cloud_run.wrong not in cloud_run.rejected_body
    assert cloud_run.token not in box.read_log()


def test_the_token_reaches_nx_config_set_on_standard_input_and_never_on_a_command_line(cloud_run: CloudRun) -> None:
    box = cloud_run.box
    assert all(cloud_run.token not in " ".join(argv) for argv in box.argvs()), "the token is on a command line"
    sets = [c for c in box.calls("nx") if c["args"][:2] == ["config", "set"]]
    by_key = {c["args"][2]: c for c in sets}
    assert set(by_key) == {"service_url", "service_token"}, [c["args"] for c in sets]
    assert by_key["service_token"]["args"] == ["config", "set", "service_token", "--stdin"]
    assert by_key["service_token"]["stdin_text"] == cloud_run.token
    assert by_key["service_url"]["args"] == ["config", "set", "service_url", cloud_run.service_url]


def test_a_rejected_pair_through_the_run_leaves_the_status_waiting_for_another_try(cloud_run: CloudRun) -> None:
    assert cloud_run.status_when_rejected["credentials"] == _SIX[2]
    assert "refus" in cloud_run.rejected_body.lower()


def test_an_accepted_pair_closes_the_listener_and_is_saved_only_after_the_build(cloud_run: CloudRun) -> None:
    box = cloud_run.box
    assert cloud_run.listener_refused_after_accept
    assert cloud_run.config_calls_when_accepted == [], "nothing was saved while the build was still running"
    assert cloud_run.status_when_accepted["credentials"] == _SIX[3]
    events = box.events()
    build_end = max(e["t"] for e in events if e["ev"] == "end" and e["prog"] == "uv")
    first_save = min(e["t"] for e in events if e["ev"] == "start" and e["prog"] == "nx" and e["args"][:2] == ["config", "set"])
    assert first_save >= build_end, "the pair is saved after the build ends"
    assert cloud_run.final["state"] == "done" and cloud_run.final["credentials"] == _SIX[4]
    assert cloud_run.final["steps_done"][-2:] == ["credentials", "verify"]


def test_the_run_takes_only_the_six_status_values_in_order(cloud_run: CloudRun) -> None:
    assert set(cloud_run.credentials_seen) <= set(_SIX), cloud_run.credentials_seen
    order = [v for v in cloud_run.credentials_seen]
    assert order[0] == _SIX[0] and order[-1] == _SIX[4]
    for earlier, later in ((_SIX[0], _SIX[2]), (_SIX[2], _SIX[3]), (_SIX[3], _SIX[4])):
        assert order.index(earlier) < order.index(later), order


def test_cloud_downloads_no_service_files(cloud_run: CloudRun) -> None:
    box = cloud_run.box
    argvs = box.argvs()
    assert not [a for a in argvs if a[1:2] == ["init"] or a[1:3] == ["daemon", "service"]], argvs
    assert not (box.config / "service").exists() and not (box.config / "postgres").exists()
    assert [a[0] for a in argvs].count("uv") == 1, "only the CLI build downloads anything"
    assert [a for a in argvs if a[0] == "nx" and a[1:2] == ["doctor"]], "the run verifies at the end"


def test_a_child_that_echoes_the_token_does_not_leave_it_in_the_log(tmp_path: Path, service: Callable[..., FakeService]) -> None:
    """The token is never written to the log, even by a child program that prints what it was given."""
    box = Box(tmp_path)
    good = _token()
    svc = service(good)
    box.layout()
    box.behave([{"prog": "nx", "match": ["config", "set", "service_token"], "echo_stdin": True, "out": "\n"}])
    assert box.start("cloud")["started"] is True
    _opener, address = _page(box)
    _request(address, "POST", fields={"service_url": svc.url, "service_token": good})
    final = box.wait_terminal(60)
    assert final["state"] == "done", (final, box.read_log())
    log = box.read_log()
    assert good not in log and "***" in log, "the echoed token is replaced in the log"
    assert [c["stdin_text"] for c in box.calls("nx") if c["args"][:3] == ["config", "set", "service_token"]] == [good]
    for text in _tree_text(box.config).values():
        assert good not in text


# -- the run: the timeout path, in this process with the page's clock ---------------------------------


def test_a_cloud_run_that_nobody_answers_says_page_not_loaded_at_two_minutes_and_ends_failed_at_ten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = Box(tmp_path)
    mod = _in_process(box, monkeypatch)
    clock = Clock()
    monkeypatch.setattr(mod, "_clock", clock)
    box.layout()
    box.setup.mkdir(parents=True)
    token = mod.acquire_lock(box.setup)
    assert token
    results: list[int] = []
    thread = threading.Thread(target=lambda: results.append(mod.run_setup("cloud", False, token)), daemon=True)
    thread.start()
    try:
        _wait_for(lambda: _credentials(box) == _SIX[0], 30, "the page to be served")
        status = box.status_file() or {}
        assert status["state"] == "running" and "nx config init" not in status["message"]
        clock.advance(120)
        _wait_for(lambda: _credentials(box) == _SIX[1], 15, "'page not loaded' at two minutes")
        status = box.status_file() or {}
        assert "nx config init" in status["message"] + status["next"], "the status names the other route"
        assert status["state"] == "running"
        assert (box.setup / _OPENER).exists()
        clock.advance(480)
        thread.join(60)
        assert not thread.is_alive()
    finally:
        clock.advance(10_000)
        thread.join(30)
    assert results == [1]
    final = box.status_file() or {}
    assert final["state"] == "failed" and final["failed_step"] == "credentials"
    assert final["credentials"] == _SIX[5]
    assert not (box.setup / "lock").exists()
    assert not (box.setup / _OPENER).exists(), "the opener file is gone: the run closed without a load"
    assert [a for a in box.argvs() if a[1:3] == ["config", "set"]] == []


# -- the run: killed between acceptance and saving ---------------------------------------------------


def test_a_run_killed_after_acceptance_loses_the_pair_and_the_next_cloud_run_opens_a_new_page(tmp_path: Path, service: Callable[..., FakeService]) -> None:
    box = Box(tmp_path)
    good = _token()
    svc = service(good)
    box.behave([box.uv_builds_nx(sleep=8.0)])
    assert box.start("cloud")["started"] is True
    opener, address = _page(box)
    _request(address)
    _request(address, "POST", fields={"service_url": svc.url, "service_token": good})
    _wait_for(lambda: _credentials(box) == _SIX[3], 20, "the acceptance")
    pid = box.status()["pid"]
    _terminate(pid)
    _wait_for(lambda: not _alive(pid), 15, "the run to die")
    for text in _tree_text(box.config).values():
        assert good not in text, "an accepted pair is not written to disk to survive a kill"
    assert [c for c in box.calls("nx") if c["args"][:1] == ["config"]] == []
    _age_lock(box.setup)
    box.open_record.write_text("", encoding="utf-8")
    box.behave([box.uv_builds_nx(sleep=0.0)])
    out = box.start("cloud")
    assert out["started"] is True, out
    opener2, address2 = _page(box)
    assert address2 != address and urllib.parse.urlsplit(address2).path != urllib.parse.urlsplit(address).path
    _wait_for(lambda: _credentials(box) == _SIX[0], 20, "the status to say the credentials are needed again")
    status = box.status_file() or {}
    assert status["credentials"] == _SIX[0] and status["state"] == "running"
    assert [c for c in box.calls("nx") if c["args"][:1] == ["config"]] == [], "nothing was saved"
    _terminate(box.status()["pid"])


# -- the run: cloud and local ---------------------------------------------------------------------


def test_local_serves_no_page_and_asks_the_system_to_open_nothing(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.layout()
    box.hook_site("import socket\ndef _r(*a, **k):\n    raise RuntimeError('listen')\nsocket.socket.bind = _r\nsocket.socket.listen = _r\n")
    box.behave([{"prog": "nx", "match": ["init"], "effects": box.init_effects()}])
    box.start("local")
    assert box.wait_terminal()["state"] == "done"
    assert not box.open_record.exists(), "the system was asked to open nothing"
    assert not (box.setup / _OPENER).exists()
    assert "credentials" not in (box.status_file() or {})


def test_cli_only_serves_no_page(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.layout()
    box.start("cli-only")
    assert box.wait_terminal()["state"] == "done"
    assert not box.open_record.exists()
    assert "credentials" not in (box.status_file() or {})


def test_a_service_that_cannot_listen_fails_the_cloud_run_before_any_download(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.hook_site("import socket\ndef _r(*a, **k):\n    raise OSError('no sockets here')\nsocket.socket.bind = _r\n")
    box.behave([box.uv_builds_nx()])
    box.start("cloud")
    final = box.wait_terminal()
    assert final["state"] == "failed" and final["failed_step"] == "credentials"
    assert box.calls() == [], "nothing was downloaded"
    assert not (box.setup / _OPENER).exists()


def test_the_system_open_step_is_the_only_place_a_browser_could_start() -> None:
    """The seam: the real opener consults ``NX_SETUP_OPEN_RECORD`` first, so a run whose environment sets
    it starts nothing; and one function alone launches anything for the page."""
    mod = _load_module()
    assert callable(mod._ask_system_to_open) and callable(mod._launch_opener)
    tree = ast.parse(_SETUP.read_text(encoding="utf-8"))
    launchers = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(n, ast.Attribute) and n.attr == "startfile" for n in ast.walk(node))
    ]
    assert launchers == ["_launch_opener"]


def test_the_record_seam_writes_the_openers_path_and_launches_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    record = tmp_path / "record"
    monkeypatch.setenv("NX_SETUP_OPEN_RECORD", str(record))
    launched: list[str] = []
    monkeypatch.setattr(mod, "_launch_opener", launched.append)
    assert mod._ask_system_to_open(str(tmp_path / "x.html")) is True
    assert record.read_text(encoding="utf-8").splitlines() == [str(tmp_path / "x.html")]
    assert launched == []
    monkeypatch.delenv("NX_SETUP_OPEN_RECORD")
    mod._ask_system_to_open(str(tmp_path / "y.html"))
    assert launched == [str(tmp_path / "y.html")], "with no record seam the real launcher is asked"


# -- the check: the token is checked on an authenticated /v1 route ------------------------------------


def test_the_token_is_checked_on_the_v1_route_not_on_health_or_version(service: Callable[..., FakeService]) -> None:
    """Sam, 2026-10-15: /health and /version cannot carry the token check (a local or self-hosted engine
    answers anyone there; this service does too), so a wrong token must fail the authenticated route."""
    mod = _load_module()
    good, wrong = _token(), _token()
    svc = service(good)
    ok, text = mod.check_pair(svc.url, wrong)
    assert ok is False and wrong not in text and "refus" in text.lower()
    assert ("/version", None) in svc.seen, "the version is read without the token"
    assert svc.authorized_paths() == ["/health", "/v1/_whoami"], "the token goes to the service's own routes, no others"
    assert [p for p, _a in svc.seen][-1] == "/v1/_whoami", "the verdict is the authenticated route's"
    svc.seen.clear()
    ok, text = mod.check_pair(svc.url + "/", good)
    assert ok is True and "9.9.9" in text and good not in text
    assert [p for p, _a in svc.seen] == ["/version", "/health", "/v1/_whoami"]


def test_a_managed_edge_that_refuses_health_without_a_token_is_handled(service: Callable[..., FakeService]) -> None:
    """Measured 2026-10-14 (nexus_rdr/228-research-36): the managed edge answers /health 403 to a request with no token
    and 401 to a wrong one. The check sends the token there, so a right pair passes and a wrong one is refused."""
    mod = _load_module()
    good, wrong = _token(), _token()
    svc = service(good, edge=True)
    ok, text = mod.check_pair(svc.url, good)
    assert ok is True, text
    ok, text = mod.check_pair(svc.url, wrong)
    assert ok is False and "refus" in text.lower() and wrong not in text


def test_a_mint_scoped_credential_is_refused_because_the_page_saves_only_a_service_token(service: Callable[..., FakeService]) -> None:
    mod = _load_module()
    good = _token()
    svc = service(good, whoami="forbidden")
    ok, text = mod.check_pair(svc.url, good)
    assert ok is False and "mint" in text.lower() and good not in text


def test_an_address_that_is_not_http_or_https_is_refused_before_any_request(service: Callable[..., FakeService]) -> None:
    mod = _load_module()
    good = _token()
    svc = service(good)
    for bad in ("", "   ", "file:///etc/passwd", "ftp://example.com", "javascript:alert(1)", "127.0.0.1:80", "http://", svc.url.replace("http://", "gopher://")):
        ok, text = mod.check_pair(bad, good)
        assert ok is False, bad
        assert good not in text
    assert svc.seen == []
    ok, _text = mod.check_pair(svc.url + "/?x=1#frag", good)
    assert ok is True, "an http address is accepted"


def test_an_address_that_does_not_answer_is_refused_with_a_reason(service: Callable[..., FakeService]) -> None:
    mod = _load_module()
    good = _token()
    svc = service(good)
    dead = svc.url
    svc.close()
    ok, text = mod.check_pair(dead, good)
    assert ok is False and "did not answer" in text and good not in text


def test_a_redirect_is_not_followed_so_the_token_goes_nowhere_else(service: Callable[..., FakeService]) -> None:
    mod = _load_module()
    good = _token()
    first, other = service(good), service(good)
    first.redirect_to = other.url + "/v1/_whoami"
    ok, text = mod.check_pair(first.url, good)
    assert ok is False and good not in text
    assert other.seen == [], "the second service saw nothing"


# -- Cloud is offered once this step is present --------------------------------------------------------


def test_cloud_is_offered_by_the_tools_the_table_and_the_command() -> None:
    sys.path.insert(0, str(_ROOT / "conexus" / "launch"))
    try:
        import nx_standin  # noqa: PLC0415 -- the plugin file under test
    finally:
        sys.path.remove(str(_ROOT / "conexus" / "launch"))
    from nexus.mcp import setup_tools  # noqa: PLC0415

    assert "cloud" in nx_standin.MODES and "cloud" in setup_tools.MODES and "cloud" in _load_module().MODES
    row = next(line for line in nx_standin.QUESTION_TABLE.splitlines() if line.startswith("| Cloud |"))
    assert row in _RECORD.read_text(encoding="utf-8"), "the table's Cloud row is the record's"
    command = (_ROOT / "conexus" / "commands" / "setup.md").read_text(encoding="utf-8")
    assert nx_standin.QUESTION_TABLE in command and "mode` set to `local`, `cloud`" in command
    for text in (nx_standin.SETUP_DESCRIPTION, nx_standin.MODE_DESCRIPTION, setup_tools.SETUP_DESCRIPTION, setup_tools.MODE_DESCRIPTION):
        assert "cloud" in text.lower()
    assert "| Cloud |" in nx_standin.instructions()
    assert not hasattr(nx_standin, "CLOUD_REFUSAL") and not hasattr(setup_tools, "CLOUD_REFUSAL"), "cloud is no longer refused"
    assert "Unknown mode" in nx_standin.mode_refusal("bogus")
