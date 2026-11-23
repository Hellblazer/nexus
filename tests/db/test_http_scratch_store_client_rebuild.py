# SPDX-License-Identifier: AGPL-3.0-or-later
"""The T1 scratch store's HTTP client must survive its CA bundle vanishing.

Incident 2026-11-21/22 (sessions tamsin, elsinore, kestrel): a long-lived MCP
server's T1 scratch call failed once with a pathless
``[Errno 2] No such file or directory``, then every later scratch call in
that process failed with httpx's ``Cannot send a request, as the client has
been closed.`` until the session restarted. Every affected server ran from an
install generation that no longer existed.

Mechanism: ``httpx.Client()`` eagerly builds an SSL context, reading the CA
bundle (``certifi.where()`` inside the install tree, or ``SSL_CERT_FILE``)
from disk; a missing file raises a ``FileNotFoundError`` with no filename.
``HttpScratchStore`` rebuilt its client on a 401 heal or an endpoint rebind
by closing the old client FIRST and building the new one second, so the
failed build left the cached store holding a closed client for good.

These tests delete the CA file mid-session (``SSL_CERT_FILE`` at a tmp copy)
and pin: the heal still works (one process-wide SSL context, read once); a
failed rebuild leaves the old client open; a closed client is rebuilt on the
next call; and a CA-bundle failure names the path.
"""

from __future__ import annotations

import shutil

import certifi
import httpx
import pytest

from nexus.db.http_scratch_store import DEFAULT_TENANT, HttpScratchStore
from nexus.db.t1 import publish_t1_session_lease
from nexus.db.t2 import shared_ssl
from tests.db._fake_t2_server import fake_http_server
from tests.db.test_http_scratch_store import (
    _REQUIRED_T1_TOKEN,
    _STORE,
    SESSION,
    TOKEN,
    _FakeScratchHandler,
)

FRESH = "fresh-rotated-token"
STALE = "stale-borrowed-token"


@pytest.fixture(scope="module")
def fake_server():
    with fake_http_server(_FakeScratchHandler) as url:
        yield url


@pytest.fixture
def ca_file(tmp_path, monkeypatch):
    """A CA bundle the test can delete, with the process-wide SSL context
    cleared so the next client build reads THIS file (monkeypatch restores
    the cached context afterwards)."""
    path = tmp_path / "ca" / "cacert.pem"
    path.parent.mkdir()
    shutil.copyfile(certifi.where(), path)
    monkeypatch.setenv("SSL_CERT_FILE", str(path))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(shared_ssl, "_shared_ssl_context", None)
    return path


@pytest.fixture
def gated_store(fake_server, tmp_path, monkeypatch, ca_file):
    """A borrower holding a rotated-out session token: its next call 401s and
    heals from the lease, which rebuilds the client."""
    _STORE.clear()
    _REQUIRED_T1_TOKEN["token"] = FRESH
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_T1_SESSION", STALE)
    monkeypatch.setenv("NX_T1_SESSION_ID", SESSION)
    store = HttpScratchStore(
        base_url=fake_server, tenant=DEFAULT_TENANT, session_id=SESSION,
        _token=TOKEN, _session_token=STALE,
    )
    yield store
    _REQUIRED_T1_TOKEN["token"] = None


@pytest.fixture
def store(fake_server, ca_file):
    _STORE.clear()
    return HttpScratchStore(base_url=fake_server, tenant=DEFAULT_TENANT, session_id=SESSION, _token=TOKEN)


def test_heal_after_ca_bundle_deleted_keeps_scratch_working(gated_store, ca_file, tmp_path):
    """The incident: the CA file goes away under a live store, then a 401
    heal rebuilds the client. Before the fix the rebuild raised the pathless
    ENOENT and every later call failed 'client has been closed'."""
    ca_file.unlink()
    publish_t1_session_lease(SESSION, FRESH, tmp_path, ttl_seconds=300.0)

    id_ = gated_store.put("healed after the install tree lost its CA bundle")

    assert gated_store._session_token == FRESH
    assert gated_store.get(id_) is not None
    assert gated_store.search("healed")


def test_failed_rebuild_leaves_the_old_client_open(store, ca_file, monkeypatch):
    """A rebuild that fails must not strand the store on a closed client:
    build the new client first, close the old one only after."""
    old = store._client
    ca_file.unlink()
    monkeypatch.setattr(shared_ssl, "_shared_ssl_context", None)  # force a fresh read of the missing file

    with pytest.raises(OSError) as excinfo:
        store._adopt_session_token("another-token", SESSION)

    assert str(ca_file) in str(excinfo.value), "the error must name the CA bundle that is missing"
    assert store._client is old
    assert not old.is_closed
    assert store.put("old client still serves") is not None


def test_closed_client_is_rebuilt_on_the_next_call(store):
    """Whatever closed the client (a past failed rebuild, a concurrent heal),
    the next call rebuilds it once instead of failing for the process's life."""
    store._client.close()

    id_ = store.put("after the client was closed")

    assert store.get(id_) is not None
    assert not store._client.is_closed


def test_a_client_swapped_mid_send_retries_on_the_replacement(store, monkeypatch):
    """Folio, 2026-11-23T07:26:11Z: one scratch call's 401 heal swapped (and closed) the client while a
    concurrent call was sending on the old one, and that call failed once with 'client has been closed'.
    The send now retries once on the replacement."""
    real_live_client = store._live_client
    stale = store._client
    calls = {"n": 0}

    def swapped_after_the_check():
        calls["n"] += 1
        if calls["n"] == 1:
            store._replace_client()  # the concurrent heal lands between the check and the send
            return stale
        return real_live_client()

    monkeypatch.setattr(store, "_live_client", swapped_after_the_check)

    id_ = store.put("sent during a concurrent heal")

    assert stale.is_closed
    assert calls["n"] == 2
    assert store.get(id_) is not None


def test_owner_close_is_not_undone(store):
    """``close()`` by the owner stays closed: the reopen is for a client the
    store lost, never for one its owner shut."""
    store.close()

    with pytest.raises(RuntimeError, match="closed"):
        store.put("after close()")


def test_ca_bundle_failure_names_the_path(fake_server, ca_file):
    """The incident's first error carried no path. A store that cannot build
    its client says which CA bundle it could not read."""
    ca_file.unlink()

    with pytest.raises(OSError) as excinfo:
        HttpScratchStore(base_url=fake_server, tenant=DEFAULT_TENANT, session_id=SESSION, _token=TOKEN)

    assert str(ca_file) in str(excinfo.value)
    assert isinstance(excinfo.value, FileNotFoundError), "callers that catch FileNotFoundError keep working"


def test_token_store_builds_without_rereading_the_ca_bundle(fake_server, ca_file):
    """The MCP lifespan opens an HttpTokenStore per session-token refresh. It
    shares the process-wide SSL context, so a CA bundle that vanished after
    the first build does not fail every later refresh."""
    from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: PLC0415 — test-local

    HttpTokenStore(base_url=fake_server, _token=TOKEN).close()  # first build reads the bundle
    ca_file.unlink()

    with HttpTokenStore(base_url=fake_server, _token=TOKEN) as later:
        assert isinstance(later._client, httpx.Client)
