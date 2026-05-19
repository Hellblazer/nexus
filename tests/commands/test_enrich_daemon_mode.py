# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.2 (nexus-yfqv) — ``nx enrich`` CLI under ``NX_STORAGE_MODE=daemon``.

Two subcommands need contract-pinning:

* ``nx enrich bib`` — the only enrich subcommand with a direct
  ``make_t3()`` call pre-yfqv (lines 171/177 in enrich.py). The yfqv
  flip replaces that with ``get_t3()`` so the bib enricher hits the
  daemon's chroma rather than racing it with a parallel
  ``PersistentClient`` on the same on-disk path.
* ``nx enrich aspects`` — already routes through ``t2_ctx()`` +
  ``get_t3()`` (pre-yfqv), but we contract-pin it here so a future
  refactor cannot silently regress.

We do not exercise the live Semantic Scholar / OpenAlex API. The bib
enricher backend is monkey-patched to return a deterministic synthetic
bib dict, so the test exercises:

1. ``get_t3()`` returns a daemon-bound HttpClient under daemon mode
   (the yfqv flip).
2. ``col.get(...)`` reads from the daemon's chroma.
3. ``col.update(...)`` writes back through the daemon's chroma.
4. An independent HttpClient (mirroring ``make_t3_client``'s
   resolution) sees the bib_* fields after the run — proving the write
   did NOT land in a parallel PersistentClient.

The vm3t lesson applies: unit tests that mocked ``make_t3`` could pass
forever while the bib enricher silently raced the daemon. Only an
end-to-end CliRunner invocation against a live daemon catches this.
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


# ── In-thread T2 daemon harness ─────────────────────────────────────────────


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
        # chak review IMPORTANT-1: drop the process-singleton T2Client
        # before stopping the daemon so the orphan socket pool does not
        # hold the daemon's UDS open past ``server.wait_closed``'s 5 s
        # timeout. Without this teardown a subsequent daemon-mode suite
        # that starts a new T2Daemon flaps on the unrelated previous
        # daemon's leaked sockets. Matches the pattern in
        # test_catalog_daemon_mode.py / test_dt_daemon_mode.py.
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


def _direct_http_client(payload: dict):
    """Independent HttpClient pointed at the same daemon — used to
    verify state without depending on the test's get_t3 singleton."""
    import chromadb
    return chromadb.HttpClient(host=payload["tcp_host"], port=payload["tcp_port"])


# ── nx enrich bib ───────────────────────────────────────────────────────────


