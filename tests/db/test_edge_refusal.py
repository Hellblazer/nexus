# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cmzib: edge/WAF refusals must surface as structured errors, never
raw HTML — and name the shell-substitution trigger when the request body
carries it.

Measured 2026-08-20: three CLI puts — plain text stored; text with plain
shell stored; text containing substitution syntax refused by the edge with
an HTML 403 that the MCP tool relayed verbatim, no hint. The classifier is
the nexus-1jtob POSITIVE Server-header test (``awselb/``), shared from the
T3 vector client; T2 (the mixin's ``_raise_for_status``) and T1
(``HttpScratchStore._post``/``_post_raw``) adopt it here.

The fake server is a real ``http.server`` on port 0 — the assertion rides
actual httpx responses, headers included, not hand-built Response objects.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from nexus.db.edge_refusal import edge_refusal_message, shell_substitution_hint

# Assembled, not literal — mirrors the production module's own discipline so
# this test file can itself be indexed/persisted through the edge it tests.
SUBST_PAREN = "$" + "("
SUBST_BRACE = "$" + "{"

_EDGE_HTML = b"<html><body><h1>403 Forbidden</h1></body></html>"


class _EdgeHandler(BaseHTTPRequestHandler):
    """Plays the AWS edge: HTML 403 with the awselb Server header."""

    server_header: str | None = "awselb/2.0"
    status: int = 403

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        cls = type(self)
        _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(cls.status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_EDGE_HTML)

    def send_response(self, code, message=None):
        # send_response normally adds its own Server header via
        # version_string(); emit exactly the scripted one (or none).
        self.log_request(code)
        self.send_response_only(code, message)
        if type(self).server_header is not None:
            self.send_header("Server", type(self).server_header)
        self.send_header("Date", self.date_time_string())

    def log_message(self, *args):
        pass


@pytest.fixture
def edge_server():
    httpd = HTTPServer(("127.0.0.1", 0), _EdgeHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def _arm(server_header: str | None = "awselb/2.0", status: int = 403) -> str:
        _EdgeHandler.server_header = server_header
        _EdgeHandler.status = status
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _arm
    httpd.shutdown()
    httpd.server_close()


# ── the hint ────────────────────────────────────────────────────────────────


def test_hint_fires_on_both_substitution_shapes():
    assert shell_substitution_hint(f"echo {SUBST_PAREN}date)") is not None
    assert shell_substitution_hint(f"x={SUBST_BRACE}HOME}}") is not None


def test_hint_silent_on_plain_shell_text():
    # The measured negative case: 'git checkout -- file' stored fine.
    assert shell_substitution_hint("git checkout -- file && echo $HOME") is None
    assert shell_substitution_hint("") is None


def test_hint_text_is_self_safe():
    """The hint may itself be persisted through the edge it describes — it
    must not contain the trigger it names."""
    hint = shell_substitution_hint(SUBST_PAREN)
    assert hint is not None
    assert SUBST_PAREN not in hint
    assert SUBST_BRACE not in hint
    assert "nexus-cmzib" in hint


# ── the classifier ──────────────────────────────────────────────────────────


def test_no_server_header_is_not_reframed():
    assert edge_refusal_message("op", 403, httpx.Headers({}), "body") is None


def test_nginx_server_is_not_reframed():
    # nexus-1jtob positive-test contract: the engine sits behind an nginx TLS
    # sidecar, so 'not awselb' must never be read as 'therefore edge'.
    headers = httpx.Headers({"server": "nginx/1.25"})
    assert edge_refusal_message("op", 403, headers, "body") is None


def test_edge_message_names_edge_and_appends_hint():
    headers = httpx.Headers({"server": "awselb/2.0"})
    msg = edge_refusal_message("T2 put", 403, headers, f"note {SUBST_PAREN}echo)")
    assert msg is not None
    assert "EDGE" in msg
    assert "nexus-cmzib" in msg
    msg_plain = edge_refusal_message("T2 put", 403, headers, "plain body")
    assert msg_plain is not None
    assert "nexus-cmzib" not in msg_plain  # hint only when the trigger is present


# ── through the T2 mixin ────────────────────────────────────────────────────


def test_t2_put_surfaces_structured_refusal_not_html(edge_server):
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    url = edge_server()
    store = HttpMemoryStore(base_url=url, _token="t")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        store.put("proj", "title", f"guard note {SUBST_PAREN}echo a)")
    msg = str(exc.value)
    assert "<html" not in msg.lower()
    assert "EDGE" in msg
    assert "nexus-cmzib" in msg


def test_t2_non_edge_403_keeps_existing_rendering(edge_server):
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    url = edge_server(server_header=None)
    store = HttpMemoryStore(base_url=url, _token="t")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        store.put("proj", "title", "plain")
    # No edge signature: the pre-cmzib rendering (status + body detail) stands.
    assert "HTTP 403" in str(exc.value)
    assert "EDGE" not in str(exc.value)


# ── through the T1 scratch store ────────────────────────────────────────────


def _scratch_store(url: str, monkeypatch):
    from nexus.db.http_scratch_store import HttpScratchStore

    store = HttpScratchStore(base_url=url, session_id="edge-test", _token="t", _session_token="s")
    # This box may be ARMED (a live mint_token credential): the 401 self-heal
    # chain must never reach the real lease files or mint against the real
    # engine from a unit test. Pin both heal legs to "did not heal".
    monkeypatch.setattr(store, "_refresh_session_token_from_lease", lambda t: False)
    monkeypatch.setattr(store, "_remint_data_token_and_rebuild", lambda: False)
    return store


def test_t1_post_surfaces_structured_refusal_not_html(edge_server, monkeypatch):
    monkeypatch.setenv("NX_SERVICE_TOKEN", "t")
    url = edge_server()
    store = _scratch_store(url, monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        store._post("/v1/t1/put", {"content": f"note {SUBST_BRACE}HOME}}"})
    msg = str(exc.value)
    assert "<html" not in msg.lower()
    assert "EDGE" in msg
    assert "nexus-cmzib" in msg


def test_t1_edge_401_short_circuits_the_heal_chain(edge_server, monkeypatch):
    """critic kopmj: an edge-authored 401 (hypothetical — conexus verified
    their edge never authors one post-forward, but the precedence must not
    depend on that) must neither be misread as a stale session token NOR
    burn the heal chain: lease refresh / re-mint cannot fix a rejection the
    application never saw."""
    from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER

    monkeypatch.setenv("NX_SERVICE_TOKEN", "t")
    url = edge_server(status=401)
    store = _scratch_store(url, monkeypatch)
    heal_calls: list[str] = []
    monkeypatch.setattr(
        store, "_refresh_session_token_from_lease",
        lambda t: heal_calls.append("lease") or False,
    )
    monkeypatch.setattr(
        store, "_remint_data_token_and_rebuild",
        lambda: heal_calls.append("mint") or False,
    )
    for call in (lambda: store._post("/v1/t1/put", {"content": "x"}),
                 lambda: store._post_raw("/v1/t1/get", {"id": "x"})):
        with pytest.raises(RuntimeError) as exc:
            call()
        assert SESSION_UNAUTHORIZED_MARKER not in str(exc.value)
        assert "EDGE" in str(exc.value)
    assert heal_calls == [], "edge-authored 401 must not trigger lease refresh or re-mint"


def test_edge_5xx_renders_availability_not_waf_text(edge_server):
    """critic ql1wf: an edge-authored 5xx is an availability event, not the
    WAF's deterministic body match — the message must not send an operator
    to the WAF logs (or suggest defanging) during an outage."""
    headers = httpx.Headers({"server": "awselb/2.0"})
    msg = edge_refusal_message("T2 put", 503, headers, f"note {SUBST_PAREN}echo)")
    assert msg is not None
    assert "EDGE" in msg
    assert "WAF" not in msg
    assert "nexus-cmzib" not in msg  # no defang hint on availability errors
    assert "transient" in msg.lower() or "outage" in msg.lower()


# ── T3 remedy hint (the nexus-1jtob text gains the cmzib hint) ─────────────


def test_t3_remedy_appends_hint_only_for_trigger_bodies():
    from nexus.db.http_vector_client import _edge_refusal_remedy

    body = json.dumps({"content": f"probe {SUBST_PAREN}echo)"})
    with_hint = _edge_refusal_remedy("awselb/2.0", 403, body)
    assert "nexus-cmzib" in with_hint
    without = _edge_refusal_remedy("awselb/2.0", 403, json.dumps({"content": "plain"}))
    assert "nexus-cmzib" not in without
    assert "nexus-1jtob" in without  # the original remedy text is intact
