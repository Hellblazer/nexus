# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k.7 RUNFENCE GATE — the phase-exit integration falsification.

PHASE-EXIT GATE for the whole 5xn3k RUNFENCE arc (design memo T2
``nx memory get -p nexus -t "5xn3k-design-2026-08-02"``, memo §7 "the
falsification, stated first"). Nothing in the arc is done until this file
is green — WITH its kill control.

Core finding this arc exists to fix (memo §1): no comparison of two
artifacts both written by the SAME run can detect that the run did not
finish. A death mid-index leaves T3 chunks == manifest rows, both
truncated — a CONSISTENT truncation that the pre-existing ``manifest -
T3`` check (AC2, landed in 6ad6b2a9) returns clean on. Only a record of
intent vs completion sees it. That record is the RUNFENCE
(``index_state`` / ``index_content_hash`` on the catalog Document row).

Every scenario in this file drives PRODUCTION entry points (``index_pdf``,
``_index_document``, ``_index_pdf_incremental``, and — transitively, via
``index_pdf``'s streaming route — ``pipeline_stages.pipeline_index_pdf``)
against a REAL Java engine + PostgreSQL instance booted by this module.
Both T3 (vectors) and the catalog (manifest + fence) are routed at the
SAME running service, never a decoupled double — the entire point of the
dcv2k finding this gate also verifies: a fence read from a substrate
different than where the chunks landed rebuilds the exact vacuity this
arc exists to kill.

## Scenarios (bead nexus-5xn3k.7 + its four registered comment additions)

1.  BASE (bead body): ``test_consistent_truncation_recovers_without_force``
    — kill mid-index via an injected exception inside ``uploader_loop``'s
    upsert call (never timing-based); assert the CONSISTENT-truncation
    shape (T3 chunks == manifest rows, both < total, ``index_state ==
    'indexing'``); re-index WITHOUT ``--force``, driven at ``index_pdf``
    (which reads the fence and re-invokes ``pipeline_index_pdf``); assert
    full recovery and ``index_state == 'complete'``.

2.  KILL CONTROL (bead body, non-negotiable):
    ``test_kill_control_fence_pinned_complete_blocks_recovery`` — pins the
    fence read (``nexus.doc_indexer._index_fence_state``) to a constant
    ``('complete', matching_hash)`` and asserts the silent no-op
    REAPPEARS. Proves the base scenario's recovery is driven by the
    fence's real value and nothing else (a chash probe happening to miss,
    for instance).

3.  COMPLETION-STAMP REACHABILITY (2026-08-02 comment, dcv2k provenance):
    ``test_single_flush_completion_stamp_reachable_and_noop_repeat`` —
    dcv2k proved the RUNFENCE .4 completion ride was structurally
    unreachable in production (``write_manifest_many`` is not on the
    ``_ServiceCatalogWriter`` whitelist; every pre-dcv2k unit test drove a
    bare ``MagicMock``, which answers to ANY attribute, so the dead branch
    looked 100% covered — the kgos1 class). This test drives the REAL
    ``_ServiceCatalogWriter`` (no MagicMock catalog writer anywhere) via
    ``_index_document``'s single-flush path and reads ``index_state`` /
    ``index_content_hash`` BACK from the engine.

4.  SINGLE-FLUSH PRODUCER registration (2026-08-02 comment): this gate's
    base scenario (multi-batch, via ``pipeline_index_pdf`` /
    ``uploader_loop``) never touches the single-flush ride
    (``_index_document`` / small-doc inline -> ``fire_batch(manifest_
    complete=...)`` -> ``mcp_infra._manifest_write_loop``'s per-doc
    branch) — the actual path the dcv2k regression lived in and the ONLY
    thing that caught it:
    ``tests/db/test_indexer_seam_b_integration.py::
    test_indexer_seam_b_index_search_round_trip``. That file is a
    REQUIRED CO-GATE for this arc — run it alongside this file, never
    substitute one for the other. Scenario 3 above is this gate's own
    "equivalent single-flush truncation/no-op scenario alongside the
    multi-batch one": it exercises the identical
    ``_ServiceCatalogWriter``-shaped single-flush ride and, like seam_b,
    asserts a same-content repeat index is a genuine no-op.

5.  MULTI-BATCH (2026-08-02 comment): ``test_multi_batch_incremental_
    explicit_complete_round_trip`` — the >128-chunk incremental path
    (``doc_indexer._index_pdf_incremental``, distinct from
    ``pipeline_stages.pipeline_index_pdf``'s streaming path). 'indexing'
    is observed mid-run via a deterministic ``threading.Event`` pause
    (never a sleep/poll race); completion is stamped at the tail with the
    run's total ``chunk_count``.

## Registered items explicitly NOT covered here (deferred, not silently
## dropped)

- The "small-PDF inline" ride (``doc_indexer.index_pdf``'s non-streaming,
  <=128-chunk branch, lines ~2099-2177) is architecturally the SAME
  ``manifest_complete``-ride / per-doc-stamp mechanism as
  ``_index_document``'s (scenario 3 above), sharing the identical dcv2k
  fix in ``mcp_infra._manifest_write_loop``. It is not independently
  driven against the real substrate in THIS file because exercising it
  requires a genuinely pymupdf-openable PDF (this gate's PDF scenarios
  all use ``streaming="always"``/prepared-chunk injection specifically to
  avoid depending on a real extractor/parser). Residual gap; register a
  follow-up if a real small-PDF fixture becomes cheap.
- AC8 (``col.get(where=...)`` non-determinism), the ``--extractor
  docling`` silent quality collapse, and nexus-mok9x boilerplate ingest
  are explicitly OUT of scope per the design memo §8 and the bead's own
  "Explicitly OUT of scope" section — separate beads, not absorbed here.
- Genuine UNCAUGHT-crash recovery (a real process death — SIGKILL, OOM —
  at the PIPELINE-BUFFER layer, as opposed to the fence layer this gate
  makes crash-equivalent via a stub; see ``_kill_mid_index``'s docstring)
  is unverified ANYWHERE in this suite. The scenarios here drive the
  CAUGHT-exception retry path — ``pipeline_index_pdf``'s real
  ``mark_failed`` + ``clear_orphan_wal`` handler runs for real. That path
  used to truncate silently (nexus-gl99l: stale progress counters
  survived a WAL clear, so a resume undercounted and falsely reported
  'complete') — FIXED in ``extractor_loop``'s resume-fast-path, which now
  verifies the actual WAL page count before trusting the claim, and gated
  by ``test_consistent_truncation_recovers_without_force_no_reset`` below
  (the reset-free retry). A WAL-preserved variant of the same retry may
  still hang (nexus-glzrn, P2, unconfirmed root cause) — that remains the
  one unverified residual of the buffer-resume path; gl99l's own fix does
  not touch it.

## Substrate

Modeled on ``tests/db/test_indexer_seam_b_integration.py``'s module
fixtures: a fresh, hermetic PostgreSQL 16 instance + the shaded service
JAR in LOCAL mode (bge-base-en-v15-768 ONNX, no Voyage key), booted once
per module. Every scenario routes T3 (``NX_STORAGE_BACKEND_VECTORS=
service``), the catalog (always service-mode per
``nexus.catalog.factory``), and the pipeline buffer
(``HttpPipelineDB``, constructed lazily from env) at this SAME instance.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

from tests.db._service_fixture import (
    SERVICE_ROLES_SQL,
    pg_bin_dir,
    spawn_service,
    wait_for_service,
)

# ── Prerequisite detection (mirrors test_indexer_seam_b_integration.py) ─────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN = pg_bin_dir()

_INITDB = _PG_BIN / "initdb"
_PG_CTL = _PG_BIN / "pg_ctl"
_PSQL = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = (
    Path(_JAVA_HOME) / "bin" / "java"
    if _JAVA_HOME
    else Path(shutil.which("java") or "java")
)

_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
    and _CREATEDB.exists()
    and (_JAVA.exists() if _JAVA_HOME else shutil.which("java") is not None)
)

pytestmark = [
    pytest.mark.integration,
    # nexus-ao29z: this suite constructs a vector client, whose cloud version
    # probe fail-closes on a jar with no release_version — every jar a plain
    # `mvn package` produces. Build with scripts/build-gate-jar.sh.
    pytest.mark.needs_stamped_jar,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar or PG binaries "
            f"(jar={_JAR.exists()}, pg={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]

# ── Bootstrap SQL — a dedicated role/db so this module never collides with
# a sibling fixture's hermetic PG cluster (each gets its own tmp cluster
# regardless, but the role name is kept file-scoped for clarity). ──────────

_BOOTSTRAP_SQL = """\
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_runfence_gate') THEN
    CREATE ROLE svc_runfence_gate LOGIN PASSWORD 'svc_runfence_gate_pass';
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO svc_runfence_gate;
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop_service(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


# ── Module-scoped Postgres + service fixtures ───────────────────────────────


@pytest.fixture(scope="module")
def pg_instance() -> Generator[dict, None, None]:
    """Hermetic Postgres 16 instance, owned by this module."""
    pgdata = tempfile.mkdtemp(prefix="nexus_runfence_gate_pg_")
    pg_port = _free_port()
    pglog = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]

    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog,
             "-o", f"-p {pg_port} -k {pgdata}", "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "runfencegate"],
            check=True, capture_output=True,
        )
        pg = {"port": pg_port, "dbname": "runfencegate", "user": pg_user}

        def _run_sql(sql: str) -> None:
            proc = subprocess.run(
                [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
                 "-U", pg_user, "-d", "runfencegate",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"SQL failed (rc={proc.returncode}):\n"
                    f"stdout={proc.stdout}\nstderr={proc.stderr}"
                )

        _run_sql(SERVICE_ROLES_SQL)
        _run_sql(_BOOTSTRAP_SQL)

        yield pg
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def local_service(pg_instance: dict) -> Generator[tuple[str, str], None, None]:
    """Java service in LOCAL mode (ONNX bge-base-en-v15-768, no Voyage key).

    Yields ``(base_url, token)``.
    """
    token = "runfence-gate-test-token"
    svc_port = _free_port()
    pg_user = pg_instance["user"]

    # Ephemeral Chroma data dir — see test_indexer_seam_b_integration.py's
    # identical rationale: the service defaults NX_CHROMA_PATH to a
    # persistent location, which would leave a previously-indexed doc on
    # disk and break the staleness/recovery round-trips this gate depends
    # on being deterministic from a clean slate.
    chroma_data = tempfile.mkdtemp(prefix="runfence-gate-chroma-")

    pg_jdbc = (
        f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}/{pg_instance['dbname']}"
    )
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": pg_jdbc,
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "4",
        "NX_DB_ADMIN_URL": pg_jdbc,
        "NX_DB_ADMIN_USER": pg_user,
        "NX_DB_ADMIN_PASS": "",
        "NX_CHROMA_MODE": "local",
        "NX_CHROMA_PATH": chroma_data,
    }
    env.pop("NX_VOYAGE_API_KEY", None)
    env.pop("VOYAGE_API_KEY", None)
    env.pop("NX_STORAGE_BACKEND", None)

    proc, _svc_log = spawn_service([str(_JAVA), "-jar", str(_JAR)], env)
    try:
        wait_for_service("127.0.0.1", svc_port, proc=proc, log_path=_svc_log, timeout=60.0)
        yield f"http://127.0.0.1:{svc_port}", token
    finally:
        _stop_service(proc)
        shutil.rmtree(chroma_data, ignore_errors=True)


@pytest.fixture()
def service_env(
    local_service: tuple[str, str], monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    """Route T3 (vectors), the catalog, and the pipeline buffer at the SAME
    local service instance — the entire point of this gate (dcv2k): the
    fence read must come from the substrate the chunks actually landed in,
    never a decoupled double.

    ``tests/conftest.py``'s autouse ``_pin_t2_substrate`` fixture already
    pointed this test at the SESSION-wide engine substrate before this
    fixture runs (non-autouse fixtures resolve after autouse ones); the
    explicit ``setenv`` calls below land later on the same monkeypatch and
    win, redirecting everything at THIS module's own service instead — the
    identical pattern ``test_indexer_seam_b_integration.py`` already relies
    on successfully from the same ``tests/db/`` directory.
    """
    base_url, token = local_service
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_SERVICE_URL", base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)
    monkeypatch.delenv("NX_LOCAL", raising=False)
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "0")

    from nexus.catalog.factory import reset_shared_service_catalog_client_for_tests
    from nexus.db.http_vector_client import reset_http_vector_client_for_tests
    from nexus.mcp_infra import reset_singletons

    reset_http_vector_client_for_tests()
    reset_singletons()
    reset_shared_service_catalog_client_for_tests()

    return base_url, token


# ── Fake extraction/chunking helpers (no real MinerU/docling/pymupdf) ───────


def _extraction_result(page_count: int = 2):
    from nexus.pdf_extractor import ExtractionResult

    pages = [f"Page {i} runfence gate content." for i in range(page_count)]
    pos, bounds = 0, []
    for i, p in enumerate(pages):
        bounds.append({"page_number": i + 1, "start_char": pos, "page_text_length": len(p) + 1})
        pos += len(p) + 1
    return ExtractionResult(
        text="\n".join(pages),
        metadata={
            "extraction_method": "docling", "page_count": page_count,
            "page_boundaries": bounds, "table_regions": [], "format": "markdown",
        },
    )


def _extract_side_effect(page_count: int, result):
    def extract(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None):
        for i in range(page_count):
            if on_page:
                on_page(i, f"Page {i} runfence gate content.", {"page_number": i + 1})
        return result
    return extract


def _fake_chunks(n: int, *, prefix: str = "chunk"):
    from nexus.pdf_chunker import TextChunk

    return [
        TextChunk(
            text=f"{prefix} {i} unique_runfence_gate_content_{i}",
            chunk_index=i, metadata={"page": (i % 2) + 1},
        )
        for i in range(n)
    ]


def _fresh_pdf(tmp_path: Path, name: str = "doc.pdf", *, marker: str = "") -> Path:
    p = tmp_path / name
    # Deliberately NOT a real PDF: every scenario in this file drives PDF
    # collaborators with `streaming="always"` and/or prepared-chunk
    # injection, so pymupdf.open()/real extraction is never on the path —
    # keeping the fixture file-content-only avoids a MinerU/docling
    # dependency for this gate.
    #
    # *marker* MUST be unique per scenario. The T2 pipeline buffer (module-
    # scoped ``local_service``) keys its row by content_hash, not by
    # doc_id or pdf_path — two scenarios that write byte-identical PDF
    # content share ONE pipeline row. A prior scenario's row left
    # 'completed' makes ``create_pipeline`` return "skip" for the next
    # scenario's supposedly-fresh index, so its injected kill is never
    # reached at all (silent false pass of the "did not raise" kind).
    p.write_bytes(f"%PDF-1.4 runfence gate fake content [{marker}]\n".encode())
    # index_pdf() normalizes to pdf_path.resolve() before its own
    # _register_or_lookup_doc_id call (doc_indexer.py "Normalize to
    # absolute so staleness checks are path-form-independent"). Resolving
    # HERE too is load-bearing, not cosmetic: on macOS pytest's tmp_path
    # lives under /var/folders/... which is a symlink to
    # /private/var/folders/...; a caller that pre-registers the doc_id
    # against the UNRESOLVED path while index_pdf registers against the
    # RESOLVED one gets two different file_path strings for the identical
    # file — a genuine catalog fork (two Document tumblers), and every
    # fence/manifest read against the caller's doc_id then reads an empty
    # sibling document. Matching index_pdf's own normalization here is
    # what keeps this test's pre-resolved doc_id identical to the one
    # index_pdf resolves internally.
    return p.resolve()


def _count_chunks(col, content_hash: str) -> int:
    """Paginated T3 chunk count for *content_hash* (QUOTAS: limit<=300)."""
    total = 0
    offset = 0
    while True:
        batch = col.get(where={"content_hash": content_hash}, include=[], limit=300, offset=offset)
        ids = batch.get("ids", [])
        total += len(ids)
        if len(ids) < 300:
            break
        offset += 300
    return total


_COLLECTION = "docs__runfence-gate__bge-base-en-v15-768__v1"
_TOTAL_CHUNKS = 200
_KILL_AT_CALL = 2  # raise on the SECOND upsert call — the first batch (<=128
                   # chunks, _UPLOAD_BATCH_SIZE) always lands fully first.


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1 (BASE) + Scenario 2 (KILL CONTROL)
# ─────────────────────────────────────────────────────────────────────────────


def _reset_pipeline_buffer(content_hash: str) -> None:
    """Reset the T2 pipeline-buffer bookkeeping for *content_hash* between
    the kill and the recovery run.

    SCOPE NOTE (found while building this gate, 2026-08-02; defect FIXED
    2026-08-02): the caught-exception path in ``pipeline_index_pdf``
    (``db.mark_failed`` + ``db.clear_orphan_wal``) leaves the pipeline
    row's PROGRESS COUNTERS (``pages_extracted``, ``total_pages``,
    ``chunks_created``, ``chunks_embedded``, ``chunks_uploaded``,
    ``extraction_meta``) stale while wiping the underlying WAL rows those
    counters describe. ``extractor_loop``'s resume-fast-path USED TO trust
    the stale ``pages_extracted >= total_pages`` + ``extraction_meta`` to
    skip re-extraction (returns empty text) without checking whether the
    WAL pages backing that claim still existed — nexus-gl99l, now fixed:
    the fast path verifies the actual WAL page count against the claim
    before trusting it, and falls through to a real re-extraction when
    they disagree. See ``test_consistent_truncation_recovers_without_
    force_no_reset`` below, which drives this exact retry WITHOUT this
    helper and asserts full recovery — the fix's own falsification.

    This helper is RETAINED, not because the defect is unaddressed, but
    for TEST ISOLATION: a from-scratch pipeline-buffer reset (the same
    primitive `--force` itself calls) lets the BASE scenario isolate what
    IT is actually testing — does the CATALOG fence (index_state /
    index_content_hash), independent of the pipeline buffer's own resume
    mechanics, correctly force a re-index and correctly converge — by
    separating fence-behavior scenarios from buffer-resume-behavior
    scenarios rather than conflating both in one assertion block. Unlike
    `index_pdf(force=True)`, this does NOT touch T3 (no orphan-chunk
    delete) — the partial T3 landing this gate depends on stays intact.
    """
    from nexus.db.http_pipeline_client import HttpPipelineDB

    HttpPipelineDB().delete_pipeline_data(content_hash)


def _kill_mid_index(tmp_path: Path, corpus: str, chunk_prefix: str):
    """Drive index_pdf -> pipeline_index_pdf against the real substrate and
    interrupt uploader_loop's upsert deterministically after the first
    landed batch. Returns (pdf_path, content_hash, doc_id, t3, result,
    fake_chunks) for the caller to continue against.

    PRECISELY what this models, and what it does not (code-review-expert
    IMPORTANT, 2026-08-02): the exception is a REAL, injected failure in
    uploader_loop's upsert call (never timing-based) — that part is a
    genuine interruption, not a mock. But it is a CAUGHT Python exception,
    not a process death: pipeline_index_pdf's own except-block runs for
    real, including the real ``db.mark_failed`` + ``db.clear_orphan_wal``
    (the pipeline-buffer's own cleanup path). Only the FENCE layer is
    made crash-equivalent — the ADVISORY best-effort ``_fence_fail``
    transition to 'failed' is stubbed to a no-op for this run only,
    because a genuine crash (SIGKILL, OOM) would never reach it either.
    That isolates THIS scenario to exactly the fence-side bug class the
    arc exists to catch: nothing ever told the fence the run died.

    It does NOT model a genuine uncaught crash at the pipeline-buffer
    layer — ``mark_failed``/``clear_orphan_wal`` running for real is the
    exact path nexus-gl99l used to find broken (stale progress counters
    survived a WAL clear and silently truncated a resume; FIXED
    2026-08-02 in ``extractor_loop``'s resume-fast-path). Callers of this
    helper decide whether to reset the buffer between the kill and
    recovery: ``_reset_pipeline_buffer`` (invoked by the BASE scenario)
    isolates the FENCE's behavior from the buffer's own resume mechanics
    for that scenario's purposes — it does not skip the reset-free retry
    path, which ``test_consistent_truncation_recovers_without_force_
    no_reset`` drives straight off this helper's real ``mark_failed`` +
    ``clear_orphan_wal`` cleanup, with no reset at all. See the module
    docstring's "Registered items explicitly NOT covered here" for the
    one residual gap (nexus-glzrn, a WAL-preserved hang variant) this
    still leaves unverified.
    """
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _register_or_lookup_doc_id, _sha256, index_pdf

    pdf_path = _fresh_pdf(tmp_path, marker=chunk_prefix)
    content_hash = _sha256(pdf_path)

    doc_id = _register_or_lookup_doc_id(
        pdf_path, corpus, content_type="paper", physical_collection=_COLLECTION,
    )
    assert doc_id, "catalog registration must succeed against the real service"

    t3 = HttpVectorClient()
    calls = {"n": 0}
    orig_upsert = t3.upsert_chunks_with_embeddings

    def _kill_after_first_batch(*a, **k):
        calls["n"] += 1
        if calls["n"] >= _KILL_AT_CALL:
            raise RuntimeError("runfence_gate_injected_kill")
        return orig_upsert(*a, **k)

    result = _extraction_result(2)
    fake_chunks = _fake_chunks(_TOTAL_CHUNKS, prefix=chunk_prefix)

    with patch("nexus.pipeline_stages.PDFExtractor") as ME, \
         patch("nexus.pipeline_stages.PDFChunker") as MC, \
         patch("nexus.pipeline_stages._POLL_INTERVAL", 0.01), \
         patch.object(t3, "upsert_chunks_with_embeddings", side_effect=_kill_after_first_batch), \
         patch("nexus.doc_indexer._fence_fail", lambda *a, **k: None):
        ME.return_value.extract.side_effect = _extract_side_effect(2, result)
        MC.return_value.chunk.return_value = fake_chunks

        with pytest.raises(RuntimeError, match="runfence_gate_injected_kill"):
            index_pdf(
                pdf_path, corpus, t3=t3, collection_name=_COLLECTION,
                streaming="always",
            )

    return pdf_path, content_hash, doc_id, t3, result, fake_chunks


def _consistent_truncation_recovery_scenario(
    tmp_path: Path, corpus: str, chunk_prefix: str, *, reset_before_recovery: bool,
) -> None:
    """Shared body for the BASE scenario and its nexus-gl99l reset-free
    falsification sibling (below). A hard interruption inside uploader_loop
    leaves T3 chunks == manifest rows, both < total, and index_state ==
    'indexing' — the CONSISTENT truncation AC2 structurally cannot see
    (memo §1). The next run, driven WITHOUT --force at index_pdf (the real
    staleness gate + the real pipeline_index_pdf it calls), reads the
    fence, sees 'indexing', and re-indexes to full completion.

    *reset_before_recovery* — when True (the BASE scenario), the pipeline
    buffer is reset from scratch before the recovery run, isolating the
    fence's own behavior from the separate pipeline-buffer resume defect
    (see ``_reset_pipeline_buffer``'s docstring). When False (the
    nexus-gl99l falsification), recovery is driven straight off the
    real ``mark_failed`` + ``clear_orphan_wal`` cleanup left by the kill —
    the ordinary retry path a real crash-resume would take.
    """
    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _index_fence_state, index_pdf

    pdf_path, content_hash, doc_id, t3, result, fake_chunks = _kill_mid_index(
        tmp_path, corpus, chunk_prefix,
    )

    # ── Consistent-truncation shape ──────────────────────────────────────
    col = t3.get_or_create_collection(_COLLECTION)
    t3_count = _count_chunks(col, content_hash)
    manifest = make_catalog_reader().get_manifest(doc_id)

    assert 0 < t3_count < _TOTAL_CHUNKS, (
        f"expected a partial landing strictly between 0 and {_TOTAL_CHUNKS}, "
        f"got {t3_count}"
    )
    assert len(manifest) == t3_count, (
        "T3 chunks and manifest rows must AGREE — this is the CONSISTENT "
        f"truncation shape that AC2 (manifest - T3) cannot see "
        f"(T3={t3_count}, manifest={len(manifest)})"
    )
    state, _fence_hash = _index_fence_state(doc_id)
    assert state == "indexing", (
        "expected the fence to remain 'indexing' after the interrupted run "
        f"(nothing ever transitioned it away); got {state!r}"
    )

    # ── Recovery WITHOUT --force, driven at index_pdf (which reads the
    #    fence via _index_run_fresh and re-invokes pipeline_index_pdf) ────
    if reset_before_recovery:
        # _reset_pipeline_buffer: see its docstring — sidesteps a separate,
        # real pipeline-buffer resume defect (nexus-gl99l) so this
        # assertion block isolates the FENCE's behavior, which is what the
        # BASE scenario tests.
        _reset_pipeline_buffer(content_hash)
    with patch("nexus.pipeline_stages.PDFExtractor") as ME2, \
         patch("nexus.pipeline_stages.PDFChunker") as MC2, \
         patch("nexus.pipeline_stages._POLL_INTERVAL", 0.01):
        ME2.return_value.extract.side_effect = _extract_side_effect(2, result)
        MC2.return_value.chunk.return_value = fake_chunks

        recovered = index_pdf(
            pdf_path, corpus, t3=HttpVectorClient(), collection_name=_COLLECTION,
            streaming="always",
        )

    assert recovered > 0, "recovery run must report indexed chunks, not a no-op"
    t3_count_final = _count_chunks(col, content_hash)
    manifest_final = make_catalog_reader().get_manifest(doc_id)
    assert t3_count_final == _TOTAL_CHUNKS, t3_count_final
    assert len(manifest_final) == _TOTAL_CHUNKS, len(manifest_final)

    entry = make_catalog_reader().resolve(doc_id)
    assert entry is not None
    assert entry.index_state == "complete"
    assert entry.index_content_hash == content_hash


def test_consistent_truncation_recovers_without_force(
    tmp_path: Path, service_env: tuple[str, str],
) -> None:
    """BASE (nexus-5xn3k.7 bead body): see
    ``_consistent_truncation_recovery_scenario``'s docstring."""
    _consistent_truncation_recovery_scenario(
        tmp_path, "runfence-gate-base", "base", reset_before_recovery=True,
    )


def test_consistent_truncation_recovers_without_force_no_reset(
    tmp_path: Path, service_env: tuple[str, str],
) -> None:
    """nexus-gl99l FALSIFICATION (2026-08-02 scope-reframe comment,
    critic-verified arc-blocking promotion): the reset-free retry — i.e.
    WITHOUT ``_reset_pipeline_buffer``'s from-scratch pipeline-buffer wipe
    — converges to full recovery now that nexus-gl99l is fixed.

    Was RED before the fix: ``extractor_loop``'s resume-fast-path trusted
    the pipeline row's stale ``pages_extracted``/``total_pages``/
    ``extraction_meta`` (never reset by ``clear_orphan_wal``, which wipes
    only the WAL rows those counters describe) and skipped real
    re-extraction, silently truncating the retry at the pre-failure chunk
    count — the SAME headline as nexus-5xn3k itself ("every re-index
    silently no-ops while reporting success"), recreated sequentially on
    the plain retry-after-caught-exception path. Recorded red: a bare
    retry landed 100 of 200 chunks (``assert t3_count_final ==
    _TOTAL_CHUNKS`` failed with ``100 == 200``). Fixed by making the
    fast-path verify actual WAL page presence before trusting the claim.
    This test promotes that gap (module docstring's "Registered items
    explicitly NOT covered here", third bullet) from documented-and-
    skipped to gated — it is the fix's own falsification.
    """
    _consistent_truncation_recovery_scenario(
        tmp_path, "runfence-gate-base-noreset", "base-noreset",
        reset_before_recovery=False,
    )


def test_kill_control_fence_pinned_complete_blocks_recovery(
    tmp_path: Path, service_env: tuple[str, str],
) -> None:
    """KILL CONTROL (nexus-5xn3k.7 bead body, non-negotiable): pin the
    fence read (``_index_fence_state``) to a constant ``('complete',
    matching_hash)`` and assert the silent no-op REAPPEARS.

    Without this control, the base scenario's green could be incidental
    (e.g. a chash probe happening to miss); with it, the base scenario's
    pass is provably driven by the fence's real value and nothing else. A
    green base test with no kill control is a vacuous gate.
    """
    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_pdf

    corpus = "runfence-gate-killctl"
    pdf_path, content_hash, doc_id, t3, result, fake_chunks = _kill_mid_index(
        tmp_path, corpus, "killctl",
    )

    col = t3.get_or_create_collection(_COLLECTION)
    truncated_count = _count_chunks(col, content_hash)
    assert 0 < truncated_count < _TOTAL_CHUNKS

    # Blind the fence read to a constant "complete" + matching hash.
    with patch(
        "nexus.doc_indexer._index_fence_state",
        return_value=("complete", content_hash),
    ), patch("nexus.pipeline_stages.PDFExtractor") as ME2, \
         patch("nexus.pipeline_stages.PDFChunker") as MC2, \
         patch("nexus.pipeline_stages._POLL_INTERVAL", 0.01):
        ME2.return_value.extract.side_effect = _extract_side_effect(2, result)
        MC2.return_value.chunk.return_value = fake_chunks

        result_count = index_pdf(
            pdf_path, corpus, t3=HttpVectorClient(), collection_name=_COLLECTION,
            streaming="always",
        )

    assert result_count == 0, (
        "the no-op MUST reappear when the fence is blinded to a constant "
        "'complete' — proving the base scenario's recovery is driven by "
        "the fence's real value, not an incidental side effect (e.g. a "
        "chash probe miss)"
    )
    entry_after = make_catalog_reader().resolve(doc_id)
    assert entry_after is not None
    assert entry_after.index_state == "indexing", (
        "the REAL fence state must still read 'indexing' — only OUR patched "
        "read was blinded, never the engine's actual row"
    )
    assert _count_chunks(col, content_hash) == truncated_count, (
        "chunk count must stay at the truncated level — a real re-index "
        "must NOT have happened while the fence read was blinded"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3: single-flush completion-stamp reachability (dcv2k) + no-op
# repeat (also satisfies item #4's "equivalent single-flush scenario").
# Co-gate: tests/db/test_indexer_seam_b_integration.py::
#          test_indexer_seam_b_index_search_round_trip
# ─────────────────────────────────────────────────────────────────────────────

_PROSE_COLLECTION = "docs__runfence-gate-prose__bge-base-en-v15-768__v1"


def test_single_flush_completion_stamp_reachable_and_noop_repeat(
    tmp_path: Path, service_env: tuple[str, str],
) -> None:
    """COMPLETION-STAMP REACHABILITY (dcv2k, 2026-08-02 comment on
    nexus-5xn3k.7): after a REAL single-flush index (the prose
    ``_index_document`` path) against the real substrate, read BACK from
    the engine: ``index_state == 'complete'`` AND ``index_content_hash ==
    <hash>``. No MagicMock catalog writer anywhere in this test —
    ``make_catalog_writer()`` returns the REAL production
    ``_ServiceCatalogWriter`` over ``HttpCatalogClient``, driven at the
    real service, so the dcv2k class (a bare MagicMock answering to any
    attribute, making a production-unreachable branch look 100% covered)
    cannot recur here.

    Also serves item #4's "equivalent single-flush truncation/no-op
    scenario alongside the multi-batch one": a same-content repeat index
    must be a genuine no-op, the same assertion shape seam_b's own
    ``test_indexer_seam_b_index_search_round_trip`` makes for its Seam B
    write path.
    """
    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import (
        _index_document,
        _markdown_chunks,
        _register_or_lookup_doc_id,
        _sha256,
    )

    f = tmp_path / "runfence_prose.md"
    f.write_text(
        "# RUNFENCE gate\n\nSingle-flush completion-stamp reachability "
        "content: unique_marker_dcv2k_reachability.\n"
    )
    content_hash = _sha256(f)
    corpus = "runfence-gate-prose"

    t3 = HttpVectorClient()
    n = _index_document(
        f, corpus, _markdown_chunks, t3, collection_name=_PROSE_COLLECTION,
    )
    assert n > 0, "the first index must land real chunks"

    doc_id = _register_or_lookup_doc_id(
        f, corpus, content_type="prose", physical_collection=_PROSE_COLLECTION,
    )
    assert doc_id

    entry = make_catalog_reader().resolve(doc_id)
    assert entry is not None
    assert entry.index_state == "complete", (
        "completion stamp unreachable — the production writer never got "
        f"past the write_manifest_many capability check (the dcv2k class); "
        f"got index_state={entry.index_state!r}"
    )
    assert entry.index_content_hash == content_hash

    manifest = make_catalog_reader().get_manifest(doc_id)
    assert len(manifest) == n

    # Single-flush no-op registration (item #4).
    n2 = _index_document(
        f, corpus, _markdown_chunks, HttpVectorClient(),
        collection_name=_PROSE_COLLECTION,
    )
    assert n2 == 0, "repeat single-flush index of identical content must no-op"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 5: multi-batch incremental (>128 chunks) explicit-complete
# round-trip, with a deterministic mid-run 'indexing' observation.
# ─────────────────────────────────────────────────────────────────────────────

_INCR_COLLECTION = "docs__runfence-gate-incr__bge-base-en-v15-768__v1"
_INCR_TOTAL = 150  # > _INCREMENTAL_BATCH_SIZE (128) -> forces >=2 batches


def _incr_prepared(n: int, content_hash: str, nonce: str) -> list[tuple[str, str, dict]]:
    """*nonce* MUST be unique per test run (code-review-expert MINOR,
    2026-08-02): chash is content-addressed off the chunk TEXT, so a
    fixed text set collides with a prior same-process run of this test —
    e.g. a pytest rerun/retry within one session. A colliding chash
    routes batch 1's upsert through the existing-chunk metadata-refresh
    branch instead of a fresh insert, and the test's Event-based mid-run
    pause then waits on a signal whose surrounding call shape no longer
    matches what was verified — an unhelpful timeout, not a clear
    assertion failure. Matches the marker discipline already applied to
    every PDF fixture in this file (``_fresh_pdf``'s ``marker`` param).
    """
    out: list[tuple[str, str, dict]] = []
    for i in range(n):
        text = f"incremental chunk {i} unique_runfence_gate_multi_batch_{nonce}_{i}"
        chash = hashlib.sha256(text.encode()).hexdigest()
        out.append((
            chash,
            text,
            {
                "content_hash": content_hash,
                "embedding_model": "bge-base-en-v15-768",
                "chunk_text_hash": chash,
                "content_type": "pdf",
            },
        ))
    return out


def test_multi_batch_incremental_explicit_complete_round_trip(
    tmp_path: Path, service_env: tuple[str, str],
) -> None:
    """MULTI-BATCH (2026-08-02 comment on nexus-5xn3k.7): the incremental
    path (``doc_indexer._index_pdf_incremental``, >128 chunks — DISTINCT
    from ``pipeline_stages.pipeline_index_pdf``'s streaming path)
    explicit-complete round-trip against the real substrate.

    'indexing' is observed mid-run via a deterministic ``threading.Event``
    pause — the background thread blocks right after batch 1's upsert
    returns, before batch 2 or the tail ``/complete`` call, so the read is
    guaranteed to land inside the run's own window rather than racing it.
    Completion is stamped at the tail with the run's total ``chunk_count``.
    """
    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import (
        _index_pdf_incremental,
        _register_or_lookup_doc_id,
        _sha256,
    )

    pdf_path = tmp_path / "incremental.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 runfence gate incremental fake content\n")
    content_hash = _sha256(pdf_path)
    corpus = "runfence-gate-incr"

    doc_id = _register_or_lookup_doc_id(
        pdf_path, corpus, content_type="pdf", physical_collection=_INCR_COLLECTION,
    )
    assert doc_id

    t3 = HttpVectorClient()
    prepared = _incr_prepared(_INCR_TOTAL, content_hash, uuid.uuid4().hex[:12])

    batch1_landed = threading.Event()
    proceed = threading.Event()
    calls = {"n": 0}
    orig_upsert = t3.upsert_chunks_with_embeddings

    def _pausing_upsert(*a, **k):
        calls["n"] += 1
        res = orig_upsert(*a, **k)
        if calls["n"] == 1:
            batch1_landed.set()
            if not proceed.wait(timeout=15):
                raise RuntimeError("test stalled waiting on the proceed event")
        return res

    result_holder: dict = {}
    error_holder: dict = {}

    def _run() -> None:
        try:
            with patch.object(t3, "upsert_chunks_with_embeddings", side_effect=_pausing_upsert):
                result_holder["total"] = _index_pdf_incremental(
                    pdf_path, corpus, prepared, content_hash, _INCR_COLLECTION, t3,
                    doc_id=doc_id,
                )
        except BaseException as exc:  # noqa: BLE001 — surfaced to the main thread below
            error_holder["exc"] = exc

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    try:
        assert batch1_landed.wait(timeout=15), "batch 1 never landed"
        entry_mid = make_catalog_reader().resolve(doc_id)
        assert entry_mid is not None
        assert entry_mid.index_state == "indexing", (
            "expected 'indexing' visible mid-run (begin precedes every "
            f"batch); got {entry_mid.index_state!r}"
        )
    finally:
        proceed.set()
    th.join(timeout=30)
    assert not th.is_alive(), "incremental indexer thread did not finish"
    if "exc" in error_holder:
        raise error_holder["exc"]

    assert result_holder.get("total") == _INCR_TOTAL

    col = t3.get_or_create_collection(_INCR_COLLECTION)
    assert _count_chunks(col, content_hash) == _INCR_TOTAL
    manifest = make_catalog_reader().get_manifest(doc_id)
    assert len(manifest) == _INCR_TOTAL

    entry = make_catalog_reader().resolve(doc_id)
    assert entry is not None
    assert entry.index_state == "complete"
    assert entry.index_content_hash == content_hash
