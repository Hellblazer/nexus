# SPDX-License-Identifier: AGPL-3.0-or-later
"""The release residuals of nexus-4vx0a: SIGHUP (nexus-eq03c), the release budget, the create
window and the completion guard (nexus-euczk), and the resume over another extractor's pages
(nexus-hvwif).

The SIGTERM and SIGHUP journey through a real child process is
``tests/test_4vx0a_pipeline_sigterm_row_handback.py``; the cases here run in a fresh interpreter
where a signal would end the test process, and in process against the in-memory pipeline twin
otherwise.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

import nexus.pipeline_stages as ps
from nexus.db.http_pipeline_client import HttpPipelineDB, PipelineConflictRunning
from nexus.db.inmemory_pipeline import InMemoryPipelineEngine
from nexus.pdf_extractor import ExtractionResult
from nexus.pipeline_stages import _release_interrupted_run, extractor_loop, pipeline_index_pdf
from tests.pipeline_fake_engine import make_fake_engine_db

#: The test clients' own timeout, the production client's 30 s, so a request that lost its cap
#: reads differently from one that kept it (httpx's default, 5 s, equals the cap).
_CLIENT_TIMEOUT_S = 30.0

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP and SIGTERM delivery is POSIX")


#: A runner that starts the suite in the background (a non-interactive shell's ``&``) hands every
#: process SIGINT ignored, and an ignored signal is not held: put Python's own handler back first.
_SIGINT_DEFAULT = "import signal as _s; _s.signal(_s.SIGINT, _s.default_int_handler)\n"


@pytest.fixture(autouse=True)
def _sigint_raises_keyboard_interrupt() -> Iterator[None]:
    """The in-process tests send themselves SIGINT; see :data:`_SIGINT_DEFAULT`."""
    before = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, before)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _SIGINT_DEFAULT + script], capture_output=True, text=True, timeout=60,
        cwd=str(Path(__file__).parent.parent), env={**os.environ, "NX_NO_TELEMETRY": "1"},
    )


def _markers(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("M:")]


# ── nexus-eq03c: SIGHUP ──────────────────────────────────────────────────────


@posix_only
def test_an_ignored_sighup_stays_ignored_inside_the_block() -> None:
    """``nohup`` ignores SIGHUP so a run outlives its terminal; the block must not undo that."""
    result = _run("""
import os, signal, sys
from nexus.util.termination import sigterm_raises
signal.signal(signal.SIGHUP, signal.SIG_IGN)
with sigterm_raises():
    os.kill(os.getpid(), signal.SIGHUP)
    print("M:survived the hangup", flush=True)
print("M:ignored after the block" if signal.getsignal(signal.SIGHUP) is signal.SIG_IGN else "M:disposition lost", flush=True)
""")
    assert result.returncode == 0, result.stderr
    assert _markers(result.stdout) == ["M:survived the hangup", "M:ignored after the block"]


# ── nexus-euczk (3): the create window ───────────────────────────────────────


@posix_only
def test_a_deferred_sigterm_raises_when_the_held_call_returns() -> None:
    """Under ``sigterm_raises`` a SIGTERM inside ``interrupts_deferred`` is acted on at the block's end."""
    result = _run("""
import os, signal
from nexus.util.termination import TerminationRequested, interrupts_deferred, sigterm_raises
with sigterm_raises():
    try:
        with interrupts_deferred():
            os.kill(os.getpid(), signal.SIGTERM)
            print("M:the held call finished", flush=True)
        print("M:not raised", flush=True)
    except TerminationRequested:
        print("M:raised at the end of the hold", flush=True)
""")
    assert result.returncode == -signal.SIGTERM, (result.returncode, result.stderr)
    assert _markers(result.stdout) == ["M:the held call finished", "M:raised at the end of the hold"]


@posix_only
def test_a_deferred_ctrl_c_raises_keyboard_interrupt_when_the_held_call_returns() -> None:
    result = _run("""
import os, signal
from nexus.util.termination import interrupts_deferred
try:
    with interrupts_deferred():
        os.kill(os.getpid(), signal.SIGINT)
        print("M:the held call finished", flush=True)
    print("M:not raised", flush=True)
except KeyboardInterrupt:
    print("M:KeyboardInterrupt at the end of the hold", flush=True)
print("M:handler restored" if signal.getsignal(signal.SIGINT) is signal.default_int_handler else "M:handler lost", flush=True)
""")
    assert result.returncode == 0, result.stderr
    assert _markers(result.stdout) == [
        "M:the held call finished", "M:KeyboardInterrupt at the end of the hold", "M:handler restored",
    ]


