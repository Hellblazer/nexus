# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/wait_pypi_simple_index.py`` (nexus-r433b).

PyPI's simple index lags the upload API by ~10-25 minutes; resolvers read
the simple index, so a pinned ``==``/``>=`` resolution hard-fails inside
that window (measured on four consecutive releases). The script polls the
resolver-visible signal — the simple endpoint with the PEP 691 JSON Accept
header — until the version appears or a deadline passes. Terminal failure
is the deadline ONLY; every transient shape (non-200, unreachable,
malformed payload) polls through.

``scripts/`` is on ``pythonpath`` via ``[tool.pytest.ini_options]``, so the
module imports directly. HTTP is exercised against a real local
``http.server`` on port 0 (no mocks at the network boundary).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import wait_pypi_simple_index as gate


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Serves a scripted sequence of responses; repeats the last one."""

    script: list[tuple[int, bytes]] = []
    seen: list[dict] = []
    _index = 0

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        cls = type(self)
        cls.seen.append({"path": self.path, "accept": self.headers.get("Accept")})
        status, body = cls.script[min(cls._index, len(cls.script) - 1)]
        cls._index += 1
        self.send_response(status)
        self.send_header("Content-Type", gate.SIMPLE_V1_JSON)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence per-request stderr noise
        pass


@pytest.fixture
def server():
    """Real HTTP server on port 0; yields a factory that scripts responses
    and returns the base index URL."""
    httpd = HTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def _arm(script: list[tuple[int, bytes]]) -> str:
        _ScriptedHandler.script = script
        _ScriptedHandler.seen = []
        _ScriptedHandler._index = 0
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _arm
    httpd.shutdown()
    httpd.server_close()


def _files(versions: list[str]) -> list[dict]:
    """PEP 691 files entries: one wheel + one sdist per version, with the
    PEP 427 dash-to-underscore escape wheels actually use."""
    files: list[dict] = []
    for v in versions:
        files.append({"filename": f"conexus-{v.replace('-', '_')}-py3-none-any.whl"})
        files.append({"filename": f"conexus-{v}.tar.gz"})
    return files


def _body(versions: list[str], files_for: list[str] | None = None) -> bytes:
    """Payload whose files array matches its versions list unless
    ``files_for`` narrows it (the nexus-tt5vm lagging-files shape)."""
    served = versions if files_for is None else files_for
    return json.dumps(
        {"name": "conexus", "versions": versions, "files": _files(served)}
    ).encode()


def test_version_present_succeeds_first_probe(server, capsys):
    url = _arm_url = server([(200, _body(["7.24.1", "7.25.0"]))])
    rc = gate.wait_for_version("conexus", "7.25.0", index_url=url, timeout_seconds=5, poll_seconds=0.05)
    assert rc == 0
    assert len(_ScriptedHandler.seen) == 1
    # The resolver-visible signal: the PEP 691 Accept header on /<package>/.
    assert _ScriptedHandler.seen[0]["accept"] == gate.SIMPLE_V1_JSON
    assert _ScriptedHandler.seen[0]["path"] == "/conexus/"
    assert "OK" in capsys.readouterr().out


def test_lagging_index_polls_until_version_appears(server):
    lagging = _body(["7.24.1"])
    caught_up = _body(["7.24.1", "7.25.0"])
    url = server([(200, lagging), (200, lagging), (200, caught_up)])
    rc = gate.wait_for_version("conexus", "7.25.0", index_url=url, timeout_seconds=10, poll_seconds=0.05)
    assert rc == 0
    assert len(_ScriptedHandler.seen) == 3


def test_deadline_exceeded_fails_loud(server, capsys):
    url = server([(200, _body(["7.24.1"]))])
    rc = gate.wait_for_version("conexus", "7.25.0", index_url=url, timeout_seconds=0.3, poll_seconds=0.05)
    assert rc == 1
    err = capsys.readouterr().err
    assert "propagation" in err
    assert "7.25.0" in err


@pytest.mark.parametrize(
    "transient",
    [
        (503, b"upstream connect error"),
        (404, b"not found"),
        (200, b"this is not json"),
        (200, json.dumps({"name": "conexus"}).encode()),  # no versions key
        (200, json.dumps({"versions": "7.25.0"}).encode()),  # wrong type
        (200, json.dumps({"versions": ["7.25.0"]}).encode()),  # no files key
        (200, json.dumps({"versions": ["7.25.0"], "files": "x"}).encode()),
    ],
    ids=[
        "503", "404", "not-json", "no-versions-key", "versions-not-a-list",
        "version-listed-but-no-files-key", "files-not-a-list",
    ],
)
def test_transient_shapes_poll_through_to_success(server, transient):
    """Every non-success response shape is 'not yet', never terminal."""
    url = server([transient, (200, _body(["7.25.0"]))])
    rc = gate.wait_for_version("conexus", "7.25.0", index_url=url, timeout_seconds=10, poll_seconds=0.05)
    assert rc == 0
    assert len(_ScriptedHandler.seen) == 2


def test_version_listed_but_wheel_lagging_polls_until_files_catch_up(server, capsys):
    """The nexus-tt5vm live shape (7.26.0 publish): the index's versions
    list gains the release ~70s BEFORE its files array does, and a resolver
    still fails in that window. A wait keyed on versions alone green'd
    straight into `uv tool install` failing 'no version of conexus==7.26.0';
    the success criterion is versions AND the version's wheel in files."""
    lagging = _body(["7.25.0", "7.26.0"], files_for=["7.25.0"])
    caught_up = _body(["7.25.0", "7.26.0"])
    url = server([(200, lagging), (200, caught_up)])
    rc = gate.wait_for_version("conexus", "7.26.0", index_url=url, timeout_seconds=10, poll_seconds=0.05)
    assert rc == 0
    assert len(_ScriptedHandler.seen) == 2
    out = capsys.readouterr().out
    assert "not yet in the files array" in out
    assert "wheel present" in out


