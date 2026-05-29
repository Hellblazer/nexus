import logging

import pytest
from pathlib import Path

import chromadb
import structlog
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database


def _enable_t2_test_auto_migrate() -> None:
    """RDR-120 P3b: T2Database.__init__ no longer auto-runs migrations
    in production (the daemon owns ``apply_pending``). The test suite
    has hundreds of direct-open call sites that rely on a freshly-
    migrated schema, so we opt the in-process default ON and also
    set the ``NX_T2_AUTO_MIGRATE`` env var so subprocesses
    (``subprocess.run`` / ``claude -p`` / MCP children) that inherit
    ``os.environ`` but not Python module state get the same default.
    Production code paths (CLI, MCP servers) keep the
    daemon-owns-migration semantic; only the test process tree sees
    the flipped default.
    """
    import os

    from nexus.db import t2 as _t2

    _t2._DEFAULT_RUN_MIGRATIONS = True
    os.environ.setdefault(_t2._RUN_MIGRATIONS_ENV, "1")


_enable_t2_test_auto_migrate()


def pytest_configure(config):
    """Configure structlog level to match pytest's --log-level.

    Default run: WARNING level — quiet, no clutter.
    Validation run: pytest --log-level=DEBUG — full structlog output to stdout.

    Example:
        uv run pytest                          # quiet (WARNING)
        uv run pytest --log-level=DEBUG        # full debug output
    """
    try:
        level_str = (config.getoption("log_level") or "WARNING").upper()
    except (ValueError, AttributeError):
        level_str = "WARNING"
    level = getattr(logging, level_str, logging.WARNING)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


# nexus-nifd: prefixes that the indexer's repo cache uses for
# pytest fixture-named test repos. Files at
# ``~/.config/nexus/<prefix>-*-<repo_hash>.cache`` matching one of
# these are evidence that a test bypassed the autouse
# ``_isolate_config_dir`` fixture (e.g. a subprocess that didn't
# inherit ``NEXUS_CONFIG_DIR``, or a test that explicitly
# ``monkeypatch.delenv("NEXUS_CONFIG_DIR")``). Update this list when
# adding a new fixture-named test repo.
_FIXTURE_CACHE_PREFIXES: tuple[str, ...] = (
    "nexus-rich0",
    "nexus-mini0",
    "code-repo",
    "prose-repo",
    "pdf-repo",
    "stage-b-repo",
    "sentinel-repo",
    "test-repo",
    "nx-shakeout-",
)


def _scan_fixture_cache_files() -> set[Path]:
    """Return the set of *.cache files in the REAL ~/.config/nexus/
    whose basename starts with a fixture-cache prefix. Empty when
    the directory doesn't exist.

    Uses Path.home() rather than ``nexus_config_dir()`` to bypass
    any test-time NEXUS_CONFIG_DIR override; the leak we're guarding
    against is precisely tests that hit the REAL config dir.
    """
    real_config = Path.home() / ".config" / "nexus"
    if not real_config.exists():
        return set()
    return {
        p for p in real_config.glob("*.cache")
        if p.name.startswith(_FIXTURE_CACHE_PREFIXES)
    }


_fixture_cache_baseline: set[Path] = set()


def pytest_sessionstart(session):
    """Snapshot fixture cache files in ~/.config/nexus/ at session
    start so ``pytest_sessionfinish`` can detect leaks introduced
    during the session (nexus-nifd).
    """
    global _fixture_cache_baseline
    _fixture_cache_baseline = _scan_fixture_cache_files()


def pytest_sessionfinish(session, exitstatus):
    """nexus-nifd: fail the session when any new test-fixture cache
    file appears in the REAL ~/.config/nexus/ during the session.

    Background: 2026-05-08 prod shakeout found 1,707 leaked
    test-fixture cache files (~121.5 MB) accumulated over weeks.
    The autouse ``_isolate_config_dir`` fixture (PR #601 / nexus-
    mrmq) prevents future leakage for tests that USE it, but a
    test that bypasses the fixture or spawns a subprocess without
    propagating ``NEXUS_CONFIG_DIR`` could re-introduce the leak
    silently. This guard catches that class.

    Best-effort cleanup: any newly-leaked file is unlinked before
    the failure surfaces so the next run starts from a clean
    baseline. The session is still failed so the offending test
    is visible in CI.
    """
    after = _scan_fixture_cache_files()
    leaked = after - _fixture_cache_baseline
    if not leaked:
        return
    # Surface and clean up.
    leaked_sorted = sorted(leaked)
    for path in leaked_sorted:
        try:
            path.unlink()
        except OSError:
            pass
    names = ", ".join(p.name for p in leaked_sorted[:5])
    suffix = "" if len(leaked_sorted) <= 5 else f" (+{len(leaked_sorted) - 5} more)"
    session.exitstatus = 1
    print(
        f"\n\nFAIL: nexus-nifd cache-leak guard caught "
        f"{len(leaked_sorted)} fixture-cache file(s) leaked into "
        f"~/.config/nexus/: {names}{suffix}\n"
        f"  Cause: a test bypassed the autouse `_isolate_config_dir` "
        f"fixture or spawned a subprocess without inheriting "
        f"NEXUS_CONFIG_DIR.\n"
        f"  Cleanup: leaked files removed; failing the session.\n",
        flush=True,
    )