@posix_only
def test_a_second_ctrl_c_inside_the_hold_acts_at_once() -> None:
    """An engine that never answers must not leave Ctrl-C dead for the create's whole budget."""
    result = _run("""
import os, signal
from nexus.util.termination import interrupts_deferred
try:
    with interrupts_deferred():
        os.kill(os.getpid(), signal.SIGINT)
        print("M:first held", flush=True)
        os.kill(os.getpid(), signal.SIGINT)
        print("M:second ignored", flush=True)
except KeyboardInterrupt:
    print("M:raised inside the call", flush=True)
print("M:handler restored" if signal.getsignal(signal.SIGINT) is signal.default_int_handler else "M:handler lost", flush=True)
""")
    assert result.returncode == 0, result.stderr
    assert _markers(result.stdout) == ["M:first held", "M:raised inside the call", "M:handler restored"]


@posix_only
def test_a_signal_after_the_hold_limit_acts_at_once() -> None:
    result = _run("""
import os, signal, time
from nexus.util.termination import interrupts_deferred
try:
    with interrupts_deferred(limit_s=0.05):
        time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGINT)
        print("M:not raised in the call", flush=True)
except KeyboardInterrupt:
    print("M:raised inside the call", flush=True)
""")
    assert result.returncode == 0, result.stderr
    assert _markers(result.stdout) == ["M:raised inside the call"]


def _embed(texts, model):  # noqa: ANN001, ANN202
    return [[0.1] * 4 for _ in texts], model


