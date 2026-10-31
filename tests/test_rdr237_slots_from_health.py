# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-489n1): the MinerU slot count is read from the live server.

``/health`` reports ``max_concurrent_requests`` (MinerU 3.1.11 ``fast_api.py``). A server started by
``nx mineru start`` or launchd under another environment, or one that is not this process's child,
does not run with this process's ``MINERU_API_MAX_CONCURRENT_REQUESTS``, so the platform and
environment guess can be wrong. The live number decides when a timed-out server is replaced; the
guess stays as the fallback when the server does not say.

The health reads here hit a real HTTP server on port 0. The restart-rule test runs the extractor
over the fake MinerU of ``tests/test_rdr237_bounds.py``.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_extractor import PDFExtractor, _mineru_server_slots
from tests._module_seam import patch_in
from tests.test_rdr237_bounds import REGISTERED, World, _isolated, _restart_spy, times_out  # noqa: F401 — _isolated is an autouse fixture


@contextmanager
def health_server(body: bytes | None, *, status: int = 200) -> Iterator[str]:
    """A loopback server answering ``/health`` with *body* (``None``: 404 for everything)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server's name
            if body is None or self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _health(**fields) -> bytes:
    return json.dumps({"status": "healthy", "version": "3.1.11", **fields}).encode()


@pytest.mark.parametrize("platform,env,reported", [
    ("darwin", None, 5),     # the guess says 1; the server says 5
    ("linux", None, 1),      # the guess says 3; the server says 1
    ("linux", "8", 2),       # this process's environment is not the server's
])
def test_the_live_servers_slot_count_wins_over_the_platform_and_environment_guess(
    monkeypatch, platform, env, reported,
) -> None:
    if env is not None:
        monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", env)
    with health_server(_health(max_concurrent_requests=reported)) as url, \
            patch_in("nexus.pdf_extractor", "sys.platform", platform):
        assert _mineru_server_slots(url) == reported


@pytest.mark.parametrize("body,status", [
    (_health(), 200),                                     # no count in the answer
    (_health(max_concurrent_requests=0), 200),            # not a positive count
    (_health(max_concurrent_requests=-2), 200),
    (_health(max_concurrent_requests="3"), 200),          # a string is not a count
    (_health(max_concurrent_requests=True), 200),         # nor is a bool
    (_health(max_concurrent_requests=2.5), 200),
    (b"not json", 200),
    (b"[1, 2]", 200),                                     # JSON, but not an object
    (_health(max_concurrent_requests=4), 503),            # an unhealthy answer is not evidence
    (None, 404),
])
def test_a_health_answer_without_a_usable_count_falls_back_to_the_guess(monkeypatch, body, status) -> None:
    with health_server(body, status=status) as url, patch_in("nexus.pdf_extractor", "sys.platform", "linux"):
        assert _mineru_server_slots(url) == 3


def test_an_unreachable_server_falls_back_to_the_guess() -> None:
    with health_server(_health(max_concurrent_requests=7)) as url:
        pass  # the server is shut down: the port refuses
    with patch_in("nexus.pdf_extractor", "sys.platform", "darwin"):
        assert _mineru_server_slots(url) == 1


def test_no_url_is_the_guess_alone(monkeypatch) -> None:
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "4")
    with patch_in("nexus.pdf_extractor", "sys.platform", "linux"):
        assert _mineru_server_slots() == 4
        assert _mineru_server_slots(None) == 4


class _Reporting(World):
    """A fake MinerU whose ``/health`` says how many slots it runs."""

    def __init__(self, *a, slots: int, **kw) -> None:
        super().__init__(*a, **kw)
        self.slots = slots

    def get(self, url, timeout=None, **_kw):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"status": "healthy", "max_concurrent_requests": self.slots}
        return resp


def test_the_restart_rule_follows_the_slot_count_the_server_reports(tmp_path, monkeypatch) -> None:
    """Linux with no environment guesses 3 slots (a restart after the SECOND consecutive timeout);
    the server says 1, so the first rung-1 timeout replaces it."""
    world = _Reporting(tmp_path, 4, times_out(("server", 2, 1)), slots=1)
    ext = PDFExtractor()
    spy = _restart_spy(world)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        world.extract(ext)

    spy.assert_called_once_with(REGISTERED)


def test_a_server_that_reports_more_slots_than_the_guess_waits_for_a_second_timeout(tmp_path, monkeypatch) -> None:
    """The mirror: macOS guesses 1 slot (restart on the first timeout); the server says 3."""
    world = _Reporting(tmp_path, 4, times_out(("server", 2, 1)), slots=3)
    ext = PDFExtractor()
    spy = _restart_spy(world)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="darwin"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        world.extract(ext)

    spy.assert_not_called()