@pytest.fixture(autouse=True)
def _restore_structlog_after_test():
    """Save and restore structlog config around every test so any test
    that calls ``structlog.configure(...)`` (directly or via
    ``nexus.logging_setup.configure_logging``) does not leak its
    config to downstream tests.

    Background: tests that swap ``logger_factory`` from the default
    ``PrintLoggerFactory`` to ``LoggerFactory(stdlib)`` reroute every
    structlog event from stderr to stdlib logging. ``capsys``-based
    assertions in unrelated tests then read empty strings while the
    event sits in caplog. The originally-affected test was
    ``test_plan_audit_logs_warning_on_clamp``, which fails when run
    after any test that pollutes structlog. Solving it per-file via
    individual autouse fixtures drifted; a global one is cheap and
    closes the door for new tests too.
    """
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


@pytest.fixture(autouse=True)
def _pin_storage_mode_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """RDR-120 P6 (nexus-qg86h): direct mode decommissioned.
    ``storage_mode()`` always returns ``"daemon"`` now and the
    NX_STORAGE_MODE env-var is a deprecation-warning shim. The
    test conftest no longer pins a mode — any test that previously
    relied on direct semantics (``make_t3()`` without ``_client``
    injection getting a ``PersistentClient``) must now inject
    ``_client=chromadb.EphemeralClient()`` explicitly.

    Kept as a (mostly) no-op autouse fixture so test files that
    reference the symbol via ``request.getfixturevalue`` still
    resolve; ``monkeypatch.delenv`` clears any caller-set value so
    ``storage_mode()`` doesn't fire the deprecation warning during
    normal pytest runs.
    """
    monkeypatch.delenv("NX_STORAGE_MODE", raising=False)


@pytest.fixture(autouse=True)
def _isolate_t1_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force tests onto the explicit-isolation T1 path.

    RDR-105 P4 (nexus-jnx7) collapsed T1 discovery to a single
    four-branch fail-loud gate. With no env vars and no addr file,
    the constructor raises ``T1ServerNotFoundError``. Tests that
    previously relied on the legacy EphemeralClient fallback opt
    in via the ``NX_T1_ISOLATED=1`` (or its deprecated
    ``NEXUS_SKIP_T1=1`` alias) Path C; this autouse fixture sets
    the alias process-wide so the suite gets a per-test
    EphemeralClient by default. Tests that need a different mode
    (env-passdown, addr file, fail-loud raise) override the env
    inside the test.
    """
    monkeypatch.setenv("NEXUS_SKIP_T1", "1")


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect NEXUS_CONFIG_DIR so child processes write under tmp_path.

    nexus-mrmq: integration tests that dispatch ``claude -p`` subprocesses
    (the operator dispatch path, plan-runner, nx_answer equivalence
    suite) inherit the parent's ``os.environ``. Without this fixture
    the child resolves ``nexus_config_dir()`` to the user's real
    ``~/.config/nexus/`` and writes ``current_session`` /
    ``t1_addr.<claude_pid>`` files there. Reproduced 2026-05-08 during
    4.27.1 shakeout: a transient ``claude_dispatch -p`` subprocess
    rewrote the live MCP's session file and unlinked its addr file
    mid-session.

    Setting ``NEXUS_CONFIG_DIR`` here is read at call time inside
    ``nexus.config.nexus_config_dir()`` and propagates to children
    via ``os.environ`` inheritance, so every spawned subprocess
    (regardless of operator-dispatch mode) writes its config files
    under the per-test tmp dir.

    Tests that need to assert the default path (``Path.home() /
    .config / nexus``) explicitly ``monkeypatch.delenv`` first; that
    still works because this fixture's ``monkeypatch.setenv`` is
    overridden by any later test-local ``setenv`` / ``delenv`` call.

    Path layout mirrors the natural ``~/.config/nexus`` relative
    layout (``tmp_path/.config/nexus``) so per-test fixtures that
    set ``HOME=tmp_path`` and write into ``tmp_path/.config/nexus/``
    (e.g. ``test_scratch_cmd.fake_home``) land at the same path
    ``read_claude_session_id`` resolves to via ``NEXUS_CONFIG_DIR``.

    The directory itself is *not* pre-created — write helpers
    (``write_claude_session_id``, ``write_t1_addr``, etc.) all do
    ``parents=True, exist_ok=True`` themselves, and tests that
    explicitly call ``mkdir(parents=True)`` without ``exist_ok``
    on the same path would otherwise hit ``FileExistsError``.
    """
    config_dir = tmp_path / ".config" / "nexus"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))


