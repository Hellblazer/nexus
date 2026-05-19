# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.4 (nexus-uar6) — ``nx catalog`` CLI under
``NX_STORAGE_MODE=daemon``.

Scope: this file covers the T3-side catalog port. Several ``nx
catalog`` subcommands call ``_make_t3()`` (now daemon-aware via
``mcp_infra.get_t3``) to enumerate or write T3 collections. Without
the daemon-aware seam, the legacy ``make_t3()`` factory opens a
``PersistentClient`` on the same on-disk path the T3 daemon owns —
the chroma writer cannot tolerate two live processes on the same
DuckDB+Parquet store.

RDR-112 6shq.2 (nexus-3gdg) augmentation: now that ``_get_catalog``
also routes through the daemon (via ``open_catalog``), every CLI verb
in this file needs both a live T2 daemon AND a live T3 daemon. The
fixture ``live_t2_and_t3`` spawns both in-thread.
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


# ── In-thread T2 daemon harness (matches yfqv/idqd/lj2l pattern) ───────────


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
def catalog_dir(tmp_path: Path, monkeypatch) -> Path:
    """Initialize a real catalog under tmp_path and route
    ``nexus.config.catalog_path`` at it for the test."""
    from nexus.catalog import Catalog
    cd = tmp_path / "catalog"
    cd.mkdir()
    Catalog.init(cd)
    monkeypatch.setattr(
        "nexus.config.catalog_path",
        lambda: cd,
    )
    monkeypatch.setattr(
        "nexus.commands.catalog.catalog_path",
        lambda: cd,
        raising=False,
    )
    return cd


@pytest.fixture
def daemon_catalog_dir(
    tmp_path: Path, live_t2_daemon, monkeypatch,
) -> Path:
    """RDR-112 6shq.5 (nexus-o0pe): catalog dir initialized AFTER the
    T2 daemon is running.

    The dependency on ``live_t2_daemon`` forces fixture ordering so
    ``Catalog.init`` runs with both ``NX_STORAGE_MODE=daemon`` set
    AND the daemon reachable. Under daemon mode ``Catalog.init``
    routes through ``open_catalog`` and does NOT create a local
    ``.catalog.db``: the daemon owns the schema in ``memory.db``.
    The split-store regression assertion in the E2E tests relies on
    this ordering.
    """
    from nexus.catalog import Catalog
    cd = tmp_path / "catalog"
    cd.mkdir()
    Catalog.init(cd)
    monkeypatch.setattr(
        "nexus.config.catalog_path", lambda: cd,
    )
    monkeypatch.setattr(
        "nexus.commands.catalog.catalog_path", lambda: cd, raising=False,
    )
    return cd


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
        # RDR-112 6shq.2 (nexus-3gdg): drop the process-singleton
        # T2Client before stopping the daemon so the daemon's
        # server.wait_closed completes within its 5 s timeout. Without
        # this, the orphan client sockets held by the catalog cache
        # block the daemon's accept loop from finishing teardown.
        from nexus.catalog import reset_cache
        reset_cache()
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


