# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client against an engine that issues identity tokens and fences the four families (RDR-253 Step 2,
nexus-hczdo.1.2). Written to the Step 2 contract in ``nexus.identity.token_contract``; every test here SKIPS on an
engine that does not issue ``identity_token`` (the Step 1 engine this branch is built on) and runs once Step 2's
engine is the session jar. ``NX_IDENTITY_REQUIRE_STEP2=1`` turns the skip into a failure, so the day the jar
carries Step 2 a green run cannot be a vacuous one (the nexus-moht0 doctrine); the hand-back lists the three tests.

Covers the bead's tests that need tokens: the first write after ``/clear`` succeeds; a Bash child from before the
``/clear`` still writes; a refresh rewrites the process's one token file; a superseded process's write is a typed
stop; a tokenless write to a threads entry is refused.
"""
from __future__ import annotations

import os
import time

import pytest

from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.identity import token_header
from nexus.identity.token_file import read_token_file, token_path
from nexus.mcp.identity_lease import IdentityLease
from tests.identity.conftest import VERSION, World

T1 = "2026-11-17T09:00:00Z"


@pytest.fixture
def step2(world: World) -> World:
    """The world, after checking the engine issues identity tokens: skip (or fail, when required) otherwise."""
    # The hook stamps the token file's expiry from the world's clock. These tests send the token to a real engine, whose
    # tokens live one real hour, and the client does not send a token its file records as expired: so the clock is real.
    world.now = time.time()
    probe = world.seat("probe-seat")
    world.process(90, name="probe-seat", os_start=T1, session_id="probe")
    out = world.hook(90, "startup", "probe")
    token = read_token_file(token_path(world.identity_dir, 90, 90_000)).token
    world.procs.stop(90, keep_registry=False)
    world.client.bind(probe, "end", out.generation)
    if token is None:
        reason = "the engine issues no identity tokens (Step 2, nexus-hczdo.1.2, is not in this jar)"
        if os.environ.get("NX_IDENTITY_REQUIRE_STEP2") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return world


def _threads(world: World, monkeypatch: pytest.MonkeyPatch, pid: int) -> HttpMemoryStore:
    """A T2 store as a Bash child of Claude process *pid* would build one: its token file is the identity's."""
    monkeypatch.setattr(token_header, "_discover", lambda: token_path(world.identity_dir, pid, pid * 1000))
    token_header.reset_for_tests()
    return HttpMemoryStore(base_url=world.base_url, tenant="default", _token=os.environ["NX_SERVICE_TOKEN"])


def _entry(identity: str, session: str) -> str:
    return f"session: {session}\nidentity: {identity}\n\n## work\nref: none\n"


@pytest.mark.t2_tenant
def test_a_threads_write_after_clear_works_with_the_new_token_and_the_old_one_and_a_superseded_process_is_stopped(
        step2: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world = step2
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    started = world.hook(100, "startup", "s1")
    assert started.identity == alpha
    old_token = read_token_file(token_path(world.identity_dir, 100, 100_000)).token
    store = _threads(world, monkeypatch, 100)
    store.put("threads", "alpha", _entry(alpha, "s1"))                         # a write with the identity token

    cleared = world.hook(100, "clear", "s2")
    new_token = read_token_file(token_path(world.identity_dir, 100, 100_000)).token
    assert cleared.generation == 1 and new_token and new_token != old_token
    store.put("threads", "alpha", _entry(alpha, "s2"))                         # a threads write after /clear: no refusal (a threads write never checks the session id, so this proves the generation held, not the stamp)
    # a Bash child from before the /clear holds the old token in its (cached) view: still the same generation
    from nexus.db.t2 import _refreshable_client
    monkeypatch.setattr(_refreshable_client, "fenced_identity_headers",
                        lambda method, path: {"X-Nexus-Identity-Token": old_token})
    store.put("threads", "alpha", _entry(alpha, "s2"))

    # process 100 crashes, 101 takes the seat at generation 2: 100's token is for generation 1
    world.procs.stop(100, keep_registry=True)
    world.process(101, os_start=T1, name="alpha")
    assert world.hook(101, "startup", "s3").generation == 2
    with pytest.raises(token_header.GenerationSupersededError) as err:
        store.put("threads", "alpha", _entry(alpha, "s2"))
    assert err.value.identity == alpha and err.value.current_generation == 2 and err.value.holder["pid"] == 101


@pytest.mark.t2_tenant
def test_a_refresh_at_half_life_rewrites_the_one_token_file_and_the_hook_files_stay_readable(step2: World) -> None:
    """One clock drives the hook that writes the file's expiry, the lease and the test (``world.now``, real time here)."""
    world = step2
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "s1")
    path = token_path(world.identity_dir, 100, 100_000)
    first = read_token_file(path)
    assert first.token and first.expires_at == pytest.approx(world.now + 3600, abs=5), "the file's expiry is one hour on the shared clock"
    lease = IdentityLease(client_factory=lambda: world.data_client, config_dir=world.config_dir, claude_pid=100,
                          procs=world.procs, clock=lambda: world.now, client_version=VERSION, sleep=lambda s: None)
    start = lease.tick()
    assert start.adopted and start.renewed and not start.refreshed, "a fresh token is not refreshed"
    assert read_token_file(path).token == first.token
    world.now = first.expires_at - 1_000.0                                    # less than half of the hour left
    report = lease.tick()
    second = read_token_file(path)
    assert report.refreshed and second.token and second.token != first.token and second.generation == first.generation == 1
    assert second.expires_at > first.expires_at
    assert [p.name for p in world.identity_dir.iterdir() if p.name.endswith(".token") and p.name.startswith("100.")] == ["100.100000.token"]
    assert world.events(alpha) == ["start"], "a refresh is not a new binding"


@pytest.mark.t2_tenant
def test_a_tokenless_write_to_a_fenced_threads_entry_is_refused(step2: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world = step2
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "s1")
    _threads(world, monkeypatch, 100).put("threads", "alpha", _entry(alpha, "s1"))
    monkeypatch.setattr(token_header, "_discover", lambda: None)               # a shell with no Claude ancestor
    token_header.reset_for_tests()
    bare = HttpMemoryStore(base_url=world.base_url, tenant="default", _token=os.environ["NX_SERVICE_TOKEN"])
    with pytest.raises(Exception) as err:
        bare.put("threads", "alpha", _entry(alpha, "s9"))
    assert "403" in str(err.value) or "identity_token_required" in str(err.value)