def _interrupting_db(engine: InMemoryPipelineEngine, sent: list[str]) -> HttpPipelineDB:
    """A client of *engine* whose transport sends SIGINT once the engine has answered the create
    and before the client has the answer: the moment an interrupt in flight lands in."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        answer = engine.handler(request)
        if request.url.path == "/v1/pipeline/create":
            os.kill(os.getpid(), signal.SIGINT)
        return answer

    return HttpPipelineDB(
        base_url="http://in-memory-pipeline", _token="in-memory",
        client=httpx.Client(transport=httpx.MockTransport(handler), timeout=_CLIENT_TIMEOUT_S))


def _index(db: HttpPipelineDB, content_hash: str) -> None:
    pipeline_index_pdf(
        Path("/euczk.pdf"), content_hash, "docs__test", MagicMock(), db=db, embed_fn=_embed, doc_id="1.1.9",
    )


def test_an_interrupt_during_create_releases_the_row_it_made() -> None:
    """A Ctrl-C that lands while ``create_pipeline``'s HTTP call is in flight. Before the fix it
    raised inside the call, the client never learned the row's ids, and the row stayed
    ``running`` for the stale window; now the create finishes and the row is released."""
    engine = InMemoryPipelineEngine()
    db = _interrupting_db(engine, [])
    with pytest.raises(KeyboardInterrupt):
        _index(db, "h-euczk-create")
    (row,) = engine.rows_for("h-euczk-create")
    assert row["status"] == "failed", row
    assert row["error"].startswith("interrupted;"), row


def test_an_interrupt_during_a_refused_create_sends_no_release() -> None:
    """The create was refused (another process runs the document), so this client names no row,
    and a release would name only the hash. Nothing is sent, and the live run is untouched."""
    engine = InMemoryPipelineEngine()
    HttpPipelineDB(
        base_url="http://in-memory-pipeline", _token="in-memory",
        client=httpx.Client(transport=httpx.MockTransport(engine.handler)),
    ).create_pipeline("h-euczk-live", "/euczk.pdf", "docs__test")
    sent: list[str] = []
    with pytest.raises(KeyboardInterrupt) as raised:
        _index(_interrupting_db(engine, sent), "h-euczk-live")
    assert isinstance(raised.value.__context__, PipelineConflictRunning)
    assert "/v1/pipeline/fail" not in sent, sent
    (row,) = engine.rows_for("h-euczk-live")
    assert row["status"] == "running" and not row.get("error"), row


# ── nexus-euczk (1): the release budget ──────────────────────────────────────


def test_the_release_sends_one_capped_request_and_reports_an_unreachable_engine() -> None:
    engine = InMemoryPipelineEngine()
    fails: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/pipeline/fail":
            fails.append(request.extensions.get("timeout", {}).get("read"))
            raise httpx.ReadTimeout("black-holed", request=request)
        return engine.handler(request)

    db = HttpPipelineDB(
        base_url="http://in-memory-pipeline", _token="in-memory",
        client=httpx.Client(transport=httpx.MockTransport(handler), timeout=_CLIENT_TIMEOUT_S))
    db.create_pipeline("h-euczk-budget", "/a.pdf", "docs__test")

    assert _release_interrupted_run(db, "h-euczk-budget", KeyboardInterrupt()) == "unreached"
    assert fails == [ps._RELEASE_TIMEOUT_S], "one /fail, capped, never retried"


def _capturing_db(engine: InMemoryPipelineEngine, timeouts: dict[str, list], black_hole: set[str]) -> HttpPipelineDB:
    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.setdefault(request.url.path, []).append(request.extensions.get("timeout", {}).get("read"))
        if request.url.path in black_hole:
            raise httpx.ReadTimeout("black-holed", request=request)
        return engine.handler(request)

    return HttpPipelineDB(
        base_url="http://in-memory-pipeline", _token="in-memory",
        client=httpx.Client(transport=httpx.MockTransport(handler), timeout=_CLIENT_TIMEOUT_S))


def test_the_release_flushes_buffered_pages_capped_and_still_fails_the_row() -> None:
    engine, timeouts = InMemoryPipelineEngine(), {}
    db = _capturing_db(engine, timeouts, {"/v1/pipeline/pages"})
    db.create_pipeline("h-euczk-flush", "/a.pdf", "docs__test")
    db.write_page("h-euczk-flush", 0, "buffered, not yet sent")
    assert _release_interrupted_run(db, "h-euczk-flush", KeyboardInterrupt()) == "released"
    assert timeouts["/v1/pipeline/pages"] == [ps._RELEASE_TIMEOUT_S], "one capped page flush"
    assert timeouts["/v1/pipeline/fail"] == [ps._RELEASE_TIMEOUT_S]
    (row,) = engine.rows_for("h-euczk-flush")
    assert row["status"] == "failed"


def test_the_stage_failure_cleanup_of_an_interrupted_run_is_capped_and_skips_the_stamp_when_unreached() -> None:
    """Every stage has finished when the interrupt lands after a stage failed: the WAL-wiping cleanup
    runs, capped, and an engine that does not answer skips the catalog stamp as the release does."""
    engine, timeouts = InMemoryPipelineEngine(), {}
    db = _capturing_db(engine, timeouts, {"/v1/pipeline/fail"})

    class _FailingExtractor:
        def extract(self, pdf_path, *, extractor="mineru", on_page=None, allow_degraded=False, on_heartbeat=None):
            raise RuntimeError("boom: deterministic stage failure")

    real_wait = ps.wait

    def _wait(fs, timeout=None, return_when="ALL_COMPLETED"):  # noqa: ANN001, ANN202
        if return_when != "ALL_COMPLETED":
            return real_wait(fs, timeout=timeout, return_when=return_when)
        # The drain after the failure: cancel is set, so every stage finishes; then the interrupt.
        real_wait(fs, timeout=10, return_when="ALL_COMPLETED")
        raise KeyboardInterrupt

    with patch("nexus.pipeline_stages.PDFExtractor", return_value=_FailingExtractor()), \
            patch("nexus.pipeline_stages.wait", side_effect=_wait), \
            patch("nexus.doc_indexer._fence_fail") as fence_fail, \
            capture_logs() as cap, \
            pytest.raises(KeyboardInterrupt):
        _index(db, "h-euczk-stage")
    events = [e["event"] for e in cap]
    assert "pipeline_terminal_mark_failed" in events, "the WAL-wiping branch ran, not the kept-buffer release"
    assert timeouts["/v1/pipeline/fail"] == [ps._RELEASE_TIMEOUT_S], timeouts
    fence_fail.assert_not_called()


def test_an_unreached_release_skips_the_catalog_stamp_but_drops_the_deferred_sweep() -> None:
    with patch("nexus.doc_indexer._fence_fail") as fence_fail, \
            patch("nexus.mcp_infra.discard_deferred_superseded_vectors") as discard:
        ps._stamp_interrupted("1.1.7", False, "unreached", "interrupted")
        fence_fail.assert_not_called()
        discard.assert_called_once_with("1.1.7")
        ps._stamp_interrupted("1.1.7", False, "released", "interrupted")
        fence_fail.assert_called_once_with("1.1.7", "interrupted")


# ── nexus-euczk (2): the completion guard ────────────────────────────────────


def test_a_straggler_completion_does_not_flip_a_released_row() -> None:
    """The interrupted run released its row as ``failed``; its uploader thread completes late."""
    db, engine = make_fake_engine_db()
    db.create_pipeline("h-euczk-done", "/a.pdf", "docs__test")
    ids = (db.pipeline_id_for("h-euczk-done"), db.run_epoch_for("h-euczk-done"))
    db.mark_failed("h-euczk-done", error="interrupted; the buffer is kept")
    straggler = {"content_hash": "h-euczk-done", "pipeline_id": ids[0], "run_epoch": ids[1]}
    assert db._client.post("http://in-memory-pipeline/v1/pipeline/complete", json=straggler).status_code == 200
    (row,) = engine.rows_for("h-euczk-done")
    assert row["status"] == "failed", "a released row stays released"


def test_a_live_run_still_completes() -> None:
    db, engine = make_fake_engine_db()
    db.create_pipeline("h-euczk-live-done", "/a.pdf", "docs__test")
    db.mark_completed("h-euczk-live-done")
    (row,) = engine.rows_for("h-euczk-live-done")
    assert row["status"] == "completed"


# ── nexus-hvwif: a resume over another extractor's pages ─────────────────────


def _buffer(pages: list[tuple[str, str | None]], *, meta: dict | None = None) -> MagicMock:
    """Buffered pages as ``(rung, extractor stamp or None for a buffer an older build wrote)``."""
    db = MagicMock()
    db.get_pipeline_state.return_value = {
        "pages_extracted": len(pages), "total_pages": len(pages) if meta else None,
        "extraction_meta": json.dumps(meta) if meta else None,
    }
    db.read_pages.return_value = [
        {"page_index": i, "page_text": f"text {i}",
         "metadata_json": json.dumps(
             {"page_number": i + 1, "page_method": rung} | ({"extractor": by} if by else {}))}
        for i, (rung, by) in enumerate(pages)
    ]
    return db


def _resume(monkeypatch: pytest.MonkeyPatch, db: MagicMock, pages: int, extractor: str = "mineru") -> list[dict]:
    class Extractor:
        def extract(self, pdf_path, *, extractor="mineru", on_page=None, allow_degraded=False, on_heartbeat=None):
            for i in range(pages):
                on_page(i, f"text {i}", {"page_number": i + 1, "page_method": "mineru"})
            return ExtractionResult(text="", metadata={"extraction_method": "mineru", "extraction_pages": {}, "page_count": pages})

    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.setattr("nexus.pipeline_stages.PDFExtractor", Extractor)
    with capture_logs() as cap:
        extractor_loop(Path("/tmp/x.pdf"), "h" * 64, db, threading.Event(), extractor=extractor)
    return [e for e in cap if e.get("event") == "pipeline_resume_kept_other_extractor_pages"]


def test_a_mineru_resume_over_an_interrupted_pymupdf_run_says_its_pages_are_kept(monkeypatch) -> None:
    db = _buffer([("pymupdf-text", "pymupdf"), ("pymupdf-text", "pymupdf")])
    warned = _resume(monkeypatch, db, pages=4)
    assert len(warned) == 1 and warned[0]["first_pages"] == [1, 2] and warned[0]["written_by"] == ["pymupdf"], warned
    assert "--force" in warned[0]["remedy"]


def test_a_mineru_resume_over_a_finished_pymupdf_extraction_says_so(monkeypatch) -> None:
    db = _buffer([("pymupdf-text", "pymupdf")] * 3, meta={"extraction_method": "pymupdf", "page_count": 3})
    warned = _resume(monkeypatch, db, pages=3)
    assert len(warned) == 1 and warned[0]["kept_pages"] == 3, warned


def test_a_mineru_resume_over_its_own_degraded_pages_says_nothing(monkeypatch) -> None:
    """The ladder settles pages on the same text-layer rungs pymupdf makes; the stamp tells them apart."""
    db = _buffer([("mineru", "mineru"), ("pymupdf-text", "mineru"), ("unextracted", "mineru")])
    assert _resume(monkeypatch, db, pages=4) == []


def test_a_buffer_an_older_build_wrote_says_nothing(monkeypatch) -> None:
    assert _resume(monkeypatch, _buffer([("pymupdf-text", None), ("mineru", None)]), pages=3) == []


def test_each_written_page_records_its_extractor(monkeypatch) -> None:
    db = _buffer([])
    db.get_pipeline_state.return_value = None
    _resume(monkeypatch, db, pages=2, extractor="mineru")
    stamps = [c.kwargs["metadata"]["extractor"] for c in db.write_page.call_args_list]
    assert stamps == ["mineru", "mineru"]