def _seed_one_paper_chunk(
    payload: dict,
    *,
    collection: str,
    title: str,
    source_path: str,
    body: str,
) -> str:
    """Write one chunk into a conformant ``knowledge__*`` collection
    via an independent HttpClient. Returns the chunk id.

    The collection is created with a stub embedding function so the
    daemon's HttpClient accepts the put without a Voyage round-trip
    (this is the daemon-mode local path; the LocalEmbeddingFunction is
    bundled with nexus and provides deterministic 384-dim vectors).
    """
    import chromadb
    from nexus.db.local_ef import LocalEmbeddingFunction

    client = chromadb.HttpClient(
        host=payload["tcp_host"], port=payload["tcp_port"],
    )
    # Match the embedding function the daemon-mode T3Database uses under
    # NX_LOCAL=1 (``nexus_local``) so the chunk added here resolves
    # against the same persisted-EF identity when ``nx enrich bib``
    # re-opens the collection via ``get_or_create_collection``.
    ef = LocalEmbeddingFunction()
    col = client.get_or_create_collection(
        collection,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    chunk_id = "yfqv-bib-test-chunk-0"
    col.add(
        ids=[chunk_id],
        documents=[body],
        metadatas=[
            {
                "title": title,
                "source_path": source_path,
                # ``bib_openalex_id`` is pre-seeded as empty string
                # (not absent) so the enrich loop's
                # ``if meta.get(id_field, ""):`` skip-already-enriched
                # check returns falsy and the chunk is processed.
                # Post-enrich it carries the synthetic id.
                "bib_openalex_id": "",
            }
        ],
    )
    return chunk_id


class TestEnrichBib:
    def test_bib_flip_writes_via_daemon(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """End-to-end bib enrichment through the daemon.

        Pre-yfqv: ``make_t3()`` opened a ``PersistentClient`` directly
        at the daemon's on-disk path, racing the chroma subprocess.
        Post-yfqv: ``get_t3()`` returns an HttpClient bound to the
        daemon. This test seeds a chunk via an independent HttpClient,
        runs ``nx enrich bib``, and verifies the bib_* fields landed
        in the daemon's chroma (visible via a SECOND independent
        HttpClient).
        """
        # Use a conformant collection name so the strict gate in
        # T3Database.get_or_create_collection accepts it. The daemon
        # was started fresh in local_path, so this is a brand-new
        # collection from chroma's perspective.
        collection = "knowledge__yfqv-bib__minilm-l6-v2-384__v1"
        chunk_id = _seed_one_paper_chunk(
            live_t3_daemon,
            collection=collection,
            title="Attention Is All You Need",
            source_path="/tmp/attention.pdf",
            body="Transformer architecture body text for yfqv smoke.",
        )

        # Replace the OpenAlex backend with a deterministic stub so the
        # test does not hit the network. The bib enricher resolves via
        # ``_resolve_bib_backend("openalex")`` → ``enrich`` callable.
        # We also stub the by-DOI / by-arxiv paths because the body
        # text contains no identifiers, but be defensive.
        import nexus.bib_enricher_openalex as oa
        synthetic_bib = {
            "year": 2017,
            "venue": "NeurIPS",
            "authors": "Vaswani et al.",
            "citation_count": 99999,
            "openalex_id": "W2963446712",
        }
        monkeypatch.setattr(
            oa, "enrich", lambda title: dict(synthetic_bib),
        )
        monkeypatch.setattr(
            oa, "enrich_by_doi",
            lambda doi, expected_title=None: dict(synthetic_bib),
        )
        monkeypatch.setattr(
            oa, "enrich_by_arxiv_id",
            lambda arxiv, expected_title=None: dict(synthetic_bib),
        )

        result = runner.invoke(
            main,
            [
                "enrich",
                "bib",
                collection,
                "--source",
                "openalex",
                "--delay",
                "0",  # no inter-call sleep
                "--limit",
                "1",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Done: enriched 1 chunks across 1 titles" in result.output, result.output

        # Verify via a second independent HttpClient (paranoia: prove
        # the write hit THE daemon, not a parallel PersistentClient).
        client = _direct_http_client(live_t3_daemon)
        col = client.get_collection(
            collection,
            # Match the embedding-fn so the collection handle resolves.
            embedding_function=None,
        )
        fetched = col.get(ids=[chunk_id], include=["metadatas"])
        meta = fetched["metadatas"][0]
        assert meta.get("bib_year") == 2017, meta
        assert meta.get("bib_venue") == "NeurIPS", meta
        assert meta.get("bib_openalex_id") == "W2963446712", meta

    def test_bib_empty_collection_routes_via_daemon(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """Even an empty collection exercises ``get_t3()`` +
        ``get_or_create_collection`` + ``col.get(...)`` against the
        daemon. Confirms the flip routes through the daemon when there
        is nothing to enrich (the early-exit path)."""
        collection = "knowledge__yfqv-bib-empty__minilm-l6-v2-384__v1"
        # Pre-create the collection via the independent HttpClient so
        # the empty-collection path is reached deterministically.
        import chromadb
        from nexus.db.local_ef import LocalEmbeddingFunction
        client = chromadb.HttpClient(
            host=live_t3_daemon["tcp_host"],
            port=live_t3_daemon["tcp_port"],
        )
        client.get_or_create_collection(
            collection,
            embedding_function=LocalEmbeddingFunction(),
            metadata={"hnsw:space": "cosine"},
        )

        result = runner.invoke(
            main,
            [
                "enrich",
                "bib",
                collection,
                "--source",
                "openalex",
                "--delay",
                "0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "is empty" in result.output, result.output


# ── nx enrich aspects ───────────────────────────────────────────────────────


class TestEnrichAspects:
    def test_aspects_dry_run_under_daemon(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """``nx enrich aspects --dry-run`` against a collection with no
        registered extractor exits cleanly. The path still imports
        ``t2_ctx`` / ``get_t3`` at runtime; this contract-pins the
        already-routed call sites against the live daemon."""
        # docs__ has no registered extractor — the command should echo
        # the "no extractor config" message and return cleanly.
        result = runner.invoke(
            main,
            [
                "enrich",
                "aspects",
                "docs__yfqv-aspects-nope__minilm-l6-v2-384__v1",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "No extractor config registered" in result.output


# ── _StoreProxy hygiene (unit-level pin for the yfqv refactor) ────────────


class TestStoreProxyHygiene:
    def test_document_aspects_proxy_skips_underscore_methods(self) -> None:
        """``_StoreProxy`` skips ``_``-prefixed methods at dispatch-table
        build (proxy invariant). Pre-yfqv, ``enrich aspects-show``
        called ``db.document_aspects._has_doc_id_pk()`` directly — that
        call would AttributeError against the daemon facade. yfqv
        refactored the call site to use the public ``get_by_doc_id`` +
        chained ``or`` ``get(...)`` instead. This test pins the
        invariant against future regressions: no underscore-prefixed
        method may be relied on across the T2Client boundary."""
        from nexus.daemon.t2_client import _StoreProxy
        from nexus.db.t2.document_aspects import DocumentAspects

        # Build a proxy with a None pool — we never call methods on it,
        # we only inspect which method names were registered. The
        # construction path is method-name-only and pool-independent.
        proxy = _StoreProxy("document_aspects", DocumentAspects, pool=None)  # type: ignore[arg-type]
        # The methods dict is populated at construction; both public
        # methods must be present, the underscore probe must be absent.
        assert "get_by_doc_id" in proxy._methods
        assert "get" in proxy._methods
        assert "_has_doc_id_pk" not in proxy._methods


# ── nx enrich list (uses t2_ctx; pre-yfqv was already routed) ──────────────


class TestEnrichList:
    def test_list_routes_through_daemon(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """``nx enrich list`` reads aspect rows from T2 — verifies the
        ``t2_ctx()`` call site in enrich.py:1201 still resolves to the
        daemon (an empty result is fine; we only assert routing)."""
        result = runner.invoke(
            main,
            [
                "enrich",
                "list",
                "knowledge__yfqv-list__minilm-l6-v2-384__v1",
            ],
        )
        assert result.exit_code == 0, result.output


# ── RDR-112 6shq.3 (nexus-siy7): enrich Catalog opens flipped to factory ─────


@pytest.fixture
def catalog_dir(tmp_path: Path, monkeypatch) -> Path:
    """Initialize a real catalog under tmp_path and route
    ``nexus.config.catalog_path`` at it for the test. Mirrors the
    fixture in ``test_catalog_daemon_mode.py``."""
    from nexus.catalog import Catalog
    cd = tmp_path / "catalog"
    cd.mkdir()
    Catalog.init(cd)
    monkeypatch.setattr(
        "nexus.config.catalog_path",
        lambda: cd,
    )
    monkeypatch.setattr(
        "nexus.commands.enrich.catalog_path",
        lambda: cd,
        raising=False,
    )
    return cd


class TestEnrichDaemonDownClickException:
    """siy7 (3gdg-style) regression: ``DaemonNotRunningError`` is a
    ``RuntimeError`` subclass; Click does NOT translate it automatically.
    The flipped ``open_cached`` / ``open_catalog`` sites in
    ``_resolve_catalog_entry`` + ``_select_entries`` + ``aspects-list
    --missing`` must wrap the call in ``try/except RuntimeError`` and
    re-raise ``click.ClickException`` so the operator sees a one-line
    message instead of a Python traceback.
    """

    def test_aspects_show_under_daemon_no_daemon_is_click_exception(
        self,
        runner: CliRunner,
        daemon_env,
        catalog_dir: Path,
    ) -> None:
        """``nx enrich aspects-show <tumbler>`` under daemon mode with no
        daemon running surfaces a ``ClickException`` via
        ``_resolve_catalog_entry`` -> ``open_cached``."""
        # A non-existent tumbler is fine; the open_cached call fails
        # before tumbler resolution because the daemon is down.
        result = runner.invoke(
            main, ["enrich", "aspects-show", "x.0.0.0"],
        )
        assert result.exit_code == 1, (
            f"daemon-down should exit 1 (ClickException), got "
            f"{result.exit_code}; output: {result.output!r}; "
            f"exc: {result.exception!r}"
        )
        assert result.output.startswith("Error:"), (
            f"expected 'Error: ...' ClickException line; got: {result.output!r}"
        )
        assert "Traceback" not in result.output, (
            f"daemon-down should NOT surface a Python traceback; got: {result.output!r}"
        )
        assert "daemon" in result.output.lower(), result.output

    def test_aspects_list_missing_under_daemon_no_daemon_is_click_exception(
        self,
        runner: CliRunner,
        daemon_env,
        catalog_dir: Path,
    ) -> None:
        """``nx enrich aspects-list <coll> --missing`` flows through
        ``open_catalog`` -> ClickException translation. Pins the
        second of the two ClickException-translating siy7 sites in
        enrich.py."""
        result = runner.invoke(
            main,
            [
                "enrich",
                "aspects-list",
                "--collection",
                "knowledge__siy7-noop__minilm-l6-v2-384__v1",
                "--missing",
            ],
        )
        assert result.exit_code == 1, (
            f"daemon-down should exit 1 (ClickException), got "
            f"{result.exit_code}; output: {result.output!r}"
        )
        assert result.output.startswith("Error:"), (
            f"expected 'Error: ...'; got: {result.output!r}"
        )
        assert "Traceback" not in result.output
