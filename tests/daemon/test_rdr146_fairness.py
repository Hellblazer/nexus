# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-146 Phase 2 / bead nexus-5p2ci.12: interactive-vs-batch fairness.

The single daemon already serialises catalog writes (Phase 1); Phase 2
ensures a foreground interactive catalog write is not starved by a
sustained background batch write burst (GH #1046 inverted). The mechanism
is producer back-pressure: an interactive RPC frame carries
``priority="interactive"`` which opens an in-memory deadline window on the
daemon; the background indexer polls ``catalog.is_interactive_write_pending``
and yields. No daemon priority queue is introduced.

Layers:
1. Pure units (no daemon): ``resolve_write_priority`` + ``await_fair_window``.
2. Daemon window semantics with an injected monotonic clock.
3. End-to-end over real sockets: priority frame opens the window; probe op
   is reachable; a batch frame (and a no-priority frame) never opens it.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import types
from pathlib import Path

import pytest

from nexus.catalog.write_priority import (
    INDEXER_YIELD_SLEEPS,
    INTERACTIVE_WINDOW_S,
    WRITE_PRIORITY_ENV,
    await_fair_window,
    resolve_write_priority,
)


@pytest.fixture
def config_dir():
    cd = Path(tempfile.mkdtemp(prefix="nxfair-", dir="/tmp"))
    yield cd
    shutil.rmtree(cd, ignore_errors=True)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Layer 1 — pure units
# ---------------------------------------------------------------------------


class TestResolveWritePriority:
    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(WRITE_PRIORITY_ENV, "batch")
        # Explicit interactive + a tty must both lose to the env override.
        monkeypatch.setattr("sys.stdout", types.SimpleNamespace(isatty=lambda: True))
        assert resolve_write_priority("interactive") == "batch"

    def test_env_interactive_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(WRITE_PRIORITY_ENV, "interactive")
        monkeypatch.setattr("sys.stdout", types.SimpleNamespace(isatty=lambda: False))
        assert resolve_write_priority(None) == "interactive"

    def test_explicit_used_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(WRITE_PRIORITY_ENV, raising=False)
        monkeypatch.setattr("sys.stdout", types.SimpleNamespace(isatty=lambda: False))
        assert resolve_write_priority("interactive") == "interactive"

    def test_isatty_fallback_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(WRITE_PRIORITY_ENV, raising=False)
        monkeypatch.setattr("sys.stdout", types.SimpleNamespace(isatty=lambda: True))
        assert resolve_write_priority(None) == "interactive"

    def test_isatty_fallback_non_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(WRITE_PRIORITY_ENV, raising=False)
        monkeypatch.setattr("sys.stdout", types.SimpleNamespace(isatty=lambda: False))
        assert resolve_write_priority(None) == "batch"

    def test_invalid_env_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(WRITE_PRIORITY_ENV, "garbage")
        monkeypatch.setattr("sys.stdout", types.SimpleNamespace(isatty=lambda: False))
        assert resolve_write_priority("interactive") == "interactive"


class TestAwaitFairWindow:
    def test_proceeds_immediately_when_not_pending(self) -> None:
        slept: list[float] = []
        result = await_fair_window(
            lambda: False, "skip", sleep_fn=slept.append
        )
        assert result == "proceed"
        assert slept == []  # never sleeps in the common case

    def test_skip_terminal_when_always_pending(self) -> None:
        slept: list[float] = []
        result = await_fair_window(
            lambda: True, "skip", sleep_fn=slept.append
        )
        assert result == "skip"
        # Full bounded budget consumed, in escalating order.
        assert slept == list(INDEXER_YIELD_SLEEPS)

    def test_wait_terminal_proceeds_after_budget(self) -> None:
        slept: list[float] = []
        result = await_fair_window(
            lambda: True, "wait", sleep_fn=slept.append
        )
        assert result == "proceed"
        assert slept == list(INDEXER_YIELD_SLEEPS)

    def test_proceeds_when_window_clears_during_final_sleep(self) -> None:
        # Pending through all in-loop probes, then clears on the post-loop
        # final probe (exercises the after-budget "proceed" arm, GAP-1).
        calls = {"n": 0}

        def probe() -> bool:
            calls["n"] += 1
            return calls["n"] <= len(INDEXER_YIELD_SLEEPS)

        slept: list[float] = []
        result = await_fair_window(probe, "skip", sleep_fn=slept.append)
        assert result == "proceed"
        assert slept == list(INDEXER_YIELD_SLEEPS)
        assert calls["n"] == len(INDEXER_YIELD_SLEEPS) + 1  # final probe ran

    def test_detached_stdout_defaults_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # isatty() raising (detached / closed stdout) falls to safe batch.
        monkeypatch.delenv(WRITE_PRIORITY_ENV, raising=False)

        def _boom() -> bool:
            raise ValueError("I/O operation on closed file")

        monkeypatch.setattr("sys.stdout", types.SimpleNamespace(isatty=_boom))
        assert resolve_write_priority(None) == "batch"

    def test_proceeds_when_window_clears_midloop(self) -> None:
        # Pending for the first two probes, then clears.
        calls = {"n": 0}

        def probe() -> bool:
            calls["n"] += 1
            return calls["n"] <= 2

        slept: list[float] = []
        result = await_fair_window(probe, "skip", sleep_fn=slept.append)
        assert result == "proceed"
        # Two sleeps consumed before the window cleared on the third probe.
        assert slept == list(INDEXER_YIELD_SLEEPS[:2])


# ---------------------------------------------------------------------------
# Layer 2 — daemon window semantics with an injected monotonic clock
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestDaemonWindow:
    """Drive start -> dispatch -> probe -> stop inside ONE event loop. The
    daemon binds its reassert task to the loop running ``start()``, so the
    whole scenario must live in a single ``_run`` coroutine."""

    @staticmethod
    def _drive(config_dir: Path, db_path: Path, scenario):
        from nexus.daemon.t2_daemon import T2Daemon

        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        clock = _FakeClock()
        daemon._monotonic = clock

        async def _main():
            await daemon.start()
            try:
                await scenario(daemon, clock)
            finally:
                await daemon.stop()

        _run(_main())

    @staticmethod
    def _owner_frame(repo_hash: str, priority: str | None = None) -> dict:
        frame = {
            "op": "catalog_write.register_owner",
            "args": ["acme", "project"],
            "kwargs": {"repo_hash": repo_hash, "repo_root": "/tmp/acme"},
        }
        if priority is not None:
            frame["priority"] = priority
        return frame

    def test_interactive_write_opens_window(self, config_dir, db_path) -> None:
        async def scenario(daemon, clock):
            assert daemon._is_interactive_write_pending() is False
            await daemon._dispatch(self._owner_frame("h1", "interactive"), is_uds=True)
            # Pending immediately and right up to (but not past) the window.
            assert daemon._is_interactive_write_pending() is True
            clock.advance(INTERACTIVE_WINDOW_S - 0.01)
            assert daemon._is_interactive_write_pending() is True
            clock.advance(0.02)
            assert daemon._is_interactive_write_pending() is False

        self._drive(config_dir, db_path, scenario)

    def test_batch_write_does_not_open_window(self, config_dir, db_path) -> None:
        async def scenario(daemon, clock):
            await daemon._dispatch(self._owner_frame("h2", "batch"), is_uds=True)
            assert daemon._is_interactive_write_pending() is False

        self._drive(config_dir, db_path, scenario)

    def test_no_priority_field_is_batch(self, config_dir, db_path) -> None:
        async def scenario(daemon, clock):
            await daemon._dispatch(self._owner_frame("h3"), is_uds=True)
            assert daemon._is_interactive_write_pending() is False

        self._drive(config_dir, db_path, scenario)

    def test_burst_refreshes_deadline(self, config_dir, db_path) -> None:
        async def scenario(daemon, clock):
            await daemon._dispatch(self._owner_frame("b1", "interactive"), is_uds=True)
            clock.advance(INTERACTIVE_WINDOW_S - 0.1)
            assert daemon._is_interactive_write_pending() is True
            # A second interactive write refreshes the deadline.
            await daemon._dispatch(self._owner_frame("b2", "interactive"), is_uds=True)
            clock.advance(INTERACTIVE_WINDOW_S - 0.1)
            # Still pending: total elapsed > one window, but the refresh held it.
            assert daemon._is_interactive_write_pending() is True
            clock.advance(0.2)
            assert daemon._is_interactive_write_pending() is False

        self._drive(config_dir, db_path, scenario)

    def test_probe_op_registered_in_dispatch_table(self, config_dir, db_path) -> None:
        async def scenario(daemon, clock):
            assert "catalog.is_interactive_write_pending" in daemon._dispatch_table

        self._drive(config_dir, db_path, scenario)

    def test_no_central_op_queue_invariant(self, config_dir, db_path) -> None:
        """Design gate scenario #6: fairness is producer-side back-pressure,
        NOT a daemon priority queue. The dispatch path stays unchanged in
        structure — write serialisation is still the asyncio.Lock from
        Phase 1, and no asyncio.Queue is introduced anywhere on the daemon."""
        import asyncio as _asyncio

        async def scenario(daemon, clock):
            assert isinstance(daemon._catalog_write_lock, _asyncio.Lock)
            queues = [
                name for name, val in vars(daemon).items()
                if isinstance(val, _asyncio.Queue)
            ]
            assert queues == [], f"unexpected op queue(s): {queues}"

        self._drive(config_dir, db_path, scenario)


# ---------------------------------------------------------------------------
# Layer 3 — end-to-end over real sockets
# ---------------------------------------------------------------------------


def _run_daemon_in_thread(daemon, ready: threading.Event, stop_evt: threading.Event):
    async def _main() -> None:
        await daemon.start()
        ready.set()
        while not stop_evt.is_set():
            await asyncio.sleep(0.05)
        await daemon.stop()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_main())
    finally:
        loop.close()