class TestDaemonDownClickException:
    """3gdg review IMPORTANT-1 regression: ``DaemonNotRunningError`` is
    a ``RuntimeError`` subclass; Click does NOT translate it
    automatically. ``_get_catalog`` and the two direct opens in
    ``setup_cmd`` / ``_run_t3_doc_id_coverage`` must wrap the
    ``open_catalog`` call in ``try/except RuntimeError`` and re-raise
    ``click.ClickException`` so the operator sees a one-line message
    instead of a Python traceback.
    """

    def test_get_catalog_under_daemon_no_daemon_is_click_exception(
        self,
        runner: CliRunner,
        daemon_env,
        catalog_dir: Path,
    ) -> None:
        """``nx catalog list`` under daemon mode with no daemon
        running surfaces a ``ClickException`` (exit code 1, single
        message line), not a Python traceback."""
        result = runner.invoke(main, ["catalog", "list"])
        # ClickException -> exit_code 1 with the message printed to
        # output as a single ``Error: ...`` line. CliRunner translates
        # ClickException to ``SystemExit(1)`` so we can't isinstance-
        # check the exception class; instead we verify the output
        # shape that operators actually see.
        assert result.exit_code == 1, (
            f"daemon-down should exit 1 (ClickException), got "
            f"{result.exit_code}; output: {result.output!r}; "
            f"exc: {result.exception!r}"
        )
        # The output must be a single ``Error: ...`` line, not a
        # multi-line Python traceback. Pre-fix the operator saw the
        # full ``DaemonNotRunningError`` stack dump because Click does
        # not translate ``RuntimeError`` subclasses automatically.
        assert result.output.startswith("Error:"), (
            f"expected 'Error: ...' ClickException line; got: {result.output!r}"
        )
        assert "Traceback" not in result.output, (
            f"daemon-down should NOT surface a Python traceback; got: {result.output!r}"
        )
        # The original DaemonNotRunningError message points operators
        # at ``nx daemon t2 start`` — that hint must survive the
        # translation.
        assert "daemon" in result.output.lower(), result.output


