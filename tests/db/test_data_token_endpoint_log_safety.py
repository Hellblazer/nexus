# SPDX-License-Identifier: AGPL-3.0-or-later
"""Log events and error messages carry ``host[:port]``, never userinfo or a
query (nexus-q26w1).

``_host`` is the whole netloc, which includes ``user:password@`` when the
configured ``service_url`` carries it (``service_url`` is used verbatim and
nothing validates it). It stays as the hash input of the lease and mint-lock
file names, so no file is renamed, but every log event and message goes
through ``_log_endpoint``.
"""
from __future__ import annotations

import hashlib
import logging
import time
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from nexus._log_safe_url import scrub_transport_error
from nexus.db import data_token
from nexus.db.data_token import (
    DataTokenManager,
    DataTokenMintError,
    _data_token_lease_path,
    _data_token_mint_lock_path,
    _host,
    _lease_key,
    _log_endpoint,
)
from tests._module_seam import setattr_in

PW = "SECRETPW-9f3a"
QUERY = "SECRETQUERY-77c1"
TENANT = "acme"
URL = f"http://user:{PW}@127.0.0.1:9999/p?token={QUERY}"
SECRETS = (PW, QUERY, f"user:{PW}", f"token={QUERY}")


@pytest.fixture(autouse=True)
def debug_logging() -> Iterator[None]:
    prior = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=prior)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _assert_clean(surface: Any) -> None:
    text = repr(surface)
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} reached {text[:300]}"


def _manager(cfg: Path, poster: Any, **kw: Any) -> DataTokenManager:
    return DataTokenManager(
        poster=poster, mint_credential=lambda: "mintcred", mint_tenant=lambda: "",
        config_dir=cfg, sleep=lambda s: None, **kw,
    )


def _ok(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
    return 200, {"data_token": "tok-minted", "expires_in_seconds": 300}, {}


# ── the helper ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:9999", "127.0.0.1:9999"),
        (URL, "127.0.0.1:9999"),
        ("https://u:p@example.com/x?y=1#z", "example.com"),
        ("https://example.com:443/", "example.com:443"),
        ("tok-NAKED@host:80", "<unparseable-endpoint>"),  # no scheme: the old fallback echoed it
        ("http://:80", "<unparseable-endpoint>"),
        ("http://h:notaport", "<unparseable-endpoint>"),
        ("", "<unparseable-endpoint>"),
    ],
)
def test_log_endpoint_is_host_and_port_only(url: str, expected: str) -> None:
    assert _log_endpoint(url) == expected


def test_the_old_helper_is_the_netloc_and_still_the_hash_input() -> None:
    """Pins both halves of the split: ``_host`` keeps returning the netloc
    (so ``_lease_key`` and every file name on disk is unchanged), and the key
    is that netloc's digest."""
    assert _host(URL) == f"user:{PW}@127.0.0.1:9999"
    assert _lease_key(URL, TENANT) == hashlib.sha256(f"user:{PW}@127.0.0.1:9999\x00{TENANT}".encode()).hexdigest()
    # a plain endpoint's file names, as written by every release so far
    plain = hashlib.sha256(f"127.0.0.1:9999\x00{TENANT}".encode()).hexdigest()
    assert _lease_key("http://127.0.0.1:9999", TENANT) == plain
    cfg = Path("/cfg")
    assert _data_token_lease_path("http://127.0.0.1:9999", TENANT, cfg).name == f"data_token_lease.{plain}"
    assert _data_token_mint_lock_path("http://127.0.0.1:9999", TENANT, cfg).name == f"data_token_mint_lock.{plain}"


def test_scrub_withholds_text_that_quotes_the_credentials_and_keeps_the_rest() -> None:
    quoted = ValueError(f"nonnumeric port: '{PW}@127.0.0.1' while fetching {URL}")
    out = scrub_transport_error(quoted, URL)
    _assert_clean(out)
    assert out.startswith("ValueError") and out.endswith("127.0.0.1:9999")
    # text that quotes nothing of the URL's credentials survives unchanged
    assert scrub_transport_error(OSError("connection refused"), "http://h:80") == "connection refused"
    assert scrub_transport_error(OSError("plain"), "http://u:pw@h:80") == "plain"


