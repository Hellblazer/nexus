# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-169 G6 staleness/dangling signal in aspect_readers.

Covers:
  staleness_signal()   — pure decision function (4-value: fresh/stale/dangling/unknown)
  stat_source()        — scheme dispatcher
  _stat_file_uri()     — via stat_source: absent vs error reason taxonomy
  _stat_obsidian_uri() — via stat_source: BLOCKED_ROOTS guard, traversal, absent vs error
  _stat_scratch_uri()  — via stat_source: canonical session URI parsing, absent vs error
  _stat_chroma_uri()   — always-fresh content-addressed scheme
  _parse_http_date_to_mtime() — pure HTTP-date parser (nexus-oqenh leg 1)
  _stat_https_uri()    — nexus-oqenh leg 1: real HEAD + Last-Modified via the
                         injectable http_client seam (Last-Modified, ETag-only,
                         neither, 404, 5xx, unparseable date, timeout)

All tests use deterministic fixed mtimes or tmp_path, or a real local loopback
HTTP server for the https:// cases (never a mocked httpx); no calls to any
real external host.
"""

import datetime
import http.server
import os
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus import aspect_readers as ar_mod
from nexus.aspect_readers import (
    StalenessSignal,
    StatFail,
    StatOk,
    _parse_http_date_to_mtime,
    _stat_https_uri,
    staleness_signal,
    stat_source,
)


# ── staleness_signal pure-function tests ──────────────────────────────────────


class TestStalenessSignal:
    """Unit tests for the staleness_signal() pure decision function."""

    def test_fresh_when_recorded_ge_current(self) -> None:
        """recorded_mtime >= current_mtime → fresh (no change since indexing)."""
        result = staleness_signal(1_000_000.0, StatOk(current_mtime=999_999.0))
        assert result == "fresh"

    def test_fresh_when_recorded_eq_current(self) -> None:
        """recorded_mtime == current_mtime → fresh (exact match)."""
        result = staleness_signal(1_000_000.0, StatOk(current_mtime=1_000_000.0))
        assert result == "fresh"

    def test_stale_when_recorded_lt_current(self) -> None:
        """recorded_mtime < current_mtime → stale (source changed after indexing)."""
        result = staleness_signal(1_000_000.0, StatOk(current_mtime=1_000_001.0))
        assert result == "stale"

    def test_stale_with_zero_recorded_mtime(self) -> None:
        """recorded_mtime=0.0 (catalog NULL→0.0 coercion) is stale when the
        scheme provides any real mtime.  Callers coerce NULL → 0.0 at SQL boundary.
        """
        result = staleness_signal(0.0, StatOk(current_mtime=1_000_000.0))
        assert result == "stale"

    def test_fresh_when_no_scheme_mtime(self) -> None:
        """StatOk(current_mtime=None) → fresh for schemes without mtime.

        Applies to chroma:// (content-addressed), nx-scratch:// (no per-entry mtime),
        and https:// in Phase A (deferred — can't check → treat as fresh).
        """
        result = staleness_signal(1_000_000.0, StatOk(current_mtime=None))
        assert result == "fresh"

    # dangling — confirmed-absent (reason="absent") only

    def test_confirmed_absent_raises_by_default(self) -> None:
        """StatFail(reason='absent') → raises ValueError with 'dangling reference'
        when allow_dangling=False.
        """
        with pytest.raises(ValueError, match="dangling reference"):
            staleness_signal(
                1_000_000.0,
                StatFail(reason="absent", detail="file not found"),
            )

    def test_confirmed_absent_returns_signal_when_allowed(self) -> None:
        """StatFail(reason='absent') + allow_dangling=True → 'dangling' without raising."""
        result = staleness_signal(
            1_000_000.0,
            StatFail(reason="absent", detail="file not found"),
            allow_dangling=True,
        )
        assert result == "dangling"

    def test_confirmed_absent_error_includes_detail(self) -> None:
        """ValueError detail from StatFail.detail is surfaced in the error message."""
        with pytest.raises(ValueError, match="file not found at /tmp/gone.txt"):
            staleness_signal(
                0.0,
                StatFail(reason="absent", detail="file not found at /tmp/gone.txt"),
            )

    # unknown — indeterminate (non-absent failures)

    def test_transient_error_returns_unknown_not_dangling(self) -> None:
        """StatFail(reason='error') → 'unknown', NEVER raises regardless of allow_dangling."""
        result = staleness_signal(
            1_000_000.0,
            StatFail(reason="error", detail="PermissionError: /tmp/secret"),
        )
        assert result == "unknown"

    def test_transient_error_unknown_with_allow_dangling_false(self) -> None:
        """Indeterminate error does NOT raise even with allow_dangling=False."""
        result = staleness_signal(
            0.0,
            StatFail(reason="error", detail="OSError: something transient"),
            allow_dangling=False,
        )
        assert result == "unknown"

    def test_deferred_scheme_returns_unknown(self) -> None:
        """StatFail(reason='deferred') → 'unknown' (check not performed, not confirmed absent)."""
        result = staleness_signal(
            1_000_000.0,
            StatFail(reason="deferred", detail="https:// deferred to Phase B"),
            allow_dangling=True,
        )
        assert result == "unknown"

    def test_scheme_unknown_returns_unknown(self) -> None:
        """StatFail(reason='scheme_unknown') → 'unknown'."""
        result = staleness_signal(
            0.0,
            StatFail(reason="scheme_unknown", detail="no handler for s3://"),
            allow_dangling=True,
        )
        assert result == "unknown"


# ── stat_source dispatcher tests ─────────────────────────────────────────────


class TestStatSourceDispatch:
    """Unit tests for stat_source() scheme routing."""

    def test_empty_uri_returns_statfail_error(self) -> None:
        result = stat_source("")
        assert isinstance(result, StatFail)
        assert result.reason == "error"

    def test_unknown_scheme_returns_statfail_scheme_unknown(self) -> None:
        result = stat_source("s3://my-bucket/key")
        assert isinstance(result, StatFail)
        assert result.reason == "scheme_unknown"

    def test_no_scheme_returns_statfail_scheme_unknown(self) -> None:
        result = stat_source("/absolute/path/without/scheme")
        assert isinstance(result, StatFail)
        assert result.reason == "scheme_unknown"


# ── file:// tests ─────────────────────────────────────────────────────────────


class TestStatFileUri:
    """stat_source for file:// — reads current mtime from real filesystem."""

    def test_existing_file_returns_statok_with_mtime(self, tmp_path: Path) -> None:
        f = tmp_path / "chunk.txt"
        f.write_text("hello")
        uri = f.as_uri()  # file:///...

        result = stat_source(uri)

        assert isinstance(result, StatOk)
        assert result.current_mtime == pytest.approx(os.stat(f).st_mtime)

    def test_missing_file_returns_statfail_absent(self, tmp_path: Path) -> None:
        """FileNotFoundError → reason='absent' (confirmed gone)."""
        uri = (tmp_path / "does-not-exist.txt").as_uri()

        result = stat_source(uri)

        assert isinstance(result, StatFail)
        assert result.reason == "absent"
        assert "FileNotFoundError" in result.detail

    def test_missing_file_produces_dangling_signal(self, tmp_path: Path) -> None:
        """Confirmed absent file → staleness_signal 'dangling' with allow_dangling."""
        f = tmp_path / "gone.txt"
        f.write_text("was here")
        uri = f.as_uri()
        f.unlink()

        signal = staleness_signal(0.0, stat_source(uri), allow_dangling=True)
        assert signal == "dangling"

    def test_missing_file_raises_without_allow_dangling(self, tmp_path: Path) -> None:
        """Confirmed absent file → raises ValueError with allow_dangling=False (default)."""
        f = tmp_path / "gone.txt"
        f.write_text("was here")
        uri = f.as_uri()
        f.unlink()

        with pytest.raises(ValueError, match="dangling reference"):
            staleness_signal(0.0, stat_source(uri))

    def test_stale_detection_via_stat_source(self, tmp_path: Path) -> None:
        """Full pipeline: file updated after indexing → staleness_signal 'stale'."""
        f = tmp_path / "doc.txt"
        f.write_text("v1")
        uri = f.as_uri()

        current = os.stat(f).st_mtime
        recorded = current - 1.0  # recorded 1s before current mtime

        signal = staleness_signal(recorded, stat_source(uri))
        assert signal == "stale"

    def test_fresh_detection_via_stat_source(self, tmp_path: Path) -> None:
        """Full pipeline: file not changed since indexing → staleness_signal 'fresh'."""
        f = tmp_path / "doc.txt"
        f.write_text("v1")
        uri = f.as_uri()

        current = os.stat(f).st_mtime
        recorded = current + 5.0  # indexed AFTER last modification

        signal = staleness_signal(recorded, stat_source(uri))
        assert signal == "fresh"


