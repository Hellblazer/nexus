# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Regression test for nexus-65a9k / GH #1454.

A tool-free ``claude -p`` operator dispatch strips the child's
``NX_T1_SESSION`` / ``NX_T1_SESSION_ID`` env (the T1-mint gate,
nexus-bjltu) but still forwards ``NX_SESSION_ID=<PARENT session id>``
(``_build_dispatch_env``'s ``parent_session_id`` handling). When that
child process exits, its SessionEnd hook
(``nx-session-end-launcher`` -> ``hooks.session_end_flush``) inherits
that env in a detached grandchild. Tier 1 (``USE_INHERITED``) misses
because the T1 token/id were stripped; tier 2 (``USE_LEASED``) then
resolves the PARENT's session id, finds the parent MCP's own live,
fresh published lease, and binds a store to it. Pre-fix,
``session_end_flush`` then called ``t1.clear()`` unconditionally on
any non-``None`` store -- ``POST /v1/t1/session/close``, a hard
DELETE of every row for the PARENT's session, not the child's.

Hermetic: no live service required. ``HttpScratchStore`` is faked at
its import site; the T1 lease file is published via the real
``publish_t1_session_lease`` so the on-disk format can never drift
out from under this test.
"""
import uuid

import pytest

from nexus.db.t1 import publish_t1_session_lease


PARENT_SESSION = "parent-session-" + uuid.uuid4().hex[:8]
PARENT_TOKEN = "parent-token-" + uuid.uuid4().hex[:8]


class FakeScratchStore:
    """Records what session it was bound to and whether it was cleared."""

    instances: list["FakeScratchStore"] = []

    def __init__(self, base_url=None, tenant="default", *, session_id=None,
                 _token=None, _session_token=None):
        import os

        env_id = os.environ.get("NX_T1_SESSION_ID", "").strip()
        env_token = os.environ.get("NX_T1_SESSION", "").strip()
        self._session_id = session_id or env_id or env_token
        self.cleared = False
        FakeScratchStore.instances.append(self)

    @property
    def session_id(self):
        return self._session_id

    def flagged_entries(self):
        return []

    def clear(self):
        self.cleared = True
        return 0

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_fake_store_instances():
    FakeScratchStore.instances.clear()
    yield
    FakeScratchStore.instances.clear()


@pytest.fixture
def parent_lease(monkeypatch):
    """A live MCP session (the parent) has published a fresh T1 lease.

    Relies on the suite's autouse ``_isolate_config_dir`` fixture
    (tests/conftest.py) for ``NEXUS_CONFIG_DIR``; this fixture only
    adds the lease file and the parent's own env.
    """
    from nexus.config import nexus_config_dir

    publish_t1_session_lease(PARENT_SESSION, PARENT_TOKEN, nexus_config_dir())
    monkeypatch.setenv("NX_T1_SESSION", PARENT_TOKEN)
    monkeypatch.setenv("NX_T1_SESSION_ID", PARENT_SESSION)


def test_tool_free_dispatch_env_forwards_parent_session_id(parent_lease):
    """Leg 1: the child env keeps NX_SESSION_ID=<parent> with T1 vars
    stripped -- the precondition the rest of this bug depends on."""
    from nexus.operators.dispatch import _build_dispatch_env

    child_env = _build_dispatch_env(
        ephemeral=True,
        parent_session_id=PARENT_SESSION,
        grants_tool_access=False,  # operator_summarize / operator_extract / ...: the tool-free path
    )

    assert "NX_T1_SESSION" not in child_env
    assert "NX_T1_SESSION_ID" not in child_env
    # ...but the PARENT's conversation id is forwarded verbatim.
    assert child_env["NX_SESSION_ID"] == PARENT_SESSION


def test_child_session_end_does_not_clear_the_parents_t1_scope(parent_lease, monkeypatch):
    """Leg 2 (the bug, nexus-65a9k / GH #1454): the child's SessionEnd
    flush binds to the PARENT's session via its lease (USE_LEASED) and
    must NOT call clear() on it -- that would delete the parent's rows
    while the parent MCP session is still live.

    The store is still opened and flushed (a borrowed live scope's
    flagged entries are not the unsafe part) -- only the destructive
    clear() is gated on ownership.
    """
    import nexus.db.http_scratch_store as hss
    import nexus.mcp_infra as mcp_infra
    from nexus import hooks
    from nexus.operators.dispatch import _build_dispatch_env

    child_env = _build_dispatch_env(
        ephemeral=True,
        parent_session_id=PARENT_SESSION,
        grants_tool_access=False,
    )

    # Become the detached SessionEnd grandchild: it inherits the child's
    # env by fork(), so replace this process's env with the child's exactly.
    for key in ("NX_T1_SESSION", "NX_T1_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NX_SESSION_ID", child_env["NX_SESSION_ID"])

    monkeypatch.setattr(hss, "HttpScratchStore", FakeScratchStore)
    monkeypatch.setattr(mcp_infra, "t2_index_write", lambda fn: (0, 0))

    hooks.session_end_flush()

    assert FakeScratchStore.instances, (
        "session_end_flush resolved no T1 store at all"
    )
    store = FakeScratchStore.instances[-1]
    assert store.session_id == PARENT_SESSION, (
        f"expected the flush to bind the PARENT session, got {store.session_id!r}"
    )
    assert not store.cleared, (
        "REGRESSION nexus-65a9k: the child dispatch's SessionEnd flush called "
        f"clear() on the PARENT MCP session {PARENT_SESSION!r} -- "
        "POST /v1/t1/session/close, a hard DELETE of every row."
    )


def test_control_tool_granting_dispatch_binds_and_clears_the_childs_own_session(
    parent_lease, monkeypatch
):
    """CONTROL: when the dispatch DOES mint (grants_tool_access=True), the
    child env carries its own NX_T1_SESSION, tier-1 USE_INHERITED wins,
    and the flush clears the CHILD's own session -- never the parent's.

    This isolates the causal variable: substituting a minting dispatch
    for a tool-free one is the only thing that changes between this test
    and ``test_child_session_end_does_not_clear_the_parents_t1_scope``.
    Without this control, a regression test asserting only "clear() was
    not called" would pass even if clear() were broken for every case,
    not just the borrowed-lease one.
    """
    import nexus.db.http_scratch_store as hss
    import nexus.db.t1 as t1mod
    import nexus.mcp_infra as mcp_infra
    from nexus import hooks
    from nexus.operators.dispatch import _build_dispatch_env

    child_session = "child-" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(
        t1mod, "mint_t1_session_token",
        lambda sid, *, context: {"session_token": "child-token", "expires_in_seconds": 3600},
    )
    monkeypatch.setattr(uuid, "uuid4", lambda: child_session, raising=False)

    child_env = _build_dispatch_env(
        ephemeral=True, parent_session_id=PARENT_SESSION, grants_tool_access=True,
    )
    assert child_env["NX_T1_SESSION"] == "child-token"
    assert child_env["NX_T1_SESSION_ID"] != PARENT_SESSION

    monkeypatch.setenv("NX_T1_SESSION", child_env["NX_T1_SESSION"])
    monkeypatch.setenv("NX_T1_SESSION_ID", child_env["NX_T1_SESSION_ID"])
    monkeypatch.setenv("NX_SESSION_ID", child_env["NX_SESSION_ID"])
    monkeypatch.setattr(hss, "HttpScratchStore", FakeScratchStore)
    monkeypatch.setattr(mcp_infra, "t2_index_write", lambda fn: (0, 0))

    hooks.session_end_flush()

    store = FakeScratchStore.instances[-1]
    assert store.session_id != PARENT_SESSION, (
        "even the minting path reached the parent scope"
    )
    assert store.cleared, (
        "control regressed: the child's OWN (USE_INHERITED) scope should "
        "still be cleared as before this fix"
    )