# Each is a form of the same secret that http.client / urllib can quote:
# percent-encoded, colon-bearing, decoded, split on the last ':'.
_SECRET_URLS = [
    pytest.param("https://user:p%40ssSECRET@127.0.0.1", id="percent-encoded-at"),
    pytest.param("https://user:a:b:SECRET@127.0.0.1", id="colons-in-password"),
    pytest.param("https://user:p%3AwSECRET@127.0.0.1", id="percent-encoded-colon"),
    pytest.param(f"https://user:{PW}@127.0.0.1", id="plain"),
    pytest.param("https://user:p%40ssSECRET@127.0.0.1?token=SECRETQ", id="encoded-with-query"),
]


@pytest.mark.parametrize("url", _SECRET_URLS)
def test_the_real_poster_error_carries_no_form_of_the_secret_anywhere(tmp_path: Path, url: str) -> None:
    """Through the real ``_default_poster`` and the real manager. No port, so
    ``http.client`` raises ``InvalidURL`` quoting a decoded or truncated tail of
    the password before any network I/O. The secret, in no form, may appear
    in an event, in the error message, or in the formatted traceback (the
    error is not chained to the original)."""
    with structlog.testing.capture_logs() as captured, pytest.raises(DataTokenMintError) as err:
        _manager(tmp_path, data_token._default_poster).bearer_for(url, TENANT)
    failed = [e for e in captured if e["event"] == "data_token_mint_failed"]
    assert len(failed) == 1
    assert failed[0]["error"].startswith("InvalidURL")  # the type survives, the text does not
    assert failed[0]["endpoint"] == "127.0.0.1"
    rendered = repr(captured) + str(err.value) + "".join(traceback.format_exception(err.value))
    for secret in ("SECRET", PW, "p@ss", "p%40ss", "p%3A", "a:b", "wSECRET", "user:"):
        assert secret not in rendered, f"{secret!r} reached the output"
    assert err.value.__cause__ is None and err.value.__context__ is None


def test_each_scrub_rule_stands_alone() -> None:
    """The rules overlap on the common quotes, so each is pinned by a text only
    it catches."""
    url = "https://user:p%3AwSECRET@127.0.0.1"
    # an '@' alone (no form of the credentials in the text) is withheld
    out = scrub_transport_error(ValueError("bad netloc 'zz@127.0.0.1'"), url)
    assert "zz" not in out and out.startswith("ValueError")
    # a ':'-split piece of the decoded password, quoted without any '@'
    out = scrub_transport_error(ValueError("nonnumeric port: 'wSECRET'"), url)
    assert "wSECRET" not in out and out.startswith("ValueError")
    # the decoded whole password, quoted without any '@'
    out = scrub_transport_error(ValueError("rejected p:wSECRET"), url)
    assert "wSECRET" not in out
    # the query, quoted without any '@'
    out = scrub_transport_error(ValueError("GET failed ?token=QSECRETQ"), "https://h:1/p?token=QSECRETQ")
    assert "QSECRETQ" not in out
    # a short piece is matched as a whole token only: no spurious withholding
    assert scrub_transport_error(OSError("timed out after 30s"), "https://u:ab@h:1") == "timed out after 30s"


def test_the_scrub_does_not_depend_on_the_url_parsing() -> None:
    """A URL that does not parse is itself the secret."""
    assert "tok-NAKED" not in scrub_transport_error(ValueError("bad url tok-NAKED@host:80"), "tok-NAKED@host:80")
    assert "tok-NAKED" not in scrub_transport_error(ValueError("bad url tok-NAKED"), "tok-NAKED@host:80")


# ── every event across mint, reuse, renew, invalidate and lease write ────


