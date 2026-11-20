# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 6: the identity scratch scope's caps, held CLIENT-SIDE by ``HttpScratchStore`` (no engine change).

An identity-scope put is refused over 2 KB; a non-resume put is refused once the scope holds 20 non-resume entries;
the resume pointer (tag ``resume-pointer``, line head ``resume <stamp> | ``) has its own slot and is never refused for
the 20. Nothing is ever evicted. The session scope is untouched. The engine is stubbed at ``_post``: no storage.
"""
from __future__ import annotations

import pytest

from nexus.db import http_scratch_store as hss
from nexus.db.http_scratch_store import HttpScratchStore, IdentityScratchCapError

HEAD = "resume 2026-11-20T09:00:00Z | thread: t"


class Engine:
    """``_post`` for ``/v1/t1/put`` and ``/v1/t1/list``: an in-memory scope, a log of what was asked."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.calls: list[str] = []

    def __call__(self, path, payload, **_kw):
        self.calls.append(path)
        if path == "/v1/t1/list":
            return {"entries": list(self.rows)}
        if path == "/v1/t1/put":
            self.rows.append({"id": payload["id"], "content": payload["content"], "tags": payload["tags"]})
            return {"id": payload["id"]}
        raise AssertionError(path)


def make(scope: str, engine: Engine, monkeypatch) -> HttpScratchStore:
    store = HttpScratchStore(base_url="http://127.0.0.1:1", tenant="t", session_id=scope, _token="x")
    monkeypatch.setattr(store, "_post", engine)
    return store


@pytest.fixture()
def engine() -> Engine:
    return Engine()


@pytest.fixture()
def ident(engine, monkeypatch) -> HttpScratchStore:
    return make("identity:i7", engine, monkeypatch)


class TestEntrySize:
    def test_an_entry_over_2_kb_is_refused_and_nothing_is_sent(self, ident, engine) -> None:
        with pytest.raises(IdentityScratchCapError, match="2048"):
            ident.put("x" * 2049, tags="working")
        assert engine.calls == [] and engine.rows == []

    def test_the_limit_counts_bytes_not_characters(self, ident, engine) -> None:
        with pytest.raises(IdentityScratchCapError):
            ident.put("é" * 1025, tags="working")  # 2050 bytes
        ident.put("é" * 1024, tags="working")  # exactly 2048
        assert len(engine.rows) == 1

    def test_the_resume_pointer_is_held_to_the_size_too(self, ident) -> None:
        with pytest.raises(IdentityScratchCapError):
            ident.put(HEAD + "x" * 2100, tags=hss.IDENTITY_RESUME_TAG)

    def test_the_session_scope_has_no_such_cap(self, engine, monkeypatch) -> None:
        session = make("a-session-id", engine, monkeypatch)
        session.put("x" * 5000, tags="")
        assert engine.calls == ["/v1/t1/put"], "no list call, no cap, on the session scope"


class TestEntryCount:
    def fill(self, ident, n, tags="working") -> None:
        for i in range(n):
            ident.put(f"note {i}", tags=tags)

    def test_the_21st_non_resume_put_is_refused_and_nothing_is_evicted(self, ident, engine) -> None:
        self.fill(ident, 20)
        with pytest.raises(IdentityScratchCapError, match="20"):
            ident.put("one too many", tags="working")
        assert [r["content"] for r in engine.rows] == [f"note {i}" for i in range(20)], "nothing evicted, nothing added"
        assert engine.calls.count("/v1/t1/put") == 20

    def test_the_resume_pointer_is_never_refused_for_the_count(self, ident, engine) -> None:
        self.fill(ident, 20)
        ident.put(HEAD, tags=hss.IDENTITY_RESUME_TAG)
        ident.put(HEAD + " again", tags=hss.IDENTITY_RESUME_TAG)  # put-before-delete: two at once is normal
        assert sum(1 for r in engine.rows if r["tags"] == hss.IDENTITY_RESUME_TAG) == 2 and len(engine.rows) == 22

    def test_pointers_do_not_use_up_the_20(self, ident, engine) -> None:
        for _ in range(3):
            ident.put(HEAD, tags=hss.IDENTITY_RESUME_TAG)
        self.fill(ident, 20)  # all 20 still fit beside three pointers
        assert len(engine.rows) == 23

    def test_a_tag_alone_does_not_make_a_pointer(self, ident, engine) -> None:
        """The reserved slot is for a line that really is a pointer: the tag and the ``resume <stamp> | `` head."""
        self.fill(ident, 20)
        with pytest.raises(IdentityScratchCapError):
            ident.put("just a note wearing the tag", tags=hss.IDENTITY_RESUME_TAG)

    def test_a_pointer_put_that_floods_the_scope_is_bounded_too(self, ident, engine) -> None:
        for _ in range(hss.IDENTITY_SCOPE_MAX_ENTRIES):
            ident.put(HEAD, tags=hss.IDENTITY_RESUME_TAG)
        with pytest.raises(IdentityScratchCapError):
            ident.put(HEAD, tags=hss.IDENTITY_RESUME_TAG)

    def test_a_refusal_is_a_runtime_error_so_the_cli_and_mcp_surface_it_as_a_message(self) -> None:
        assert issubclass(IdentityScratchCapError, RuntimeError)

    def test_the_session_scope_is_not_counted_against_anything(self, engine, monkeypatch) -> None:
        session = make("a-session-id", engine, monkeypatch)
        for i in range(25):
            session.put(f"n{i}", tags="")
        assert len(engine.rows) == 25