class TestEndToEndFairness:
    def test_priority_frame_opens_window_probe_reads_it(
        self, config_dir, db_path
    ) -> None:
        from nexus.daemon.t2_client import make_t2_client
        from nexus.daemon.t2_daemon import T2Daemon

        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready, stop_evt = threading.Event(), threading.Event()
        th = threading.Thread(
            target=_run_daemon_in_thread,
            args=(daemon, ready, stop_evt), daemon=True,
        )
        th.start()
        assert ready.wait(timeout=10), "daemon did not start"
        try:
            client = make_t2_client(config_dir=config_dir)
            # Probe is reachable through the read proxy and starts False.
            assert client.catalog.is_interactive_write_pending() is False

            # An interactive-priority write opens the window.
            client.catalog_write.register_owner(
                "acme", "project", repo_hash="h1", repo_root="/tmp/acme",
                _priority="interactive",
            )
            assert client.catalog.is_interactive_write_pending() is True
            client.close()
        finally:
            stop_evt.set()
            th.join(timeout=10)

    def test_batch_frame_does_not_open_window(self, config_dir, db_path) -> None:
        from nexus.daemon.t2_client import make_t2_client
        from nexus.daemon.t2_daemon import T2Daemon

        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready, stop_evt = threading.Event(), threading.Event()
        th = threading.Thread(
            target=_run_daemon_in_thread,
            args=(daemon, ready, stop_evt), daemon=True,
        )
        th.start()
        assert ready.wait(timeout=10), "daemon did not start"
        try:
            client = make_t2_client(config_dir=config_dir)
            # Default (no _priority) write is batch and must not open the window.
            client.catalog_write.register_owner(
                "acme", "project", repo_hash="h2", repo_root="/tmp/acme2",
            )
            assert client.catalog.is_interactive_write_pending() is False
            client.close()
        finally:
            stop_evt.set()
            th.join(timeout=10)

    def test_interactive_not_starved_by_batch_storm(
        self, config_dir, db_path
    ) -> None:
        """Design gate scenario #1 (the #1046 repro INVERTED): a background
        thread issues a sustained storm of ``priority="batch"`` catalog writes
        through the daemon; one ``priority="interactive"`` write issued
        concurrently must complete in a bounded time, NOT block for the storm's
        duration.

        Scope: the storm is a single synchronous client (sequential RPCs), so
        what this proves is that the daemon dispatch path stays FIFO-fair and
        does not deadlock or serialise the interactive write behind the storm's
        whole lifetime — the dangerous regression classes. The producer-side
        yield CHAIN (indexer sees the window and defers) is proven directly by
        ``test_real_indexer_yields_to_open_window`` below; together they cover
        the full no-starvation claim.

        Wall-clock (not fixed-clock) because the claim is about completion
        latency under real concurrent dispatch; the 5s bound is generous."""
        import time

        from nexus.daemon.t2_client import make_t2_client
        from nexus.daemon.t2_daemon import T2Daemon

        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready, stop_evt = threading.Event(), threading.Event()
        th = threading.Thread(
            target=_run_daemon_in_thread,
            args=(daemon, ready, stop_evt), daemon=True,
        )
        th.start()
        assert ready.wait(timeout=10), "daemon did not start"

        storm_stop = threading.Event()
        storm_started = threading.Event()
        storm_count = {"n": 0}

        def _storm() -> None:
            bc = make_t2_client(config_dir=config_dir)
            try:
                i = 0
                while not storm_stop.is_set():
                    bc.catalog_write.register_owner(
                        f"batch{i}", "project",
                        repo_hash=f"sh{i}", repo_root=f"/tmp/s{i}",
                    )
                    storm_count["n"] += 1
                    storm_started.set()
                    i += 1
            finally:
                bc.close()

        storm_th = threading.Thread(target=_storm, daemon=True)
        storm_th.start()
        try:
            assert storm_started.wait(timeout=10), "batch storm did not start"
            client = make_t2_client(config_dir=config_dir)
            t0 = time.monotonic()
            owner = client.catalog_write.register_owner(
                "foreground", "project",
                repo_hash="fg", repo_root="/tmp/fg",
                _priority="interactive",
            )
            elapsed = time.monotonic() - t0
            from nexus.catalog.tumbler import Tumbler
            assert isinstance(owner, Tumbler)
            # Bounded completion: the interactive write is not held for the
            # storm's lifetime. The asyncio dispatch lock is FIFO-fair, so this
            # completes within a small multiple of one op's latency. 5s is a
            # generous ceiling that still catches a hang / full-storm serialise.
            assert elapsed < 5.0, f"interactive write starved: {elapsed:.2f}s"
            # The storm kept running throughout (it was a real sustained load).
            assert storm_count["n"] >= 1
            client.close()
        finally:
            storm_stop.set()
            storm_th.join(timeout=10)
            stop_evt.set()
            th.join(timeout=10)