def test_no_event_carries_userinfo_or_query_across_the_whole_lifecycle(tmp_path: Path) -> None:
    clock = _Clock()
    wall = _Clock()
    wall.t = time.time()
    with structlog.testing.capture_logs() as captured:
        m = _manager(tmp_path, _ok, clock=clock, wall_clock=wall)
        assert m.bearer_for(URL, TENANT) == "tok-minted"  # data_token_minted, lock created
        # a cold process borrows the published lease: data_token_lease_reused
        assert _manager(tmp_path, _ok, clock=_Clock(), wall_clock=wall).bearer_for(URL, TENANT) == "tok-minted"
        # renew: past the refresh threshold -> data_token_refresh + a lease write
        clock.t += 290
        wall.t += 290
        m.bearer_for(URL, TENANT)
        # invalidate: data_token_invalidated
        m.invalidate(URL, TENANT)
        # a damaged lease: data_token_lease_unparseable
        _data_token_lease_path(URL, TENANT, tmp_path).write_text("{not json")
        _manager(tmp_path, _ok, clock=_Clock(), wall_clock=wall).bearer_for(URL, TENANT)
    events = {e["event"] for e in captured}
    assert {
        "data_token_minted", "data_token_lease_reused", "data_token_refresh",
        "data_token_invalidated", "data_token_lease_unparseable", "data_token_mint_lock_created",
    } <= events
    endpoints = {e["endpoint"] for e in captured if "endpoint" in e}
    assert endpoints == {"127.0.0.1:9999"}
    _assert_clean(captured)


def test_lease_write_failure_event_carries_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(src: Any, dst: Any) -> None:
        raise OSError(f"replace refused for {dst}")

    setattr_in(monkeypatch, data_token, "os.replace", refuse)
    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path, _ok).bearer_for(URL, TENANT) == "tok-minted"
    failed = [e for e in captured if e["event"] == "data_token_lease_write_failed"]
    assert len(failed) == 1 and failed[0]["endpoint"] == "127.0.0.1:9999"
    _assert_clean(captured)


def test_lock_unavailable_event_carries_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: Any, **k: Any) -> int:
        raise PermissionError("no lock for you")

    monkeypatch.setattr(data_token, "_open_mint_lock_logged", refuse)
    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path, _ok).bearer_for(URL, TENANT) == "tok-minted"
    assert any(e["event"] == "data_token_mint_lock_unavailable" for e in captured)
    _assert_clean(captured)


def test_retry_and_failure_events_and_messages_carry_no_secret(tmp_path: Path) -> None:
    statuses = iter([(429, {}, {}), (200, {"data_token": "t", "expires_in_seconds": 300}, {})])

    def flaky(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        return next(statuses)

    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path, flaky).bearer_for(URL, TENANT) == "t"
    assert any(e["event"] == "data_token_mint_retry" for e in captured)
    _assert_clean(captured)

    for status, payload in ((403, {"error": "bound to other"}), (500, {"error": "boom"}), (200, {})):
        def failing(url: str, headers: dict, body: dict, _s: int = status, _p: dict = payload) -> tuple[int, dict, dict]:
            return _s, _p, {}

        cfg = tmp_path / f"c{status}"
        cfg.mkdir()
        with structlog.testing.capture_logs() as captured, pytest.raises(DataTokenMintError) as err:
            _manager(cfg, failing).bearer_for(URL, TENANT)
        assert any(e["event"] == "data_token_mint_failed" for e in captured)
        _assert_clean(captured)
        _assert_clean(str(err.value))
        assert "127.0.0.1:9999" in str(err.value)


def test_a_transport_error_that_quotes_the_url_is_scrubbed(tmp_path: Path) -> None:
    def quoting(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        raise RuntimeError(f"cannot connect to {url} (user:{PW}@)")

    with structlog.testing.capture_logs() as captured, pytest.raises(DataTokenMintError) as err:
        _manager(tmp_path, quoting).bearer_for(URL, TENANT)
    _assert_clean(captured)
    _assert_clean(str(err.value))


def test_a_transport_error_that_quotes_the_url_without_an_at_sign_is_still_withheld(tmp_path: Path) -> None:
    def quoting(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        raise RuntimeError(f"gave up on user and {PW}")

    with structlog.testing.capture_logs() as captured, pytest.raises(DataTokenMintError) as err:
        _manager(tmp_path, quoting).bearer_for(URL, TENANT)
    _assert_clean(captured)
    _assert_clean(str(err.value))
    _assert_clean("".join(traceback.format_exception(err.value)))
    assert "RuntimeError" in str(err.value)