# ── obsidian:// tests ─────────────────────────────────────────────────────────


class TestStatObsidianUri:
    """stat_source for obsidian:// — uses vault_root via tenant context."""

    def test_missing_vault_root_returns_statfail_error(self) -> None:
        uri = "obsidian://open?vault=MyVault&file=notes%2Ftest.md"
        result = stat_source(uri)  # no tenant

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert "vault_root" in result.detail

    def test_blocked_vault_root_slash_rejected(self) -> None:
        """vault_root='/' is blocked — same BLOCKED_ROOTS guard as _read_obsidian_uri."""
        uri = "obsidian://open?vault=MyVault&file=notes%2Ftest.md"
        result = stat_source(uri, tenant={"vault_root": "/"})

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert "not permitted" in result.detail

    def test_existing_note_returns_statok(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "notes" / "test.md"
        note.parent.mkdir()
        note.write_text("# Test")

        uri = "obsidian://open?vault=MyVault&file=notes%2Ftest.md"
        result = stat_source(uri, tenant={"vault_root": str(vault)})

        assert isinstance(result, StatOk)
        assert result.current_mtime == pytest.approx(os.stat(note).st_mtime)

    def test_missing_note_returns_statfail_absent(self, tmp_path: Path) -> None:
        """FileNotFoundError in obsidian path → reason='absent' (confirmed gone)."""
        vault = tmp_path / "vault"
        vault.mkdir()

        uri = "obsidian://open?vault=MyVault&file=no-such-note.md"
        result = stat_source(uri, tenant={"vault_root": str(vault)})

        assert isinstance(result, StatFail)
        assert result.reason == "absent"

    def test_missing_file_param_returns_statfail_error(self, tmp_path: Path) -> None:
        uri = "obsidian://open?vault=MyVault"  # no file= param
        result = stat_source(uri, tenant={"vault_root": str(tmp_path)})

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert "file" in result.detail

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        evil = "../../etc/passwd"

        uri = f"obsidian://open?vault=MyVault&file={evil}"
        result = stat_source(uri, tenant={"vault_root": str(vault)})

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert "traversal" in result.detail

    def test_percent_encoded_traversal_rejected(self, tmp_path: Path) -> None:
        """Percent-encoded path traversal is also rejected (unquote before resolve)."""
        vault = tmp_path / "vault"
        vault.mkdir()

        uri = "obsidian://open?vault=MyVault&file=%2e%2e%2f%2e%2e%2fetc%2fpasswd"
        result = stat_source(uri, tenant={"vault_root": str(vault)})

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert "traversal" in result.detail


# ── nx-scratch:// tests ───────────────────────────────────────────────────────


class TestStatScratchUri:
    """stat_source for nx-scratch:// — canonical session URI parsing, no mtime."""

    def _make_scratch(self, entries: dict[str, str]) -> object:
        """Minimal scratch stub: get(id) returns entry or None."""

        class _Scratch:
            def get(self, entry_id: str) -> dict | None:
                val = entries.get(entry_id)
                return {"content": val} if val is not None else None

        return _Scratch()

    def test_no_scratch_client_returns_statfail_error(self) -> None:
        result = stat_source("nx-scratch://session/sess123/entry-abc123")
        assert isinstance(result, StatFail)
        assert result.reason == "error"

    def test_canonical_uri_live_entry_returns_statok_none_mtime(self) -> None:
        """Canonical nx-scratch://session/<sid>/<eid> parses entry_id correctly."""
        scratch = self._make_scratch({"entry-abc123": "content here"})
        result = stat_source(
            "nx-scratch://session/sess123/entry-abc123", scratch=scratch
        )

        assert isinstance(result, StatOk)
        assert result.current_mtime is None  # no mtime on scratch

    def test_canonical_uri_live_entry_is_always_fresh(self) -> None:
        scratch = self._make_scratch({"entry-abc123": "content here"})
        signal = staleness_signal(
            0.0,
            stat_source("nx-scratch://session/sess123/entry-abc123", scratch=scratch),
        )
        assert signal == "fresh"

    def test_canonical_uri_deleted_entry_is_absent(self) -> None:
        """Deleted scratch entry → reason='absent' (confirmed gone)."""
        scratch = self._make_scratch({})  # entry gone
        result = stat_source(
            "nx-scratch://session/sess123/entry-gone", scratch=scratch
        )
        assert isinstance(result, StatFail)
        assert result.reason == "absent"

    def test_canonical_uri_deleted_entry_produces_dangling_signal(self) -> None:
        scratch = self._make_scratch({})
        signal = staleness_signal(
            1_000_000.0,
            stat_source("nx-scratch://session/sess123/entry-gone", scratch=scratch),
            allow_dangling=True,
        )
        assert signal == "dangling"

    def test_wrong_netloc_returns_statfail_error(self) -> None:
        """Non-'session' netloc is a malformed URI → reason='error'."""
        scratch = self._make_scratch({"entry-abc123": "x"})
        result = stat_source("nx-scratch:///entry-abc123", scratch=scratch)
        assert isinstance(result, StatFail)
        assert result.reason == "error"

    def test_missing_entry_id_returns_statfail_error(self) -> None:
        """No entry-id segment → reason='error' (malformed, not confirmed absent)."""
        scratch = self._make_scratch({})
        result = stat_source("nx-scratch://session/sess123/", scratch=scratch)
        assert isinstance(result, StatFail)
        assert result.reason == "error"


# ── chroma:// tests ───────────────────────────────────────────────────────────


class TestStatChromaUri:
    """chroma:// is content-addressed — always StatOk(current_mtime=None) → fresh."""

    def test_chroma_uri_returns_statok_none_mtime(self) -> None:
        result = stat_source("chroma://tenant1/collection1/deadbeef01234567deadbeef01234567")
        assert isinstance(result, StatOk)
        assert result.current_mtime is None

    def test_chroma_is_always_fresh(self) -> None:
        result = stat_source("chroma://t/col/abc123abc123abc123abc123abc12312")
        signal = staleness_signal(0.0, result)
        assert signal == "fresh"


# ── https:// tests (nexus-oqenh leg 1: real HEAD + Last-Modified) ────────────


class TestParseHttpDateToMtime:
    """Pure-function tests for the RFC 7231 HTTP-date -> POSIX mtime parser."""

    def test_parses_standard_rfc7231_date(self) -> None:
        result = _parse_http_date_to_mtime("Wed, 21 Oct 2015 07:28:00 GMT")
        expected = datetime.datetime(2015, 10, 21, 7, 28, 0, tzinfo=datetime.UTC).timestamp()
        assert result == pytest.approx(expected)

    def test_returns_none_for_unparseable_value(self) -> None:
        assert _parse_http_date_to_mtime("not-a-date") is None

    def test_returns_none_for_empty_string(self) -> None:
        assert _parse_http_date_to_mtime("") is None


class _StatHandler(http.server.BaseHTTPRequestHandler):
    """Configurable HEAD-only handler for https:// stat tests.

    Class attributes are set per-test before a request is made; ``do_HEAD``
    replies with them.  Every test below sends a REAL HEAD request across a
    real loopback socket to this handler — httpx itself is never mocked.
    """

    response_status: int = 200
    response_headers: dict[str, str] = {}
    delay: float = 0.0

    def do_HEAD(self) -> None:  # noqa: N802 — stdlib handler method name
        if self.delay:
            import time as _time  # noqa: PLC0415 — test-local, avoids a module-level time import collision
            _time.sleep(self.delay)
        self.send_response(self.response_status)
        for key, value in self.response_headers.items():
            self.send_header(key, value)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


class _StatHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded so a slow (timeout-path) request never blocks the accept
    loop, and daemonized so a lingering slow-handler thread never blocks
    process/test-session exit."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that gave up on a slow/aborted request closes its socket
        # under the still-sleeping handler; the resulting BrokenPipeError
        # is expected in the timeout test and would otherwise print a scary
        # (but harmless) traceback to stderr on every run.
        pass


@pytest.fixture
def https_stat_server():
    """Real local HTTP server for https:// stat tests. Yields
    ``(base_url, handler_class)``; each test sets the handler's
    response_status / response_headers / delay before building its own
    ``httpx.Client(base_url=base_url, ...)`` — the injectable-client seam
    ``_stat_https_uri`` exposes for tests.
    """
    _StatHandler.response_status = 200
    _StatHandler.response_headers = {}
    _StatHandler.delay = 0.0
    srv = _StatHTTPServer(("127.0.0.1", 0), _StatHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", _StatHandler
    finally:
        srv.shutdown()
        thread.join(timeout=5)


class TestStatHttpsUri:
    """nexus-oqenh leg 1: real HEAD request via the injectable http_client
    seam.  Every case hits the real local server from ``https_stat_server``
    over loopback — no mocked httpx, no calls to any real external host.
    """

    def test_last_modified_returns_real_mtime(self, https_stat_server) -> None:
        base_url, handler = https_stat_server
        handler.response_headers = {"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"}
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/paper.pdf", http_client=client)
        finally:
            client.close()

        assert isinstance(result, StatOk)
        expected = datetime.datetime(2015, 10, 21, 7, 28, 0, tzinfo=datetime.UTC).timestamp()
        assert result.current_mtime == pytest.approx(expected)

    def test_last_modified_drives_staleness_signal(self, https_stat_server) -> None:
        base_url, handler = https_stat_server
        handler.response_headers = {"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"}
        mtime = datetime.datetime(2015, 10, 21, 7, 28, 0, tzinfo=datetime.UTC).timestamp()
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/paper.pdf", http_client=client)
        finally:
            client.close()

        assert staleness_signal(mtime + 10, result) == "fresh"
        assert staleness_signal(mtime - 10, result) == "stale"

    def test_etag_only_returns_statfail_naming_no_last_modified(self, https_stat_server) -> None:
        """ETag with no Last-Modified: nexus records no stored ETag at index
        time to compare a fresh one against, so this is a named StatFail,
        not a best-effort ETag comparison (RDR-169 G6 residual decision)."""
        base_url, handler = https_stat_server
        handler.response_headers = {"ETag": '"abc123"'}
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/paper.pdf", http_client=client)
        finally:
            client.close()

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert "Last-Modified" in result.detail
        assert "abc123" in result.detail

    def test_neither_header_returns_statfail(self, https_stat_server) -> None:
        base_url, handler = https_stat_server
        handler.response_headers = {}
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/paper.pdf", http_client=client)
        finally:
            client.close()

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert "no Last-Modified" in result.detail

    def test_unparseable_last_modified_returns_statfail(self, https_stat_server) -> None:
        base_url, handler = https_stat_server
        handler.response_headers = {"Last-Modified": "not-a-date"}
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/paper.pdf", http_client=client)
        finally:
            client.close()

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert "could not be parsed" in result.detail

    def test_404_returns_statfail_absent(self, https_stat_server) -> None:
        base_url, handler = https_stat_server
        handler.response_status = 404
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/gone.pdf", http_client=client)
        finally:
            client.close()

        assert isinstance(result, StatFail)
        assert result.reason == "absent"
        assert "404" in result.detail

    def test_404_produces_dangling_signal_when_allowed(self, https_stat_server) -> None:
        base_url, handler = https_stat_server
        handler.response_status = 404
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/gone.pdf", http_client=client)
        finally:
            client.close()

        assert staleness_signal(0.0, result, allow_dangling=True) == "dangling"

    def test_404_raises_without_allow_dangling(self, https_stat_server) -> None:
        base_url, handler = https_stat_server
        handler.response_status = 404
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/gone.pdf", http_client=client)
        finally:
            client.close()

        with pytest.raises(ValueError, match="dangling reference"):
            staleness_signal(0.0, result)

    def test_5xx_returns_statfail_error_after_bounded_retry(self, https_stat_server) -> None:
        base_url, handler = https_stat_server
        handler.response_status = 503
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/broken.pdf", http_client=client)
        finally:
            client.close()

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert "503" in result.detail

    def test_5xx_produces_unknown_signal_never_raises(self, https_stat_server) -> None:
        base_url, handler = https_stat_server
        handler.response_status = 503
        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            result = _stat_https_uri("/broken.pdf", http_client=client)
        finally:
            client.close()

        assert staleness_signal(1_000_000.0, result) == "unknown"

    def test_slow_handler_times_out_and_returns_statfail(self, https_stat_server) -> None:
        """The handler sleeps far longer than the client's own read timeout,
        so every bounded-retry attempt times out client-side; this must
        return promptly (bounded retry, never hang) with an indeterminate
        StatFail rather than raising."""
        base_url, handler = https_stat_server
        handler.delay = 1.0
        client = httpx.Client(base_url=base_url, timeout=0.1)
        try:
            result = _stat_https_uri("/slow.pdf", http_client=client)
        finally:
            client.close()

        assert isinstance(result, StatFail)
        assert result.reason == "error"
        assert staleness_signal(1_000_000.0, result) == "unknown"


class TestStatSourceHttpsForwarding:
    """stat_source threads http_client through to whichever handler is
    registered for the https:// scheme.  Verified via the internal
    dispatch registry — no network here; the real HEAD round-trip is
    covered by TestStatHttpsUri above against a real local server."""

    def test_forwards_http_client_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _recorder(uri: str, *, http_client: Any = None, **_kw: Any) -> StatOk:
            captured["uri"] = uri
            captured["http_client"] = http_client
            return StatOk(current_mtime=123.0)

        monkeypatch.setitem(ar_mod._STAT_HANDLERS, "https", _recorder)
        sentinel = object()

        result = stat_source("https://example.com/doc", http_client=sentinel)

        assert result == StatOk(current_mtime=123.0)
        assert captured["uri"] == "https://example.com/doc"
        assert captured["http_client"] is sentinel