class TestRealChainIntegration:
    """Proves the full producer-yield chain WITHOUT stubbing: a real daemon
    with an open interactive window + the real ``_catalog_hook`` batch
    producer routing through it -> the indexer probes, sees the window, and
    defers (skip). This is the end-to-end causal proof that the isolated unit
    tests only cover by composition (substantive-critic coverage note)."""

    @pytest.fixture(autouse=True)
    def _git_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")

    def test_real_indexer_yields_to_open_window(
        self, config_dir, tmp_path, monkeypatch
    ) -> None:
        from nexus import indexer
        from nexus.catalog.catalog import Catalog
        from nexus.daemon.t2_client import make_t2_client
        from nexus.daemon.t2_daemon import T2Daemon

        # Point the config + catalog at the SHORT /tmp config_dir (the deep
        # pytest tmp path overflows the AF_UNIX UDS limit). Overriding
        # NEXUS_CONFIG_DIR here means the default-config make_catalog_writer()
        # inside _catalog_hook resolves to THIS daemon. Keep the window open
        # well past the ~1.75s indexer yield budget so the skip is
        # deterministic (no wall-clock race).
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        cat_path = config_dir / "catalog"
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_path))
        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._INTERACTIVE_WINDOW_S", 30.0,
        )
        Catalog.init(cat_path)

        daemon = T2Daemon(config_dir=config_dir, db_path=config_dir / "memory.db")
        ready, stop_evt = threading.Event(), threading.Event()
        th = threading.Thread(
            target=_run_daemon_in_thread,
            args=(daemon, ready, stop_evt), daemon=True,
        )
        th.start()
        assert ready.wait(timeout=10), "daemon did not start"
        try:
            # Open the interactive window through the real daemon.
            client = make_t2_client(config_dir=config_dir)
            client.catalog_write.register_owner(
                "fg", "curator", repo_hash="fgh", repo_root="/tmp/fg",
                _priority="interactive",
            )
            assert client.catalog.is_interactive_write_pending() is True
            client.close()

            # The real batch producer routes through the same daemon, probes
            # the open window, yields the bounded budget, then skips.
            src = tmp_path / "src" / "main.py"
            src.parent.mkdir(parents=True)
            src.write_text("print('hello')")
            result = indexer._catalog_hook(
                repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
                head_hash="abc",
                indexed_files=[(src, "code", "code__nexus")],
                on_locked="skip",
            )
            # Deferred: the file was not registered this pass.
            assert result == {}
            reader = Catalog(cat_path, cat_path / ".catalog.db", read_only=True)
            try:
                owner = reader.owner_for_repo("571b8edd")
                # Owner creation is not gated (it precedes the per-file loop),
                # but the document write was deferred by the open window.
                assert owner is None or reader.by_file_path(owner, "src/main.py") is None
            finally:
                reader._db.close()
        finally:
            stop_evt.set()
            th.join(timeout=10)


