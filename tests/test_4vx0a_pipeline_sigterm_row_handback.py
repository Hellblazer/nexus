# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4vx0a: a SIGTERM or Ctrl-C to a live PDF pipeline run gives its row back.

Before the fix SIGTERM had Python's default disposition: the process died with
no cleanup, its ``nexus.pdf_pipeline`` row stayed ``running``, and every retry
of the document was refused with ``PipelineConflictRunning`` until the
heartbeat was five minutes old (seen in a bulk ``nx dt index`` of 161 PDFs,
2026-10-25).

The journey runs the real ``pipeline_index_pdf`` and a real ``HttpPipelineDB``
in a child process, kills it with SIGTERM while its extractor is inside a call
no signal can interrupt, and retries at once from the parent. The two processes
share one store: the parent serves the in-memory pipeline stand-in (the engine
twin whose contract ``tests/db/test_pipeline_fake_engine_parity.py`` pins to the
Java ``PipelineHandlerTest``) over a loopback port, so every request crosses a
real socket. The retry must be answered ``resuming``, with the pages the killed
run extracted still in the buffer. No tenant: Sam's call on nexus-kuhqo, rather
than raising the provisioned-test ceiling. The child is a fresh interpreter (the
``test_hg2dw_run_dies_before_flush`` precedent: forking this test process is
unsafe), and its extractor is the only fake besides the catalog fence write it
records.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import http.server
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

import nexus.pipeline_stages as ps
from nexus import pdf_extractor
from nexus.db.http_pipeline_client import HttpPipelineDB, PipelineConflictRunning
from nexus.db.inmemory_pipeline import InMemoryPipelineEngine
from nexus.hook_registry import HookRegistry
from nexus.pipeline_stages import _release_interrupted_run, pipeline_index_pdf
from nexus.util.termination import TerminationRequested
from tests.pipeline_fake_engine import make_fake_engine_db

