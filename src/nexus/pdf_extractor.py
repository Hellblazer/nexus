# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text extraction: MinerU, with a per-page fallback ladder, and a text-only option.

Extraction backends (selected by the ``extractor`` param):

1. MinerU (the default) is math- and layout-aware. It is part of the on-demand
   ``pdf`` extra (nexus-2k66t.4.2): a fresh install leaves it out and the first
   PDF index fetches it (``nexus.pdf_stack``), an existing install keeps it. No
   entry point ever falls back to a plainer extractor on its own: it installs the
   stack or stops with the command to run. A page whose MinerU attempt fails walks
   the per-page ladder (RDR-237) instead of failing the document.
2. PyMuPDF (``extractor='pymupdf'``) is the explicit text-only extractor: every
   page's own text layer, no layout, tables or math, and none of the PDF stack.
   It is never chosen automatically.

The two values older configs and scripts may still carry are aliases for
``mineru`` (``nexus.config.resolve_extractor``).
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

import httpx
import structlog

from nexus._vcrt import MINERU_CHILD_PROLOGUE
from nexus.config import DEFAULT_EXTRACTOR, resolve_extractor
from nexus.errors import ExtractionQualityError, UnextractableContentError
from nexus.redact import redact_credentials

# MinerU is resolved LAZILY (nexus-ct17r). ``mineru.cli.common`` imports
# magika, which imports onnxruntime at module scope, and this module is
# imported by doc_indexer / pipeline_stages on every ``nx`` command that
# touches the store — so an eager import here loaded ORT into every
# ``nx store put`` that never parsed a PDF. ORT's telemetry dispatcher then
# raced interpreter teardown ("recursive_mutex lock failed", Abort trap 6).
# The parent only needs to know MinerU is importable; the parse itself runs
# in a subprocess (see ``_MINERU_WORKER_SCRIPT``). ``do_parse`` stays a
# module attribute so tests can patch it (``None`` = unavailable).
_DO_PARSE_UNRESOLVED = object()
do_parse = _DO_PARSE_UNRESOLVED


def _mineru_do_parse():  # noqa: ANN202 — mineru's callable type is not importable eagerly, by design
    """Resolve ``mineru.cli.common.do_parse`` on first use; ``None`` if absent."""
    global do_parse
    if do_parse is _DO_PARSE_UNRESOLVED:
        try:
            from mineru.cli.common import do_parse as _resolved  # noqa: PLC0415 — deliberately lazy (nexus-ct17r)
        except ImportError:
            _resolved = None
        do_parse = _resolved
    return do_parse


# Inline script executed in a child Python process for memory isolation.
# Uses os._exit to force-terminate without waiting for daemon threads / worker
# pools that MinerU's pipeline may leave running.
#
# RDR-148 Gap 3 (macOS spawn-guard) — VERIFY-FIRST spike outcome, source-
# verified 2026-06-24: the original diagnosis (in-process MinerU worker failing
# on macOS under multiprocessing's `spawn` start method without an
# `if __name__ == "__main__"` guard — exit 1 + "leaked semaphore") is MOOT for
# the boundary it described. The worker is now a plain
# ``subprocess.Popen([sys.executable, "-c", _MINERU_WORKER_SCRIPT, ...])`` — a
# fresh interpreter, NOT a multiprocessing-spawn child, so the parent-__main__
# re-import recursion that the guard protects against is categorically
# inapplicable at the nexus->worker boundary. ``os._exit(0)`` further skips the
# pool teardown that leaked the semaphore. Residual, distinct, and UNVERIFIED:
# if MinerU's ``do_parse`` itself spawns multiprocessing children, the un-
# guarded ``-c`` __main__ could re-trigger an analogous issue; reproducing that
# needs model weights (CA-3/CA-4 deferred — do not run casually on a dev host).
# Do NOT add a speculative multiprocessing guard here without that repro: it is
# untested surface (feedback_no_preventive_scope_beyond_evidence). The
# fresh-interpreter `-c` form is a load-bearing invariant — see the structural
# guard in tests/test_mineru_spawn_logging.py.
# RDR-148 Gap 5: distinct exit code for an in-process MemoryError so the parent
# can classify a memory exhaustion (the RLIMIT_AS-ceiling-breach path added by
# Gap 6) separately from a generic non-zero exit. A bare RLIMIT_AS breach exits
# the worker via an in-process MemoryError (code path below), NOT the OS SIGKILL
# (-9) path, so the -9-only mapping would miss it (gate finding). Sentinel is
# substituted into the worker script template below.
_MINERU_OOM_EXIT = 42

#: How long a restart waits for the replacement server's /health (models are cached by the OS).
_RESTART_HEALTH_WAIT_S = 60.0

#: nexus-5ny9r: how long a "no live MinerU server" verdict is trusted before
#: the next page re-probes the endpoint (one cheap /health, no autostart).
_MINERU_SERVER_RECHECK_S = 60.0

#: RDR-237: how many concurrent ``/file_parse`` requests MinerU's server runs when
#: ``MINERU_API_MAX_CONCURRENT_REQUESTS`` is unset and the platform is not macOS
#: (``DEFAULT_MAX_CONCURRENT_REQUESTS`` in MinerU's ``api_protocol.py``).
_MINERU_DEFAULT_SLOTS = 3


#: RDR-237: how long the slot count's ``/health`` read waits. The server answers it from its event
#: loop even with every slot held, so a longer wait only delays a restart that is already due.
_MINERU_HEALTH_SLOTS_TIMEOUT_S = 2.0


def _mineru_server_slots(url: str | None = None) -> int:
    """How many requests the MinerU server parses at once.

    With *url*, the server's own answer wins: ``/health`` reports ``max_concurrent_requests``
    (MinerU 3.1.11 ``fast_api.py``), and that also covers a server this process did not start (``nx
    mineru start``, launchd) whose environment is not ours. When the server does not say (it is
    unreachable, answered with an error, or sent no positive integer), and with no *url*, the count
    is read the way MinerU reads it: ONE slot on macOS whatever the environment says, else
    ``MINERU_API_MAX_CONCURRENT_REQUESTS`` when it is a positive integer, else 3. An abandoned
    parse holds its slot until it finishes (the parse runs in a thread nothing can cancel), which is
    what makes the count decide when a server that timed out is replaced (RDR-237).
    """
    if url:
        live = _live_server_slots(url)
        if live is not None:
            return live
    if sys.platform == "darwin":
        return 1
    raw = os.environ.get("MINERU_API_MAX_CONCURRENT_REQUESTS")
    if raw is not None:
        try:
            slots = int(raw)
        except ValueError:
            slots = 0
        if slots > 0:
            return slots
    return _MINERU_DEFAULT_SLOTS