def test_sdist_alone_is_not_installability(server):
    """An sdist for the version without its wheel keeps polling — the gate
    waits for the artifact uv actually installs, not any file at all."""
    sdist_only = json.dumps(
        {
            "versions": ["7.26.0"],
            "files": [{"filename": "conexus-7.26.0.tar.gz"}],
        }
    ).encode()
    url = server([(200, sdist_only), (200, _body(["7.26.0"]))])
    rc = gate.wait_for_version("conexus", "7.26.0", index_url=url, timeout_seconds=10, poll_seconds=0.05)
    assert rc == 0
    assert len(_ScriptedHandler.seen) == 2


def test_serves_wheel_matches_pep427_escaped_filenames():
    """Pure-function pin: wheel filenames escape non-alphanumeric runs in
    both distribution and version to '_' (PEP 427); a dashed package or
    version must still match, and a different version must not."""
    payload = {
        "files": [
            {"filename": "some_pkg-1.0.post1-py3-none-any.whl"},
            {"filename": "some_pkg-1.0.post1.tar.gz"},
        ]
    }
    assert gate.serves_wheel(payload, "some-pkg", "1.0.post1") is True
    assert gate.serves_wheel(payload, "some-pkg", "1.0") is False
    assert gate.serves_wheel(payload, "other-pkg", "1.0.post1") is False
    assert gate.serves_wheel({"files": []}, "some-pkg", "1.0.post1") is False
    # The escape is the spec's regex verbatim: '_' is \w and SURVIVES —
    # a pre-existing double underscore must not collapse (critic finding 1;
    # a hand-rolled isalnum() loop gets exactly this wrong).
    assert gate._wheel_escape("foo__bar") == "foo__bar"
    assert gate._wheel_escape("weird-tag") == "weird_tag"
    assert gate._wheel_escape("a -+ b") == "a_b"


def test_below_served_max_fast_exits_code_three(server, capsys):
    """Propagation lag only affects the NEWEST release: an index already
    serving strictly past the requested version will never gain it, so the
    script exits immediately (one probe) with the DISTINCT code 3 — not 0 —
    so a caller with a bounded post-wait retry window (fresh-install-mvv)
    can tell "proceed, retry once at most" from "served, retry for the
    window": an operator typo must burn neither the poll budget nor the
    caller's retry window (nexus-r433b + nexus-tt5vm critic finding 2).
    3 rather than 2 because argparse exits 2 on a usage error."""
    url = server([(200, _body(["7.24.1", "7.25.0"]))])
    rc = gate.wait_for_version("conexus", "1.2.3", index_url=url, timeout_seconds=30, poll_seconds=5)
    assert rc == 3
    assert len(_ScriptedHandler.seen) == 1
    assert "NOT A PROPAGATION WAIT" in capsys.readouterr().out


def test_below_served_max_with_require_served_fails_loud(server, capsys):
    """substantive-critic (2026-08-31): release.yml's next step is the
    GitHub release — the announcement — with no downstream check, so the
    below-max case must FAIL there, never proceed. Trigger: a
    workflow_dispatch retry of an old tag after a newer version shipped,
    with that old upload missing from the index."""
    url = server([(200, _body(["7.24.1", "7.25.0"]))])
    rc = gate.wait_for_version(
        "conexus", "1.2.3", index_url=url, timeout_seconds=30, poll_seconds=5,
        require_served=True,
    )
    assert rc == 1
    assert len(_ScriptedHandler.seen) == 1
    assert "require-served" in capsys.readouterr().err


def test_require_served_does_not_affect_the_served_case(server):
    url = server([(200, _body(["7.25.0"]))])
    rc = gate.main(
        [
            "--version", "7.25.0",
            "--index-url", url,
            "--timeout-seconds", "5",
            "--poll-seconds", "0.05",
            "--require-served",
        ]
    )
    assert rc == 0


def test_ambiguous_version_never_fast_exits(server):
    """A version whose numeric prefix cannot be parsed confidently keeps
    polling (conservative) rather than risking a wrong fast-exit."""
    lagging = _body(["7.24.1", "7.25.0"])
    caught_up = _body(["7.24.1", "7.25.0", "weird-tag"])
    url = server([(200, lagging), (200, caught_up)])
    rc = gate.wait_for_version("conexus", "weird-tag", index_url=url, timeout_seconds=10, poll_seconds=0.05)
    assert rc == 0
    assert len(_ScriptedHandler.seen) == 2


def test_unreachable_index_is_transient_then_deadline():
    """Nothing listening at all: probe returns None, loop runs to deadline."""
    # Port 0 bind-then-close gives a port with no listener.
    probe = HTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    dead_url = f"http://127.0.0.1:{probe.server_address[1]}"
    probe.server_close()
    rc = gate.wait_for_version("conexus", "7.25.0", index_url=dead_url, timeout_seconds=0.3, poll_seconds=0.05)
    assert rc == 1


def test_main_cli_wiring(server):
    url = server([(200, _body(["7.25.0"]))])
    rc = gate.main(
        [
            "--package", "conexus",
            "--version", "7.25.0",
            "--index-url", url,
            "--timeout-seconds", "5",
            "--poll-seconds", "0.05",
        ]
    )
    assert rc == 0