@pytest.fixture(autouse=True)
def _isolate_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect NEXUS_CATALOG_PATH so tests never pollute the real user catalog.

    Without this, integration tests that trigger _catalog_hook() (via index_repo,
    index_markdown, or similar) register documents in the user's live catalog at
    ~/.config/nexus/. Before this fixture landed (RDR-060, 2026-04-08), 64
    orphan ``int-cce-*`` curator owners accumulated from
    ``test_cce_query_retrieves_cce_indexed_markdown`` alone.

    The fixture works because catalog write paths guard on
    ``Catalog.is_initialized(cat_path)`` — the tmp path is never initialised,
    so hooks return early. See ``tests/test_catalog_isolation.py`` for the
    regression tests that lock this behaviour in (nexus-dqr3 / nexus-b34f).
    """
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "test-catalog"))


@pytest.fixture(autouse=True)
def _reset_aspect_worker_singleton() -> None:
    """Reset the module-level aspect_worker singleton around every test.

    nexus-u0u8a: ``aspect_extraction_enqueue_hook`` lazy-spawns a singleton
    daemon-thread worker via ``ensure_worker_started()`` for any
    supported-collection (knowledge__/rdr__/docs__) document hook. Only
    ``test_aspect_worker.py`` / ``test_aspect_drain_protocol.py`` reset it,
    so any OTHER test that fires such a hook leaks the singleton. The leaked
    worker keeps polling ``t2_index_write`` (degraded fallback to
    ``T2Database(default_db_path())``), and when a later test patches
    ``default_db_path`` to its own tmp db the worker claims + ``mark_done``s
    rows out from under that test — the exact mechanism behind the
    ``test_collection_rename`` aspect-cascade canary (debugger verdict
    2026-05-28: 95% repro). Resetting before AND after each test confines a
    spawned worker to its own test so it can never poll a sibling's db.
    """
    from nexus.aspect_worker import reset_worker_for_tests
    reset_worker_for_tests()
    yield
    reset_worker_for_tests()


def set_credentials(monkeypatch) -> None:
    """Set required T3/Voyage credential env vars for tests that call _has_credentials().

    Shared helper used by test_doc_indexer.py and test_pdf_subsystem.py to avoid
    duplicating the same four setenv calls across both files.
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "vk_test")
    monkeypatch.setenv("CHROMA_API_KEY", "ck_test")
    monkeypatch.setenv("CHROMA_TENANT", "tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "db")


# RDR-109 Phase 1: cloud-mode opt-in fixture.
#
# Default test mode is local (no API keys, ONNX MiniLM EF). Tests that
# assert cloud-mode behavior — voyage-context-3 / voyage-code-3 embedder
# names, _has_credentials() gated paths, CloudClient routing — opt in via
# this fixture (or class-level
# ``pytestmark = pytest.mark.usefixtures("cloud_mode")``).
#
# The lint test ``test_mode_declarations_are_explicit`` enforces that any
# test function whose source contains ``voyage-(context|code)-3`` either
# depends on ``cloud_mode`` or is listed in ``_MODE_LINT_EXCLUDE`` below.
@pytest.fixture
def cloud_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activate cloud mode: set Voyage/Chroma credentials and force
    ``nexus.config.is_local_mode`` to return False.

    Callers that do ``from nexus.config import is_local_mode`` inside a
    function body (the established pattern in this codebase — see all
    callsites under ``src/nexus/``) pick up the patch on next call.
    """
    set_credentials(monkeypatch)
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)


# Tests whose source matches the voyage-token regex but legitimately do
# NOT need cloud_mode. Two granularities:
#   * ``_MODE_LINT_EXCLUDE_FILES`` — every test in the file is exempt.
#     Use for files whose tests are uniformly schema / name-shape /
#     canonical-set tests where the voyage token is a label, not a
#     behavior assertion.
#   * ``_MODE_LINT_EXCLUDE_NODEIDS`` — individual ``file.py::test_x``
#     entries for mixed files.
#
# Exclusion reasons fall into:
#   - "canonical-set": tests of ``corpus.canonical_embedding_model``
#     or ``CollectionName`` schema constants; the token is the schema's
#     canonical embedder name, not a behavior assertion.
#   - "string-literal-as-name": the test builds a conformant collection
#     name string and asserts on the *name shape* (RDR-103
#     ``<content_type>__<owner>__<model>__v<n>``), not on the embedder
#     that actually ran. The name is canonical regardless of mode.
#   - "parametrize-label": the voyage token appears only in a
#     ``pytest.mark.parametrize`` data tuple or test id.
#   - "docstring-or-comment": the voyage token appears only in a
#     docstring or comment, not in executable code.
#   - "mode-self-test": the test asserts local-mode behavior itself
#     (``test_local_mode.py``); cloud_mode would invert what it tests.
#
# Files that primarily exercise cloud-mode behavior (real Voyage calls,
# CloudClient routing, ``_has_credentials()`` gated paths) do NOT appear
# here; they declare ``pytestmark = pytest.mark.usefixtures("cloud_mode")``
# at module scope instead. See ``docs/contributing.md`` and
# ``tests/AGENTS.md``.
_MODE_LINT_EXCLUDE_FILES: frozenset[str] = frozenset({
    # Cloud-behavior files — Phase 1 ships the lint mechanism with these
    # excluded; subsequent PRs promote each to module-level
    # ``pytestmark = pytest.mark.usefixtures("cloud_mode")``. Promotion is
    # per-file so each can be validated against the suite independently.
    # The lint test itself contains the regex.
    "test_mode_declarations_are_explicit.py",
    # RDR-109 Phase 2 dispatch tests intentionally name voyage tokens
    # to exercise the (mode, name) matrix. Voyage names here are the
    # subject under test, not assertions of cloud-mode behavior.
    "test_rdr_109_phase2_dispatch.py",
    "test_catalog_path.py",
    "test_chroma_retry.py",
    "test_collection_cmd.py",
    "test_doc_indexer.py",
    "test_exporter.py",
    "test_index_cmd.py",
    "test_index_pdf_batch.py",
    "test_index_rdr_cmd.py",
    "test_indexer.py",
    "test_indexer_e2e.py",
    "test_integration.py",
    "test_mcp_server.py",
    "test_pdf_chunks_no_silent_zero.py",
    "test_pdf_e2e.py",
    "test_pdf_extractor.py",
    "test_pdf_subsystem.py",
    "test_pipeline_stages.py",
    "test_store_cmd.py",
    "test_voyage_retry.py",
    # Schema / canonical-set / collection-name shape — mode-independent.
    "test_backfill_hash.py",
    "test_catalog_backfill_collections.py",
    "test_catalog_cli.py",
    "test_catalog_collection_for.py",
    "test_catalog_collection_name.py",
    "test_catalog_collections.py",
    "test_catalog_collections_rebuild.py",
    "test_catalog_concurrent_writer_lock.py",
    "test_catalog_consolidation.py",
    "test_catalog_db.py",
    "test_catalog_doctor_collections_drift.py",
    # RDR-103 / nexus-j9ey + b03o advisor: voyage tokens appear in
    # synthetic collection names being asserted against, not as
    # cloud-mode behaviour under test.
    "test_catalog_doctor_name_vs_embed_dim.py",
    "test_upgrade_name_vs_embed_dim_advisory.py",
    "test_catalog_incremental_rebuild.py",
    "test_catalog_manifest_backfill.py",
    "test_catalog_migrate_fallback.py",
    "test_catalog_papers_curator_isolation.py",
    "test_catalog_rename_collection.py",
    "test_catalog_spans_chunk_char.py",
    "test_checkpoint.py",
    "test_collection_gc.py",
    "test_collection_name_migration.py",
    # RDR-137 P1.5a: voyage tokens appear in synthetic conformant
    # collection names used as backfill fixtures (e.g.
    # ``code__nexus-1-1__voyage-code-3__v1``). Tests exercise pure
    # SQLite + string parsing; no Voyage call is ever made.
    "test_collections_owner_backfill.py",
    # RDR-137 P2a (nexus-tts0d.4): same voyage-token-in-fixture pattern
    # — the catalog-backed reader tests register synthetic conformant
    # collection names and read them back; no Voyage call.
    "test_repos_reader.py",
    # RDR-137 P4.3 (nexus-tts0d.17): same pattern — knowledge__ /
    # docs__ collection names used as fixtures for the catalog
    # writer+reader cycle; no Voyage call.
    "test_index_corpus_knowledge_e2e.py",
    # RDR-137 followup CRITICAL-3/4/5 (nexus-43qgm.3-5): voyage tokens
    # appear in synthetic conformant collection names used as
    # adapter-test fixtures; no Voyage call is ever made.
    "test_rdr137_followup_critical_345.py",
    # RDR-137 followup SIG-6/8/11 (nexus-43qgm.6,8,11): same pattern
    # — voyage tokens in synthetic collection-name fixtures for the
    # OQ-5 deterministic-ordering and catalog-missing observability
    # tests; no Voyage call.
    "test_rdr137_followup_reader_sigs.py",
    # RDR-137 followup SIG-10/13/14/17 (nexus-43qgm.10,13,14,17):
    # voyage tokens in adapter / context / collection synthetic
    # fixtures; no Voyage call.
    "test_rdr137_followup_batch_sigs.py",
    # RDR-137 followup IMP-18..27 (nexus-43qgm.18-27): voyage tokens
    # in list_sibling_collections + adapter fixtures; no Voyage call.
    "test_rdr137_followup_p2_batch.py",
    # RDR-137 P3.5 (nexus-tts0d.10): same pattern — phantom
    # docs__1-2188 in the regression fixture for nexus-9iw41.
    "test_context_catalog_cutover.py",
    "test_commands_dt.py",
    "test_corpus.py",
    "test_doc_indexer_hash_sync.py",
    "test_doctor_cmd.py",
    "test_doctor_integrity.py",
    "test_doctor_search.py",
    "test_indexer_conformant_names.py",
    "test_indexer_duplicate_content.py",
    "test_indexer_modules.py",
    "test_indexer_utils_repo.py",
    "test_memory.py",
    "test_metadata_consistency.py",
    "test_metadata_schema.py",
    "test_migrations_rdr108_phase1c.py",
    "test_plan_run.py",
    "test_rdr_hook.py",  # tests/hooks/ — collection-name shape only
    "test_registry.py",
    "test_source_uri_home_key.py",
    "test_store_enrich_doc_id.py",
    "test_store_put_cli_parity.py",
    "test_t3_strict_collection_naming.py",
    "test_t3.py",
    "test_tuning_config.py",
    # Mode-self-tests — these assert local-mode behavior; cloud_mode
    # would invert what they test.
    "test_local_mode.py",
})

_MODE_LINT_EXCLUDE_NODEIDS: frozenset[str] = frozenset({
    # Reserved for individual mixed-file exclusions. Format:
    # "tests/test_file.py::test_func"  (no parametrize suffix).
})


@pytest.fixture
def db(tmp_path: Path) -> T2Database:
    """Provide a T2Database backed by a temporary SQLite file."""
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


@pytest.fixture
def local_t3() -> T3Database:
    """T3Database backed by an in-memory EphemeralClient and DefaultEmbeddingFunction.

    Each test gets a fresh, isolated database — no API keys required.
    DefaultEmbeddingFunction uses the bundled ONNX MiniLM-L6-v2 model,
    so semantic similarity works correctly without Voyage AI.
    """
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    return T3Database(_client=client, _ef_override=ef)


# ── PDF fixture generators ─────────────────────────────────────────────────

_PAGE_TOPICS = [
    "Apple orchards produce fruit in autumn harvests.",
    "Database transactions ensure ACID consistency in storage systems.",
    "Network protocols define communication rules between distributed nodes.",
]


def _make_simple_pdf(path: Path) -> None:
    """1-page TrueType PDF with embedded metadata."""
    import pymupdf  # lazy

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 100),
        "Hello World. This is a test document for PDF ingest.",
        fontsize=12,
    )
    doc.set_metadata({
        "title": "Test Document",
        "author": "Test Author",
        "subject": "PDF Ingest Testing",
        "keywords": "test, pdf, nexus",
        "creationDate": "D:20260301000000",
    })
    doc.save(str(path))
    doc.close()


def _make_multipage_pdf(path: Path) -> None:
    """3-page TrueType PDF with semantically distinct content per page.

    Each page uses insert_textbox to fill a text rectangle (~2000 chars).
    This ensures:
    - PDFChunker(chunk_chars=100) produces multiple chunks (AC-U9/U10).
    - PDFChunker with the default 1500-char limit produces at least one
      dedicated chunk per page for reliable page attribution in E2E tests (AC-E2).
    """
    import pymupdf  # lazy

    doc = pymupdf.open()
    rect = pymupdf.Rect(72, 72, 523, 750)
    for topic in _PAGE_TOPICS:
        page = doc.new_page()
        text = f"{topic} " * 30
        page.insert_textbox(rect, text.strip(), fontsize=12)
    doc.set_metadata({"title": "Multipage Test", "author": "Test Author"})
    doc.save(str(path))
    doc.close()


def _make_type3_pdf(path: Path) -> None:
    """Generate a minimal valid PDF with a Type3 font as raw bytes.

    A ~600-byte hand-crafted PDF:
    - Object 3 (page) resources reference font object 5 as /F1
    - Object 5 is a Type3 font with a single glyph 'A' defined via CharProcs
    - Object 6 is the CharProcs stream for 'A' (d0 + filled box)
    - Object 4 is the page content stream (draws 'A' using /F1)

    Docling handles Type3 fonts via its own text extraction layer.
    get_text() on a Type3 glyph returns '' or 'A' depending on pymupdf
    version — used by pymupdf_normalized fallback if Docling fails.
    """
    glyph_stream = b"100 0 d0\n0 0 100 100 re f\n"
    content_stream = b"BT /F1 12 Tf 100 700 Td (A) Tj ET\n"

    obj_bodies = [
        # 1: catalog
        b"<</Type/Catalog/Pages 2 0 R>>",
        # 2: pages tree
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        # 3: page — resources point at font object 5
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        # 4: content stream
        b"<</Length " + str(len(content_stream)).encode() + b">>"
        b"\nstream\n" + content_stream + b"endstream",
        # 5: Type3 font dictionary; CharProcs references object 6
        b"<</Type/Font/Subtype/Type3"
        b"/FontBBox[0 0 100 100]"
        b"/FontMatrix[0.01 0 0 0.01 0 0]"
        b"/FirstChar 65/LastChar 65/Widths[100]"
        b"/CharProcs<</A 6 0 R>>"
        b"/Encoding<</Type/Encoding/Differences[65/A]>>>>",
        # 6: glyph procedure stream for 'A'
        b"<</Length " + str(len(glyph_stream)).encode() + b">>"
        b"\nstream\n" + glyph_stream + b"endstream",
    ]

    header = b"%PDF-1.4\n"
    body_parts: list[bytes] = []
    offsets: list[int] = []
    pos = len(header)
    for i, body in enumerate(obj_bodies, start=1):
        obj_bytes = f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        offsets.append(pos)
        pos += len(obj_bytes)
        body_parts.append(obj_bytes)

    body = b"".join(body_parts)
    xref_pos = len(header) + len(body)
    n = len(obj_bodies) + 1  # includes free entry 0
    xref = b"xref\n" + f"0 {n}\n".encode()
    xref += b"0000000000 65535 f\r\n"
    for offset in offsets:
        xref += f"{offset:010d} 00000 n\r\n".encode()
    trailer = (
        b"trailer\n<</Size " + str(n).encode() + b"/Root 1 0 R>>\n"
        b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    )
    path.write_bytes(header + body + xref + trailer)


@pytest.fixture(scope="session")
def pdf_fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate all PDF test fixtures once per test session."""
    d = tmp_path_factory.mktemp("pdf_fixtures")
    _make_simple_pdf(d / "simple.pdf")
    _make_multipage_pdf(d / "multipage.pdf")
    _make_type3_pdf(d / "type3_font.pdf")
    return d


@pytest.fixture(scope="session")
def simple_pdf(pdf_fixtures_dir: Path) -> Path:
    return pdf_fixtures_dir / "simple.pdf"


@pytest.fixture(scope="session")
def multipage_pdf(pdf_fixtures_dir: Path) -> Path:
    return pdf_fixtures_dir / "multipage.pdf"


@pytest.fixture(scope="session")
def type3_pdf(pdf_fixtures_dir: Path) -> Path:
    return pdf_fixtures_dir / "type3_font.pdf"