def _live_server_slots(url: str) -> int | None:
    """The positive integer the server at *url* reports as ``max_concurrent_requests``, else None."""
    try:
        resp = httpx.get(f"{url.rstrip('/')}/health", timeout=_MINERU_HEALTH_SLOTS_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:  # noqa: BLE001 — a failed probe is no evidence; the caller keeps its estimate
        return None
    slots = payload.get("max_concurrent_requests") if isinstance(payload, dict) else None
    if isinstance(slots, int) and not isinstance(slots, bool) and slots > 0:
        return slots
    return None


#: nexus-11x9o: how much of the per-batch worker's stderr (its last bytes) rides
#: on ``mineru_subprocess_failed``. A traceback's end names the cause; a progress
#: bar's thousand lines do not need to be in a log event.
_MINERU_STDERR_TAIL_BYTES = 2048


def _read_stderr_tail(path: Path) -> str:
    """The last ``_MINERU_STDERR_TAIL_BYTES`` of *path* as text; ``""`` if unreadable."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _MINERU_STDERR_TAIL_BYTES))
            # The cut can land inside a multibyte sequence: its orphaned bytes
            # decode to replacement characters, which are not worth keeping.
            text = fh.read().decode("utf-8", errors="replace").lstrip("�").strip()
    except OSError:
        return ""
    # A worker's stderr can echo a rejected credential (a service 401 body does);
    # the log event goes through the same redaction doctor's exception text does.
    return redact_credentials(text)


#: RDR-237: a stderr line longer than this is cut when it becomes part of a failure class
#: (a progress bar can be one very long line; the class key stays a short, comparable string).
_STDERR_LINE_CAP = 300


#: RDR-237: what a progress bar redraw looks like (tqdm: ``45%|####5 | 9/20 [00:03<00:04, 2.9it/s]``).
#: A worker killed mid-run leaves one of these as its last stderr line, and which frame it is
#: differs from page to page, so it must never become part of a failure class.
_STDERR_PROGRESS = re.compile(r"\d+%\||\d+/\d+ \[\d|\b\d+(?:\.\d+)?\s*(?:it/s|s/it)\b")


#: RDR-237: one complete tqdm frame, up to its closing ``]``, with the short ``<description>:`` tqdm
#: writes before the percentage. tqdm redraws with ``\r`` and no newline, so a real message written
#: next can share the frame's physical line; removing the frame keeps the message.
_STDERR_FRAME = re.compile(r"(?:[^|\[\]\r\n]{0,60}?:\s*)?\d+%\|[^|\r\n]*\|[^\[\]\r\n]*\[[^\]\r\n]*\]")


def _last_stderr_line(tail: str) -> str:
    """The last non-empty line of a (redacted) stderr *tail* that is not a progress-bar frame;
    ``""`` when there is none. ``splitlines`` also splits on the carriage returns tqdm redraws with,
    and a frame glued to a message on one line is cut out of it (see :data:`_STDERR_FRAME`)."""
    for line in reversed(tail.splitlines()):
        rest = _STDERR_FRAME.sub("", line).strip()
        if rest and not _STDERR_PROGRESS.search(rest):
            return rest[:_STDERR_LINE_CAP]
    return ""


# nexus-vyy65: the prologue (``import nexus``) comes first, before the first mineru import. The
# worker is a fresh ``python -c`` child that never imported nexus, so on Windows without the VC++
# redistributable it could not load onnxruntime/torch from nx's app-local DLLs until it ran
# ``_add_vc_runtime_dirs`` for itself. ``nx doctor --check-mineru`` starts its child probe the
# same way (``_mineru_spawn.MINERU_CHILD_PROLOGUE``).
_MINERU_WORKER_SCRIPT = '''
__MINERU_CHILD_PROLOGUE__import json, sys, os
from pathlib import Path
from mineru.cli.common import do_parse

pdf_path, result_dir, start, end_str = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
end = None if end_str == "none" else int(end_str)
# RDR-237 rung 2: argv[5] is "0" for the formulas-off retry of a page; absent or "1" is rung 1.
formula_enable = not (len(sys.argv) > 5 and sys.argv[5] == "0")
try:
    do_parse(
        result_dir,
        pdf_file_names=[Path(pdf_path).name],
        pdf_bytes_list=[Path(pdf_path).read_bytes()],
        p_lang_list=["en"],
        formula_enable=formula_enable,
        table_enable=True,  # subprocess path always extracts tables; server path sends pdf.mineru_table_enable (default True, nexus-jd8fi)
        start_page_id=start,
        end_page_id=end,
    )
except MemoryError:
    # RDR-148 Gap 5/6: a memory-ceiling breach (RLIMIT_AS) surfaces in-process as
    # MemoryError, not OS SIGKILL. Exit with the sentinel so the parent maps it to
    # MineruMemoryError. os._exit (not sys.exit) to skip pool/daemon-thread teardown.
    os._exit(__MINERU_OOM_EXIT__)
os._exit(0)
'''.replace("__MINERU_CHILD_PROLOGUE__", MINERU_CHILD_PROLOGUE).replace("__MINERU_OOM_EXIT__", str(_MINERU_OOM_EXIT))

_log = structlog.get_logger(__name__)

class MineruWorkerStopped(BaseException):
    """The MinerU worker was killed by :func:`stop_mineru_workers` (nexus-4vx0a).

    The run was interrupted, so this is not a document failure: a
    ``BaseException`` so the per-page OOM retry (``except RuntimeError``)
    never catches it and starts more work."""


class _LiveWorker:
    """One running per-batch MinerU worker: how to kill its tree, and whether it was stopped."""

    def __init__(self, kill: Callable[[], None]) -> None:
        self.kill = kill
        self.stopped = False


_LIVE_MINERU_WORKERS: set[_LiveWorker] = set()
_LIVE_MINERU_WORKERS_LOCK = threading.Lock()
#: The cancel event of the indexing run the current thread extracts for, bound by
#: :func:`bind_run_cancel`. A worker that registers after its run's event is set is killed on the
#: spot: a stage thread can be between batches when the run is interrupted, and its next worker would
#: otherwise run its whole page budget unseen (nexus-4vx0a review). Per thread and per run, so an
#: interrupted run leaves nothing behind for the next run or the next test.
_RUN_CANCEL = threading.local()


def bind_run_cancel(cancel: threading.Event | None) -> None:
    """Tie the per-batch MinerU workers this thread starts to *cancel* (``None`` unbinds)."""
    _RUN_CANCEL.event = cancel


def stop_mineru_workers() -> int:
    """Kill the tree of every per-batch MinerU worker this process is waiting on.

    Called when an indexing run is interrupted (SIGTERM, Ctrl-C): the stage
    thread waiting on a worker then raises :class:`MineruWorkerStopped` at
    once instead of waiting out the worker's page budget (180 s a page by
    default). A worker its run starts after the interrupt is killed as it
    registers (:func:`bind_run_cancel`). Workers of the shared MinerU server are not this process's and are left
    alone. Returns the number of workers killed."""
    with _LIVE_MINERU_WORKERS_LOCK:
        workers = list(_LIVE_MINERU_WORKERS)
    for worker in workers:
        worker.stopped = True
        worker.kill()
    if workers:
        _log.info("mineru_workers_stopped", count=len(workers))
    return len(workers)


class MineruWorkerError(RuntimeError):
    """A MinerU subprocess failed by timing out or by exiting non-zero (RDR-237).

    The facts the failure-class key is built from ride as attributes, never as
    message text: the message names the page range and the PDF's path, so two
    pages of one broken install would never compare equal by it.

    *timed_out*: the worker was killed at its per-page budget. *returncode*: its
    exit status (``None`` for a timeout); *signal* (a property) is the signal behind a
    negative one. *stderr_last_line*: the last non-empty line of its stderr tail that is
    not a progress-bar frame (what ``mineru_subprocess_failed`` already logs), the line
    that names an ``ImportError`` or a missing weight file.
    """

    def __init__(
        self, message: str, *, timed_out: bool = False,
        returncode: int | None = None, stderr_last_line: str = "",
    ) -> None:
        super().__init__(message)
        self.timed_out = timed_out
        self.returncode = returncode
        self.stderr_last_line = stderr_last_line

    @property
    def signal(self) -> int | None:
        """The signal that ended the worker (``9`` for SIGKILL), ``None`` for a timeout and for an
        exit status. A negative ``returncode`` is POSIX's "killed by signal -N"; Windows exit
        codes are never negative."""
        return -self.returncode if self.returncode is not None and self.returncode < 0 else None


class MineruMemoryError(MineruWorkerError):
    """A MinerU subprocess died from memory exhaustion (RDR-148 Gap 5).

    Subclasses ``RuntimeError`` (through :class:`MineruWorkerError`)
    deliberately: the ladder's descend rule catches ``RuntimeError``, while
    callers that want to special-case memory exhaustion can catch this narrower
    type. Raised when a worker exits via SIGKILL (OS OOM-killer / jetsam), via
    the ``_MINERU_OOM_EXIT`` sentinel (in-process ``MemoryError`` from an
    ``RLIMIT_AS`` ceiling breach), or any non-zero exit once a memory ceiling
    was applied (Gap 6). The third arm is gated on
    ``PDFExtractor._mineru_ceiling_applied`` (default ``False`` until Gap 6
    wires the ceiling), so it is inert today and never misfires.
    """


# nexus-x3m8e: the machine-level MinerU failure type lives with the health
# probes; re-exported here so callers catch it from the extractor module.
from nexus._mineru_health import MineruMachineResourceError  # noqa: E402


#: RDR-237: what to do when the worker is killed by the OS (``signal:9``) or stopped at its memory
#: ceiling (``oom*``). The install is fine, so "repair the install" would send the operator the wrong way.
_MINERU_MEMORY_REMEDY = (
    "the worker was killed by the OS, usually out of memory. Free memory or use a machine with more; "
    "keep `pdf.mineru_page_batch` at 1 (more pages per worker run need more memory); on Linux "
    "`pdf.mineru_memory_ceiling_mb` makes the worker fail with a memory error instead of being killed."
)


def _is_memory_class(failure_class: str) -> bool:
    """True for the failure classes that mean the worker ran out of memory: ``signal:9`` (the
    OOM-killer or macOS jetsam), ``oom`` and ``oom:<rc>:<line>``."""
    return failure_class in ("signal:9", "oom") or failure_class.startswith("oom:")


class MineruSystemicFailure(Exception):
    """Consecutive pages failed both MinerU rungs with one failure class (RDR-237).

    The cause is the install or the machine, not any one page, so the document
    stops instead of being indexed page by page from its text layer. An
    ``Exception`` and not a ``RuntimeError`` on purpose: the ladder's descend
    rule is ``except RuntimeError``, and this must never be read as one page's
    failure. *failure_class* is the class key (``timeout``, ``signal:<n>``, ``oom``,
    ``oom:<rc>:<line>``, ``exit:<rc>:<line>``; see :func:`_failure_class`); *stderr_line* the worker's last stderr line, when it had one.
    """

    def __init__(self, failure_class: str, stderr_line: str, pages: list[int], file_name: str) -> None:
        self.failure_class = failure_class
        self.stderr_line = stderr_line
        self.pages = pages
        self.file_name = file_name
        shown = f"; the worker's last stderr line: {stderr_line!r}" if stderr_line else ""
        memory = _is_memory_class(failure_class)
        first = "free memory first" if memory else "repair the install first"
        kept = (
            f"No page was kept, and a rerun starts from page 1, so {first}."
            if pages[0] <= 1
            else f"The pages before page {pages[0]} stay in the kept buffer, so a rerun "
            f"does not write them again, but MinerU runs over them again, so {first}."
        )
        cause = (
            f"That points at the machine's memory, not at the pages: {_MINERU_MEMORY_REMEDY}"
            if memory
            else "That points at the install or the machine, not at the pages: run `nx doctor`."
        )
        super().__init__(
            f"MinerU failed on {len(pages)} consecutive pages of {file_name} "
            f"(pages {pages[0]}-{pages[-1]}), with and without formulas, "
            f"all with failure class {failure_class!r}{shown}. {cause} {kept} "
            f"`pdf.ladder_breaker` sets how many pages stop a "
            f"document (0 turns the check off)."
        )

    def __reduce__(self) -> tuple:
        # The base class keeps only the message, so the default reconstruction calls
        # ``MineruSystemicFailure(message)`` and raises TypeError: a process-pool caller could not
        # get this exception back. Rebuild it from the four values that make the message.
        return (type(self), (self.failure_class, self.stderr_line, self.pages, self.file_name))


#: What stops a run's MinerU work (nexus-tr1rj.13). Both name the install or the machine, not the
#: file, so every later PDF would fail the same way after its own timeouts. A batch command catches
#: this tuple, stops the PDFs it has not started, lists them, and keeps its other work.
MINERU_RUN_STOPPING: tuple[type[Exception], ...] = (MineruSystemicFailure, MineruMachineResourceError)


def mineru_stop_reason(exc: BaseException) -> tuple[str, str, str]:
    """``(what, detail, remedy)`` for a :data:`MINERU_RUN_STOPPING` exception, in the words a batch
    command prints on its ``Stopped:`` line: ``Stopped: <what> on <file> (<detail>); N remaining
    PDF(s) not attempted. <remedy>``."""
    if isinstance(exc, MineruSystemicFailure):
        remedy = (
            f"{_MINERU_MEMORY_REMEDY} Then rerun." if _is_memory_class(exc.failure_class)
            else "Repair the install (`nx doctor`), then rerun."
        )
        return ("MinerU failed systemically", f"failure class {exc.failure_class}", remedy)
    # MineruMachineResourceError has one raise site today (exhausted POSIX named semaphores), and its
    # message IS the platform's remedy (``nexus._mineru_health.semaphore_remedy``, about 400 characters).
    # The detail stays short and the remedy appears once, so the Stopped line stays one readable line.
    return ("MinerU cannot run on this machine", "POSIX named semaphores exhausted", str(exc))


class AllPagesUnextractedError(UnextractableContentError):
    """MinerU failed on every page of the document, with and without formulas, and no page has a
    text layer to fall back to (RDR-237).

    A subclass of :class:`UnextractableContentError` so every caller that already catches that keeps
    working, and a type of its own because the two mean different things. A plain
    ``UnextractableContentError`` is a file with nothing in it (blank, image-only under the text-only
    extractor): a rerun changes nothing, so a batch skips it. This is the extractor failing on a file
    that probably has text: a rerun after the install is repaired, or an OCR pass, does change it. A
    batch command reports it as an extraction failure and fails its exit over it (``nx dt index``
    counts it with the records that failed; ``nx index pdf`` already fails over any exception).

    *total_pages*: the document's page count. *last_error*: what sent the last page down its rungs
    (``rung 1: <class>; rung 2: <class>``).
    """

    def __init__(self, message: str, *, total_pages: int = 0, last_error: str = "") -> None:
        super().__init__(message)
        self.total_pages = total_pages
        self.last_error = last_error


class MineruServerTimeout(MineruWorkerError):
    """The MinerU server did not answer one request inside its page budget (RDR-237).

    A :class:`MineruWorkerError` with ``timed_out`` set, so the failure class is ``timeout`` and
    the page descends to rung 2. The type tells the ladder where that rung runs: on the
    subprocess path, the one path that can stop work, and never again on the server whose parse
    it abandoned (which cannot be cancelled and keeps running).
    """


class MineruBoundsError(RuntimeError):
    """``pdf.mineru_page_timeout_s`` x ``pdf.mineru_page_batch`` cannot be used as set (RDR-237).

    Raised where a MinerU run starts, before any page: the longest single attempt would run past
    the time a pipeline row stays alive without a progress write. The message names both settings.
    A ``RuntimeError`` so the commands that catch one print it and exit 1; raised outside the
    ladder, so no page reads it as a failure.
    """


class _HeartbeatStop(BaseException):
    """``on_heartbeat`` raised *original*: carried out of the extraction as a ``BaseException``.

    A heartbeat is called at the start of attempts that sit inside ``except Exception`` and
    ``except RuntimeError`` arms (the ladder's own, the server-restart retry, the lifecycle's
    ``never break extraction`` catch). What it raises is a stop (``PipelineRunFenced`` is a
    ``RuntimeError``), never a MinerU failure, so it travels as something none of those arms
    catches and :meth:`PDFExtractor.extract` re-raises *original* itself.
    """

    def __init__(self, original: Exception) -> None:
        super().__init__(original)
        self.original = original


@dataclass(frozen=True)
class PageOutcome:
    """Where one page settled on the ladder (RDR-237).

    *page* is 1-based. *method* is ``mineru`` (rung 1), ``mineru-no-formula``
    (rung 2), ``pymupdf-text`` (rung 3) or ``unextracted`` (rung 4). *text* is
    the page's text (for a page settled inside a multi-page MinerU batch, the
    batch's text rides on the batch's first page and the rest are empty).
    *error* names what sent the page down a rung, ``None`` for rung 1.
    """

    page: int
    text: str
    method: str
    seconds: float
    error: str | None = None


#: Settled methods below rung 1, in rung order: the keys of ``extraction_pages``.
_DEGRADED_METHODS: tuple[str, ...] = ("mineru-no-formula", "pymupdf-text", "unextracted")

#: A dropped server connection (RDR-237): the page moves on, and the failure has no class. In a real
#: run :meth:`PDFExtractor._mineru_run_isolated` absorbs these (restart, one retry, then the
#: subprocess), so the failure the ladder and the breaker see is the subprocess's own; this tuple
#: covers a drop that still reaches the ladder.
_DROPPED_CONNECTION = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError,
)

_UNCLASSIFIED = "unclassified"

#: RDR-237: values that differ from page to page inside an otherwise identical stderr line.
_KEY_PATH = re.compile(r"""(?<![\w.])(?:/|[A-Za-z]:[\\/]|~/)[^\s'"),;]*""")
_KEY_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_KEY_DIGITS = re.compile(r"\d{3,}")


def _key_line(line: str) -> str:
    """*line* with the parts that change from page to page replaced, for use inside a failure class.

    A per-page temp path, a pid, a hex address or a byte count in the worker's last stderr line would
    give every page its own class and the breaker would never count two pages as alike. Absolute
    and ``~`` paths become ``<path>``, hex addresses ``<addr>``, runs of three or more digits ``<n>``;
    the exception type and the message words stay, and so do one- and two-digit numbers."""
    line = _KEY_PATH.sub("<path>", line)
    line = _KEY_HEX.sub("<addr>", line)
    return _KEY_DIGITS.sub("<n>", line)


def _failure_class(exc: BaseException) -> str:
    """The class key of one attempt's failure, built from attributes and never message text.

    Keyed on what the worker did, in this order, so that two pages failing the same way get the
    same key and the breaker counts them:

    * ``timeout``: the worker was killed at its page budget.
    * ``signal:<n>``: the OS killed it (``signal:9`` is the OOM-killer or macOS jetsam). The
      stderr line is NOT part of the key: a killed worker's last line is whatever progress frame
      it was on.
    * ``oom``: it exited with the in-process ``MemoryError`` sentinel; stderr is again no part of it.
    * ``oom:<returncode>:<stderr line>``: a memory ceiling turns ANY non-zero exit into a
      :class:`MineruMemoryError`; the line keeps a missing library apart from a real exhaustion.
    * ``exit:<returncode>:<stderr line>``: any other non-zero exit; the line names an ``ImportError``
      or a missing weight file.

    The stderr line in the last two keys goes through :func:`_key_line` first, so a temp path, pid,
    address or byte count that differs per page does not give each page its own class.
    * ``unclassified``: no worker facts (a "produced no output" ``RuntimeError``, a dropped server
      connection). The breaker never counts it (see :class:`_SystemicFailureCounter`).

    There is no ``http:<status>`` class: a server error falls to the subprocess path, so the
    subprocess failure is the terminal one.
    """
    if isinstance(exc, MineruWorkerError):
        if exc.timed_out:
            return "timeout"
        if exc.signal is not None:
            return f"signal:{exc.signal}"
        if isinstance(exc, MineruMemoryError):
            if exc.returncode == _MINERU_OOM_EXIT:
                return "oom"
            line = _key_line(exc.stderr_last_line)
            return f"oom:{exc.returncode}:{line}" if exc.returncode is not None else f"oom:{line}"
        if exc.returncode is not None:
            return f"exit:{exc.returncode}:{_key_line(exc.stderr_last_line)}"
    return _UNCLASSIFIED


def _is_run_fence(exc: BaseException) -> bool:
    """True for ``PipelineRunFenced``, a ``RuntimeError`` that is a stop, never a page failure."""
    from nexus.db.http_pipeline_client import PipelineRunFenced  # noqa: PLC0415 — deferred import — avoids import-time cost / circular deps

    return isinstance(exc, PipelineRunFenced)


def _is_pipeline_cancel(exc: BaseException) -> bool:
    """True for ``PipelineCancelled``: the pipeline stopping this stage, never a page failure."""
    from nexus.pipeline_stages import PipelineCancelled  # noqa: PLC0415 — deferred import — avoids import-time cost / circular deps

    return isinstance(exc, PipelineCancelled)


class _SystemicFailureCounter:
    """The breaker's count: pages that failed rungs 1 and 2 with one class, in a row.

    The rule, stated once: a page counts only when BOTH rungs failed with the SAME class and
    that class is not ``unclassified``; it extends the run only when the class equals the
    previous counted page's, and restarts it at one otherwise. A page that settled on rung 1
    or 2, whose two rungs failed differently, or without a class, resets the count. The
    ladder passes ``unclassified`` for a page whose rung 1 dropped the server connection, so
    that page never counts. *threshold* 0 never trips.
    """

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.klass: str | None = None
        self.pages: list[int] = []

    def settled(self) -> None:
        self.klass, self.pages = None, []

    def failed(self, page: int, rung1: str, rung2: str) -> str | None:
        """Record a page that failed both rungs; return the class key if the rule trips."""
        if rung1 != rung2 or rung1 == _UNCLASSIFIED:
            self.settled()
            return None
        if rung1 == self.klass:
            self.pages.append(page)
        else:
            self.klass, self.pages = rung1, [page]
        if self.threshold and len(self.pages) >= self.threshold:
            return rung1
        return None


# ── NX_MINERU_FAULT_PAGES: force a page's rungs to fail (RDR-237 minimum viable validation) ──
#: ``<page>:<rungs>[:<mode>]`` entries separated by ``;``. *page* is 1-based, *rungs* is ``1``,
#: ``2`` or ``1,2``, *mode* is ``error`` (default: a worker exit), ``timeout`` or ``oom``.
#: ``5:1`` fails page 5's rung 1; ``5:1,2:timeout`` times page 5 out on rungs 1 and 2.
_FAULT_PAGES_ENV = "NX_MINERU_FAULT_PAGES"
_FAULT_MODES = ("error", "timeout", "oom")
_FAULT_STDERR_LINE = "forced by NX_MINERU_FAULT_PAGES"


def _parse_fault_pages(raw: str) -> dict[int, tuple[set[int], str]]:
    """Parse :data:`_FAULT_PAGES_ENV`; a malformed spec raises, never silently forces nothing."""
    forced: dict[int, tuple[set[int], str]] = {}
    for entry in (e.strip() for e in raw.split(";")):
        if not entry:
            continue
        parts = entry.split(":")
        try:
            if len(parts) not in (2, 3):
                raise ValueError("expected page:rungs[:mode]")
            page = int(parts[0])
            rungs = {int(r) for r in parts[1].split(",")}
            mode = parts[2] if len(parts) == 3 else "error"
            if page < 1 or not rungs or not rungs <= {1, 2} or mode not in _FAULT_MODES:
                raise ValueError("page >= 1, rungs in {1, 2}, mode one of " + "/".join(_FAULT_MODES))
            if page in forced:
                raise ValueError(f"page {page} is named twice")
        except ValueError as exc:
            raise ValueError(f"{_FAULT_PAGES_ENV}: bad entry {entry!r} ({exc})") from exc
        forced[page] = (rungs, mode)
    return forced


def _fault_wait(seconds: float) -> None:
    """Wait out a forced timeout, ending early (as a stop) when the run is cancelled."""
    cancel = getattr(_RUN_CANCEL, "event", None)
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise MineruWorkerStopped("MinerU forced-timeout wait ended: the run was interrupted")


def _forced_failure(mode: str, page: int, rung: int, span: int) -> MineruWorkerError:
    """The failure :data:`_FAULT_PAGES_ENV` forces; a ``timeout`` waits out the real page budget."""
    where = f"page {page}, rung {rung}"
    if mode == "timeout":
        if span <= 1:
            from nexus.config import get_mineru_page_timeout_s  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

            _fault_wait(get_mineru_page_timeout_s())
        return MineruWorkerError(f"forced timeout ({where})", timed_out=True)
    cls = MineruMemoryError if mode == "oom" else MineruWorkerError
    return cls(
        f"forced worker failure ({where})",
        returncode=-9 if mode == "oom" else 1, stderr_last_line=_FAULT_STDERR_LINE,
    )

# A server found broken is replaced at most once per
# ``_mineru_health.BROKEN_RESTART_COOLDOWN_S``, MACHINE-wide (a timestamp file
# next to mineru.pid, ``claim_broken_restart``), so a task error that a restart
# cannot fix never turns into a restart loop across a batch of PDFs, and N
# parallel indexers never restart the one shared server N times.


def _http_error_text(exc: httpx.HTTPStatusError) -> str:
    """The server's own account of a failed task: the JSON ``error`` /
    ``detail`` / ``message`` fields when the body is JSON, else the raw text."""
    try:
        resp = exc.response
        try:
            body = resp.json()
        except ValueError:
            return resp.text or ""
        if isinstance(body, dict):
            parts = [str(body[k]) for k in ("error", "detail", "message") if body.get(k)]
            if parts:
                return " ".join(parts)
        return str(body)
    except Exception:  # noqa: BLE001 — classification input only; empty means "unknown"
        return ""


# nexus-2fyb code-review R1-I3: progress messages are interactive UX for
# long-running PDF extractions (MinerU per-page
# inference). They MUST also go through structlog so non-interactive
# callers (library use, MCP server, batch jobs) capture them in structured
# logs. Setting NEXUS_PDF_PROGRESS_QUIET=1 disables the stderr write
# entirely (e.g. for tests that capture stderr).
import os as _os


def _progress(msg: str) -> None:
    """Emit a progress event via structlog AND optionally to stderr.

    Stderr writes are gated by ``NEXUS_PDF_PROGRESS_QUIET`` env var so
    tests and library callers can suppress the interactive output without
    losing the structured log event. This replaces the prior plain
    ``print()`` which violated the project's no-print-in-library-code
    rule and made tests that captured stderr brittle.
    """
    # nexus-z0idx follow-on: NOT "message" (stdlib logging reserves that
    # name on LogRecord) — "reason" here to match this file's OWN
    # established kwarg for this semantic (7 pre-existing uses).
    _log.info("pdf_extractor_progress", reason=msg.strip())
    if _os.environ.get("NEXUS_PDF_PROGRESS_QUIET") != "1":
        print(msg, file=sys.stderr, flush=True)  # noqa: T201 — gated interactive stderr progress; structured event emitted above via _log.info


def _restart_skip_text(reason: str, pid: object) -> str:
    """The progress sentence for a timed-out server that was NOT replaced (nexus-tr1rj.25).

    *reason* is what ``PDFExtractor._restart_broken_mineru_server`` recorded. After a skip the old
    server is still the registered one; the caller marks it down, so later pages run in a
    subprocess until the re-check finds it answering.
    """
    from nexus._mineru_health import BROKEN_RESTART_COOLDOWN_S  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    if reason == "failed":
        head = f"tried to replace it (pid {pid}) and the replacement did not come up"
    else:
        why = {
            "cooldown": (
                f"a server was already replaced on this machine in the last "
                f"{int(BROKEN_RESTART_COOLDOWN_S)} s"
            ),
            "refused": "this run's restart limit is spent or the spawn policy forbids a restart",
            "no-pid": "its pid is not recorded",
            "unreadable": "the process behind that pid could not be identified, so nothing was stopped",
        }.get(reason, "no restart happened")
        head = f"not replacing it (pid {pid}): {why}"
    return f"{head}; later pages run in a subprocess for now."


# nexus-2fyb code-review R5-I2: chained exceptions from MinerU/httpx can
# include the configured pdf.mineru_server_url. If a user (mis-)configured
# that URL with embedded credentials (http://user:pass@host/...), those
# credentials would surface in error messages, structlog events, and
# downstream log sinks. Redact userinfo from any URL we surface.
_URL_CREDENTIALS_PATTERN = re.compile(
    r"(https?://)([^/\s@]+)@",  # capture scheme + userinfo segment
)


def _redact_url_credentials(text: str) -> str:
    """Replace ``http://user:pass@host`` with ``http://[redacted]@host`` in *text*.

    Used in error-message construction to avoid leaking credentials that
    a user may have configured into ``pdf.mineru_server_url``.
    """
    return _URL_CREDENTIALS_PATTERN.sub(r"\1[redacted]@", text)


def _mineru_end_page_id(end: int | None) -> int:
    """Convert an EXCLUSIVE page bound to MinerU's INCLUSIVE ``end_page_id``.

    Every page range in this module is half-open, ``[start, end)``, the way
    Python slices are. MinerU's ``start_page_id``/``end_page_id`` are both
    inclusive. Passing ``end`` through unconverted asks for one page too many,
    and since ``mineru_page_batch`` defaults to 1 the batches are ``[0,1)``,
    ``[1,2)``, ``[2,3)``... so each also extracted its successor's first page:
    every page but the first and last came back twice, and both copies were
    concatenated into the document text.

    Measured on a 19-page paper (catalog 1.12.128) before this existed:
    270,171 chars against 136,341 for the same PDF in a single batch (1.98x),
    160 of 373 substantial blocks redundant, and MinerU's own formula count
    exactly doubled, 844 against 422. The duplicate text was chunked and
    embedded like any other. It did not corrupt storage, because identical
    chunk text collapses to one T3 row on the ``(tenant_id, collection,
    chash)`` primary key — which is why it surfaced as a manifest-versus-T3
    count divergence rather than as an error.

    ``None`` means "to the end of the document" and keeps the sentinel both
    call sites already used. This is a named function rather than a ``- 1`` at
    each call site because there are two of them, in different methods (the
    server path and the subprocess path); the next caller should not have to
    rediscover which convention it is holding.
    """
    if end is None:
        return 99999
    # Never emit -1, which MinerU reads as "to the last page". An empty range
    # is refused earlier, by _mineru_check_range; this clamp is not the guard.
    return max(0, end - 1)


def _mineru_check_range(start: int, end: int | None, total_pages: int | None) -> None:
    """Refuse a page range MinerU would silently widen to the whole document.

    MinerU builds ``range(start_page_id, end_page_id + 1)`` after clamping
    ``end_page_id`` to the last page, and passes the result to pypdfium2's
    ``import_pages``. An EMPTY list there imports every page (a NULL array
    with count 0 to ``FPDF_ImportPagesByIndex``). So an empty half-open batch
    such as ``[5, 5)``, or a ``start`` at or past the last page, does not
    extract nothing: it extracts the entire PDF, and the caller appends that
    as if it were one batch. ``ValueError`` rather than ``RuntimeError`` so
    the bisect ladder in ``_extract_with_mineru`` does not treat it as an OOM
    and retry. ``total_pages`` is ``None`` when the caller did not supply it;
    the empty-range check still applies.
    """
    if start < 0:
        raise ValueError(f"negative MinerU start page {start}")
    if end is not None and end <= start:
        raise ValueError(
            f"empty MinerU page range [{start}, {end}); MinerU would extract "
            f"the whole document for it"
        )
    if total_pages == 0:
        raise ValueError("zero-page document; there is nothing for MinerU to extract")
    if total_pages is not None and start >= total_pages:
        raise ValueError(
            f"MinerU start page {start} is past the last page of a "
            f"{total_pages}-page document; MinerU would extract the whole "
            f"document for it"
        )


def _normalize_page_text(raw: str) -> str:
    """The whitespace normalisation one page's PyMuPDF text gets (shared by every PyMuPDF path)."""
    page_text = re.sub(r" +", " ", raw)
    page_text = re.sub(r"\n{3,}", "\n\n", page_text)
    page_text = "\n".join(line.rstrip() for line in page_text.split("\n")).strip()
    return _normalize_whitespace_edge_cases(page_text)


def _pdf_info_fields(info: dict | None) -> dict[str, str]:
    """The ``pdf_*`` metadata keys, from a PyMuPDF ``doc.metadata`` dict (empty when it has none)."""
    info = info or {}
    return {
        "pdf_title": str(info.get("title") or ""),
        "pdf_author": str(info.get("author") or ""),
        "pdf_subject": str(info.get("subject") or ""),
        "pdf_keywords": str(info.get("keywords") or ""),
        "pdf_creator": str(info.get("creator") or ""),
        "pdf_producer": str(info.get("producer") or ""),
        "pdf_creation_date": str(info.get("creationDate") or ""),
        "pdf_mod_date": str(info.get("modDate") or ""),
    }


def _read_pdf_info(pdf_path: Path) -> dict[str, str]:
    """A PDF's own title, author and the rest of its document information, through PyMuPDF.

    The MinerU result carries these so a document's title chain starts at the title the PDF
    declares (then its first heading, then its file name) and ``source_author`` is the PDF's
    author, as they are for the text-only extractor. Opening the file only reads its
    information dictionary. A file PyMuPDF cannot open gives empty fields: MinerU has the text,
    and the metadata is the one thing this adds.
    """
    try:
        import pymupdf  # lazy  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

        with pymupdf.open(pdf_path) as doc:
            return _pdf_info_fields(doc.metadata)
    except Exception as exc:  # noqa: BLE001 — an unreadable information dictionary must not fail a document MinerU read
        _log.debug("pdf_info_unreadable", path=str(pdf_path), error=f"{type(exc).__name__}: {exc}")
        return _pdf_info_fields(None)


def _pymupdf_page_text(page: Any) -> str:
    """One page's text layer through PyMuPDF, normalised (RDR-237 rung 3).

    ``get_text(sort=True)`` and the whitespace normalisation, for a single page;
    the explicit ``pymupdf`` extractor calls it for every page. ``""`` when the
    page has no text layer.
    """
    return _normalize_page_text(page.get_text(sort=True))


def _pymupdf_page_text_at(pdf_path: Path, page_index: int) -> str:
    """:func:`_pymupdf_page_text` for the 0-based *page_index* of the PDF at *pdf_path*."""
    import pymupdf  # lazy  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

    with pymupdf.open(pdf_path) as doc:
        return _pymupdf_page_text(doc[page_index])


def _normalize_whitespace_edge_cases(text: str) -> str:
    """Normalize whitespace variants not covered by basic normalization.

    - Replace tab characters with a single space.
    - Collapse Unicode non-breaking and exotic whitespace to a single space.
    - Collapse 4+ consecutive newlines to three (preserving intentional breaks).
    """
    text = text.replace("\t", " ")
    text = re.sub(r"[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


# Known operator names that MinerU/UniMERNet may emit as spaced single chars
# inside \operatorname{...} or \operatorname*{...} -- e.g. ``{ m a x }`` -> max.
# The rejoin is scoped to this allowlist so genuinely separate tokens are not
# silently merged via the explicit rejoin path.  Unknown content still has its
# surrounding whitespace collapsed by Rule 3 (all-whitespace strip within formula
# context).
_KNOWN_OPERATOR_NAMES: frozenset[str] = frozenset({
    "arg", "argmax", "argmin", "cos", "deg", "det", "diag",
    "exp", "inf", "lim", "ln", "log",
    "max", "min", "rank", "sign", "sin", "softmax", "sup", "tan", "tr",
})


def normalize_latex_spacing(s: str) -> str:
    """Normalize MinerU/UniMERNet spaced-token LaTeX formula string.

    MinerU/UniMERNet emits formula LaTeX with spurious whitespace between
    commands and their tokens -- e.g. ``\\operatorname* { m a x }``,
    ``\\mathbf { s }``, ``Q _ { \\phi }``.  This normalizer collapses those
    spaces so stored chunks render correctly.

    Conservative rules (closes #1049):
    - ``{ \\bf X }`` -> ``\\mathbf{X}``
    - ``\\operatorname*{ m a x }`` / ``\\operatorname{ m i n }`` -- rejoin
      spaced single-char tokens when the joined result is in
      ``_KNOWN_OPERATOR_NAMES`` (e.g. ``max``, ``min``, ``argmax``).
      Unknown operatorname content is left to the whitespace-collapse step.
    - Collapse all remaining whitespace within the formula.
    - ``\\text{...}`` groups are protected: internal spaces are preserved.

    Idempotent: running twice produces the same result as running once.

    Designed for formula strings, not prose.  At the wiring level
    (``_normalize_mineru_latex``) this is called only within ``$...$`` and
    ``$$...$$`` delimiters so prose chunks are never touched.
    """
    # Rule 1: { \\bf token } -> \\mathbf{token}
    # Handles MinerU's {\\bf X} legacy-font group notation.
    s = re.sub(r"\{\s*\\bf\s+(\S+)\s*\}", r"\\mathbf{\1}", s)

    # Rule 2: \\operatorname*{ m a x } -> \\operatorname*{max}
    # Rejoin scoped to \\operatorname / \\operatorname* with an allowlist so that
    # genuinely separate single-char tokens are not silently merged.
    def _rejoin_opname(m: re.Match) -> str:
        cmd = m.group(1)
        content = m.group(2).strip()
        parts = content.split()
        if parts and all(len(p) == 1 for p in parts):
            joined = "".join(parts)
            if joined in _KNOWN_OPERATOR_NAMES:
                return f"{cmd}{{{joined}}}"
        return m.group(0)

    s = re.sub(r"(\\operatorname\*?)\s*\{([^}]+)\}", _rejoin_opname, s)

    # Protect \text{...} groups: save them as placeholders so whitespace
    # stripping below does not collapse spaces inside \text{some words}.
    _placeholders: list[str] = []

    def _save_text(m: re.Match) -> str:
        _placeholders.append(m.group(0))
        return f"\x00T{len(_placeholders) - 1}\x00"

    s = re.sub(r"\\text\{[^}]*\}", _save_text, s)

    # Rule 3: collapse all whitespace in the formula string, except the one
    # space that terminates a control word before a letter (nexus-pwlqq):
    # TeX reads ``\alpha A`` as two tokens and ``\alphaA`` as one undefined
    # macro. A control symbol such as ``\\`` has no letters, so it never
    # matches. Safe because this function is only called on formula content.
    s = re.sub(r"(\\[A-Za-z]+)\s+(?=[A-Za-z])", "\\1\x01", s)
    s = re.sub(r"\s+", "", s)
    s = s.replace("\x01", " ")

    # Restore \text{...} groups with their original internal spacing.
    for i, t in enumerate(_placeholders):
        s = s.replace(f"\x00T{i}\x00", t)

    return s


def _normalize_mineru_latex(md: str) -> str:
    """Apply ``normalize_latex_spacing`` within LaTeX formula blocks in markdown.

    Scopes the normalizer to ``$$...$$`` (display math) and ``$...$`` (inline
    math) delimiters so that prose text is never modified.  Idempotent.

    Called from ``PDFExtractor._extract_with_mineru`` on each per-page
    markdown fragment (batch loop and OOM-retry path) before the length
    is measured for ``per_page_lengths``/``page_boundaries``, so the
    stored offsets are consistent with the normalized text.  Existing
    indexed chunks need a re-index to pick up clean LaTeX.
    """
    # Display math first, REPLACED BY A PLACEHOLDER rather than left in place.
    #
    # nexus-gtltb: the previous version substituted the display blocks but kept
    # their `$$` delimiters in the string, then ran the inline pass over the
    # result. `\$([^$]+?)\$` cannot match the empty string between a `$$` pair,
    # so it began matching ONE `$` INTO each pair — consuming a delimiter and
    # desyncing every subsequent open/close on the page. From the first display
    # block onward the spans treated as "math" were the PROSE BETWEEN formulas,
    # and math spans get `re.sub(r"\s+", "", s)` applied. The comment claiming
    # "[^$] avoids matching across $$ boundaries" was the exact inverse of what
    # the expression does.
    #
    # Measured on tests/fixtures/bft-to-smr.pdf: 918 chars removed, 592 of them
    # word-spaces deleted from running prose, plus \n\n breaks and `##`
    # headings — so the chunker lost its boundary signals too. The inversion ran
    # both ways: eight real inline spans kept the spurious spaces #1049 exists
    # to remove, while the prose around them lost its real ones.
    #
    # Placeholders are the same discipline `normalize_latex_spacing` already
    # uses for `\text{...}`, one level down.
    #
    # nexus-cfy5k: escaped dollars must be withdrawn from the string BEFORE
    # either delimiter pass, for the same reason. MinerU writes literal currency
    # as `\$`, and neither pass honours the backslash, so a PAIR of them bracketed
    # the prose between as if it were an inline formula — `contracts above
    # \$1M.Iftheenvironmentisarelationaldatabase...`. A lone `\$` has no partner
    # and is inert, which is why this hid behind documents that mention money
    # exactly once and survived the gtltb fix.
    #
    # The match keeps backslash parity: `\\$x$` is an escaped backslash followed
    # by a REAL delimiter, so `(?:\\\\)*` consumes complete pairs first and the
    # lookbehind stops the scan from starting mid-pair. Protecting a genuine
    # delimiter would resurrect the desync this is here to prevent.
    _escaped: list[str] = []

    def _save_escaped(m: re.Match) -> str:
        _escaped.append(m.group(0))
        return f"\x00E{len(_escaped) - 1}\x00"

    md = re.sub(r"(?<!\\)(?:\\\\)*\\\$", _save_escaped, md)

    _display: list[str] = []

    def _save_display(m: re.Match) -> str:
        _display.append(f"$${normalize_latex_spacing(m.group(1))}$$")
        return f"\x00D{len(_display) - 1}\x00"

    md = re.sub(r"\$\$(.*?)\$\$", _save_display, md, flags=re.DOTALL)

    # Inline math. NOW `[^$]` genuinely cannot cross a display boundary, because
    # no display delimiter remains in the string to be re-entered.
    md = re.sub(
        r"\$([^$]+?)\$",
        lambda m: f"${normalize_latex_spacing(m.group(1))}$",
        md,
    )

    # Restore display blocks, already normalized above.
    for i, block in enumerate(_display):
        md = md.replace(f"\x00D{i}\x00", block)

    # Restore escaped dollars verbatim — they were never math, so unlike the
    # display blocks there is nothing to normalize on the way back.
    for i, esc in enumerate(_escaped):
        md = md.replace(f"\x00E{i}\x00", esc)
    return md


_MINERU_FONT_TAG_RE = re.compile(r"</?su[bp]\s*>", re.IGNORECASE)


def _unwrap_mineru_font_tags(md: str) -> str:
    """Drop MinerU's HTML ``<sub>``/``<sup>`` tags, keeping their text verbatim.

    MinerU renders small-caps headings, run-in paragraph heads, and figure/
    table captions (ACM/arXiv templates) as letter-interleaved subscript
    spans — ``U<sub>n</sub>ifi<sub>e</sub>d C<sub>os</sub>t`` for
    "Unified Cost" — because the small-cap glyphs sit below the line's
    nominal font size. Measured on papers/2512.11001.pdf (2026-08-19):
    30/70 chunks and 17/35 headings carried the tags, so heading retrieval
    embedded word fragments instead of words.

    MinerU emits real math as LaTeX (``$o_{1}$``), so HTML sub/sup in its
    markdown are a font-size heuristic, not semantics; unwrapping costs at
    most a chemical-formula subscript (``CO<sub>2</sub>`` -> ``CO2``) and
    restores whole words everywhere else. Only the ``sub``/``sup`` tag
    names match — ``<subtle>`` or ``<b>`` are untouched. Idempotent.

    Called from ``PDFExtractor._extract_with_mineru`` per page, next to
    ``_normalize_mineru_latex``, before lengths are measured. Existing
    indexed chunks need a re-index to pick up the clean text.
    """
    return _MINERU_FONT_TAG_RE.sub("", md)


_MD_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_VISUAL_LABEL_RE = re.compile(r"^\s*((?:Table|Figure|Fig\.?)\s*[A-Za-z]?\d+[a-z]?|(?:Table|Figure)\s+[IVXLC]+)\b")

#: The FIGURE-only shape of the caption-adjacency label (round-2 critique
#: on nexus-9zly6). Deliberately excludes "Table": a bare "![...]"
#: reference is presented to the reader as an image, so a
#: nearby "Table N" caption must never relabel it -- a wrong specific
#: label ("Table 9 not extracted...") is worse than the generic marker.
#: "Chart" is included because MinerU captions use it for plots
#: that are not literally titled "Figure".
_FIGURE_CAPTION_RE = re.compile(
    r"^\s*((?:Figure|Fig\.?|Chart)\s*[A-Za-z]?\d+[a-z]?|(?:Figure|Chart)\s+[IVXLC]+)\b"
)

#: A visual reference with nothing but whitespace after it, used to detect
#: "this reference is immediately preceded by ANOTHER one" -- see
#: :func:`_caption_label_after`.
_TRAILING_VISUAL_REF_RE = re.compile(r"!\[[^\]]*\]\([^)\s]+\)\s*$")

#: How far past a marker's position to look for an adjacent caption line
#: (nexus-9zly6 GAP 1/2). Generous enough for a caption on the very next
#: non-blank line without scanning the rest of the page.
_CAPTION_LOOKAHEAD_CHARS = 200

#: How far back from a reference to look for a preceding, uncaptioned
#: visual reference (round-2 critique's adjacency guard).
_PRECEDING_REF_LOOKBACK_CHARS = 200


def _visual_label(entry: dict, kind: str) -> str:
    """``"Table 6"`` / ``"Figure 5"`` from the entry's first caption, else the bare kind."""
    captions = entry.get("table_caption") or entry.get("image_caption") or []
    for caption in captions:
        m = _VISUAL_LABEL_RE.match(caption or "")
        if m:
            return m.group(1).strip()
    return kind


def _caption_label_after(text: str, start: int, end: int) -> str | None:
    """The FIGURE label (``"Fig. 2"``, ``"Chart 4"``) from the caption line
    immediately following *end* in *text*, or ``None``.

    nexus-9zly6 GAP 1: when a content_list lookup misses, the label the
    query-lands-on-the-gap contract in :func:`_mark_unextracted_visuals`
    depends on would otherwise be lost. MinerU puts an unrecognised visual's
    caption directly under its own reference in the page markdown, so the
    label usually survives in the text even when the structured lookup does
    not.

    Two guards keep a WRONG specific label from ever being worse than the
    generic marker (round-2 critique):

    - *start* is the reference's own match start. If the text immediately
      before it (skipping only whitespace) is ANOTHER visual reference,
      this one is part of an uncaptioned run -- e.g. two images stacked
      before their captions -- and any caption that follows belongs to
      that ambiguity, not unambiguously to *this* reference. Refuses
      outright rather than guess.
    - Only :data:`_FIGURE_CAPTION_RE` is tried, never the Table-inclusive
      :data:`_VISUAL_LABEL_RE` -- a "Table N" caption must never label one
      of these bare references, which are presented to the reader as
      images.
    """
    before = text[max(0, start - _PRECEDING_REF_LOOKBACK_CHARS) : start]
    if _TRAILING_VISUAL_REF_RE.search(before):
        return None
    tail = text[end : end + _CAPTION_LOOKAHEAD_CHARS].lstrip("\r\n \t")
    first_line = tail.split("\n", 1)[0]
    m = _FIGURE_CAPTION_RE.match(first_line)
    return m.group(1).strip() if m else None


def _generic_visual_marker(label: str | None) -> str:
    """The bracketed marker for a visual with no content_list entry: the
    caption-derived *label* (always figure-shaped -- see
    :func:`_caption_label_after`) in the same shape ``_repl`` below would
    have produced from an "image" content_list entry, or the bare generic
    marker when no label could be derived at all."""
    if label is None:
        return "[image not indexed as text]"
    return f"[{label} is an image; not indexed as text]"


def _marker_for_entry(entry: dict) -> str:
    """The bracketed marker for a content_list *entry*, by its own type."""
    match entry.get("type"):
        case "table":
            return f"[{_visual_label(entry, 'Table')} not extracted as text; values not indexed]"
        case "image":
            return f"[{_visual_label(entry, 'Figure')} is an image; not indexed as text]"
        case _:
            return "[image not indexed as text]"


def _mark_unextracted_visuals(md: str, content_list: list[dict]) -> str:
    """Replace MinerU image references with a text marker that names the hole.

    nexus-jd8fi: MinerU renders every figure, and every table it did not
    recognise as text, as ``![](images/<sha>.jpg)``. Indexed as-is that is
    a content-free line: a retrieval-only reader sees running prose with no
    signal that a table or figure sat there and reconstructs the paper
    around the gap. The Pangram 4 report indexed with tables off carried
    zero tabular values and nothing said so.

    Each reference becomes a bracketed marker anchored to the visual's own
    caption label, from the ``content_list`` entry whose ``img_path``
    matches: ``[Table 6 not extracted as text; values not indexed]`` for a
    table without a ``table_body``, ``[Figure 5 is an image; not indexed as
    text]`` for a figure. A reference with no matching ``content_list``
    entry falls back to the caption line immediately under the reference in
    *md* itself (:func:`_caption_label_after`), and only when that also
    finds nothing gets the bare generic ``[image not indexed as text]``.
    Tables MinerU did extract are already HTML in *md* and carry no image
    reference, so they pass through.

    A ``content_list`` entry whose ``img_path`` never appears as a ``![]``
    reference anywhere in *md* at all (MinerU's markdown and its
    content_list are two independently produced outputs, so this is
    reachable, not hypothetical) gets a marker appended at the end of the
    text -- nexus-9zly6's coverage of what used to be this function's early
    ``if not md or "![" not in md: return md`` return, under which such an
    entry vanished with no marker whatsoever.

    The marker is text the embedder sees, so a query for "Table 6" lands on
    the chunk that says the values are absent instead of on nothing.
    """
    if not md and not content_list:
        return md
    by_path: dict[str, dict] = {}
    for entry in content_list:
        img_path = entry.get("img_path")
        if img_path:
            by_path[img_path] = entry
            by_path[img_path.rsplit("/", 1)[-1]] = entry

    matched_paths: set[str] = set()

    def _repl(m: re.Match) -> str:
        ref = m.group(1)
        entry = by_path.get(ref) or by_path.get(ref.rsplit("/", 1)[-1])
        if entry is None:
            return _generic_visual_marker(_caption_label_after(md, m.start(), m.end()))
        matched_paths.add(entry.get("img_path", ""))
        return _marker_for_entry(entry)

    out = _MD_IMAGE_REF_RE.sub(_repl, md) if md and "![" in md else (md or "")

    trailing: list[str] = []
    seen_paths: set[str] = set()
    for entry in content_list:
        # nexus-9zly6 round-2 critique: scoped to "table"/"image" entries
        # ONLY -- an "equation" (or any other) entry never carries an
        # img_path either, and sweeping it in here would mislabel a
        # properly-extracted formula as an unindexed image.
        if entry.get("type") not in ("table", "image"):
            continue
        img_path = entry.get("img_path")
        if img_path and (img_path in matched_paths or img_path in seen_paths):
            continue
        if entry.get("type") == "table" and entry.get("table_body"):
            # Already extracted as HTML -- by design carries no "![...]"
            # reference anywhere (this function's own docstring), so its
            # absence from md is the expected shape, not an orphan.
            continue
        # An entry with no img_path can never match a "![...]" reference
        # by construction, so it is unconditionally an orphan -- every one
        # gets its own marker, never deduped against img_path (there is
        # none to dedupe on).
        if img_path:
            seen_paths.add(img_path)
        trailing.append(_marker_for_entry(entry))
    if trailing:
        out = f"{out}\n\n" + "\n\n".join(trailing) if out else "\n\n".join(trailing)
    return out


_TABLE_BLOCK_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.S)
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<(t[dh])\b([^>]*)>(.*?)</\1>", re.S)
_COLSPAN_RE = re.compile(r"colspan\s*=\s*\"?(\d+)\"?", re.I)
_TABLE_CAPTION_RE = re.compile(r"table\s+([IVXLC]+|\d+)\b", re.I)
#: How far back from a table to look for its caption label.
_CAPTION_LOOKBACK_CHARS = 400
#: Opening of the suspect-table marker. Also the idempotency probe.
_MARKER_PREFIX = "[table structure suspect"


def _row_widths(block: str) -> list[list[str]]:
    """Each row's cell texts, with a ``colspan``-ed cell repeated so a row's
    length is its effective column count."""
    rows: list[list[str]] = []
    for row in _TR_RE.findall(block):
        cells: list[str] = []
        for _tag, attrs, body in _TD_RE.findall(row):
            span = _COLSPAN_RE.search(attrs)
            width = max(1, int(span.group(1))) if span else 1
            cells.extend([body.strip()] * width)
        rows.append(cells)
    return rows


#: An inline math span containing an ESCAPED underscore. Real LaTeX math
#: never needs to escape ``_`` -- a bare underscore already means
#: subscript inside ``$...$`` -- so ``\_`` there is a near-diagnostic
#: signal that the span started life as a code identifier (MinerU's
#: formula-recognition model classifying a monospace table cell as math;
#: nexus-8eg4w) rather than as a mathematical expression.
_ESCAPED_UNDERSCORE_MATH_RE = re.compile(r"\$[^$]*\\_[^$]*\$")


def _code_like_math_defects(block: str) -> list[dict]:
    """Table cells whose rendered math is more likely mis-recognized code
    than genuine LaTeX (nexus-8eg4w).

    Measured on the KnowFeat paper's TABLE V Code row: MinerU's formula
    model classified plain Python (``mode_counts['fan_out'] / n``) as
    LaTeX math and rendered it as one, escaping every identifier
    underscore along the way -- ``mode\\_counts``, ``fan\\_out``. Contrast
    that keeps this narrow: the SAME extractor renders genuine numbered
    pseudocode (this paper's Algorithm 1) correctly, so the signal has to
    single out an actual math/code confusion, not fire on ordinary
    formulas that legitimately live in a table.

    This is a DETECTION, not a fix. The original cell text is gone the
    moment MinerU's model replaced it -- there is nothing here to
    reconstruct from, only something to name as suspect, the same
    non-rewrite discipline :func:`mark_misshapen_tables`'s docstring
    states for :func:`_table_shape_defects`.
    """
    cells_flagged = sum(
        1
        for row in _TR_RE.findall(block)
        for _tag, _attrs, body in _TD_RE.findall(row)
        if _ESCAPED_UNDERSCORE_MATH_RE.search(body)
    )
    if not cells_flagged:
        return []
    return [{"kind": "code_like_math", "cells": cells_flagged}]


def _table_shape_defects(block: str) -> list[dict]:
    """Structural and content disagreements inside one captured table.

    Three signals. The first two are shape mechanisms measured on the
    KnowFeat paper, different from each other and the first cannot see
    the second; the third (:func:`_code_like_math_defects`) is a content
    signal that needs no row structure to fire, so it runs even for a
    table too small for the other two to say anything:

    ``header_column_mismatch``
        The header row's effective width disagrees with the modal data-row
        width. This is the TABLE I collapse: a header line rendered as one
        ``colspan="10"`` cell over rows carrying nine, which leaves every
        value unattributable to a column.
    ``empty_row_label``
        A data row whose leading cell is empty in a table whose other rows
        are labelled. This is the TABLE V row-label merge, where two labels
        fused into one cell and shifted every later row up by one; cell
        counts stay constant, so only the orphaned row betrays it.
    ``code_like_math``
        A cell's rendered math looks like an escaped code identifier
        rather than genuine LaTeX. See :func:`_code_like_math_defects`.
    """
    defects: list[dict] = list(_code_like_math_defects(block))
    rows = _row_widths(block)
    if len(rows) < 2:
        # A header with no data rows has nothing to disagree with it.
        return defects
    header, data = rows[0], rows[1:]
    widths = [len(r) for r in data if r]
    if widths:
        modal = max(set(widths), key=widths.count)
        if len(header) != modal:
            defects.append({
                "kind": "header_column_mismatch",
                "header_columns": len(header),
                "row_columns": modal,
            })
    labelled = sum(1 for r in data if r and r[0])
    unlabelled = [i for i, r in enumerate(data, start=1) if r and not r[0]]
    if unlabelled and labelled >= 2:
        defects.append({"kind": "empty_row_label", "rows": unlabelled})
    return defects


def _table_caption_label(text: str, table_start: int) -> str:
    """The table's own caption label from the text just before it, else ``""``.

    The LAST numbered match in the window, returned as matched. This used to
    locate the label with ``window.lower().rfind("table")``, which can land
    on a different occurrence than the regex matched: a bare, unnumbered
    "table" mention after the real caption ("in the related work table
    below") produced a label of ``"table b"`` (code-review round 1).
    """
    window = text[max(0, table_start - _CAPTION_LOOKBACK_CHARS):table_start]
    matches = list(_TABLE_CAPTION_RE.finditer(window))
    return matches[-1].group(0).strip() if matches else ""


def mark_misshapen_tables(text: str) -> tuple[str, list[dict]]:
    """Prefix every structurally suspect table with a marker naming the
    disagreement, and return the defects found (nexus-stkek).

    MinerU's table HTML was stored verbatim and checked by nothing: no HTML
    parser is imported anywhere in ``src/nexus``, and the post-extraction
    quality gate reads whitespace and token statistics only. On the KnowFeat
    paper that let TABLE I into the store stating the OPPOSITE of the source
    — three of its seven property rows read as entirely blank, "Provenance
    track." among them, which is the one property the paper exists to claim.

    This marks rather than refuses, and deliberately does not join the
    quality gate. One malformed table in an otherwise clean 11-page
    extraction is not garbage text; failing the document over it would be
    overridden by habit, and a gate that is always overridden protects
    nothing. The marker is text the embedder sees, so a query that surfaces
    the table surfaces the warning with it — the same reasoning as
    :func:`_mark_unextracted_visuals`, whose idiom this follows.

    The table's own HTML is never edited. A misaligned table still holds
    values a reader may want, and silently rewriting them would be a second
    unverifiable transformation on top of the first.

    Marking is NOT guarded against running twice. It was, briefly, by a
    ``_MARKER_PREFIX in block`` substring test justified as protecting a
    ``--force`` re-index from a second marker. Review round 2 falsified both
    halves: no reachable production path feeds already-marked text back
    through here, because MinerU writes to a fresh tempdir that is rmtree'd
    on every call, and the guard's one demonstrated effect was a FALSE
    NEGATIVE — a table with a genuine ``header_column_mismatch`` whose own
    cell prose merely mentioned the marker string was silently skipped, and
    the run summary under-reported with it. A duplicate marker costs a line
    of text; a silently unflagged table costs the thing this function
    exists for.
    """
    if not text or "<table" not in text:
        return text, []
    found: list[dict] = []

    def _repl(m: re.Match) -> str:
        block = m.group(0)
        defects = _table_shape_defects(block)
        if not defects:
            return block
        reasons: list[str] = []
        for d in defects:
            match d["kind"]:
                case "header_column_mismatch":
                    reasons.append(
                        f"header describes {d['header_columns']} columns, "
                        f"rows carry {d['row_columns']}"
                    )
                case "empty_row_label":
                    n = len(d["rows"])
                    reasons.append(f"{n} row{'s' if n != 1 else ''} with no label")
                case "code_like_math":
                    n = d["cells"]
                    reasons.append(
                        f"{n} cell{'s' if n != 1 else ''} may be code "
                        "mis-recognized as math"
                    )
        label = _table_caption_label(text, m.start())
        found.extend({**d, "label": label} for d in defects)
        named = f" ({label})" if label else ""
        marker = (
            f"{_MARKER_PREFIX}{named}: {'; '.join(reasons)}; "
            "values may be misaligned or unreliable]"
        )
        # Just INSIDE the opening tag, not on a line before it. That span is
        # what PDFChunker's _table_header re-injects into every continuation
        # chunk, so every chunk of a split table carries the warning. On its
        # own line before <table>, a table big enough to trip the
        # table_break branch left the marker in the PRECEDING chunk and in
        # none of the table's own (substantive-critic round 1).
        open_end = block.find(">") + 1
        return f"{block[:open_end]}{marker}{block[open_end:]}"

    return _TABLE_BLOCK_RE.sub(_repl, text), found


@dataclass
class ExtractionResult:
    """Result of PDF text extraction."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Post-extraction quality gate (nexus-wi1uv) ──────────────────────────────
#
# Motivating incident (nexus-5xn3k, 2026-07-31): an earlier extractor (since
# removed) completed on a real 69-page paper and reported success while
# producing SPACE-STRIPPED text — "istheasetofthe", "aspartofasequenceofinput"
# — plus heavy raw-LaTeX noise. Nothing in the pipeline flagged it; the
# garbage was indexed, embedded, and paid for. That extractor was documented as
# the SAFE recovery when MinerU OOM-fails on formula-dense pages. A loud MinerU
# failure is strictly safer than a silent quality collapse that then looks like
# success.
#
# Three cheap, dependency-free signals over the raw extracted text, computed
# once per document before chunking:
#   - whitespace_ratio  — fraction of characters that are whitespace. Space
#     stripping collapses this toward zero (words run together).
#   - mean_token_len     — mean length of whitespace-split tokens. Space
#     stripping merges many words into a few enormous "tokens"; explodes.
#   - long_token_fraction — fraction of tokens longer than
#     ``_LONG_TOKEN_CHARS`` characters. A second, more robust view of the
#     same run-together-words signature (robust to a doc with one or two
#     genuinely long tokens skewing the mean).
# All three are computed over `str.split()` tokens (whitespace-delimited) —
# no lexicon, no network, no new dependency, per the bead's dependency-free
# constraint.
#
# CALIBRATION (2026-08-06, round 2 — revised after code-review-expert +
# substantive-critic independently found the round-1 calibration unsafe
# on three axes; see nx memory get -p nexus -t nexus-wi1uv-implementation
# for the full evidence table, both rounds):
#
#   HEALTHY corpus (18 real/representative samples):
#     - 5 fixture PDFs (tests/fixtures/{bft-to-smr,tc-sql,
#       distributed-bloom-filter,virgo,fireflies-tocs}.pdf), extracted with
#       BOTH the layout extractor of the time and PyMuPDF (10 whole-document samples).
#     - 2 dense-notation regions carved out of those same fixtures
#       (tc-sql's IES(FO) relational-calculus derivation; virgo's
#       cryptographic-commitment section) — real extractor output, not
#       synthetic, chosen specifically to probe the "dense math/formula
#       notation" false-positive hazard the bead names.
#     - 1 sample hydrated from T3 (knowledge__dt-papers): the Appendix H
#       attention-mechanism proofs from "Zoology" — nexus-5xn3k's OWN
#       incident paper, post-repair (a legitimate, heavy-LaTeX MinerU
#       extraction) — plus prose from knowledge__probe-mineru. This is the
#       exact document that motivated this bead, at its most formula-dense.
#     - 1 short (130-char) real-prose sample — round-2: the critic proved
#       a *short* extract needs the SAME protection as a long one; this is
#       its healthy control.
#     - 2 code-identifier-dense passages (systems-paper-with-algorithm-
#       listing prose, and pseudocode/algorithm-listing style, both built
#       from this repo's own real identifiers) — round-2: the critic's
#       Significant finding that dense identifiers can trip
#       long_token_fraction on LEGITIMATE text, the exact "dense-notation"
#       hazard class the bead itself named but round-1 only tested via
#       LaTeX.
#   Measured ranges (round-2): whitespace_ratio 0.0799-0.2395;
#   mean_token_len 4.38-13.12; long_token_fraction 0.0000-0.25. The
#   code-identifier samples set the long_token_fraction high (0.20-0.25)
#   and pulled whitespace_ratio down to its new low (0.0799) — round-1's
#   dense-LaTeX-only calibration had not exercised this axis at all.
#
#   NON-SPACED-SCRIPT (CJK) — round-2, substantive-critic Significant:
#   ``str.split()`` cannot segment Han/Hiragana/Katakana/Hangul text at
#   all (no inter-word ASCII spaces), so a real CJK document is
#   STRUCTURALLY IDENTICAL to the space-stripped-garbage signature on all
#   three signals (measured on a real Chinese-prose sample: whitespace_
#   ratio=0.0, mean_token_len=399, long_token_fraction=1.0 — i.e. it would
#   have failed unconditionally). Not a calibration-threshold problem —
#   the signals are simply not meaningful for these scripts. Detected via
#   :func:`_non_spaced_script_fraction` and SKIPPED (passed=True,
#   ``skipped_reason`` set, logged) rather than force-fit into a
#   threshold that cannot discriminate for this input class. See
#   :data:`_NON_SPACED_SCRIPT_FRACTION_THRESHOLD`.
#
#   GARBAGE corpus (14 samples): every healthy sample above with spaces
#   stripped per line (``line.replace(" ", "")`` — preserves line/paragraph
#   breaks, strips only intra-line word-boundary spaces), reproducing the
#   incident's reported signature verbatim (run this box's extractor over a
#   fixture, strip spaces, and the output reads exactly like
#   "istheasetofthe"). Includes the 130-char short sample stripped to 111
#   chars — round-2's boundary-behavior proof (see below). Measured
#   ranges: whitespace_ratio 0.0000-0.0218; mean_token_len 52.09-840.44 for
#   the >=20-token samples, mean_token_len=111.0 (n_tokens=1) for the short
#   one. long_token_fraction 0.3366-1.0 (code-identifier samples are NOT
#   in the garbage corpus — they are real legitimate text, calibrated as
#   healthy above).
#
# THRESHOLDS (round-2):
_WHITESPACE_RATIO_FLOOR = 0.05
_MEAN_TOKEN_LEN_CEILING = 20.0
# Round-2 (was 0.10): the critic reproduced a 0.42 long_token_fraction on
# synthetic code-identifier-dense text — a real false positive at the
# round-1 threshold. This repo's OWN identifiers, worked into realistic
# prose (not a worst-case "nothing but identifiers" adversarial sample),
# measured 0.20-0.25 (see HEALTHY corpus above) — comfortably under the
# revised 0.5 ceiling, itself still 2x above the critic's reported 0.42
# and ~20x above the real-dense-LaTeX healthy ceiling (0.0246). Raising
# this ceiling loses ZERO garbage-detection power: every garbage sample in
# the corpus is independently caught by whitespace_ratio AND/OR
# mean_token_len (verified — see the calibration script referenced in the
# T2 record), so long_token_fraction is corroborating, not load-bearing.
_LONG_TOKEN_FRACTION_CEILING = 0.5
_LONG_TOKEN_CHARS = 20
# Round-2 (substantive-critic Significant #4): the round-1 char-based
# floor (500 chars, auto-pass below it) reproduced the incident's own
# signature UNDETECTED at 169 chars — short garbage still yields >=1 real
# chunk, so "defer to the zero-chunks check downstream" was FALSE for
# this case, not just imprecise. Replaced with a TOKEN-count floor that
# applies ONLY to the two signals that need enough samples to be
# statistically meaningful (whitespace_ratio, long_token_fraction) — a
# single genuinely-long legitimate token (a URL, a DOI) can otherwise
# swing a ratio over few tokens by chance. mean_token_len does NOT wait
# for this floor: it is computed and compared whenever there is at least
# one token, because it is meaningful even at n=1 (a single 100+-char
# glued-together "word" is unambiguous regardless of how few tokens
# surround it) and its calibration margin (7x between the healthy ceiling
# 7.01 and the garbage floor 52.09, confirmed down to n_tokens=1 in the
# 169-char boundary case: mean_token_len=111.0) does not depend on a
# large sample. This is what makes the 169-char repro fail post-fix.
_MIN_TOKENS_FOR_RATIO_SIGNALS = 20

#: Non-spaced-script (CJK) codepoint ranges — round-2, substantive-critic
#: Significant #5. Han (+ CJK Ext-A), Hiragana, Katakana, Hangul: scripts
#: written without inter-word ASCII spaces, where str.split() cannot
#: segment tokens at all.
_NON_SPACED_SCRIPT_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7A3),   # Hangul Syllables
)

#: Fraction of non-whitespace characters that must fall in a non-spaced
#: script before the whitespace/token signals are skipped as unreliable.
#: A real Chinese-prose calibration sample measured ~1.0 (essentially
#: every non-whitespace char is Han); 0.3 leaves wide margin for a mixed
#: CJK+Latin document (e.g. English section headers in an otherwise-CJK
#: paper) while still catching a CJK-dominant one.
_NON_SPACED_SCRIPT_FRACTION_THRESHOLD = 0.3


def _non_spaced_script_fraction(text: str) -> float:
    """Fraction of non-whitespace characters in *text* belonging to a
    script conventionally written without inter-word spaces (CJK).

    Pure/cheap — a single pass over the text, no dependency. Returns 0.0
    for empty or all-whitespace input.
    """
    non_ws_count = 0
    hits = 0
    for c in text:
        if c.isspace():
            continue
        non_ws_count += 1
        cp = ord(c)
        if any(lo <= cp <= hi for lo, hi in _NON_SPACED_SCRIPT_RANGES):
            hits += 1
    return hits / non_ws_count if non_ws_count else 0.0


@dataclass
class ExtractionQualityReport:
    """Cheap post-extraction text-quality signals (nexus-wi1uv).

    See the module-level comment above :data:`_WHITESPACE_RATIO_FLOOR` for
    the calibration evidence behind the thresholds ``passed`` is computed
    from.
    """

    whitespace_ratio: float
    mean_token_len: float
    long_token_fraction: float
    n_tokens: int
    n_chars: int
    passed: bool
    failing_signals: list[str] = field(default_factory=list)
    #: Round-2: non-empty when the signals were skipped as unreliable for
    #: this input (currently: non-spaced-script/CJK dominance) rather than
    #: evaluated and passed. ``passed`` is always True when this is set —
    #: a capability-honest skip, never treated as a silent clean pass of
    #: an actually-evaluated document.
    skipped_reason: str = ""


def assess_extraction_quality(text: str) -> ExtractionQualityReport:
    """Compute the post-extraction quality signals for *text*.

    Pure function — no I/O, no dependency beyond the stdlib — so it is
    testable in isolation from any PDF backend. Called once per document on
    the full extracted text, before chunking.
    """
    n_chars = len(text)
    if n_chars == 0:
        # Nothing to judge; the separately-enforced "chunker produced zero
        # chunks despite non-empty text" check (doc_indexer._pdf_chunks)
        # covers genuinely empty extraction.
        return ExtractionQualityReport(
            whitespace_ratio=1.0, mean_token_len=0.0, long_token_fraction=0.0,
            n_tokens=0, n_chars=0, passed=True, failing_signals=[],
        )

    tokens = text.split()
    n_tokens = len(tokens)

    script_frac = _non_spaced_script_fraction(text)
    if script_frac >= _NON_SPACED_SCRIPT_FRACTION_THRESHOLD:
        # Round-2: whitespace/token signals are structurally meaningless
        # for a non-spaced script — skip rather than force-fit a
        # threshold, and say so (never a silent pass indistinguishable
        # from "evaluated and clean").
        return ExtractionQualityReport(
            whitespace_ratio=0.0, mean_token_len=float(n_chars), long_token_fraction=1.0,
            n_tokens=n_tokens, n_chars=n_chars, passed=True, failing_signals=[],
            skipped_reason=(
                f"non-spaced-script dominant ({script_frac:.0%} of non-whitespace "
                f"chars) — whitespace/token signals are not meaningful for this "
                f"script (e.g. CJK); skipped rather than evaluated"
            ),
        )

    whitespace_chars = sum(1 for c in text if c.isspace())
    whitespace_ratio = whitespace_chars / n_chars

    if n_tokens == 0:
        # Non-empty text with zero whitespace-delimited tokens is itself
        # the degenerate case this gate exists to catch (the entire
        # document collapsed into unbroken run-on text).
        mean_token_len = float(n_chars)
        long_token_fraction = 1.0
    else:
        token_lens = [len(t) for t in tokens]
        mean_token_len = sum(token_lens) / n_tokens
        long_token_fraction = sum(1 for tok_len in token_lens if tok_len > _LONG_TOKEN_CHARS) / n_tokens

    failing: list[str] = []
    # mean_token_len is UNCONDITIONAL (any n_tokens >= 1) — see the
    # round-2 comment above _MIN_TOKENS_FOR_RATIO_SIGNALS for why this is
    # the signal that must not wait for a sample-size floor.
    if n_tokens >= 1 and mean_token_len > _MEAN_TOKEN_LEN_CEILING:
        failing.append(
            f"mean_token_len={mean_token_len:.2f} > ceiling {_MEAN_TOKEN_LEN_CEILING}"
        )
    if n_tokens >= _MIN_TOKENS_FOR_RATIO_SIGNALS:
        if whitespace_ratio < _WHITESPACE_RATIO_FLOOR:
            failing.append(
                f"whitespace_ratio={whitespace_ratio:.4f} < floor {_WHITESPACE_RATIO_FLOOR}"
            )
        if long_token_fraction > _LONG_TOKEN_FRACTION_CEILING:
            failing.append(
                f"long_token_fraction={long_token_fraction:.4f} > ceiling {_LONG_TOKEN_FRACTION_CEILING}"
            )

    return ExtractionQualityReport(
        whitespace_ratio=whitespace_ratio,
        mean_token_len=mean_token_len,
        long_token_fraction=long_token_fraction,
        n_tokens=n_tokens,
        n_chars=n_chars,
        passed=not failing,
        failing_signals=failing,
    )


def _enforce_extraction_quality(
    result: "ExtractionResult", pdf_path: Path, *, allow_degraded: bool,
) -> None:
    """Gate *result* against :func:`assess_extraction_quality`.

    Raises :class:`~nexus.errors.ExtractionQualityError` (fail loud, per the
    module docstring above) unless *allow_degraded* is True, in which case
    the failure is logged at WARNING (loud in the run output, per the
    bead's "must mark the run output loudly when used" requirement) and
    extraction proceeds with the degraded text.

    Round-2: a ``skipped_reason`` (non-spaced-script/CJK dominance) always
    passes regardless of *allow_degraded* — logged at INFO (capability-
    honest note, not a warning or an error; the signals were never
    evaluated, so there is nothing to override).
    """
    report = assess_extraction_quality(result.text)
    result.metadata["quality_gate_passed"] = report.passed
    if report.skipped_reason:
        _log.info(
            "extraction_quality_gate_skipped",
            path=str(pdf_path),
            reason=report.skipped_reason,
        )
        return
    if report.passed:
        return

    reasons = "; ".join(report.failing_signals)
    method = result.metadata.get("extraction_method", "unknown")
    if allow_degraded:
        result.metadata["quality_gate_overridden"] = True
        _log.warning(
            "extraction_quality_degraded_override",
            path=str(pdf_path),
            extraction_method=method,
            failing_signals=report.failing_signals,
            whitespace_ratio=report.whitespace_ratio,
            mean_token_len=report.mean_token_len,
            long_token_fraction=report.long_token_fraction,
        )
        _progress(
            f"  WARNING: {pdf_path.name} failed the extraction quality gate "
            f"({reasons}) — indexing anyway (--allow-degraded-extraction)."
        )
        return

    _log.error(
        "extraction_quality_gate_failed",
        path=str(pdf_path),
        extraction_method=method,
        failing_signals=report.failing_signals,
        whitespace_ratio=report.whitespace_ratio,
        mean_token_len=report.mean_token_len,
        long_token_fraction=report.long_token_fraction,
    )
    if method == "pymupdf":
        # The text-only extractor needs no PDF stack and was chosen for that: suggesting the
        # stack's extractor first would send a user who cannot run it to a refusal.
        remedy = (
            "Remedy: if you have reviewed the extracted text and it is acceptable, rerun with "
            "`--allow-degraded-extraction` to index it anyway; `--extractor mineru` reads the "
            "page images and needs the PDF stack."
        )
    else:
        remedy = (
            "Remedy: if this document is legitimately dense/unusual and you have reviewed "
            "the extracted text, rerun with `--allow-degraded-extraction` to index it anyway; "
            "otherwise fix the PDF (re-export it, or OCR a scan) and index it again."
        )
    raise ExtractionQualityError(
        f"PDF {pdf_path.name} failed the post-extraction quality gate "
        f"(extraction_method={method}): {reasons}. This is the "
        f"space-stripped-garbage failure mode (nexus-wi1uv) — the extracted "
        f"text is likely unsearchable if indexed. {remedy}"
    )


class PDFExtractor:
    """Extract PDF text with MinerU (per-page fallback ladder) or, explicitly, PyMuPDF.

    MinerU is the default and the only layout- and math-aware extractor; a page it
    cannot read descends the RDR-237 ladder. ``extractor="pymupdf"`` reads every
    page's own text layer and needs none of the PDF stack.
    """

    def __init__(self) -> None:
        self._mineru_server_checked: bool = False
        self._mineru_server_up: bool = False
        # nexus-5ny9r: when the verdict was "no live server" (not skew), a
        # monotonic deadline after which the next call re-probes the
        # endpoint, so a server started mid-run is picked up instead of
        # every remaining page paying the per-page subprocess cost.
        self._mineru_server_recheck_after: float | None = None
        self._mineru_server_restarts: int = 0
        # RDR-148 Gap 5/6: set True by Gap 6 when an RLIMIT_AS memory ceiling is
        # applied to the worker, so the OOM classifier treats ANY non-zero exit
        # as a ceiling breach (a breach may surface as a plain non-zero exit, not
        # only SIGKILL / the sentinel). Default False until Gap 6 lands.
        self._mineru_ceiling_applied: bool = False
        # RDR-148 Gap 6: page count of the document currently being extracted,
        # set at the top of _extract_with_mineru so the subprocess path scales
        # the per-page timeout for the whole-doc (end is None) batch without
        # re-opening the PDF. None for direct subprocess callers.
        self._mineru_run_total_pages: int | None = None
        # RDR-237: the progress callback of the extraction in flight (set by extract()), called
        # through _beat() at the start of every attempt and during the two waits.
        self._heartbeat: Callable[[], None] | None = None
        # RDR-237: the pages of the consecutive rung-1 server requests that timed out, for the
        # restart rule (reset by any rung-1 server request that did not time out, and by a restart).
        self._server_timeout_pages: list[str] = []
        # Why the last _restart_broken_mineru_server call replaced nothing (nexus-tr1rj.25); ""
        # when it replaced the server or has not run.
        self._restart_skipped: str = ""
        # The pid of the server ANOTHER process had already started when the last restart took the
        # election (a concurrent trigger; this process stopped and spawned nothing); None otherwise
        # (nexus-tr1rj.26).
        self._restart_taken_over_by: int | None = None
        # The line a restart prints once it is decided (nexus-tr1rj.25); set only around the
        # timed-out-server restart, which knows the pages. None: nothing is announced.
        self._restart_announce: Callable[[], None] | None = None

    def _beat(self) -> None:
        """Call the progress callback, if the caller gave one (RDR-237).

        Called at the start of every MinerU attempt, outside the attempt's own ``try`` where it can
        be (so ``PipelineRunFenced`` and a cancel reach the caller), and from the two waits. What
        the callback raises is carried as :class:`_HeartbeatStop`, which no ``except Exception``
        along the way catches; ``extract`` re-raises the original.
        """
        heartbeat = self._heartbeat
        if heartbeat is None:
            return
        try:
            heartbeat()
        except Exception as exc:  # noqa: BLE001 — carried out whole as _HeartbeatStop, never read as a MinerU failure
            raise _HeartbeatStop(exc) from None

    def extract(
        self,
        pdf_path: Path,
        *,
        extractor: str = DEFAULT_EXTRACTOR,
        on_page: Callable[[int, str, dict], None] | None = None,
        allow_degraded: bool = False,
        on_heartbeat: Callable[[], None] | None = None,
    ) -> ExtractionResult:
        """Extract text from *pdf_path*. Returns ExtractionResult.

        *extractor* selects the backend:
        - ``"mineru"`` (the default) — MinerU, each page down the per-page ladder when
          its attempt fails.
        - ``"pymupdf"`` (RDR-237) — the explicit text-only extractor: every page's own
          text layer through PyMuPDF (the ladder's rung 3), no layout, tables or math. It
          needs none of the PDF stack, so it is the one choice that neither requires nor
          installs it, and it never starts or probes MinerU. Never chosen automatically.
        - The values older configs and scripts carry (``"auto"``, and the name of the
          extractor RDR-237 removed) are aliases for ``"mineru"``, with one notice per
          process (:func:`nexus.config.resolve_extractor`).

        *on_page* — optional streaming callback fired per extracted page (or
        per MinerU batch when ``mineru_page_batch > 1``):
        ``on_page(page_index, page_text, page_metadata)``.
        ``page_metadata`` contains ``"page_number"`` (1-based) and
        ``"text_length"``.

        *on_heartbeat* (RDR-237) — optional progress callback with no arguments, for a
        caller that owns a pipeline row. The MinerU path calls it at the start of every
        attempt (each server request, each subprocess run and each bisect step among
        them) and at least every 60 s during the two waits (server autostart, the
        restart's health wait), so the longest silent interval is one attempt, at most
        ``pdf.mineru_page_timeout_s`` x ``pdf.mineru_page_batch`` (checked at the start
        of a run: 240 s at most). The ladder holds some pages back from *on_page*, so
        progress comes from here, not from *on_page* alone. An exception it raises
        (``PipelineRunFenced``, a cancel) stops the extraction and reaches the caller
        as raised; it never moves a page down a rung.

        *allow_degraded* (nexus-wi1uv) bypasses the post-extraction quality
        gate (see :func:`assess_extraction_quality`). Every backend routes
        through this one method, so the gate applies uniformly to MinerU and
        PyMuPDF output — no extractor is special-cased. Default
        ``False``: a document whose extracted text trips the gate's
        thresholds (the space-stripped-garbage signature — see the
        function's calibration docstring) raises
        :class:`~nexus.errors.ExtractionQualityError` rather than being
        silently indexed as unsearchable garbage. Pass ``True`` to accept
        degraded output deliberately for a specific document (surfaced as
        ``--allow-degraded-extraction`` on the CLI).
        """
        # An unknown name raises ValueError here; an older name becomes "mineru" with the notice.
        extractor = resolve_extractor(extractor, source="the extractor argument")

        # nexus-2fyb code-review R1-I2: validate the path is readable before
        # dispatching. Without this, a directory or dangling symlink reaches
        # pymupdf/MinerU and produces an opaque internal error that leaks
        # library paths through the message.
        if not pdf_path.is_file():
            raise FileNotFoundError(
                f"PDF not found or not a regular file: {pdf_path}"
            )

        # nexus-2k66t.4.2: defense in depth. The CLI entry points call
        # pdf_stack.ensure() before they write anything; a caller that did not
        # (a library user, a future MCP tool) is refused here, by name, instead
        # of meeting an ImportError halfway through a document.
        from nexus import pdf_stack  # noqa: PLC0415 — deferred import

        pdf_stack.require(f"extracting {pdf_path.name}", extractor=extractor)

        # nexus-i0cwh: every backend fires on_page per page (or per MinerU
        # batch, which then names its pages in ``page_numbers``) with
        # ``text_length``; the pages that produced text are the coverage
        # oracle's input (chunk page numbers only mark where chunks START,
        # so a 20-page deck in 7 chunks would read as 13 missing pages).
        pages_with_text: set[int] = set()

        def _collect(page_index: int, page_text: str, page_metadata: dict) -> None:
            number = page_metadata.get("page_number")
            length = page_metadata.get("text_length")
            if length is None:
                length = len(page_text or "")
            if length and int(length) > 0:
                numbers = page_metadata.get("page_numbers") or ([number] if isinstance(number, int) else [])
                pages_with_text.update(int(n) for n in numbers)
            if on_page is not None:
                on_page(page_index, page_text, page_metadata)

        self._heartbeat = on_heartbeat
        try:
            result = self._extract_dispatch(
                pdf_path, extractor=extractor, on_page=_collect,
            )
        except _HeartbeatStop as stop:
            # The callback's own exception, as it raised it (identity, cause and all).
            raise stop.original from stop.original.__cause__
        finally:
            self._heartbeat = None
        result.metadata["pages_with_text"] = sorted(pages_with_text)
        _enforce_extraction_quality(result, pdf_path, allow_degraded=allow_degraded)
        return result

    def _extract_dispatch(
        self,
        pdf_path: Path,
        *,
        extractor: str,
        on_page: Callable[[int, str, dict], None] | None,
    ) -> ExtractionResult:
        """Backend routing for :meth:`extract`, pre-quality-gate.

        *extractor* is ``"mineru"`` or ``"pymupdf"`` here: :meth:`extract` has already
        mapped the older values. Split out (nexus-wi1uv) so ``extract()`` has exactly
        one gated exit point instead of gating each of this method's several
        ``return`` statements individually.
        """
        if extractor == "pymupdf":
            _progress(f"  PyMuPDF (text layer only): extracting {pdf_path.name}…")
            return self._extract_pymupdf(pdf_path, on_page=on_page)

        _progress(f"  MinerU: extracting {pdf_path.name}…")
        return self._extract_with_mineru_guarded(pdf_path, on_page=on_page)

    def _extract_with_mineru_guarded(
        self,
        pdf_path: Path,
        *,
        on_page: Callable[[int, str, dict], None] | None,
    ) -> ExtractionResult:
        """MinerU extraction with failures named for the operator.

        nexus-2fyb: a MinerU failure is never answered with a plainer extraction, which
        hid corruption from every caller. It fails loudly, and the message says which
        kind of failure it is: an install that cannot import MinerU (reinstall, or
        ``--extractor pymupdf`` for text only), or an operational failure on an install
        that is fine (``nx doctor``).
        """
        try:
            return self._extract_with_mineru(pdf_path, on_page=on_page)
        except ImportError as exc:
            # do_parse is None — mineru ships with the `pdf` extra (installed on demand, nexus-2k66t.4.2), so with the stack present a
            # missing import means the conexus install itself is corrupt.
            _log.error("mineru_import_failed", error=str(exc), path=str(pdf_path))
            from nexus import install_advice, pdf_stack  # noqa: PLC0415 — deferred import

            # nexus-2k66t.4.2: the stack is an extra now. An import that fails
            # because it was never installed is a different message from one that
            # fails over an installed-but-corrupt tree.
            missing = pdf_stack.missing_message(f"PDF {pdf_path.name}")
            if missing is not None:
                raise RuntimeError(
                    f"PDF {pdf_path.name}: MinerU is not importable: {exc}. {missing}"
                ) from exc

            reinstall = install_advice.upgrade_command(
                "uv tool install --reinstall conexus"
            )
            # `nx self install --extras pdf` is refused on a non-generation install (a legacy
            # uv tool tree), so the second way is the plan's command for THIS layout.
            repair = pdf_stack.repair_command()
            ways = f"`{reinstall}`" if repair == reinstall else f"`{reinstall}`, or `{repair}`"
            raise RuntimeError(
                f"PDF {pdf_path.name}: MinerU is not importable: {exc}. "
                f"MinerU is part of the pdf extra and the PDF stack reports itself "
                f"installed, so this conexus install is corrupt — reinstall with "
                f"{ways}. For text only (no layout, tables or math), "
                f"rerun with `--extractor pymupdf`."
            ) from exc
        except (
            MineruMachineResourceError, MineruSystemicFailure, UnextractableContentError,
            MineruBoundsError,
        ):
            # The machine or the install, not this PDF (nexus-x3m8e), the two RDR-237
            # ladder outcomes, and the RDR-237 start-of-run refusal (a setting, not a
            # MinerU failure): all re-raised unwrapped. The pipeline decides what to do with
            # the row from the type (MineruSystemicFailure keeps the buffer), and the CLI
            # prints the operator-written message as it stands.
            raise
        except Exception as exc:
            if _is_run_fence(exc) or _is_pipeline_cancel(exc):
                # A stop signal raised by the on_page callback, not a MinerU failure: the
                # pipeline reads the type to release the row, so it must arrive as it was raised.
                raise
            # MinerU is installed but extraction failed — subprocess timeout,
            # OOM kill, mineru-api server error, etc. Do NOT advise reinstall;
            # the install is fine and the failure is operational.
            sanitized_msg = _redact_url_credentials(str(exc))
            _log.error(
                "mineru_extraction_failed",
                error=sanitized_msg,
                error_type=type(exc).__name__,
                path=str(pdf_path),
            )
            raise RuntimeError(
                f"PDF {pdf_path.name}: MinerU extraction failed: {type(exc).__name__}: "
                f"{sanitized_msg}. Run `nx doctor` to check the MinerU install. For text only "
                f"(no layout, tables or math), rerun with `--extractor pymupdf` (or set `pdf.extractor: pymupdf`)."
            ) from exc

    # Page batch size is read from config via get_mineru_page_batch() (default 1).
    # Formula-dense PDFs OOM during MFR prediction at larger batch sizes.

    def _extract_with_mineru(
        self,
        pdf_path: Path,
        *,
        on_page: Callable[[int, str, dict], None] | None = None,
    ) -> ExtractionResult:
        """Extract text via MinerU (math-aware, optional dependency).

        Each page-range batch runs in a **subprocess** so that MinerU's
        GPU/model memory is fully reclaimed between batches.  Without this,
        memory accumulates across in-process ``do_parse`` calls and large
        formula-dense PDFs get OOM-killed.

        OOM retry: if a multi-page batch fails, bisects down to 1-page granularity.

        RDR-237 ladder: a single page whose MinerU attempt fails then walks down
        a ladder instead of failing the document. Rung 1 is the attempt just
        made (formulas on), rung 2 the same one-page call with
        ``formula_enable=False``, rung 3 the page's PyMuPDF text layer, rung 4
        nothing (``unextracted``). A page descends on a timeout, on
        :class:`MineruMemoryError`, on any other ``RuntimeError`` from the
        worker or the server call, and on a dropped server connection (which
        first retries rung 1 once on the subprocess path). Everything else
        (:class:`MineruMachineResourceError`, :class:`MineruWorkerStopped`, a
        ``PipelineCancelled`` or ``PipelineRunFenced`` from a callback, any other
        ``BaseException``) propagates and stops the document. Only the attempt
        calls sit inside a ``try``, never ``on_page``. When ``pdf.ladder_breaker``
        consecutive pages fail both rungs with one failure class, the install or
        the machine is failing and :class:`MineruSystemicFailure` stops the
        document. The pages counted toward that rule are held back from ``on_page``
        until the window resolves: a trip drops them (so the kept buffer holds no
        degraded text for a rerun to keep), anything else delivers them in page
        order before the page that resolved it.

        *on_page* fires once per batch (default batch size is 1 page via
        ``mineru_page_batch`` config).  The callback receives the batch start
        page index, the batch markdown, and metadata.

        RDR-237 start-of-run check: ``pdf.mineru_page_timeout_s`` x ``pdf.mineru_page_batch``
        is the longest one attempt runs without a progress write, and a pipeline row is taken
        over after 300 s of silence, so a product above 240 s raises
        :class:`MineruBoundsError` (naming both settings) before any page. A setting that is no
        integer is named the same way.
        """
        from nexus.config import mineru_attempt_bound_problem  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        problem = mineru_attempt_bound_problem()
        if problem is not None:
            raise MineruBoundsError(problem)
        if _mineru_do_parse() is None:
            from nexus import install_advice  # noqa: PLC0415 — deferred import

            reinstall = install_advice.upgrade_command(
                "uv tool install --reinstall conexus"
            )
            raise ImportError(
                "MinerU is not importable but is a required dependency since "
                f"nexus-2fyb. Reinstall conexus: `{reinstall}`."
            )

        import pymupdf  # lightweight — only used for page count  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

        with pymupdf.open(pdf_path) as doc:
            total_pages = len(doc)
        # Gap 6: expose to the subprocess path for whole-doc-batch timeout scaling.
        self._mineru_run_total_pages = total_pages

        from nexus.config import get_mineru_page_batch  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        batch_size = get_mineru_page_batch()

        batches: list[tuple[int, int | None]] = []
        if total_pages <= batch_size:
            batches.append((0, None))
        else:
            _log.info(
                "mineru_splitting_large_pdf",
                total_pages=total_pages,
                batch_size=batch_size,
                path=str(pdf_path),
            )
            for start in range(0, total_pages, batch_size):
                batches.append((start, min(start + batch_size, total_pages)))

        md_parts: list[str] = []
        all_content_list: list[dict] = []
        all_pdf_info: list[dict] = []
        # nexus-2fyb code-review C2: track real per-page markdown lengths
        # so page_boundaries reflect actual content distribution, not a
        # uniform char/page average. Each entry: (page_index_0based, length).
        # OOM retry loop appends per-page entries directly; batch-mode
        # appends one entry covering the batch span.
        per_page_lengths: list[tuple[int, int]] = []
        # RDR-237: where every page settled, by 0-based page index. Any method
        # other than "mineru" makes the document's extraction_method the honest
        # aggregate "mineru+degraded" (nexus-1oguj's rule, now for the ladder's
        # rungs 2 to 4), and the pages by rung are the result's extraction_pages.
        outcomes: dict[int, PageOutcome] = {}
        from nexus.config import get_pdf_ladder_breaker  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        breaker = _SystemicFailureCounter(get_pdf_ladder_breaker())
        fault_raw = os.environ.get(_FAULT_PAGES_ENV, "")
        fault_pages = _parse_fault_pages(fault_raw) if fault_raw else {}
        if fault_pages:
            _log.warning(
                "mineru_fault_pages_forced", pages=sorted(fault_pages), spec=fault_raw,
                path=str(pdf_path),
                note=f"{_FAULT_PAGES_ENV} is set: the named pages' rungs fail on purpose",
            )
        # nexus-stkek: structurally suspect tables found while marking, in
        # document order. Collected here rather than at assembly because the
        # marker is inserted into the per-batch markdown, before
        # per_page_lengths is measured (see below).
        table_defects: list[dict] = []

        def _rebase_page_idx(content_list: list[dict], batch_start: int) -> None:
            # MinerU numbers ``page_idx`` from the start of the batch it parsed.
            for entry in content_list:
                if "page_idx" in entry and "doc_page_idx" not in entry:
                    entry["doc_page_idx"] = batch_start + entry["page_idx"]

        def _append_page(
            page: int, md: str, content_list: list[dict], pdf_info: list[dict], method: str,
        ) -> None:
            # Single append point so the batch-success, 1-page-retry, and
            # ladder paths produce identical bookkeeping. For
            # batch_size > 1 the success path passes the batch start as `page`;
            # page-number metadata is only exact at batch_size == 1 (the
            # default the streaming pipeline relies on).
            # RDR-237 (nexus-tr1rj.3): ``page_method`` is the rung this page settled on. It rides
            # the page's buffer row, so a resumed run can rebuild the document's
            # ``extraction_pages`` from the pages it did not write itself.
            if on_page is not None:
                on_page(page, md, {"page_number": page + 1, "text_length": len(md), "page_method": method})
            md_parts.append(md)
            _rebase_page_idx(content_list, page)
            all_content_list.extend(content_list)
            all_pdf_info.extend(pdf_info)
            per_page_lengths.append((page, len(md)))

        def _append_batch(s: int, md: str, content_list: list[dict],
                          pdf_info: list[dict], batch_pages: int, method: str) -> None:
            # batch_size>1 success: the batch md covers `batch_pages` pages and
            # we distribute its length uniformly (the only resolution available
            # without per-page md from MinerU). on_page/md_parts/content fire
            # once for the batch; per_page_lengths is the distributed form.
            if on_page is not None:
                # page_numbers names every page the batch md covers, so the
                # pages_with_text collector counts the whole batch, not its
                # first page (critique [24937] S3).
                on_page(s, md, {
                    "page_number": s + 1, "text_length": len(md),
                    "page_numbers": list(range(s + 1, s + 1 + max(1, batch_pages))),
                    "page_method": method,
                })
            md_parts.append(md)
            _rebase_page_idx(content_list, s)
            all_content_list.extend(content_list)
            all_pdf_info.extend(pdf_info)
            per_page = len(md) // batch_pages
            remainder = len(md) % batch_pages
            for offset in range(batch_pages):
                extra = 1 if offset < remainder else 0
                per_page_lengths.append((s + offset, per_page + extra))

        # RDR-237 breaker window: a page that failed both rungs with the class the breaker is
        # counting is held here, NOT delivered, until the window resolves. A trip drops the held
        # pages (the buffer then ends at the last page before the window, and the rerun redoes
        # them with MinerU); anything else flushes them, in page order, before the page that
        # resolved it. Delivering them at once would leave their rung-3 text in the kept buffer,
        # where a rerun skips the writes and the degraded text persists labelled clean.
        held: list[tuple[int, str, PageOutcome]] = []

        def _flush_held(keep: frozenset[int] | set[int] = frozenset()) -> None:
            # Deliver the held pages, oldest first, up to the first one *keep* still counts
            # (1-based page numbers; the counted pages are always the newest).
            while held and held[0][2].page not in keep:
                s, text, outcome = held.pop(0)
                _append_page(s, text, [], [], outcome.method)
                outcomes[s] = outcome

        def _attempt(
            s: int, e: int | None, rung: int, *, via_server: bool = True,
        ) -> tuple[str, list[dict], list[dict]]:
            # One MinerU attempt on pages [s, e): rung 1 is today's call
            # (formulas on, unchanged arguments), rung 2 the same call with
            # formulas off. NX_MINERU_FAULT_PAGES forces a named page's rung to
            # fail before MinerU is reached (a batch holding one fails whole, so
            # it bisects down to the page). *via_server* False sends the attempt
            # straight to the subprocess path: the page's server request just timed
            # out, and the server is still parsing it (RDR-237).
            for page in range(s + 1, (e if e is not None else total_pages) + 1):
                forced = fault_pages.get(page)
                if forced is not None and rung in forced[0]:
                    # A forced failure is an attempt too, and a forced timeout waits out the
                    # real page budget before it fails: the heartbeat opens it like any other
                    # (the server and subprocess paths open theirs themselves).
                    self._beat()
                    raise _forced_failure(
                        forced[1], page, rung, (e if e is not None else total_pages) - s,
                    )
            # The kwarg rides only when set, so rung 1's calls keep their old shape.
            route = {} if via_server else {"use_server": False}
            if rung == 1:
                return self._mineru_run_isolated(pdf_path, s, e, **route)
            return self._mineru_run_isolated(pdf_path, s, e, formula_enable=False, **route)

        def _accept(
            s: int, rng_end: int, md: str, content_list: list[dict], pdf_info: list[dict],
            method: str, started: float, error: str | None,
        ) -> None:
            # MinerU returns one pdf_info entry per page it parsed. MORE entries
            # than pages asked for means it parsed something else: when its
            # pdfium page rewrite throws, convert_pdf_bytes_to_bytes
            # falls back to the ORIGINAL bytes and parses the whole document
            # for this one batch (MinerU 3.1.11, mineru/cli/common.py). Appending
            # that duplicates every page, the same corruption nexus-v4xg7 found
            # from an off-by-one, and chash dedup hides it in storage. ValueError,
            # not RuntimeError, so the ladder does not descend on it.
            # Only an excess is checked: whether MinerU emits an entry for a
            # blank page is not established, and a shortfall is not this leak.
            span = rng_end - s
            if len(pdf_info) > span:
                raise ValueError(
                    f"MinerU returned {len(pdf_info)} pages for pages "
                    f"{s + 1}-{rng_end} ({span} requested) of {fname}; its page "
                    f"rewrite most likely failed and it parsed the whole document"
                )
            # Success. Normalize before measuring length so per_page_lengths is
            # consistent with the stored normalized text.
            md = _unwrap_mineru_font_tags(_normalize_mineru_latex(md))
            md = _mark_unextracted_visuals(md, content_list)
            # nexus-stkek: mark suspect tables HERE, beside the visual
            # markers and before _append_page/_append_batch measures this
            # text, because both of those insert characters and
            # per_page_lengths -> page_boundaries -> chunk_start_char are
            # all measured downstream of them. Inserting at assembly
            # instead would shift every boundary past the first marker,
            # which is the defect class nexus-kas9u came from.
            # A table spanning a BATCH boundary is not checked: neither
            # half matches a complete <table>...</table>, so it passes
            # through unmarked rather than being judged on half its rows.
            md, found = mark_misshapen_tables(md)
            table_defects.extend(found)
            elapsed = time.monotonic() - started
            if span <= 1:
                _append_page(s, md, content_list, pdf_info, method)
                outcomes[s] = PageOutcome(s + 1, md, method, elapsed, error)
            else:
                _append_batch(s, md, content_list, pdf_info, span, method)
                for offset in range(span):
                    outcomes[s + offset] = PageOutcome(
                        s + offset + 1, md if offset == 0 else "", method, elapsed / span, error,
                    )

        def _is_descent(exc: BaseException) -> bool:
            # Whether a failure of one ATTEMPT moves a page a rung down: a
            # worker or server RuntimeError (a timeout and MineruMemoryError
            # included) or a dropped connection. PipelineRunFenced is a
            # RuntimeError but a stop. MineruMachineResourceError is an
            # Exception, MineruWorkerStopped a BaseException: neither is caught.
            return not _is_run_fence(exc)

        def _settle_page(
            s: int, e: int | None, rung1_exc: BaseException, started: float,
            *, via_server: bool = True,
        ) -> None:
            # Page s failed its rung-1 attempt with *rung1_exc* (already known to
            # be a descent). *via_server* False: an enclosing batch timed out on the
            # server, which is still parsing it, so no step of this page goes there.
            # Walk it down the ladder; callbacks (_append_page ->
            # on_page) run OUTSIDE every try, so a cancel or a fenced run raised
            # there is never read as MinerU failing.
            page = s + 1
            rng_end = e if e is not None else total_pages
            dropped_first = isinstance(rung1_exc, _DROPPED_CONNECTION)
            if dropped_first:
                # The server dropped us. Retry rung 1 ONCE on the subprocess path
                # with formulas on, before giving up the formulas. RDR-237 (Bounds): a dropped
                # connection counts toward neither the restart rule nor the breaker, so this page
                # is kept out of the breaker count whatever the retry then does; the retry's own
                # failure class is still logged and named in the page's error.
                _log.warning("mineru_dropped_connection_retry", page=page, path=str(pdf_path),
                             error=type(rung1_exc).__name__)
                try:
                    retried = self._mineru_run_subprocess(pdf_path, s, e)
                except (RuntimeError, *_DROPPED_CONNECTION) as retry_exc:
                    if not _is_descent(retry_exc):
                        raise
                    rung1_exc = retry_exc
                else:
                    breaker.settled()
                    _flush_held()
                    _accept(s, rng_end, *retried, "mineru", started, None)
                    return
            class1 = _failure_class(rung1_exc)
            _log.warning("mineru_page_rung_failed", page=page, rung=1, failure_class=class1,
                         path=str(pdf_path))
            _progress(f"  MinerU page {page}/{total_pages} failed ({class1}) — retrying without formulas ({fname})")
            rung2_exc: BaseException
            try:
                # RDR-237: rung 1 timed out on the server, which is still parsing that request
                # and cannot be told to stop. Rung 2 goes to the subprocess path, the one path
                # whose timeout actually kills the work.
                retried = _attempt(
                    s, e, 2,
                    via_server=via_server and not isinstance(rung1_exc, MineruServerTimeout),
                )
            except (RuntimeError, *_DROPPED_CONNECTION) as caught:
                if not _is_descent(caught):
                    raise
                rung2_exc = caught
            else:
                breaker.settled()
                _flush_held()
                _accept(s, rng_end, *retried, "mineru-no-formula", started, class1)
                return
            class2 = _failure_class(rung2_exc)
            _log.warning("mineru_page_rung_failed", page=page, rung=2, failure_class=class2,
                         path=str(pdf_path))
            tripped = breaker.failed(page, _UNCLASSIFIED if dropped_first else class1, class2)
            if tripped is not None:
                # The held window is dropped, never flushed: it stays out of the kept buffer.
                raise MineruSystemicFailure(
                    tripped, getattr(rung2_exc, "stderr_last_line", ""), list(breaker.pages), fname,
                ) from rung2_exc
            # The pages the breaker is counting stay held; any held page it stopped counting
            # (the class changed, or this page reset it) is delivered first, in order.
            counting = set(breaker.pages) if breaker.threshold else set()
            _flush_held(counting)
            # Rung 3: this page's own text layer. A page PyMuPDF cannot read is rung 4
            # like a page with no text (the failure is recorded, not raised).
            try:
                text = _pymupdf_page_text_at(pdf_path, s)
            except Exception as read_exc:  # noqa: BLE001 — a damaged page degrades to unextracted; MinerU already failed it twice
                _log.warning("mineru_page_text_layer_unreadable", page=page, path=str(pdf_path),
                             error=f"{type(read_exc).__name__}: {read_exc}")
                text = ""
            method = "pymupdf-text" if text else "unextracted"
            outcome = PageOutcome(
                page, text, method, time.monotonic() - started,
                f"rung 1: {class1}; rung 2: {class2}",
            )
            if page in counting:
                held.append((s, text, outcome))
            else:
                _append_page(s, text, [], [], method)
                outcomes[s] = outcome
            _progress(f"  MinerU page {page}/{total_pages} settled as {method} ({fname})")

        def _extract_range(s: int, e: int | None, *, via_server: bool = True) -> None:
            # RDR-148 Gap 6 batch//2 ladder: extract pages [s, e) in one
            # subprocess; on failure BISECT (full range -> halves -> ... -> one
            # page) instead of dropping straight to 1-page, so a mid-size batch
            # that fits memory is tried before the slowest per-page path. A
            # single page that fails goes down the RDR-237 ladder (_settle_page).
            # RDR-237: a range whose server request timed out never goes back to the server,
            # nor does any step below it: the server is still parsing that request and cannot
            # be told to stop. Its steps run on the subprocess path, which kills what it
            # times out, and add nothing to the restart streak (only server requests do).
            rng_end = e if e is not None else total_pages
            span = rng_end - s
            started = time.monotonic()
            failure: BaseException | None = None
            try:
                attempted = _attempt(s, e, 1, via_server=via_server)
            except (RuntimeError, *_DROPPED_CONNECTION) as exc:
                if not _is_descent(exc):
                    raise
                failure = exc
            if failure is None:
                breaker.settled()
                _flush_held()
                _accept(s, rng_end, *attempted, "mineru", started, None)
                return
            if span <= 1:
                _settle_page(s, e, failure, started, via_server=via_server)
                return
            steps_via_server = via_server and not isinstance(failure, MineruServerTimeout)
            mid = s + span // 2
            _log.warning(
                "mineru_oom_retry", pages=f"{s + 1}–{rng_end}",
                path=str(pdf_path), original_batch=span, retry_split=mid,
            )
            _progress(
                f"  MinerU: pages {s + 1}–{rng_end} failed — bisecting "
                f"at {mid + 1} ({fname})"
            )
            _extract_range(s, mid, via_server=steps_via_server)
            _extract_range(mid, rng_end, via_server=steps_via_server)

        fname = pdf_path.name
        for batch_idx, (start, end) in enumerate(batches):
            label = f"{start + 1}–{end}" if end is not None else f"{start + 1}–{total_pages}"
            _progress(
                f"  MinerU: page {start + 1}/{total_pages} ({fname})",
            )
            _log.info("mineru_batch", pages=label, path=str(pdf_path))
            _extract_range(start, end)

        if batches:
            _progress(f"  MinerU: {total_pages}/{total_pages} done ({fname})")

        _flush_held()  # a window still open at the end of the document never tripped
        extraction_pages = {
            method: sorted(o.page for o in outcomes.values() if o.method == method)
            for method in _DEGRADED_METHODS
            if any(o.method == method for o in outcomes.values())
        }
        if outcomes and all(o.method == "unextracted" for o in outcomes.values()) and (
            len(outcomes) == total_pages
        ):
            # Rungs 1 to 3 failed on every page and none has a text layer: indexing
            # this would write nothing, and say so by returning an empty document.
            last = outcomes[max(outcomes)].error
            raise AllPagesUnextractedError(
                f"every page of {fname} is unextracted: MinerU failed on all {total_pages} "
                f"pages with and without formulas ({last}) and none has a text layer. "
                f"Run `nx doctor` for the MinerU install, or OCR the PDF first.",
                total_pages=total_pages, last_error=last or "",
            )

        md_text = "\n".join(md_parts)
        return self._mineru_build_result(
            pdf_path, md_text, all_content_list, all_pdf_info,
            per_page_lengths=per_page_lengths,
            total_pages=total_pages,
            extraction_pages=extraction_pages,
            table_defects=table_defects,
        )

    def _probe_mineru_health(self, base_url: str) -> tuple[bool, str]:
        """Probe ``{base_url}/health`` once. Return ``(ok, reason)``.

        ``reason`` is empty on success, else a short diagnostic
        (``http_503`` / ``ConnectError: ...``) for the caller to surface
        on the loud fallback decision. Per-probe failures are logged at
        DEBUG — the single WARNING belongs to the final fallback in
        :meth:`_mineru_server_available`, not to each probe.
        """
        url = f"{base_url}/health"
        try:
            resp = httpx.get(url, timeout=2)
            if resp.status_code == 200:
                return True, ""
            _log.debug("mineru_health_probe_non_200", url=url,
                       http_status=resp.status_code)
            return False, f"http_{resp.status_code}"
        except (httpx.ConnectError, httpx.TimeoutException,
                httpx.RemoteProtocolError) as exc:
            # RemoteProtocolError: a server dying mid-startup can accept the
            # TCP connection but return a truncated/malformed response. The
            # parse path (_mineru_run_isolated) already treats it as a
            # crash-and-fall-back; the health probe must too, not crash.
            _log.debug("mineru_health_probe_unreachable", url=url,
                       error=f"{type(exc).__name__}: {exc}")
            return False, f"{type(exc).__name__}: {exc}"

    def _replace_server_with_deleted_cwd(self, url: str) -> None:
        """A ``/health`` 200 from a server whose working directory was deleted is
        not health: every task it takes fails (nexus-x3m8e). When the registered
        server serving *url* is in that state, replace it once, before any task
        is wasted on it. Best-effort: any failure leaves the server as found."""
        try:
            from nexus._mineru_health import server_is_broken  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            from nexus._mineru_pid import read_pid_file  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

            info = read_pid_file()
            # No error text yet: only the registered server on exactly this
            # URL, with its working directory gone, qualifies.
            if info is None or not server_is_broken(url, "", info):
                return
            _log.warning("mineru_server_cwd_gone", pid=info["pid"], url=url)
            if self._restart_broken_mineru_server(info):
                _progress("  MinerU server had a deleted working directory — restarted it.")
        except Exception:  # noqa: BLE001 — a diagnostic must never break extraction
            _log.debug("mineru_cwd_check_failed", exc_info=True)

    def _mineru_server_available(self) -> bool:
        """Check if the MinerU API server is reachable.

        Result cached for the lifetime of this PDFExtractor instance —
        a False result is never retried. Create a new instance to re-check.

        RDR-148 Gap 2 (rediscover-then-fail-loud): on a /health failure
        the run must not silently degrade to the in-process subprocess
        path (where math-heavy / large PDFs OOM-kill the worker). Before
        degrading, perform exactly ONE rediscovery pass — re-resolve the
        endpoint, which re-reads the live PID file when config is at the
        default, so a server that restarted mid-run on a new port is
        picked up. Only when rediscovery still finds no live server is the
        subprocess path selected, and that decision is logged LOUD (a
        single WARNING + ``_progress`` line naming the reason), never
        silently (nexus-h1jk warn-on-fallback, made non-silent here).

        Known limitation (by Gap 1 design, for vehin.5): "rediscover" means
        re-resolve via ``get_mineru_server_url()``, whose pid-file read is
        gated by the Gap 1 precedence — an EXPLICIT non-default operator URL
        wins and the pid file is intentionally NOT consulted. So rediscovery
        re-reads the pid file only on the default-config path; with an
        explicit URL it is a transient-recovery re-probe of the same
        endpoint. This is deliberate: honoring "operator intent wins" (Gap 1)
        precludes a pid file silently redirecting an explicitly-pinned URL.

        nexus-eti1v: a KNOWN version skew on the registered server is a
        third outcome from ``get_mineru_server_url()`` — ``None`` — and is
        NOT "no live server". The registered pid already holds this box's
        only local server, so neither dialling the built-in default port
        nor autostart's health wait can ever succeed; both were tried
        anyway before this fix, at a measured cost of ~2.5 minutes (63
        repeated skew warnings, a refused dial of the default, a
        "rediscovery" of that same refused default, then a full
        ``mineru_ensure_health_timeout`` wait) for a foregone conclusion.
        On skew, skip straight to subprocess with one loud, reasoned log.
        """
        if self._mineru_server_checked:
            if self._mineru_server_up or self._mineru_server_recheck_after is None:
                return self._mineru_server_up
            # nexus-5ny9r: a cached "no live server" verdict expires. The
            # re-check is ONE cheap health probe of the current endpoint —
            # never the rediscover + autostart walk below, which is a
            # once-per-run decision (and on skew never a retry at all:
            # that verdict leaves the deadline unset, per nexus-eti1v).
            if time.monotonic() < self._mineru_server_recheck_after:
                return False
            from nexus.config import get_mineru_server_url as _url_now  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            url_now = _url_now()
            if url_now is not None and self._probe_mineru_health(url_now)[0]:
                _log.info("mineru_server_picked_up_mid_run", url=url_now)
                _progress(f"  MinerU server is up at {url_now} — switching off the per-page subprocess path.")
                self._mineru_server_up = True
                self._mineru_server_recheck_after = None
                return True
            self._mineru_server_recheck_after = time.monotonic() + _MINERU_SERVER_RECHECK_S
            return False

        from nexus.config import get_mineru_server_url, get_mineru_skew_info  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        first_url = get_mineru_server_url()
        if first_url is None:
            self._mineru_server_up = False
            self._mineru_server_checked = True
            skew = get_mineru_skew_info()
            remedy = "nx mineru restart  (or set pdf.mineru_server_url explicitly)"
            if skew is not None:
                _log.info(
                    "mineru_skew_subprocess_fallback",
                    registered_version=skew.registered_version,
                    our_version=skew.our_version,
                    pid=skew.pid,
                    port=skew.port,
                    remedy=remedy,
                )
                _progress(
                    f"  MinerU server registered under mineru {skew.registered_version}, "
                    f"this environment pins {skew.our_version} — skipping straight to "
                    f"in-process subprocess (run `nx mineru restart` to pick this "
                    f"environment's version back up, or set pdf.mineru_server_url "
                    f"explicitly)."
                )
            else:
                # Defensive: the pid file changed state between the two
                # reads above (e.g. `nx mineru stop` mid-check). No skew
                # detail to report, but None still means "nothing to dial".
                _log.info("mineru_skew_subprocess_fallback", remedy=remedy)
                _progress(
                    "  MinerU server registration changed during startup check — "
                    "falling back to in-process subprocess."
                )
            return self._mineru_server_up

        ok, first_reason = self._probe_mineru_health(first_url)
        if ok:
            self._replace_server_with_deleted_cwd(first_url)
            self._mineru_server_up = True
            self._mineru_server_checked = True
            return True

        # Gap 2: exactly one rediscovery pass before degrading. Re-resolving
        # re-reads the PID file (default-config path), so a mid-run restart
        # to a new port is picked up; with an explicit operator URL this is
        # a single transient-recovery re-probe of the same endpoint.
        second_url = get_mineru_server_url()
        ok, reason = self._probe_mineru_health(second_url)
        if ok:
            _log.info("mineru_server_rediscovered",
                      url=second_url, prior_url=first_url)
            self._mineru_server_up = True
            self._mineru_server_checked = True
            return True

        # nexus-1qdb9: on-demand lifecycle before conceding — elect a
        # single spawner via the RDR-149 substrate and wait (bounded) for
        # health. Config-gated (pdf.mineru_autostart) and remote-intent-
        # safe; None = degrade exactly as before.
        try:
            from nexus.daemon.mineru_lifecycle import (  # noqa: PLC0415 — deferred import
                ensure_mineru_running,
                spawn_policy_allows,
            )
            if spawn_policy_allows(second_url):
                # Critique a29348b4: the wait below can run minutes with
                # only structlog — surface it on the same _progress channel
                # the fallback warning uses, or the run just looks hung.
                _progress(
                    "  MinerU server not responding — autostarting "
                    "(waits up to 2 min; first-ever start may download models)…"
                )
            # RDR-237: the wait is an attempt that writes no page, up to 2 minutes; it keeps the
            # pipeline row alive through the caller's heartbeat. What the heartbeat raises is a
            # _HeartbeatStop (a BaseException), so the catch below does not swallow it.
            ensured = ensure_mineru_running(
                **({"heartbeat": self._beat} if self._heartbeat is not None else {}),
            )
        except Exception:  # noqa: BLE001 — lifecycle must never break extraction
            _log.warning("mineru_ensure_crashed", exc_info=True)
            ensured = None
        if ensured is not None:
            _log.info("mineru_server_autostarted", url=ensured)
            self._mineru_server_up = True
            self._mineru_server_checked = True
            return True

        # No live server after rediscovery + autostart — loud, reasoned fallback.
        self._mineru_server_up = False
        self._mineru_server_checked = True
        self._mineru_server_recheck_after = time.monotonic() + _MINERU_SERVER_RECHECK_S
        _log.warning(
            "mineru_fallback_to_subprocess",
            first_url=first_url, rediscovered_url=second_url,
            reason=reason, first_reason=first_reason,
        )
        _progress(
            f"  warn: MinerU server unreachable after rediscovery "
            f"({reason}); falling back to in-process subprocess (slower, "
            f"OOM-risk on large math PDFs). Run `nx mineru start` to enable "
            f"server mode."
        )
        return self._mineru_server_up

    def _mineru_attempt_pages(self, start: int, end: int | None) -> int:
        """How many pages one attempt on ``[start, end)`` covers: the factor of its time budget.

        ``end - start`` for an explicit range; for the whole-document range (``end`` None) the
        pages from *start* to the document's last (``_mineru_run_total_pages``, known once
        ``_extract_with_mineru`` opened the PDF); 1 when neither is known (a direct caller). The
        server request and the subprocess run both take their timeout from this, so they cannot
        disagree about how long a range may run.
        """
        if end is not None:
            pages = end - start
        elif self._mineru_run_total_pages is not None:
            pages = self._mineru_run_total_pages - start
        else:
            pages = 1
        return max(1, pages)

    def _mineru_run_via_server(
        self, pdf_path: Path, start: int, end: int | None, formula_enable: bool = True,
    ) -> tuple[str, list[dict], list[dict]]:
        """Extract via MinerU HTTP server (POST /file_parse).

        *formula_enable* False is the ladder's rung 2 (RDR-237): the server skips
        its formula model for these pages.

        RDR-237: the request's timeout is ``pdf.mineru_page_timeout_s`` times the pages
        in the range (it was a fixed 300 s), and the request opens with a heartbeat."""
        self._beat()
        _mineru_check_range(start, end, self._mineru_run_total_pages)
        from nexus.config import get_mineru_page_timeout_s, get_mineru_server_url, get_mineru_table_enable  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        # nexus-eti1v code-review (substantive-critic, T2 critique-index-path
        # -four-beads-2026-09-02): get_mineru_server_url() can now return
        # None (skew sentinel) — a possibility that did not exist before
        # eti1v's fix. This method re-resolves the URL FRESH on every call
        # (unlike the once-per-instance cache in _mineru_server_available),
        # so a skew that appears mid-extraction (another process runs `nx
        # mineru restart` onto a differently-versioned server between this
        # instance's initial availability check and this page's dispatch)
        # can hit this call after _mineru_server_available already cached
        # True. An unguarded f-string turned that into the literal URL
        # "None/file_parse", which httpx refuses with UnsupportedProtocol —
        # NOT one of the exception types _mineru_run_isolated's caller
        # catches (ConnectError/TimeoutException/RemoteProtocolError/
        # HTTPStatusError), so it propagated uncaught instead of triggering
        # the existing "server crashed — invalidate cache, try restart,
        # fall back to subprocess" recovery path. Raising ConnectError here
        # routes a fresh None into that SAME existing recovery machinery.
        resolved_url = get_mineru_server_url()
        if resolved_url is None:
            raise httpx.ConnectError(
                "mineru server url resolved to None mid-extraction "
                "(version skew or deregistration since the last check)",
            )
        url = f"{resolved_url}/file_parse"
        with pdf_path.open("rb") as f:
            resp = httpx.post(
                url,
                files=[("files", (pdf_path.name, f, "application/pdf"))],
                data={
                    "backend": "pipeline",
                    "start_page_id": str(start),
                    "end_page_id": str(_mineru_end_page_id(end)),
                    "formula_enable": str(formula_enable).lower(),
                    "table_enable": str(get_mineru_table_enable()).lower(),
                    "return_md": "true",
                    "return_middle_json": "true",
                    "return_content_list": "true",
                    "parse_method": "auto",
                    "lang_list": "en",
                },
                timeout=get_mineru_page_timeout_s() * self._mineru_attempt_pages(start, end),
            )
        resp.raise_for_status()
        data = resp.json()

        all_results = data.get("results", {})
        stem = pdf_path.stem
        results = all_results.get(stem)
        if results is None:
            if len(all_results) == 1:
                results = next(iter(all_results.values()))
            else:
                raise RuntimeError(
                    f"Server results missing key {stem!r}; "
                    f"available keys: {list(all_results.keys())}"
                )

        md = results.get("md_content", "")
        if not md:
            # Empty page (image-only, blank, or figure plate) — not an error
            _log.debug("mineru_empty_page", path=str(pdf_path), start=start, end=end)
            md = ""

        raw_cl = results.get("content_list")
        raw_mj = results.get("middle_json")
        if raw_mj is None:
            _log.warning("mineru_server_no_middle_json", path=str(pdf_path))
        content_list = json.loads(raw_cl) if raw_cl else []
        middle = json.loads(raw_mj) if raw_mj else {}
        return md, content_list, middle.get("pdf_info", [])

    _MINERU_MAX_RESTARTS: int = 2

    def _restart_refused(self) -> bool:
        """Whether this extractor may not restart a server at all: its own restart budget
        (``_MINERU_MAX_RESTARTS``) is spent, or the spawn policy forbids a spawn. Logged. Checked
        before the machine-wide cooldown is claimed, so a process that cannot restart never
        spends the cooldown other processes need."""
        if self._mineru_server_restarts >= self._MINERU_MAX_RESTARTS:
            _log.warning("mineru_restart_budget_exhausted",
                         restarts=self._mineru_server_restarts)
            return True

        from nexus.daemon.mineru_lifecycle import spawn_policy_allows  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        if not spawn_policy_allows():
            _log.info("mineru_restart_blocked_by_policy")
            return True
        return False

    def _restart_mineru_server(self, replace_pid: int | None = None) -> bool:
        """Attempt to restart the MinerU server after a crash.

        Returns True if the server was restarted and is healthy.
        Limited to _MINERU_MAX_RESTARTS per PDFExtractor instance.

        nexus-c7odl: this trigger honors the same policy gates and runs
        under the same RDR-149 election as the on-demand lifecycle — a
        bare inline spawn here raced ``ensure_mineru_running`` (two live
        servers, one orphaned) and bypassed ``mineru_autostart: false``.
        The spawn itself goes through the shared ``spawn_server_process``
        core (0o600 pid file + output_root + child log — the old inline
        copy had drifted on all three) and honors a configured fixed port
        (the 2026-07-01 invisible-server class: a fixed
        ``pdf.mineru_server_url`` with a restart onto a random port left
        a live server ``get_mineru_server_url`` would never find).
        """
        if self._restart_refused():
            return False

        # RDR-237: the request that brought us here may have run its whole budget (up to 240 s)
        # since its own beat. Beat before the election, the stop's SIGTERM grace and the spawn,
        # none of which writes a page, so their time adds to a gap that starts here (the health
        # wait below beats again once the spawn is done).
        self._beat()

        self._mineru_server_restarts += 1
        _log.info("mineru_server_restarting",
                  attempt=self._mineru_server_restarts)

        import time as _time  # noqa: PLC0415 — deferred import — branch-local
        from nexus._mineru_pid import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            _pid_file_path,
            is_process_alive,
            read_pid_file,
        )
        from nexus._mineru_spawn import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            _HEALTH_POLL_INTERVAL,
            _find_free_port,
            spawn_server_process,
        )
        from nexus.config import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            get_mineru_configured_fixed_port,
            nexus_config_dir,
        )
        from nexus.daemon.mineru_lifecycle import MINERU_TIER, _SPAWN_SCOPE  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        try:
            registry = ServiceRegistry(dir=nexus_config_dir(), tier=MINERU_TIER)
            proc = None
            port = None
            with registry.election(_SPAWN_SCOPE):
                info = read_pid_file()
                if (
                    info is not None
                    and replace_pid is not None
                    and info["pid"] == replace_pid
                    and is_process_alive(replace_pid)
                ):
                    # nexus-x3m8e: the registered server is the broken one;
                    # stop it (sparing its resource tracker so its semaphores
                    # are reclaimed) and fall through to the spawn below.
                    # The pid came from a file that outlives its server, and
                    # the stop signals the pid's whole process group, so the
                    # live command is judged first, exactly as `nx mineru
                    # stop` does (nexus-5yrob): a pid the OS has recycled to
                    # an unrelated process is a stale file, not a server.
                    from nexus._mineru_spawn import classify_server_pid, stop_server_group  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

                    identity = classify_server_pid(replace_pid)
                    if identity == "unreadable":
                        # Inconclusive: signal nothing, keep the file. Nothing was attempted, which
                        # the caller's progress line says (nexus-tr1rj.25).
                        _log.warning("mineru_replace_pid_unreadable", pid=replace_pid)
                        self._restart_skipped = "unreadable"
                        return False
                    self._announce_restart()
                    if identity == "mineru":
                        outcome = stop_server_group(replace_pid)
                        _log.warning("mineru_broken_server_stopped",
                                     pid=replace_pid, outcome=outcome)
                    else:
                        _log.warning("mineru_replace_pid_recycled_not_signalled",
                                     pid=replace_pid)
                    _pid_file_path().unlink(missing_ok=True)
                    info = None
                if info is not None and is_process_alive(info["pid"]):
                    # A concurrent trigger already restarted — probe it below. The caller's
                    # progress line says another process did it (nexus-tr1rj.26).
                    port = info["port"]
                    self._restart_taken_over_by = info["pid"]
                else:
                    if info is not None:
                        _pid_file_path().unlink(missing_ok=True)
                    port = get_mineru_configured_fixed_port() or _find_free_port()
                    self._announce_restart()
                    proc = spawn_server_process(port)
                    if proc is None:
                        _log.warning("mineru_restart_failed",
                                     reason="mineru-api not found")
                        return False
        except Exception:  # noqa: BLE001 — restart must never break extraction; the caller falls back
            _log.warning("mineru_restart_failed", exc_info=True)
            return False

        # Poll health for up to 60s (models already cached in memory by OS)
        url = f"http://127.0.0.1:{port}/health"
        deadline = _time.monotonic() + _RESTART_HEALTH_WAIT_S
        # RDR-237: this wait is an attempt that writes no page. It opens with a heartbeat and
        # ticks again every 60 s (a wait this long ticks once; the interval is the lifecycle's).
        from nexus.daemon.mineru_lifecycle import HEARTBEAT_EVERY_S  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        self._beat()
        last_beat = _time.monotonic()
        while _time.monotonic() < deadline:
            if _time.monotonic() - last_beat >= HEARTBEAT_EVERY_S:
                self._beat()
                last_beat = _time.monotonic()
            if proc is not None and proc.poll() is not None:
                _log.warning("mineru_restart_failed", reason="process exited")
                return False
            try:
                resp = httpx.get(url, timeout=2)
                if resp.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            _time.sleep(_HEALTH_POLL_INTERVAL)
        else:
            _log.warning("mineru_restart_failed", reason="health timeout")
            return False

        # Reset availability cache. The pid file was already written by
        # spawn_server_process (nexus-oa7r: pid file only, never config —
        # persisting ephemeral ports drifted across reboots).
        self._mineru_server_checked = True
        self._mineru_server_up = True
        _log.info("mineru_server_restarted",
                  pid=proc.pid if proc is not None else info["pid"], port=port)
        return True

    def _announce_restart(self) -> None:
        """Print the restart's "replacing it" line, once, at the point where the stop and the
        spawn are about to start: a restart skipped before this never prints it, and the health
        wait that follows (up to 60 s) is no longer silent (nexus-tr1rj.25)."""
        announce, self._restart_announce = self._restart_announce, None
        if announce is not None:
            announce()

    def _mark_mineru_server_down(self) -> None:
        """Stop using the server for now: pages take the subprocess path until the re-check
        (``_MINERU_SERVER_RECHECK_S``, nexus-5ny9r) finds a live server again."""
        self._mineru_server_checked = True
        self._mineru_server_up = False
        self._mineru_server_recheck_after = time.monotonic() + _MINERU_SERVER_RECHECK_S

    def _mineru_server_timed_out(
        self, pdf_path: Path, start: int, end: int | None, exc: BaseException, *, rung_one: bool,
    ) -> MineruServerTimeout:
        """The server did not answer one request within its page budget (RDR-237): build the
        failure that sends the page to its next rung, and apply the restart rule.

        The server is NOT marked down and the request is NOT retried on it: the parse it
        abandoned runs on in a thread nothing can cancel, so the next rung runs as a subprocess,
        whose timeout kills its work. What a timeout does cost the server is its slot. On a
        one-slot server (macOS) the abandoned parse holds the only one, so the first rung-1
        timeout replaces the server; with more slots a second consecutive page's rung-1 timeout
        does (the first abandoned parse finishes on its own and delays nothing). Rung 2 requests
        are not counted. *exc* is the ``httpx`` timeout, for the log.
        """
        from nexus.config import get_mineru_page_timeout_s, get_mineru_server_url  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        pages = self._mineru_attempt_pages(start, end)
        budget_s = get_mineru_page_timeout_s() * pages
        label = str(start + 1) if pages == 1 else f"{start + 1}-{start + pages}"
        _log.warning(
            "mineru_server_timeout", path=str(pdf_path), pages=label, budget_s=budget_s,
            rung=1 if rung_one else 2, error=f"{type(exc).__name__}: {exc}",
        )
        if rung_one:
            self._server_timeout_pages.append(label)
            # The slot count is the live server's own (its /health), else the platform estimate.
            slots = _mineru_server_slots(get_mineru_server_url())
            if len(self._server_timeout_pages) >= (1 if slots <= 1 else 2):
                self._restart_timed_out_server(slots=slots)
        return MineruServerTimeout(
            f"MinerU server did not answer within {budget_s}s (pages {start}–{end}, path={pdf_path})",
            timed_out=True,
        )

    def _restart_timed_out_server(self, *, slots: int | None = None) -> None:
        """Replace the registered server whose rung-1 requests timed out, or mark it down.

        Only through :meth:`_restart_broken_mineru_server`, so the machine-wide cooldown and the
        per-extractor restart budget bound it, and by the pid that function takes: a restart
        without it only probes a live server (RF-16). A server nexus may not spawn (autostart
        off, a remote URL) or does not own (a local URL that is not the registered server's) is
        never restarted. With no restart, the server is marked down and later pages use the
        subprocess path. Logged with the page numbers and the old pid.
        """
        from nexus._mineru_health import is_registered_server_url  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus._mineru_pid import read_pid_file  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.config import get_mineru_server_url  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.daemon.mineru_lifecycle import spawn_policy_allows  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        timed_out = list(self._server_timeout_pages)
        self._server_timeout_pages.clear()  # whatever happens next starts a fresh count
        url = get_mineru_server_url()
        info = read_pid_file()
        if url is not None and info is not None and is_registered_server_url(url, info) \
                and spawn_policy_allows(url):
            _log.warning(
                "mineru_server_timeout_restart", pages=timed_out, old_pid=info.get("pid"),
                slots=slots if slots is not None else _mineru_server_slots(),
            )
            self._restart_skipped = ""
            # "replacing it" is printed from inside the restart, once it is decided (after the
            # budget, the spawn policy, the machine-wide cooldown and the pid check have let it
            # through), so a skipped restart never says it and a long health wait is not silent.
            # The outcome line follows (nexus-tr1rj.25).
            pages = ", ".join(timed_out)
            self._restart_announce = lambda: _progress(
                f"  MinerU server timed out on page(s) {pages} — replacing it (pid {info.get('pid')})."
            )
            try:
                restarted = self._restart_broken_mineru_server(info)
            finally:
                self._restart_announce = None
            taken_over_by = self._restart_taken_over_by
            if restarted:
                # nexus-tr1rj.26: the pid file named a different live server once the election was
                # held, so another process had already replaced this one; this process spawned nothing.
                who = (
                    f"another process already replaced it (old pid {info.get('pid')}, now pid {taken_over_by})"
                    if taken_over_by is not None else f"replaced it (old pid {info.get('pid')})"
                )
                _progress(f"  MinerU server timed out on page(s) {pages} — {who}.")
                return
            if taken_over_by is not None:
                _progress(
                    f"  MinerU server timed out on page(s) {pages} — another process replaced it "
                    f"(now pid {taken_over_by}) and that server did not come up; later pages run in a "
                    "subprocess for now."
                )
            else:
                _progress(
                    f"  MinerU server timed out on page(s) {', '.join(timed_out)} — "
                    f"{_restart_skip_text(self._restart_skipped, info.get('pid'))}"
                )
        else:
            _log.warning(
                "mineru_server_timeout_not_restartable", pages=timed_out, url=url,
                note="a server nexus does not own or may not spawn is never restarted",
            )
        self._mark_mineru_server_down()

    def _mineru_run_isolated(
        self, pdf_path: Path, start: int, end: int | None, formula_enable: bool = True,
        use_server: bool = True,
    ) -> tuple[str, list[dict], list[dict]]:
        """Dispatch to server or subprocess based on server availability.

        *formula_enable* False is the ladder's rung 2 (RDR-237) and rides every
        call below. Rung 1 passes nothing, so its calls keep the shape they had
        before the ladder.

        *use_server* False (RDR-237) goes straight to the subprocess: the ladder sets it for
        the rung after a server request that timed out.

        A server request that times out raises :class:`MineruServerTimeout` (the page descends
        a rung; see :meth:`_mineru_server_timed_out`). A dropped connection (``ConnectError``,
        ``ConnectTimeout``, ``ReadError``, ``RemoteProtocolError``) invalidates the server,
        tries one restart and retries, then falls to the subprocess. Every attempt below opens
        with a heartbeat, in the primitive that makes it."""
        rung = {} if formula_enable else {"formula_enable": False}
        if use_server and self._mineru_server_available():
            try:
                result = self._mineru_run_via_server(pdf_path, start, end, **rung)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                    httpx.RemoteProtocolError) as exc:
                if formula_enable:
                    self._server_timeout_pages.clear()
                # Server crashed — invalidate cache, try restart
                self._mineru_server_checked = True
                self._mineru_server_up = False
                _log.warning("mineru_server_lost", path=str(pdf_path),
                             pages=f"{start}–{end}", error=str(exc))
                if self._restart_mineru_server():
                    # Retry this page on the new server
                    try:
                        return self._mineru_run_via_server(pdf_path, start, end, **rung)
                    except Exception:  # noqa: BLE001 — best-effort server call; falls through to subprocess mode
                        pass  # fall through to subprocess
                # nexus-5ny9r: a lost-then-unrestartable server is the same
                # degrade as "no live server" and gets the same expiring
                # verdict, so an operator restart mid-run is picked up.
                self._mineru_server_recheck_after = time.monotonic() + _MINERU_SERVER_RECHECK_S
                return self._mineru_run_subprocess(pdf_path, start, end, **rung)
            except httpx.TimeoutException as exc:
                # Not a lost server: it is slow, or wedged on an earlier parse. ConnectTimeout, the
                # one timeout that does mean "nothing answered", is caught above.
                raise self._mineru_server_timed_out(
                    pdf_path, start, end, exc, rung_one=formula_enable,
                ) from exc
            except httpx.HTTPStatusError as exc:
                if formula_enable:
                    self._server_timeout_pages.clear()
                _log.warning("mineru_server_error", path=str(pdf_path),
                             pages=f"{start}–{end}", error=str(exc))
                return self._mineru_after_server_error(pdf_path, start, end, exc, **rung)
            else:
                if formula_enable:
                    self._server_timeout_pages.clear()  # a request that answered ends the streak
                return result
        return self._mineru_run_subprocess(pdf_path, start, end, **rung)

    def _mineru_after_server_error(
        self, pdf_path: Path, start: int, end: int | None,
        exc: httpx.HTTPStatusError, formula_enable: bool = True,
    ) -> tuple[str, list[dict], list[dict]]:
        """Decide what a server-reported task failure means (nexus-x3m8e).

        * ENOSPC on a machine with no POSIX semaphores left is the MACHINE's
          fault: raise :class:`MineruMachineResourceError` (not a
          ``RuntimeError``, so the bisect ladder does not retry halves of a
          failure no page split can fix). A full disk also says ENOSPC, so the
          claim is made only after a probe on this box fails to create one.
        * A ``FileNotFoundError`` from the REGISTERED server that is about the
          server, not the request, restarts it ONCE from a stable cwd and
          retries this range: the failing URL must be the pid file's server
          (an operator-pinned or remote server is not ours to kill), and its
          working directory must be gone or the error must name a path the
          server owns (:func:`nexus._mineru_health.server_is_broken`). A
          missing PDF names neither, so it restarts nothing: it used to kill
          the machine-shared server and every other indexer's in-flight work.
          Bounded by a machine-wide cooldown and the restart budget, never a
          loop.
        * Anything else degrades to the subprocess path, as before.
        """
        from nexus._mineru_health import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            is_server_file_not_found,
            looks_like_enospc,
            semaphores_exhausted,
            server_is_broken,
        )
        from nexus._mineru_pid import read_pid_file  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        rung = {} if formula_enable else {"formula_enable": False}
        error_text = _http_error_text(exc)
        if looks_like_enospc(error_text):
            machine = semaphores_exhausted()
            if machine is not None:
                _log.error("mineru_semaphores_exhausted", path=str(pdf_path),
                           server_error=error_text[:300])
                raise MineruMachineResourceError(machine) from exc
        if is_server_file_not_found(error_text):
            try:
                failing_url = str(exc.request.url)
            except Exception:  # noqa: BLE001 — an exception built without a request: not attributable to a server
                failing_url = ""
            info = read_pid_file()
            if server_is_broken(failing_url, error_text, info) and self._restart_broken_mineru_server(info):
                try:
                    return self._mineru_run_via_server(pdf_path, start, end, **rung)
                except Exception as retry_exc:  # noqa: BLE001 — one retry; the subprocess path is the fallback
                    _log.warning("mineru_retry_after_restart_failed",
                                 path=str(pdf_path), error=str(retry_exc))
        return self._mineru_run_subprocess(pdf_path, start, end, **rung)

    def _restart_broken_mineru_server(self, info: dict | None) -> bool:
        """Replace the registered server *info* describes, found up but broken.
        Bounded: one replacement per ``BROKEN_RESTART_COOLDOWN_S`` across the
        whole machine (``claim_broken_restart``), and only inside
        ``_MINERU_MAX_RESTARTS``. Logged. The budget and the spawn policy are checked BEFORE
        the claim: the claim stamps the cooldown for every process on the machine, so a refusal
        that comes after it would burn the cooldown on a restart that never happens."""
        from nexus._mineru_health import claim_broken_restart  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        self._restart_skipped = ""
        self._restart_taken_over_by = None
        if info is None or not isinstance(info.get("pid"), int):
            self._restart_skipped = "no-pid"
            return False
        if self._restart_refused():
            self._restart_skipped = "refused"
            return False
        if not claim_broken_restart():
            _log.warning("mineru_broken_restart_in_cooldown")
            self._restart_skipped = "cooldown"
            return False
        _log.warning("mineru_server_broken_restarting", pid=info["pid"])
        restarted = self._restart_mineru_server(replace_pid=info["pid"])
        if not restarted and not self._restart_skipped:
            self._restart_skipped = "failed"  # attempted: the pid check, the stop or the spawn ran
        return restarted

    def _mineru_run_subprocess(
        self, pdf_path: Path, start: int, end: int | None, formula_enable: bool = True,
    ) -> tuple[str, list[dict], list[dict]]:
        """Run MinerU in a fresh OS process for full memory isolation.

        Uses ``subprocess.run`` with an inline Python script so the child
        loads MinerU models independently.  When the child exits, all
        GPU/model memory is reclaimed by the OS — no leaks across batches.

        *formula_enable* False is the ladder's rung 2 (RDR-237): the worker is
        told through its sixth argument to skip the formula model.

        RDR-237: the run opens with a heartbeat, before anything is spawned.
        """
        self._beat()
        _mineru_check_range(start, end, self._mineru_run_total_pages)
        result_dir = tempfile.mkdtemp()
        try:
            import os as _os  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local
            from nexus.util.process_group import KILL_SIGNAL  # noqa: PLC0415 — deferred import — branch-local, matches this block's own imports

            # RDR-148 Gap 6: optional RLIMIT_AS address-space ceiling on the
            # worker. LINUX-GATED: macOS raises ValueError on
            # setrlimit(RLIMIT_AS) and does not enforce it (verified spike,
            # RDR-148 Spike Result 1) — an ungated preexec crashes darwin. When
            # a ceiling IS applied, set _mineru_ceiling_applied so the Gap 5
            # classifier treats any non-zero exit as OOM (a breach can surface
            # as a plain non-zero exit, not only SIGKILL / the sentinel).
            from nexus.config import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
                get_mineru_memory_ceiling_mb,
                get_mineru_page_timeout_s,
            )
            ceiling_mb = get_mineru_memory_ceiling_mb()
            preexec_fn = None
            if ceiling_mb > 0 and sys.platform.startswith("linux"):
                ceiling_bytes = ceiling_mb * 1024 * 1024

                def _apply_rlimit() -> None:  # pragma: no cover — runs in the child after fork
                    import resource  # noqa: PLC0415 — child-only, POSIX
                    resource.setrlimit(
                        resource.RLIMIT_AS, (ceiling_bytes, ceiling_bytes),
                    )

                preexec_fn = _apply_rlimit
            elif ceiling_mb > 0:
                # Operator configured a ceiling but it cannot be enforced here —
                # surface it loudly (a silent no-op resource cap is a hazard).
                _log.warning(
                    "mineru_memory_ceiling_unenforced_on_platform",
                    platform=sys.platform, ceiling_mb=ceiling_mb,
                    consequence="RLIMIT_AS is Linux-only; relying on the OS OOM-killer",
                )
            # Reflect THIS call's ceiling state for the OOM classifier.
            self._mineru_ceiling_applied = preexec_fn is not None

            # RDR-148 Gap 6: per-page wall-clock budget replaces the old fixed
            # batch-level 180s. For the whole-doc end==None batch the page count
            # is supplied by the caller (total_pages, already opened upstream) so
            # the budget scales without re-opening the PDF; explicit batches
            # scale by their span. Falls back to 1 page (the old flat 180s, no
            # regression) when total_pages is unknown (direct subprocess calls).
            timeout_s = get_mineru_page_timeout_s() * self._mineru_attempt_pages(start, end)

            # Short-lived per-batch worker (Gap 4 carve-out): stdin and stdout
            # stay DEVNULL, a judged choice — failure is returncode-detected by
            # the caller (killpg + the Gap 5 OOM classification below). stderr
            # goes to a file in result_dir (not a pipe: nothing can block on a
            # full buffer) so a failure carries the traceback's tail
            # (nexus-11x9o: a cv2 that could not load was visible nowhere).
            from nexus.util.process_group import contain, isolation_popen_kwargs  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

            from nexus.util.stable_cwd import stable_daemon_cwd  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

            stderr_path = Path(result_dir) / "worker-stderr.log"
            with open(stderr_path, "wb") as stderr_file:  # the child inherits the fd; the parent's copy closes at once
                proc = subprocess.Popen(
                    [
                        sys.executable, "-c", _MINERU_WORKER_SCRIPT,
                        # Absolute: the worker starts in the filesystem root below.
                        os.path.abspath(pdf_path), result_dir,
                        # Inclusive, like the server path: the worker passes this
                        # straight to do_parse's end_page_id.
                        str(start), "none" if end is None else str(_mineru_end_page_id(end)),
                        "1" if formula_enable else "0",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    preexec_fn=preexec_fn,  # Linux-only RLIMIT_AS ceiling (or None)
                    # nexus-x3m8e: not the caller's directory, which may be a
                    # disposable worktree deleted under a long page run (any
                    # getcwd() in MinerU or torch then raises). Short-lived and
                    # bounded, but it imports the same libraries the server does.
                    cwd=stable_daemon_cwd(),
                    **isolation_popen_kwargs(),  # own process group (POSIX) / job object (Windows)
                )
            # Use killpg(getpgid(pid)) rather than killpg(pid) directly —
            # with start_new_session=True the pgid equals pid at spawn time,
            # but by the time we kill the child may be dead and the PID
            # recycled by the kernel. getpgid() resolves the current pgid
            # from the live PID slot (raises ProcessLookupError if the
            # process is already gone, which we swallow). Matches the
            # session.py:301 idiom (indexing review C1).
            # nexus-5ny9r: the worker's session id, recorded while it is
            # alive. start_new_session=True makes pgid == pid at spawn; once
            # the worker exits and is reaped, safe_killpg's os.getpgid(pid)
            # raises and the post-exit sweep silently did nothing, so
            # MinerU's multiprocessing pool child and resource tracker
            # (spawned inside do_parse, abandoned by the worker's os._exit)
            # survived every page, reparented to init: 324 on one box after
            # a 38-page fallback run. Sweeping by the recorded group id
            # reaches them whether or not the leader still exists.
            worker_pgid = proc.pid
            # nexus-6y4e0: Windows job-object containment, assigned right
            # after spawn. No-op (None) on POSIX -- the process group above
            # already covers it.
            worker_job = contain(proc)

            def _killpg_safe() -> None:
                # Delegated to nexus.util.process_group so the mock-guard +
                # error-swallow contract is consistent across every
                # subprocess cleanup site in the codebase.
                from nexus.util.process_group import kill_tree, safe_killpg_group  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

                # On Windows, kill_tree closes the job (reaching the whole
                # pool the worker spawned) when containment succeeded at
                # spawn; otherwise it degrades to the direct worker only,
                # exactly as before nexus-6y4e0. nexus-6y4e0 review
                # (SIGNIFICANT): the return value used to be discarded --
                # on Windows safe_killpg_group below is ALWAYS a no-op (no
                # process groups there), so a failed close_job (job already
                # gone, or a genuine API failure) left the worker itself
                # running with nothing left to fall back to, and the
                # TimeoutExpired branch's untimed proc.wait() right after
                # this call would then hang on a worker that was never
                # actually killed. Matches the fallback the other three
                # contain() call sites already use.
                #
                # nexus-x3m8e: on POSIX both calls below are hard group kills
                # that spare the group's multiprocessing resource tracker (it
                # is what unlinks the pool's named semaphores once the worker,
                # which os._exit()s, is gone); that rule lives in
                # nexus.util.process_group, not here.
                if not kill_tree(proc, worker_job):
                    with contextlib.suppress(Exception):
                        proc.kill()
                safe_killpg_group(worker_pgid)

            live = _LiveWorker(_killpg_safe)
            with _LIVE_MINERU_WORKERS_LOCK:
                _LIVE_MINERU_WORKERS.add(live)
            run_cancel = getattr(_RUN_CANCEL, "event", None)
            if run_cancel is not None and run_cancel.is_set():
                live.stopped = True
                _killpg_safe()
            try:
                returncode = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _killpg_safe()
                proc.wait()
                raise MineruWorkerError(
                    f"MinerU subprocess timed out after {timeout_s}s "
                    f"(pages {start}–{end}, path={pdf_path})",
                    timed_out=True,
                )
            except BaseException:
                # nexus-6y4e0 measurement review: any OTHER exception out of
                # the wait (KeyboardInterrupt, a signal-driven interrupt)
                # used to skip every cleanup path, leaving the worker's job
                # open and its tree running until this process exited. The
                # other three contain() sites release in an unconditional
                # finally; this is that same guarantee for this site.
                _killpg_safe()
                raise
            finally:
                with _LIVE_MINERU_WORKERS_LOCK:
                    _LIVE_MINERU_WORKERS.discard(live)
            if live.stopped:
                # nexus-4vx0a: killed by stop_mineru_workers, not by memory:
                # the SIGKILL return code must not be read as an OOM.
                _killpg_safe()
                raise MineruWorkerStopped(
                    f"MinerU worker stopped: the run was interrupted (pages {start}–{end}, path={pdf_path})"
                )
            if returncode != 0:
                # Clean up any orphaned children in the process group
                _killpg_safe()
                # RDR-148 Gap 5: 3-way OOM classification. A memory exhaustion
                # surfaces three ways: (1) OS OOM-killer / macOS jetsam ->
                # negative SIGKILL returncode; (2) an RLIMIT_AS breach caught
                # in-process -> the _MINERU_OOM_EXIT sentinel; (3) once a memory
                # ceiling is applied (Gap 6), any non-zero exit is treated as a
                # breach. The SIGKILL-only mapping would miss (2) and (3) — the
                # gate finding that motivated this classification.
                is_oom = (
                    returncode == -KILL_SIGNAL  # POSIX only: Windows exit codes are never negative
                    or returncode == _MINERU_OOM_EXIT
                    or self._mineru_ceiling_applied
                )
                stderr_tail = _read_stderr_tail(stderr_path)
                _log.error(
                    "mineru_subprocess_failed",
                    returncode=returncode,
                    classified_oom=is_oom,
                    pages=f"{start}–{end}",
                    path=str(pdf_path),
                    stderr_tail=stderr_tail,
                )
                msg = (
                    f"MinerU subprocess exited with code {returncode} "
                    f"(pages {start}–{end}, path={pdf_path})"
                )
                # RDR-237: the facts the failure class is keyed on ride as attributes.
                error_cls = MineruMemoryError if is_oom else MineruWorkerError
                raise error_cls(
                    msg, returncode=returncode, stderr_last_line=_last_stderr_line(stderr_tail),
                )
            # Kill any lingering workers in the process group
            _killpg_safe()

            pdf_name = pdf_path.name
            base = Path(result_dir) / pdf_name / "auto"
            # Indexing review I2: assume MinerU's output layout but fail
            # loudly with a useful message when it diverges (e.g. version
            # upgrade changes the "auto" directory name). The subprocess
            # already exited 0, so a missing output file is an unexpected
            # state not a runtime error.
            md_file = base / f"{pdf_name}.md"
            if not md_file.exists():
                raise RuntimeError(
                    f"MinerU produced no output at {md_file} "
                    f"(subprocess exited 0; layout may have changed). "
                    f"Pages {start}–{end}, path={pdf_path}"
                )
            md = md_file.read_text(encoding="utf-8")
            content_list: list[dict] = json.loads(
                (base / f"{pdf_name}_content_list.json").read_text(encoding="utf-8")
            )
            middle: dict = json.loads(
                (base / f"{pdf_name}_middle.json").read_text(encoding="utf-8")
            )
            return md, content_list, middle.get("pdf_info", [])
        finally:
            import shutil  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local
            shutil.rmtree(result_dir, ignore_errors=True)

    @staticmethod
    def _mineru_build_result(
        pdf_path: Path, md_text: str,
        content_list: list[dict], pdf_info: list[dict],
        *,
        per_page_lengths: list[tuple[int, int]] | None = None,
        total_pages: int | None = None,
        extraction_pages: dict[str, list[int]] | None = None,
        table_defects: list[dict] | None = None,
    ) -> ExtractionResult:
        """Assemble an ExtractionResult from (merged) MinerU outputs.

        *per_page_lengths* (nexus-2fyb code-review C2): list of
        ``(page_index_0based, markdown_char_length)`` tuples captured from
        the batch loop. Used to build accurate ``page_boundaries`` so
        chunks get correct ``page_number`` attribution. When ``None``
        (legacy callers, defensive), falls back to uniform char/page
        distribution — but logs a warning because that path produces
        wrong page_number metadata for any non-uniform document.

        *total_pages* (RDR-237): the PDF's own page count. The document's page
        count and page boundaries come from it and the per-page outcomes, not
        from ``len(pdf_info)``, which a page that settled on the text layer or
        on nothing leaves short. ``None`` (legacy callers) falls back to
        ``len(pdf_info)``.

        *extraction_pages* (RDR-237): rung (``mineru-no-formula``,
        ``pymupdf-text``, ``unextracted``) to the sorted 1-based pages that
        settled there; empty or ``None`` when every page settled on rung 1. A
        bare ``extraction_method="mineru"`` would overstate coverage when even
        one page was not MinerU-with-formulas output (nexus-1oguj's rule), so
        any degradation flips the recorded value to the honest aggregate
        ``"mineru+degraded"``.
        """
        extraction_pages = extraction_pages or {}
        display_count = sum(1 for e in content_list if e.get("type") == "equation")

        inline_count = 0
        for page in pdf_info:
            for block in page.get("para_blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("type") == "inline_equation":
                            inline_count += 1

        formula_count = display_count + inline_count
        page_count = total_pages if total_pages is not None else len(pdf_info)

        # md_text is already normalized: _extract_with_mineru applies
        # _normalize_mineru_latex + _unwrap_mineru_font_tags per-page so per_page_lengths and
        # page_boundaries are consistent with the stored text.
        total_len = len(md_text)

        page_boundaries: list[dict] = []
        if per_page_lengths is not None and page_count > 0:
            # Use the real per-page lengths captured from the batch loop.
            # md_text is "\n".join(md_parts), so each per-page segment has
            # +1 separator except the last. start_char accumulates.
            page_lengths_by_idx = {idx: length for idx, length in per_page_lengths}
            pos = 0
            for i in range(page_count):
                length = page_lengths_by_idx.get(i, 0)
                # Add +1 for the "\n" separator (matches the join), except final.
                stored_length = length + (1 if i < page_count - 1 else 0)
                page_boundaries.append({
                    "page_number": i + 1,
                    "start_char": pos,
                    "page_text_length": stored_length,
                })
                pos += stored_length
        elif page_count > 0 and total_len > 0:
            # Fallback (legacy callers, no per-batch tracking). Uniform
            # distribution gives wrong page_number for non-uniform docs.
            _log.warning(
                "mineru_uniform_page_boundaries",
                path=str(pdf_path),
                page_count=page_count,
                reason="per_page_lengths not provided",
            )
            chars_per_page = total_len / page_count
            for i in range(page_count):
                start = int(i * chars_per_page)
                length = int(chars_per_page) + (1 if i < page_count - 1 else 0)
                page_boundaries.append({
                    "page_number": i + 1,
                    "start_char": start,
                    "page_text_length": length,
                })

        if formula_count > 0:
            _log.info(
                "mineru_formulas_extracted",
                formula_count=formula_count,
                path=str(pdf_path),
            )

        extraction_method = "mineru+degraded" if extraction_pages else "mineru"
        # nexus-jd8fi: tables MinerU rendered as text, keyed by the page they
        # sit on, so PDFChunker tags their chunks ``table_page`` exactly as it
        # does for MinerU's own tables. ``page_idx`` in a MinerU content_list is relative
        # to the batch that produced it; _extract_with_mineru rebases it to
        # the document before it lands here (``doc_page_idx``).
        table_regions = [
            {"page": e.get("doc_page_idx", e.get("page_idx", 0)) + 1}
            for e in content_list
            if e.get("type") == "table" and e.get("table_body")
        ]
        if table_defects:
            _log.warning(
                "table_structure_suspect",
                count=len(table_defects),
                tables=sorted({d.get("label") or "(unlabelled)" for d in table_defects}),
                path=str(pdf_path),
            )
        return ExtractionResult(
            text=md_text,
            metadata={
                "extraction_method": extraction_method,
                "extraction_pages": {m: sorted(p) for m, p in extraction_pages.items()},
                "page_count": page_count,
                "format": "markdown",
                "formula_count": formula_count,
                "page_boundaries": page_boundaries,
                "table_regions": table_regions,
                "table_defects": table_defects or [],
                **_read_pdf_info(pdf_path),
            },
        )

    def _extract_pymupdf(
        self,
        pdf_path: Path,
        *,
        on_page: Callable[[int, str, dict], None] | None = None,
    ) -> ExtractionResult:
        """The explicit ``pymupdf`` extractor (RDR-237): every page's own text layer, nothing else.

        Runs the ladder's one-page rung-3 read (:func:`_pymupdf_page_text`) on every page.
        A page with a text layer settles
        ``pymupdf-text``; a page without one (an image-only page, or one PyMuPDF cannot read)
        settles ``unextracted``. Like the MinerU ladder, ``on_page`` fires for EVERY page, an
        unextracted one with ``""``, each carrying ``page_method`` so the pipeline buffer can
        rebuild the document's ``extraction_pages`` on a resume, and every page has a page
        boundary. The document's ``extraction_method`` is ``pymupdf``; ``extraction_pages`` lists
        every page under its rung. It needs no PDF stack and never starts or probes MinerU.

        Raises :class:`UnextractableContentError` when no page has a text layer, the way the
        ladder fails a document whose every page is unextracted: indexing would write nothing.
        A password-protected PDF, one with no pages and one whose every page PyMuPDF cannot read
        each fail with a message of their own: none of them is an image-only document to OCR.
        """
        import pymupdf  # lazy  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

        fname = pdf_path.name
        parts: list[str] = []
        outcomes: dict[int, str] = {}
        unreadable: list[str] = []  # the error of each page PyMuPDF raised on
        with pymupdf.open(pdf_path) as doc:
            if doc.needs_pass:
                raise UnextractableContentError(
                    f"{fname} is password-protected: PyMuPDF cannot read its text layer without the "
                    f"password. Remove the password, then index it again."
                )
            total_pages = len(doc)
            if total_pages == 0:
                raise UnextractableContentError(f"{fname} has no pages, so there is nothing to extract.")
            doc_meta = doc.metadata or {}
            for index in range(total_pages):
                started = time.monotonic()
                try:
                    text = _pymupdf_page_text(doc[index])
                except Exception as read_exc:  # noqa: BLE001 — a damaged page is unextracted; the rest of the document is still read
                    error = f"{type(read_exc).__name__}: {read_exc}"
                    _log.warning(
                        "pymupdf_page_text_layer_unreadable", page=index + 1, path=str(pdf_path), error=error,
                    )
                    unreadable.append(error)
                    text = ""
                method = "pymupdf-text" if text else "unextracted"
                outcomes[index + 1] = method
                parts.append(text)
                if on_page is not None:
                    on_page(index, text, {"page_number": index + 1, "text_length": len(text), "page_method": method})
                _log.debug("pymupdf_page", page=index + 1, method=method, seconds=time.monotonic() - started)

        if len(unreadable) == total_pages:
            raise UnextractableContentError(
                f"PyMuPDF could not read any of the {total_pages} page(s) of {fname} (the first error: "
                f"{unreadable[0]}); the file is damaged or in a form it cannot open. OCR will not help: "
                f"repair or re-export the PDF."
            )
        if not any(m == "pymupdf-text" for m in outcomes.values()):
            if unreadable:
                # Some pages raised, the rest are image-only: neither "OCR it" nor "repair it" alone is true.
                raise UnextractableContentError(
                    f"every page of {fname} is unextracted, from two causes. PyMuPDF could not read "
                    f"{len(unreadable)} page(s) (the first error: {unreadable[0]}): repair or re-export "
                    f"the PDF. The other {total_pages - len(unreadable)} page(s) are image-only, with no "
                    f"text layer: OCR the PDF first, or index it with `--extractor mineru` (needs the "
                    f"PDF stack)."
                )
            raise UnextractableContentError(
                f"every page of {fname} is unextracted: `--extractor pymupdf` reads only a PDF's own "
                f"text layer, and none of its {total_pages} page(s) has one. OCR the PDF first, or "
                f"index it with `--extractor mineru` (needs the PDF stack)."
            )

        extraction_pages = {
            method: [page for page, m in outcomes.items() if m == method]
            for method in _DEGRADED_METHODS
            if any(m == method for m in outcomes.values())
        }
        # "pymupdf-text" is in _DEGRADED_METHODS (it is a rung below MinerU's), so the map above
        # already lists every page under its rung: text pages and unextracted ones alike.
        page_boundaries: list[dict] = []
        pos = 0
        for i, text in enumerate(parts):
            # +1 for the "\n" separator of the join below, except the last page (as the MinerU build).
            length = len(text) + (1 if i < total_pages - 1 else 0)
            page_boundaries.append({"page_number": i + 1, "start_char": pos, "page_text_length": length})
            pos += length

        return ExtractionResult(
            text="\n".join(parts),
            metadata={
                "extraction_method": "pymupdf",
                "extraction_pages": extraction_pages,
                "page_count": total_pages,
                "format": "normalized",
                "page_boundaries": page_boundaries,
                "table_regions": [],
                **_pdf_info_fields(doc_meta),
                "formula_count": 0,
            },
        )