class TestIndexerYieldIntegration:
    """The background ``_catalog_hook`` is the batch producer. Stub the yield
    decision so the break-vs-proceed control flow is deterministic and fast
    (no real sleeps); the bounded-loop timing itself is covered by
    ``TestAwaitFairWindow``."""

    @pytest.fixture(autouse=True)
    def _git_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")

    def _catalog(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from nexus.catalog.catalog import Catalog

        catalog_dir = tmp_path / "catalog"
        cat = Catalog.init(catalog_dir)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        return cat

    def test_skip_terminal_defers_registration(
        self, tmp_path, monkeypatch
    ) -> None:
        from nexus import indexer

        cat = self._catalog(tmp_path, monkeypatch)
        # Force the yield loop to a skip terminal (window stuck open).
        monkeypatch.setattr(
            "nexus.catalog.write_priority.await_fair_window",
            lambda *a, **k: "skip",
        )
        src = tmp_path / "src" / "main.py"
        src.parent.mkdir(parents=True)
        src.write_text("print('hello')")

        result = indexer._catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc", indexed_files=[(src, "code", "code__nexus")],
            on_locked="skip",
        )
        # Deferred: nothing registered this pass, empty doc-id map returned.
        assert result == {}
        owner = cat.owner_for_repo("571b8edd")
        assert cat.by_file_path(owner, "src/main.py") is None

    def test_proceed_terminal_registers(self, tmp_path, monkeypatch) -> None:
        from nexus import indexer

        cat = self._catalog(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "nexus.catalog.write_priority.await_fair_window",
            lambda *a, **k: "proceed",
        )
        src = tmp_path / "src" / "main.py"
        src.parent.mkdir(parents=True)
        src.write_text("print('hello')")

        result = indexer._catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc", indexed_files=[(src, "code", "code__nexus")],
            on_locked="skip",
        )
        assert src in result
        owner = cat.owner_for_repo("571b8edd")
        assert cat.by_file_path(owner, "src/main.py") is not None

    def test_interactive_producer_never_yields(self, tmp_path, monkeypatch) -> None:
        """GAP-4: an interactive ``nx index repo`` (NX_WRITE_PRIORITY=interactive)
        is itself latency-sensitive and must NOT run the yield gate. Force the
        yield decision to raise; if the gate were invoked the hook would error
        and register nothing. The file registering proves the gate is bypassed."""
        from nexus import indexer

        cat = self._catalog(tmp_path, monkeypatch)
        monkeypatch.setenv("NX_WRITE_PRIORITY", "interactive")

        def _boom(*a, **k):
            raise AssertionError("yield gate must not run for an interactive producer")

        monkeypatch.setattr(
            "nexus.catalog.write_priority.await_fair_window", _boom,
        )
        src = tmp_path / "src" / "main.py"
        src.parent.mkdir(parents=True)
        src.write_text("print('hello')")

        result = indexer._catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc", indexed_files=[(src, "code", "code__nexus")],
            on_locked="skip",
        )
        assert src in result
        owner = cat.owner_for_repo("571b8edd")
        assert cat.by_file_path(owner, "src/main.py") is not None

    def test_direct_fallback_probe_is_false(self, tmp_path, monkeypatch) -> None:
        """GAP-3: with no daemon, CatalogWriter falls back to a direct in-process
        Catalog (routed=False) and the probe returns False unconditionally, so a
        batch producer never yields when there is no cross-process contention."""
        self._catalog(tmp_path, monkeypatch)
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer(priority="batch")
        try:
            assert writer.routed is False
            assert writer.is_interactive_write_pending() is False
        finally:
            writer.close()

    def test_skip_then_reconciles_next_pass(self, tmp_path, monkeypatch) -> None:
        """Idempotent reconciliation: a skipped file registers on the next
        pass once the window has cleared (proceed)."""
        from nexus import indexer

        cat = self._catalog(tmp_path, monkeypatch)
        src = tmp_path / "src" / "main.py"
        src.parent.mkdir(parents=True)
        src.write_text("print('hello')")

        monkeypatch.setattr(
            "nexus.catalog.write_priority.await_fair_window",
            lambda *a, **k: "skip",
        )
        indexer._catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc", indexed_files=[(src, "code", "code__nexus")],
            on_locked="skip",
        )
        owner = cat.owner_for_repo("571b8edd")
        assert cat.by_file_path(owner, "src/main.py") is None

        # Next pass, window clear -> proceed -> registered.
        monkeypatch.setattr(
            "nexus.catalog.write_priority.await_fair_window",
            lambda *a, **k: "proceed",
        )
        indexer._catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc", indexed_files=[(src, "code", "code__nexus")],
            on_locked="skip",
        )
        assert cat.by_file_path(owner, "src/main.py") is not None


