# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-batch MinerU worker's failure carries a bounded tail of its stderr (nexus-11x9o).

The worker's stderr used to go to DEVNULL, so ``mineru_subprocess_failed`` said
"returncode=1" and nothing else. On 2026-10-25 the server path answered 409 and
this fallback exited 1 for ONE reason (``ImportError: libGL.so.1: cannot open
shared object file`` out of ``import cv2``), visible only after re-running the
import by hand. The traceback's last lines were the whole diagnosis.

These run the real worker script in a real child interpreter against a stub
``mineru.cli.common`` placed first on the CHILD's PYTHONPATH (read at interpreter
startup, so the parent's already-imported mineru is untouched), the same shape
tests/test_gqrg0_mineru_real_parse_check.py uses.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from nexus import pdf_extractor
from nexus.pdf_extractor import PDFExtractor

LIBGL = "libGL.so.1: cannot open shared object file: No such file or directory"


def _stub_mineru(base: Path, do_parse_body: str) -> None:
    pkg = base / "mineru" / "cli"
    pkg.mkdir(parents=True)
    (base / "mineru" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "common.py").write_text(
        "import sys\n\ndef do_parse(*args, **kwargs):\n"
        + "".join(f"    {ln}\n" for ln in do_parse_body.splitlines())
    )


def _run_failing_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> dict:
    """Run the worker against a stub that fails; return the failure log event."""
    stub_root = tmp_path / "stub"
    _stub_mineru(stub_root, body)
    monkeypatch.setenv("PYTHONPATH", str(stub_root))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("nexus.config.get_mineru_page_timeout_s", return_value=120))
        stack.enter_context(patch("nexus.config.get_mineru_memory_ceiling_mb", return_value=0))
        logs = stack.enter_context(capture_logs())
        with pytest.raises(RuntimeError, match="exited with code 1"):
            PDFExtractor()._mineru_run_subprocess(pdf, 0, 1)
    events = [e for e in logs if e.get("event") == "mineru_subprocess_failed"]
    assert len(events) == 1, logs
    return events[0]


def test_the_failure_event_carries_the_workers_stderr_tail(monkeypatch, tmp_path) -> None:
    event = _run_failing_worker(monkeypatch, tmp_path, f"raise ImportError({LIBGL!r})")
    tail = event.get("stderr_tail")
    assert tail, f"mineru_subprocess_failed carries no stderr_tail: {event}"
    assert "ImportError" in tail and LIBGL in tail, tail


def test_the_tail_is_bounded_and_keeps_the_end_not_the_start(monkeypatch, tmp_path) -> None:
    body = (
        'sys.stderr.write("HEAD-MARKER\\n" + ("x" * 20000) + "\\nTAIL-MARKER\\n")\n'
        'raise RuntimeError("boom")'
    )
    event = _run_failing_worker(monkeypatch, tmp_path, body)
    tail = event["stderr_tail"]
    assert "RuntimeError: boom" in tail, "the traceback's last line is the diagnosis"
    assert "HEAD-MARKER" not in tail, "the start of a long stderr must be dropped, not the end"
    assert len(tail.encode("utf-8")) <= pdf_extractor._MINERU_STDERR_TAIL_BYTES + 4
    assert pdf_extractor._MINERU_STDERR_TAIL_BYTES == 2048


def test_a_worker_with_no_stderr_logs_an_empty_tail_not_a_crash(monkeypatch, tmp_path) -> None:
    event = _run_failing_worker(monkeypatch, tmp_path, "import os\nos._exit(1)")
    assert event.get("stderr_tail") == ""


def test_a_credential_in_the_tail_is_redacted_before_it_is_logged(monkeypatch, tmp_path) -> None:
    """A worker's stderr can echo a rejected credential (a service 401 body does);
    the tail goes through the repo's redaction helper, as doctor's exception text does."""
    secret = "sk-live-0123456789abcdef"
    body = f'raise RuntimeError("HTTP 401: invalid api_key={secret}")'
    event = _run_failing_worker(monkeypatch, tmp_path, body)
    tail = event["stderr_tail"]
    assert secret not in tail, tail
    assert "[redacted]" in tail and "HTTP 401" in tail, tail