_CHILD = Path(__file__).parent / "_4vx0a_sigterm_child.py"
_COLLECTION = "docs__4vx0a-sigterm__bge-base-en-v15-768__v1"
_READY_TIMEOUT_S = 120.0
#: The release happens at once on the interrupt, before anything that waits, so the child is gone
#: in well under a supervisor's SIGKILL window (Nomad's default kill_timeout is 5 s).
_EXIT_BUDGET_S = 5.0

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM delivery is POSIX")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.fixture()
def served_pipeline() -> Iterator[tuple[str, InMemoryPipelineEngine]]:
    """The in-memory pipeline stand-in behind a real HTTP server on 127.0.0.1, port 0."""
    engine = InMemoryPipelineEngine()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _forward(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            answer = engine.handler(httpx.Request(
                self.command, f"http://stand-in{self.path}", content=body,
                headers={"content-type": self.headers.get("Content-Type") or "application/json"},
            ))
            payload = answer.content
            self.send_response(answer.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _forward

        def log_message(self, *args: object) -> None:  # quiet
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@posix_only
def test_sigterm_mid_extraction_releases_the_row_for_an_immediate_retry(
    tmp_path: Path, served_pipeline: tuple[str, InMemoryPipelineEngine],
) -> None:
    url, engine = served_pipeline
    pdf = tmp_path / "blocked.pdf"
    pdf.write_bytes(f"nexus-4vx0a {tmp_path}".encode())
    content_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    fence_log = tmp_path / "fence_fail.log"

    env = os.environ.copy()
    env.update({
        "NX_4VX0A_PDF": str(pdf),
        "NX_4VX0A_HASH": content_hash,
        "NX_4VX0A_COLLECTION": _COLLECTION,
        "NX_4VX0A_PIPELINE_URL": url,
        "NX_4VX0A_FENCE_LOG": str(fence_log),
        # The child's only store is the loopback stand-in; the dev-checkout write guard does not
        # know that (the parent's in-process exemption does not reach a child).
        "NX_ALLOW_PROD_WRITE": "nexus-4vx0a sigterm test child: writes only to a loopback in-memory stand-in",
        "NX_NO_TELEMETRY": "1",
        "PYTHONFAULTHANDLER": "1",
    })
    # stderr to a file: a pipe read only at the end could fill and block the child.
    err_path = tmp_path / "child.stderr"
    with err_path.open("w") as err_file:
        child = subprocess.Popen(
            [sys.executable, "-u", str(_CHILD)], env=env, cwd=str(Path(__file__).parent.parent),
            stdout=subprocess.PIPE, stderr=err_file, text=True,
        )
    try:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        line = ""
        while (left := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select([child.stdout], [], [], left)
            if not readable:
                break
            line = child.stdout.readline()
            if line.strip() == "READY" or not line:
                break
        assert line.strip() == "READY", (
            f"the child never reached its extraction (rc={child.poll()!r})\n{err_path.read_text()}"
        )

        retry = HttpPipelineDB(base_url=url, _token="loopback-stand-in")
        # Precondition, so the retry below proves the release and not an absent row: while the
        # child runs, its row is live and a retry is refused.
        with pytest.raises(PipelineConflictRunning):
            retry.create_pipeline(content_hash, str(pdf.resolve()), _COLLECTION)

        child.send_signal(signal.SIGTERM)
        try:
            rc = child.wait(timeout=_EXIT_BUDGET_S)
        except subprocess.TimeoutExpired:
            child.send_signal(signal.SIGABRT)  # PYTHONFAULTHANDLER: every thread's stack to stderr
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            pytest.fail(f"the child was still running {_EXIT_BUDGET_S}s after SIGTERM\n{err_path.read_text()}")
        assert rc == -signal.SIGTERM, (
            f"SIGTERM must still end the process as SIGTERM (re-delivered), got rc={rc!r}\n{err_path.read_text()}"
        )

        assert retry.create_pipeline(content_hash, str(pdf.resolve()), _COLLECTION) == "resuming"
        state = retry.get_pipeline_state(content_hash)
        assert "interrupted by SIGTERM" in (state.get("error") or ""), state
        pages = retry.read_pages(content_hash)
        assert [p["page_index"] for p in pages] == [0, 1, 2], "the killed run's pages are kept for the resume"
        assert len(engine.rows_for(content_hash)) == 1, "the retry resumed the killed run's row"
        assert fence_log.read_text().splitlines()[0] == "1.1.4242", "the catalog fence was failed on the way out"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def _embed(texts, model):
    return [[0.1] * 4 for _ in texts], model


def test_an_interrupt_during_the_drain_after_a_stage_failure_propagates_and_keeps_the_buffer() -> None:
    """Review findings (0b6c3ee4a, d05c032c1): a stage fails, the orchestrator drains the others,
    and a SIGTERM lands during that drain. The INTERRUPT is what leaves ``pipeline_index_pdf``
    (the stage failure as its cause), so ``sigterm_raises`` can deliver the signal again; before
    the fix the stage's RuntimeError left instead and the SIGTERM was lost. A stage is still
    running at that moment, so the WAL is not wiped under it (a straggler's page write after the
    wipe would leave a counter that skips pages on resume): the row is released with its buffer
    kept, and the record names both the interrupt and the stage's error."""
    db, engine = make_fake_engine_db()
    uploader_may_finish = threading.Event()

    class _FailingExtractor:
        def extract(self, pdf_path, *, extractor="mineru", on_page=None,
                    allow_degraded=False, on_heartbeat=None):
            for i in range(2):
                on_page(i, f"page {i}", {"page_number": i + 1, "text_length": 6})
            db.flush("h4vx0a-drain")
            raise RuntimeError("boom: deterministic stage failure")

    def _slow_uploader(content_hash, _db, _t3, _collection, cancel, *_a, **_k):
        cancel.wait(10)
        uploader_may_finish.wait(10)

    real_wait = concurrent.futures.wait

    def _wait(fs, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
        if return_when == concurrent.futures.ALL_COMPLETED:  # the drain after the first failure
            raise TerminationRequested(signal.SIGTERM)
        return real_wait(fs, timeout=timeout, return_when=return_when)

    fence_fail = MagicMock()
    try:
        with patch("nexus.pipeline_stages.PDFExtractor", return_value=_FailingExtractor()), \
                patch("nexus.pipeline_stages.uploader_loop", _slow_uploader), \
                patch("nexus.pipeline_stages.wait", side_effect=_wait), \
                patch("nexus.doc_indexer._fence_fail", fence_fail), \
                pytest.raises(TerminationRequested) as raised:
            pipeline_index_pdf(
                Path("/4vx0a-drain.pdf"), "h4vx0a-drain", "docs__test", MagicMock(),
                db=db, embed_fn=_embed, doc_id="1.1.9",
            )
    finally:
        uploader_may_finish.set()

    assert isinstance(raised.value.__cause__, RuntimeError), "the stage failure rides as the cause"
    (row,) = engine.rows_for("h4vx0a-drain")
    assert row["status"] == "failed"
    assert "interrupted by SIGTERM after a stage failed" in row["error"] and "boom" in row["error"], row["error"]
    assert [p["page_index"] for p in db.read_pages("h4vx0a-drain")] == [0, 1], (
        "no WAL wipe while a stage may still write"
    )
    fence_fail.assert_called_once()
    assert "boom" in fence_fail.call_args.args[1]


def test_a_straggler_stage_after_an_interrupted_drain_never_writes_over_a_wiped_wal() -> None:
    """Confirmation critique of d05c032c1: when an interrupt cut the drain after a stage failure
    short, the failure's WAL wipe ran while the chunker was still embedding, and the chunker's
    writes then landed over the empty WAL (5 chunks, 0 pages: orphan chunks the next run
    replays). The chunker here is the real one, held inside its embed call until the bookkeeping
    is done. With the release-instead-of-wipe guard the pages it chunked from are still there when
    its chunks land; with the guard removed they are gone."""
    db, _engine = make_fake_engine_db()
    h = "h4vx0a-straggler"
    page_text = "alpha beta gamma delta. " * 260
    in_flight, release = threading.Event(), threading.Event()

    class _Extractor:
        def extract(self, pdf_path, *, extractor="mineru", on_page=None,
                    allow_degraded=False, on_heartbeat=None):
            time.sleep(1.0)  # the chunker seeds its empty cache and enters its poll loop first
            for i in range(2):
                on_page(i, page_text, {"page_number": i + 1, "text_length": len(page_text)})
                db.flush(h)
                time.sleep(0.3)
            if not in_flight.wait(15):
                raise AssertionError("the chunker never reached its embed call")
            raise RuntimeError("boom: deterministic stage failure")

    def _embed_and_hold(texts, model):
        in_flight.set()
        release.wait(30)
        return [[0.1] * 4 for _ in texts], model

    real_wait = concurrent.futures.wait

    def _wait(fs, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
        if return_when == concurrent.futures.ALL_COMPLETED:
            raise TerminationRequested(signal.SIGTERM)
        return real_wait(fs, timeout=timeout, return_when=return_when)

    try:
        with patch("nexus.pipeline_stages.PDFExtractor", return_value=_Extractor()), \
                patch.object(ps, "_POLL_INTERVAL", 0.05), \
                patch("nexus.pipeline_stages.wait", side_effect=_wait), \
                patch("nexus.doc_indexer._fence_fail", MagicMock()), \
                pytest.raises(TerminationRequested):
            pipeline_index_pdf(
                Path("/4vx0a-straggler.pdf"), h, "docs__test", MagicMock(),
                db=db, embed_fn=_embed_and_hold, doc_id="1.1.9", hooks=HookRegistry(),
            )
    finally:
        release.set()
    end = time.monotonic() + 10
    while db.count_embedded_chunks(h) == 0 and time.monotonic() < end:
        time.sleep(0.05)
    chunks, pages = db.count_embedded_chunks(h), len(db.read_pages(h))
    assert chunks > 0, "the straggler never wrote; the test proves nothing"
    assert pages == 2, f"{chunks} chunks landed over a WAL holding {pages} of their 2 pages"


@posix_only
def test_a_stopped_mineru_worker_and_one_started_after_the_stop_are_both_killed(tmp_path: Path) -> None:
    """The real per-batch worker path (``_mineru_run_subprocess``) with a worker that records its
    pid and sleeps. ``stop_mineru_workers`` kills the live one, and the stage raises
    ``MineruWorkerStopped`` (a stop, not an out-of-memory failure) at once. A worker started after
    its run was interrupted (a stage between batches at that moment) is killed as it registers:
    the thread's run is bound with ``bind_run_cancel``, and the binding goes with the thread."""

    def _run(pid_file: Path, outcome: dict) -> None:
        script = (
            "import os, time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(600)\n"
        )
        try:
            with patch.object(pdf_extractor, "_MINERU_WORKER_SCRIPT", script):
                pdf_extractor.PDFExtractor()._mineru_run_subprocess(tmp_path / "x.pdf", 0, 1)
        except BaseException as exc:  # noqa: BLE001 — the test reads what the stage raised
            outcome["exc"] = exc

    def _wait_for(pred, budget_s: float) -> bool:
        end = time.monotonic() + budget_s
        while not pred() and time.monotonic() < end:
            time.sleep(0.05)
        return pred()

    try:
        first_pid, first = tmp_path / "first.pid", {}
        stage = threading.Thread(target=_run, args=(first_pid, first))
        stage.start()
        assert _wait_for(lambda: first_pid.exists() and first_pid.stat().st_size > 0, 60), "worker never started"
        started = time.monotonic()
        assert pdf_extractor.stop_mineru_workers() == 1
        stage.join(10)
        assert not stage.is_alive(), "the stage still waits on a killed worker"
        assert time.monotonic() - started < 3.0
        assert isinstance(first.get("exc"), pdf_extractor.MineruWorkerStopped), first
        assert _wait_for(lambda: not _pid_alive(int(first_pid.read_text())), 5), "the worker outlived the stop"

        interrupted = threading.Event()
        interrupted.set()
        pdf_extractor.bind_run_cancel(interrupted)
        late_pid, late = tmp_path / "late.pid", {}
        _run(late_pid, late)  # its run is already interrupted: killed as it registers, never waits its budget
        assert isinstance(late.get("exc"), pdf_extractor.MineruWorkerStopped), late
    finally:
        pdf_extractor.bind_run_cancel(None)


def test_a_fenced_run_does_not_mark_the_new_owners_row() -> None:
    """An interrupted run whose row a newer run already took over (``run_epoch``,
    nexus-8vu8p) must not move that row: the engine refuses the stale write and
    the release reports it fenced, so the caller leaves the catalog fence alone."""
    now = [datetime(2026, 10, 25, 12, 0, tzinfo=UTC)]
    old_run, engine = make_fake_engine_db(clock=lambda: now[0])
    old_run.create_pipeline("h4vx0a", "/a.pdf", "docs__test")
    now[0] += timedelta(minutes=6)
    new_owner, _ = make_fake_engine_db(clock=lambda: now[0])
    new_owner._client = old_run._client
    assert new_owner.create_pipeline("h4vx0a", "/a.pdf", "docs__test") == "resuming"

    assert _release_interrupted_run(old_run, "h4vx0a", TerminationRequested(signal.SIGTERM)) is True
    (row,) = engine.rows_for("h4vx0a")
    assert row["status"] == "resuming", "the new owner's row was moved by the fenced run"
    assert "interrupted" not in (row.get("error") or "")


def test_release_reports_an_unfenced_release() -> None:
    db, engine = make_fake_engine_db()
    db.create_pipeline("h4vx0a-ok", "/a.pdf", "docs__test")
    assert _release_interrupted_run(db, "h4vx0a-ok", KeyboardInterrupt()) is False
    (row,) = engine.rows_for("h4vx0a-ok")
    assert row["status"] == "failed"
    assert row["error"].startswith("interrupted;")


_CHAIN_SCRIPT = """
import os, signal, sys
from nexus.util.termination import TerminationRequested, sigterm_raises

def earlier(signum, frame):
    sys.stdout.write("earlier handler ran\\n")
    sys.stdout.flush()
    os._exit(7)

signal.signal(signal.SIGTERM, earlier)
try:
    with sigterm_raises():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            signal.pause()
        except TerminationRequested:
            sys.stdout.write("block saw TerminationRequested\\n")
            raise
except TerminationRequested:
    sys.stdout.write("re-delivery did not reach the earlier handler\\n")
"""

_SWALLOW_SCRIPT = """
import os, signal, sys
from nexus.util.termination import TerminationRequested, sigterm_raises

try:
    with sigterm_raises():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            signal.pause()
        except TerminationRequested:
            os.kill(os.getpid(), signal.SIGTERM)  # a repeat during the cleanup is ignored
            sys.stdout.write("cleanup ran\\n")
            sys.stdout.flush()
            raise ValueError("something else leaves the block")
except ValueError:
    pass
sys.stdout.write("survived\\n")
"""


def _markers(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if "sigterm_redelivered" not in line]


@posix_only
def test_sigterm_chains_to_an_earlier_python_handler() -> None:
    """``nx dt index`` installs its own SIGTERM handler (the DEVONthink stdio child's teardown,
    ``mcp_client.devonthink``). Inside ``sigterm_raises`` the run sees the exception first, then
    that earlier handler still runs, as it would have without the block."""
    result = subprocess.run(
        [sys.executable, "-c", _CHAIN_SCRIPT], capture_output=True, text=True, timeout=60,
        env={**os.environ, "NX_NO_TELEMETRY": "1"},
    )
    assert _markers(result.stdout) == ["block saw TerminationRequested", "earlier handler ran"], (
        result.stdout + result.stderr
    )
    assert result.returncode == 7


@posix_only
def test_a_fired_sigterm_is_delivered_again_whatever_exception_leaves_the_block() -> None:
    """Review finding (0b6c3ee4a): re-delivery only from ``except TerminationRequested`` lost the
    signal when the code under the block raised something else, and the process carried on. Now
    a fired SIGTERM ends the process on the way out of the block, and a repeat SIGTERM during the
    cleanup does not cut the cleanup short."""
    result = subprocess.run(
        [sys.executable, "-c", _SWALLOW_SCRIPT], capture_output=True, text=True, timeout=60,
        env={**os.environ, "NX_NO_TELEMETRY": "1"},
    )
    assert _markers(result.stdout) == ["cleanup ran"], result.stdout + result.stderr
    assert result.returncode == -signal.SIGTERM