class TestCatalogWriterPriority:
    def test_writer_injects_interactive_priority(
        self, config_dir, db_path
    ) -> None:
        """A CatalogWriter(priority="interactive") opens the window; a batch
        writer does not. Exercised through the real factory + daemon."""
        from nexus.daemon.t2_client import make_t2_client
        from nexus.daemon.t2_daemon import T2Daemon

        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready, stop_evt = threading.Event(), threading.Event()
        th = threading.Thread(
            target=_run_daemon_in_thread,
            args=(daemon, ready, stop_evt), daemon=True,
        )
        th.start()
        assert ready.wait(timeout=10), "daemon did not start"
        try:
            from nexus.catalog.factory import make_catalog_writer

            probe_client = make_t2_client(config_dir=config_dir)

            batch_writer = make_catalog_writer(
                config_dir=config_dir, priority="batch",
            )
            assert batch_writer.routed is True
            batch_writer.register_owner(
                "acme", "project", repo_hash="hb", repo_root="/tmp/ab",
            )
            assert probe_client.catalog.is_interactive_write_pending() is False
            assert batch_writer.is_interactive_write_pending() is False
            batch_writer.close()

            inter_writer = make_catalog_writer(
                config_dir=config_dir, priority="interactive",
            )
            inter_writer.register_owner(
                "acme2", "project", repo_hash="hi", repo_root="/tmp/ai",
            )
            assert probe_client.catalog.is_interactive_write_pending() is True
            inter_writer.close()
            probe_client.close()
        finally:
            stop_evt.set()
            th.join(timeout=10)
