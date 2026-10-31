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

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP and SIGTERM delivery is POSIX")


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
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
        client=httpx.Client(transport=httpx.MockTransport(handler)))


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
        client=httpx.Client(transport=httpx.MockTransport(handler)))
    db.create_pipeline("h-euczk-budget", "/a.pdf", "docs__test")

    assert _release_interrupted_run(db, "h-euczk-budget", KeyboardInterrupt()) == "unreached"
    assert fails == [ps._RELEASE_TIMEOUT_S], "one /fail, capped, never retried"


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


# ── nexus-hvwif: a MinerU resume over text-layer pages ───────────────────────


def _buffer(rungs: list[str], *, meta: dict | None = None) -> MagicMock:
    db = MagicMock()
    db.get_pipeline_state.return_value = {
        "pages_extracted": len(rungs), "total_pages": len(rungs) if meta else None,
        "extraction_meta": json.dumps(meta) if meta else None,
    }
    db.read_pages.return_value = [
        {"page_index": i, "page_text": f"text {i}",
         "metadata_json": json.dumps({"page_number": i + 1, "page_method": r})}
        for i, r in enumerate(rungs)
    ]
    return db


def _resume(monkeypatch: pytest.MonkeyPatch, db: MagicMock, pages: int) -> list[dict]:
    class Extractor:
        def extract(self, pdf_path, *, extractor="mineru", on_page=None, allow_degraded=False, on_heartbeat=None):
            for i in range(pages):
                on_page(i, f"text {i}", {"page_number": i + 1, "page_method": "mineru"})
            return ExtractionResult(text="", metadata={"extraction_method": "mineru", "extraction_pages": {}, "page_count": pages})

    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.setattr("nexus.pipeline_stages.PDFExtractor", Extractor)
    with capture_logs() as cap:
        extractor_loop(Path("/tmp/x.pdf"), "h" * 64, db, threading.Event(), extractor="mineru")
    return [e for e in cap if e.get("event") == "pipeline_resume_kept_text_layer_pages"]


def test_a_mineru_resume_over_an_interrupted_pymupdf_run_says_its_pages_are_kept(monkeypatch) -> None:
    warned = _resume(monkeypatch, _buffer(["pymupdf-text", "pymupdf-text"]), pages=4)
    assert len(warned) == 1 and warned[0]["first_pages"] == [1, 2], warned
    assert "--force" in warned[0]["remedy"]


def test_a_mineru_resume_over_a_finished_pymupdf_extraction_says_so(monkeypatch) -> None:
    db = _buffer(["pymupdf-text"] * 3, meta={"extraction_method": "pymupdf", "page_count": 3})
    warned = _resume(monkeypatch, db, pages=3)
    assert len(warned) == 1 and warned[0]["text_layer_pages"] == 3, warned


def test_a_mineru_resume_over_mineru_pages_says_nothing(monkeypatch) -> None:
    assert _resume(monkeypatch, _buffer(["mineru", "mineru-no-formula"]), pages=3) == []
