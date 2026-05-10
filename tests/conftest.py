import logging

import pytest
from pathlib import Path

import chromadb
import structlog
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database


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
def _restore_post_store_batch_hooks_after_test():
    """Snapshot and restore ``mcp_infra._post_store_batch_hooks`` around
    every test, and clear the cached catalog singleton so per-test
    ``NEXUS_CATALOG_PATH`` redirects take effect.

    RDR-108 Phase 3 (nexus-bdag) made the three batch hooks
    (``chash_dual_write_batch_hook``, ``taxonomy_assign_batch_hook``,
    ``manifest_write_batch_hook``) self-register at module load in
    ``nexus.mcp_infra`` so CLI ingest fires them. Several legacy tests
    inline-clear ``_post_store_batch_hooks`` to assert specific hooks
    in isolation; without restoration, the cleared list permanently
    loses those load-bearing registrations for the rest of the
    session and downstream catalog-manifest assertions silently fail.

    The catalog singleton in ``mcp_infra._catalog_instance`` is also
    cleared. ``manifest_write_batch_hook`` resolves the catalog via
    ``get_catalog()``; without per-test reset the first test that
    initialises the singleton pins it to its own tmp_path, so
    subsequent tests' manifest writes target the wrong (deleted) tmp
    catalog and the assertion ``cat.get_manifest(tumbler)`` returns
    ``[]``.
    """
    import nexus.mcp_infra as _mod
    from nexus.catalog import reset_cache as _reset_catalog_cache
    snapshot_batch = list(_mod._post_store_batch_hooks)
    snapshot_single = list(_mod._post_store_hooks)
    # Snapshot the catalog_doc_id-aware classification set too: a test
    # that registers a fresh batch hook adds its ``id(fn)`` here, and
    # without restoration the entry leaks for the rest of the session.
    # Python may recycle the id() for a later object, which would then
    # be (wrongly) classified as catalog_doc_id-aware on first dispatch.
    snapshot_catalog_doc_id_set = set(
        _mod._post_store_batch_hooks_with_catalog_doc_id
    )
    _mod._catalog_instance = None
    _mod._catalog_mtime = 0.0
    _reset_catalog_cache()
    yield
    _mod._post_store_batch_hooks.clear()
    _mod._post_store_batch_hooks.extend(snapshot_batch)
    _mod._post_store_hooks.clear()
    _mod._post_store_hooks.extend(snapshot_single)
    _mod._post_store_batch_hooks_with_catalog_doc_id.clear()
    _mod._post_store_batch_hooks_with_catalog_doc_id.update(
        snapshot_catalog_doc_id_set
    )
    _mod._catalog_instance = None
    _mod._catalog_mtime = 0.0
    _reset_catalog_cache()


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
def _force_cloud_mode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-59vl: default tests to cloud mode so legacy assertions
    that pin ``voyage-context-3`` / ``voyage-code-3`` keep passing in
    CI (where neither ``CHROMA_API_KEY`` nor ``VOYAGE_API_KEY`` is
    set, and ``is_local_mode()`` would otherwise return True).

    Tests that need to exercise local-mode behavior (the
    ``test_local_onnx_naming.py`` suite, any future mode-flip
    integration tests) override this with
    ``monkeypatch.setenv("NX_LOCAL", "1")`` or
    ``patch("nexus.config.is_local_mode", return_value=True)``.

    Without this fixture, the
    ``effective_embedding_model_for_writes`` mode-aware function
    returns the local-EF token (``minilm-l6-v2-384``) for every
    write-path call site, and ~30 legacy tests that assert voyage
    tokens fail in CI.
    """
    monkeypatch.setenv("NX_LOCAL", "0")


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


def set_credentials(monkeypatch) -> None:
    """Set required T3/Voyage credential env vars for tests that call _has_credentials().

    Shared helper used by test_doc_indexer.py and test_pdf_subsystem.py to avoid
    duplicating the same four setenv calls across both files.
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "vk_test")
    monkeypatch.setenv("CHROMA_API_KEY", "ck_test")
    monkeypatch.setenv("CHROMA_TENANT", "tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "db")


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