class TestCatalogVerbsDaemonE2E:
    """RDR-112 6shq.5 (nexus-o0pe): end-to-end CLI tests under daemon
    mode for ``nx catalog show / link / search / setup``.

    Each test runs the verb against a live T2 + T3 daemon, seeded by a
    daemon-mode Catalog (so writes go through ``ExecuteProxy`` to the
    daemon's ``CatalogStore``). The split-store regression guard
    (``test_no_split_store_residue_after_lifecycle``) asserts that no
    ``.catalog.db`` file exists in the temp dir after the full
    lifecycle. The persistence assertions inside each verb test prove
    no ``EphemeralClient`` leak in the daemon session: a second CLI
    invocation observes the same data the first one wrote.
    """

    @staticmethod
    def _seed_two_docs(catalog_dir: Path):
        """Seed two documents through a daemon-mode Catalog so the CLI
        verbs have something to operate on.

        Returns ``(tumbler_a, tumbler_b)``. Both writes flow through
        the singleton T2Client / ExecuteProxy, so the daemon's
        ``memory.db`` is the canonical store.
        """
        from nexus.catalog import open_catalog
        cat = open_catalog(catalog_dir)
        owner = cat.register_owner(
            name="o0pe_e2e",
            owner_type="curator",
            description="6shq.5 E2E seed",
        )
        ta = cat.register(
            owner=owner,
            title="Alpha Document",
            content_type="text",
            physical_collection="docs__o0pe",
            chunk_count=1,
            head_hash="a" * 64,
        )
        tb = cat.register(
            owner=owner,
            title="Beta Document",
            content_type="text",
            physical_collection="docs__o0pe",
            chunk_count=1,
            head_hash="b" * 64,
        )
        return ta, tb

    @staticmethod
    def _assert_no_split_store(tmp_path: Path) -> None:
        """The 6shq.5 mandate: a daemon-mode catalog lifecycle must
        leave no ``.catalog.db`` files anywhere under tmp_path. The
        daemon owns its catalog state in ``memory.db``; a stray
        ``.catalog.db`` proves a CLI seam opened a direct CatalogDB
        and silently split the store.
        """
        residue = list(tmp_path.rglob(".catalog.db"))
        assert residue == [], (
            f"split-store regression: found {len(residue)} "
            f".catalog.db file(s) under {tmp_path}: {residue!r}. "
            f"Daemon mode must keep all catalog state on the daemon's "
            f"memory.db, not on a per-process .catalog.db."
        )

    def test_show_under_daemon_round_trips_through_proxy(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        daemon_catalog_dir: Path,
        tmp_path: Path,
    ) -> None:
        """``nx catalog show <tumbler>`` prints the seeded entry under
        daemon mode. The output must contain the title and
        physical_collection that the seed wrote.

        Cross-invocation persistence: run ``show`` twice. The second
        invocation must see the same data, proving the daemon's
        store is canonical (not a per-process EphemeralClient leak).
        """
        ta, _tb = self._seed_two_docs(daemon_catalog_dir)

        result1 = runner.invoke(main, ["catalog", "show", str(ta)])
        assert result1.exit_code == 0, result1.output
        assert "Alpha Document" in result1.output
        assert "docs__o0pe" in result1.output

        # Second invocation must observe the same state.
        result2 = runner.invoke(main, ["catalog", "show", str(ta)])
        assert result2.exit_code == 0, result2.output
        # Indexed_at line varies per run; compare the title block.
        assert "Alpha Document" in result2.output
        assert "docs__o0pe" in result2.output

        self._assert_no_split_store(tmp_path)

    def test_link_under_daemon_round_trips_through_catalogstore(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        daemon_catalog_dir: Path,
        tmp_path: Path,
    ) -> None:
        """``nx catalog link A B --type cites`` writes through the
        daemon's CatalogStore. The link must round-trip: a
        subsequent ``nx catalog show A`` displays the outgoing edge
        and ``nx catalog show B`` displays the incoming edge. Proves
        both write-path (link) and read-path (show -> links_from /
        links_to) work through the proxy.
        """
        ta, tb = self._seed_two_docs(daemon_catalog_dir)

        link_result = runner.invoke(
            main, ["catalog", "link", str(ta), str(tb), "--type", "cites"],
        )
        assert link_result.exit_code == 0, link_result.output
        assert "Linked" in link_result.output

        # Cross-invocation: a NEW CLI invocation must see the link.
        show_from = runner.invoke(main, ["catalog", "show", str(ta)])
        assert show_from.exit_code == 0, show_from.output
        assert "Links out:" in show_from.output
        assert str(tb) in show_from.output
        assert "cites" in show_from.output

        show_to = runner.invoke(main, ["catalog", "show", str(tb)])
        assert show_to.exit_code == 0, show_to.output
        assert "Links in:" in show_to.output
        assert str(ta) in show_to.output

        self._assert_no_split_store(tmp_path)

    def test_search_under_daemon_matches_seeded_titles(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        daemon_catalog_dir: Path,
        tmp_path: Path,
    ) -> None:
        """``nx catalog search <query>`` over the daemon's FTS5 index
        finds seeded titles. Exercises ``CatalogStore.search`` over
        RPC via ``ExecuteProxy.search`` (the FTS5 wrapper, not the
        raw execute path).
        """
        ta, tb = self._seed_two_docs(daemon_catalog_dir)

        # Match on "Alpha": only one doc should come back.
        alpha = runner.invoke(main, ["catalog", "search", "Alpha"])
        assert alpha.exit_code == 0, alpha.output
        assert "Alpha Document" in alpha.output
        assert "Beta Document" not in alpha.output

        # Match on "Document": both docs come back.
        both = runner.invoke(main, ["catalog", "search", "Document"])
        assert both.exit_code == 0, both.output
        assert "Alpha Document" in both.output
        assert "Beta Document" in both.output

        # Cross-invocation: re-run "Alpha" search; same result.
        alpha2 = runner.invoke(main, ["catalog", "search", "Alpha"])
        assert alpha2.exit_code == 0, alpha2.output
        assert "Alpha Document" in alpha2.output

        self._assert_no_split_store(tmp_path)

    def test_setup_under_daemon_writes_to_daemon_t2_sqlite(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """``nx catalog setup`` against a fresh catalog dir under
        daemon mode must:

        1. Exit 0 with the bootstrap echo lines printed.
        2. NOT create a ``.catalog.db`` file under tmp_path; the
           daemon's ``memory.db`` is the canonical store.
        3. Leave the schema reachable via the daemon (a follow-up
           ``nx catalog search`` succeeds without re-running setup).

        Uses its own catalog_dir construction (not the shared
        ``catalog_dir`` fixture) so we exercise the
        ``Catalog.is_initialized`` -> ``Catalog.init`` branch end-
        to-end rather than running setup against an already-initialised
        dir.
        """
        from nexus.catalog import Catalog
        cat_dir = tmp_path / "catalog_setup"
        cat_dir.mkdir()
        monkeypatch.setattr("nexus.config.catalog_path", lambda: cat_dir)
        monkeypatch.setattr(
            "nexus.commands.catalog.catalog_path", lambda: cat_dir, raising=False,
        )
        assert not Catalog.is_initialized(cat_dir)

        result = runner.invoke(main, ["catalog", "setup"])
        assert result.exit_code == 0, result.output
        # The setup verb prints a sequence of bootstrap status lines.
        # We check the first one to confirm the verb reached the
        # daemon-routed catalog open. The exact wording is part of
        # the user-visible CLI contract.
        assert (
            "Catalog initialized" in result.output
            or "Catalog already initialized" in result.output
        ), result.output

        # Split-store regression: setup must NOT have created a
        # .catalog.db file. Pre-3gdg the direct ``Catalog(path,
        # path / ".catalog.db")`` open in setup_cmd would have
        # created one even under daemon mode.
        self._assert_no_split_store(tmp_path)

        # The daemon's catalog must be queryable after setup. Run a
        # search to prove the schema is reachable through the daemon
        # (not just present in some isolated store).
        search = runner.invoke(main, ["catalog", "search", "anything"])
        assert search.exit_code == 0, search.output
        # Empty catalog returns "No results.", which is the correct
        # answer (setup against an empty T3 + empty registry
        # populates nothing).
        assert "No results" in search.output or search.output.strip() == "", (
            f"expected 'No results' or empty for empty catalog; "
            f"got: {search.output!r}"
        )

    def test_no_split_store_residue_after_full_lifecycle(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        daemon_catalog_dir: Path,
        tmp_path: Path,
    ) -> None:
        """End-to-end split-store regression check: run setup ->
        register -> show -> search -> link -> show in sequence and
        assert tmp_path holds no ``.catalog.db`` files. This is the
        bead's mandate (no_split_store_residue) made explicit.

        ``catalog_dir`` is already initialised by the fixture (which
        calls ``Catalog.init``); the test exercises every read +
        write verb against the live daemon end-to-end.
        """
        ta, tb = self._seed_two_docs(daemon_catalog_dir)

        sequence = [
            ["catalog", "show", str(ta)],
            ["catalog", "search", "Document"],
            ["catalog", "link", str(ta), str(tb), "--type", "relates"],
            ["catalog", "show", str(tb)],
        ]
        for cmd in sequence:
            result = runner.invoke(main, cmd)
            assert result.exit_code == 0, (
                f"{cmd!r} failed under daemon mode: {result.output!r}"
            )

        # Final assertion: nowhere under tmp_path should there be a
        # .catalog.db. The daemon owns the store.
        self._assert_no_split_store(tmp_path)


class TestCatalogBackfillCollections:
    def test_backfill_dry_run_under_daemon(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
        catalog_dir: Path,
    ) -> None:
        """``nx catalog backfill-collections --dry-run`` calls
        ``_make_t3().list_collections()`` to enumerate T3 collections,
        then reads the catalog ``documents.physical_collection`` column
        to compute the union. Under daemon mode, BOTH the T3 call
        routes through ``mcp_infra.get_t3`` AND the catalog read
        routes through ``open_catalog`` -> ``ExecuteProxy`` (RDR-112
        6shq.2, nexus-3gdg).

        The test does not need any pre-seeded T3 collections; an empty
        list is the correct dry-run output (\"Nothing to backfill\")
        and proves both round-trips work."""
        result = runner.invoke(
            main, ["catalog", "backfill-collections", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        # Either "Nothing to backfill" (empty T3 + empty catalog) or
        # a candidate list — both indicate the round-trip succeeded
        # without racing the daemon.
        assert (
            "Nothing to backfill" in result.output
            or "Would register" in result.output
            or "candidates" in result.output.lower()
        ), result.output
