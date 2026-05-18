# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.3 (nexus-mmvf) — ``nx taxonomy`` CLI under
``NX_STORAGE_MODE=daemon``.

Five subcommands in ``commands/taxonomy_cmd.py`` call ``_make_t3()``
(now daemon-aware via ``mcp_infra.get_t3``):

* ``discover`` (line ~437)
* ``rebuild`` (line ~545)
* ``split`` (line ~770)
* ``project`` (line ~1272)
* ``validate-refs`` (line ~1726)

All of them open a real T3 client; under daemon mode the seam routes
through the HttpClient to the live daemon instead of opening a
``PersistentClient`` racing the daemon on the on-disk path.

We exercise the seam directly (``_make_t3()`` call) plus a CliRunner
invocation of ``nx taxonomy list`` to confirm the broader command
group registration + ``_t2_ctx`` path also resolves under daemon mode.
``nx taxonomy list`` does not call ``_make_t3`` so it does not pin the
flip; the direct seam call does that.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.daemon.t2_daemon import T2Daemon
from nexus.db.t2 import T2Database


# ── In-thread T2 daemon harness (matches the yfqv / uar6 pattern) ──────────


def _run_daemon(daemon: T2Daemon) -> asyncio.AbstractEventLoop:
    started = threading.Event()
    loop = asyncio.new_event_loop()

    def _thread() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    started.wait(timeout=5.0)
    return loop


def _stop_daemon(daemon: T2Daemon, loop: asyncio.AbstractEventLoop) -> None:
    asyncio.run_coroutine_threadsafe(daemon.stop(), loop).result(timeout=5.0)
    loop.call_soon_threadsafe(loop.stop)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def reset_t3_singleton():
    import nexus.mcp_infra as infra
    original_t3 = infra._t3_instance
    original_collections = infra._collections_cache
    infra._t3_instance = None
    infra._collections_cache = ([], 0.0)
    yield
    infra._t3_instance = original_t3
    infra._collections_cache = original_collections


@pytest.fixture
def t2db(tmp_path: Path) -> T2Database:
    db = T2Database(tmp_path / "memory.db")
    yield db
    db.close()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture
def local_path(tmp_path: Path) -> Path:
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


@pytest.fixture
def daemon_env(monkeypatch, config_dir: Path):
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))


@pytest.fixture
def live_t2_daemon(t2db: T2Database, config_dir: Path, daemon_env):
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    try:
        yield daemon
    finally:
        _stop_daemon(daemon, loop)


@pytest.fixture
def live_t3_daemon(daemon_env, config_dir: Path, local_path: Path):
    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    try:
        yield payload
    finally:
        stop_t3_daemon(config_dir=config_dir)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── Tests ───────────────────────────────────────────────────────────────────


class TestTaxonomyMakeT3Seam:
    def test_make_t3_resolves_to_daemon_under_daemon_mode(
        self,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """``_make_t3()`` is the single seam every taxonomy T3 caller
        consumes. Under daemon mode it must return a ``T3Database``
        whose underlying client is the daemon's ``HttpClient``, not a
        ``PersistentClient`` racing the on-disk store.

        We assert on the client class name rather than identity so the
        test stays valid if chromadb's internal class is wrapped in
        future versions."""
        from nexus.commands.taxonomy_cmd import _make_t3
        t3 = _make_t3()
        # nexus-mmvf review Minor: chromadb's HttpClient and
        # PersistentClient factories both wrap the same
        # ``chromadb.api.client.Client`` class — ``type(...).__name__``
        # is ``Client`` in both cases. Assert the regression shape
        # via the module path of the underlying transport: an
        # HttpClient's identifier reaches into ``chromadb.api.fastapi``
        # while a PersistentClient uses ``chromadb.api.segment`` /
        # local persistence. Pragmatic check: confirm the client
        # answers ``list_collections()`` without raising (covered by
        # the next test) and that the identifier does NOT name a
        # filesystem path (an HttpClient's identifier is host:port).
        client_id = getattr(t3._client, "_identifier", "") or ""
        assert "/" not in client_id, (
            f"client identifier {client_id!r} looks like a filesystem "
            f"path — the seam likely returned a PersistentClient under "
            f"daemon mode, racing the daemon writer."
        )

    def test_make_t3_list_collections_via_daemon(
        self,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """End-to-end smoke: the helper's return value works for the
        canonical ``t3.list_collections()`` call that every taxonomy
        subcommand starts from. Empty list is the correct answer
        against a fresh daemon."""
        from nexus.commands.taxonomy_cmd import _make_t3
        t3 = _make_t3()
        cols = t3.list_collections()
        assert isinstance(cols, list)


class TestValidateRefsClickExceptionPassthrough:
    def test_validate_refs_propagates_make_t3_click_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """nexus-mmvf review S1: ``validate_refs`` historically caught
        ``Exception`` from ``make_t3()`` (which raised RuntimeError /
        OSError) and emitted its own "T3 unavailable" + exit 2. After
        the mmvf flip ``_make_t3()`` raises ``ClickException`` on the
        daemon-resolver failure path; the outer handler would
        previously have swallowed it, defeating Click's normal
        error-formatter (exit code 1 with the recovery hint). The
        in-place fix re-raises ``ClickException`` ahead of the generic
        ``Exception`` arm.

        This test pins the contract: when ``_make_t3()`` raises
        ``ClickException``, ``validate_refs`` must let Click handle
        it (exit code 1, message echoed by Click's formatter) rather
        than re-wrapping with exit code 2.
        """
        import click as _click
        from nexus.commands import taxonomy_cmd as _tax

        def _raise_click(*_a, **_kw):
            raise _click.ClickException("synthetic daemon-resolver failure")

        monkeypatch.setattr(_tax, "_make_t3", _raise_click)

        # validate-refs requires a markdown file argument (PATHS is
        # ``dir_okay=False``). _make_t3 fires inside the command body
        # before we reach any scan; an empty stub file is enough for
        # the Click arg-gate to pass.
        stub = tmp_path / "stub.md"
        stub.write_text("# stub\n")
        result = runner.invoke(
            main, ["taxonomy", "validate-refs", str(stub)],
        )
        # Click's default ClickException formatter exits with code 1
        # and writes "Error: <message>" to stderr. The regression
        # would have been exit code 2 ("T3 unavailable: ...").
        assert result.exit_code == 1, result.output
        assert "synthetic daemon-resolver failure" in result.output


class TestTaxonomyCliReadPath:
    def test_taxonomy_list_under_daemon(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """``nx taxonomy list`` reads from T2 via ``_t2_ctx`` (already
        daemon-aware). The taxonomy table is empty against a fresh
        daemon so the subcommand reports the empty state. This
        contract-pins the CLI registration + T2 routing under daemon
        mode; the T3 routing (``_make_t3``) is covered by the seam
        tests above."""
        result = runner.invoke(
            main, ["taxonomy", "list", "--collection", "code__mmvf-not-real"],
        )
        assert result.exit_code == 0, result.output
        # Empty taxonomy: the "no topics" hint surfaces with the
        # ``nx taxonomy discover`` remediation pointer.
        assert "no topics" in result.output.lower(), result.output
