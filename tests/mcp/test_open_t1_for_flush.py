# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mtheb.9: which token ``_open_t1_for_flush`` reads a session with.

The order is the caller's token, the process env when it still names the same
session, the session's published lease, else a refusal. The server also refuses
a token minted for another session (403), so a wrong token never reads another
scope's rows whichever branch picks it; that is why these tests watch the store
the function BUILDS, the one place the client-side guard is visible. The store
class is replaced by a recorder, so nothing here reaches an engine.
"""
from __future__ import annotations

import pytest

import nexus.mcp.core as core
from nexus.config import nexus_config_dir
from nexus.db.t1 import publish_t1_session_lease


class _RecordedStore:
    built: list[dict] = []

    def __init__(self, **kw) -> None:
        type(self).built.append(kw)


@pytest.fixture
def built(monkeypatch) -> list[dict]:
    import nexus.db.http_scratch_store as store_mod

    _RecordedStore.built = []
    monkeypatch.setattr(store_mod, "HttpScratchStore", _RecordedStore)
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
    return _RecordedStore.built


def test_a_token_the_caller_gives_is_the_one_used(built, monkeypatch) -> None:
    monkeypatch.setenv("NX_T1_SESSION_ID", "sess-a")
    monkeypatch.setenv("NX_T1_SESSION", "env-token")

    core._open_t1_for_flush("sess-a", "given-token")

    assert built == [{"session_id": "sess-a", "_session_token": "given-token", "export_adopted_token": False}]


def test_the_env_token_is_used_when_the_env_names_the_given_session(built, monkeypatch) -> None:
    monkeypatch.setenv("NX_T1_SESSION_ID", "sess-a")
    monkeypatch.setenv("NX_T1_SESSION", "env-token-of-a")

    core._open_t1_for_flush("sess-a")

    assert [b["_session_token"] for b in built] == ["env-token-of-a"]


def test_the_env_token_of_another_session_is_never_used(built, monkeypatch) -> None:
    """The env holds session B's token and names session B; a flush for A must
    not send it. With no other source for A there is nothing to read A with."""
    monkeypatch.setenv("NX_T1_SESSION_ID", "sess-b")
    monkeypatch.setenv("NX_T1_SESSION", "env-token-of-b")

    with pytest.raises(RuntimeError, match="no token for T1 session 'sess-a'"):
        core._open_t1_for_flush("sess-a")

    assert built == []  # no store was built, so no request could carry B's token


def test_the_published_lease_of_the_given_session_is_the_fallback(built, monkeypatch) -> None:
    """The env names another session and no token is given: the session's own
    published lease supplies it."""
    monkeypatch.setenv("NX_T1_SESSION_ID", "sess-b")
    monkeypatch.setenv("NX_T1_SESSION", "env-token-of-b")
    publish_t1_session_lease("sess-a", "lease-token-of-a", nexus_config_dir())

    core._open_t1_for_flush("sess-a")

    assert [b["_session_token"] for b in built] == ["lease-token-of-a"]
    assert built[0]["session_id"] == "sess-a"


def test_a_lease_published_for_another_session_is_not_a_fallback(built, monkeypatch) -> None:
    publish_t1_session_lease("sess-b", "lease-token-of-b", nexus_config_dir())

    with pytest.raises(RuntimeError, match="no token for T1 session 'sess-a'"):
        core._open_t1_for_flush("sess-a")

    assert built == []
