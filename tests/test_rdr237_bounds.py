# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 Phase 1 Step 2 (nexus-tr1rj.2): the bounds on a MinerU attempt, and the heartbeat.

Driven through ``PDFExtractor.extract`` over a real PDF, with a fake clock, a fake MinerU server
(``httpx.post``) and a fake worker (``subprocess.Popen``) that the extractor's own server and
subprocess code run against, so what is asserted is the code that ships:

* the server request's timeout is ``mineru_page_timeout_s`` x the pages of the range;
* a server timeout sends the page to the NEXT rung on the subprocess path, never again to the server,
  never marks the server down; the restart rule (one slot: the first rung-1 timeout; more: the second
  consecutive one) goes through ``_restart_broken_mineru_server`` only and never touches a server
  nexus may not spawn;
* the heartbeat opens every attempt (each server request, each subprocess run, each bisect step, a
  forced fault), ticks during the two waits, and what it raises is a stop, never a ladder failure;
* the start-of-run check of ``mineru_page_timeout_s`` x ``mineru_page_batch`` against 240 s, and
  ``nx config set``'s warning.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pymupdf
import pytest
import yaml
from click.testing import CliRunner
from structlog.testing import capture_logs

from nexus._mineru_health import _RESTART_STAMP_NAME, _restart_stamp_path, is_registered_server_url
from nexus.cli import main
from nexus.config import (
    MINERU_ATTEMPT_BOUND_S,
    PdfConfigError,
    get_pdf_config,
    mineru_attempt_bound_problem,
)
from nexus.daemon.mineru_lifecycle import HEARTBEAT_EVERY_S, ensure_mineru_running
from nexus.db.http_pipeline_client import PipelineRunFenced
from nexus.pdf_extractor import (
    ExtractionResult,
    MineruBoundsError,
    MineruServerTimeout,
    PDFExtractor,
    _failure_class,
    _mineru_server_slots,
)
from nexus.pipeline_stages import PipelineCancelled
from tests._module_seam import patch_in

URL = "http://127.0.0.1:8010"
REGISTERED = {"pid": 31337, "port": 8010, "output_root": "/var/rdr237/out", "python": "/opt/rdr237/python"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A private config dir (the restart cooldown stamp lives beside the pid file), quiet progress,
    no forced faults, and a clean slot environment."""
    cd = tmp_path / "nexus-config"
    cd.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cd))
    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.delenv("NX_MINERU_FAULT_PAGES", raising=False)
    monkeypatch.delenv("MINERU_API_MAX_CONCURRENT_REQUESTS", raising=False)
    monkeypatch.setattr("nexus.config._LADDER_BREAKER_WARNED", set())


# ── the world: a clock, a server, a worker ───────────────────────────────────


def make_pdf(path: Path, pages: int) -> Path:
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 72), f"text layer of page {n + 1}")
    doc.save(str(path))
    doc.close()
    return path


class World:
    """A fake clock and the MinerU the extractor talks to.

    ``events`` is the one timeline: ``("beat", t)``, ``("page", t, index)``, ``("server", t, start,
    span, rung, timeout)``, ``("sub", t, start, span, rung, timeout)``. Time only moves when a fake
    server or worker "works" (a request costs *work_s*, a timeout costs its whole budget), so a gap is
    the sum of the attempts between two progress events, on any box.

    *plan* ``(route, start, span, rung) -> outcome`` (route ``server`` or ``sub``, start 0-based):
    ``ok``, ``timeout``, ``connect``, ``connect_timeout``, ``read_error``, ``("http", status)`` for the
    server; ``ok``, ``timeout``, ``("exit", rc)`` for the worker. Anything else is ``ok``.
    """

    def __init__(self, tmp_path: Path, pages: int, plan: Callable | None = None, *, work_s: float = 5.0) -> None:
        self.pdf = make_pdf(tmp_path / "doc.pdf", pages)
        self.pages = pages
        self.now = 1000.0
        self.t0 = self.now
        self.events: list[tuple] = []
        self.plan = plan or (lambda route, start, span, rung: "ok")
        self.work_s = work_s
        self.healthy = True

    # -- the fakes ----------------------------------------------------------

    def _body(self, start: int, span: int, rung: int, route: str) -> tuple[str, list, list]:
        md = f"{route} pages {start + 1}-{start + span} r{rung}"
        return md, [], [{"page_idx": i, "para_blocks": []} for i in range(span)]

    def post(self, url, files=None, data=None, timeout=None, **_kw):
        start = int(data["start_page_id"])
        span = min(int(data["end_page_id"]), self.pages - 1) - start + 1
        rung = 1 if data["formula_enable"] == "true" else 2
        self.events.append(("server", self.now, start, span, rung, timeout))
        outcome = self.plan("server", start, span, rung)
        if outcome == "timeout":
            self.now += timeout
            raise httpx.ReadTimeout("read timed out")
        if outcome == "connect":
            self.now += 1
            raise httpx.ConnectError("refused")
        if outcome == "connect_timeout":
            self.now += 1
            raise httpx.ConnectTimeout("connect timed out")
        if outcome == "read_error":
            self.now += 1
            raise httpx.ReadError("connection dropped mid-response")
        resp = MagicMock()
        if isinstance(outcome, tuple) and outcome[0] == "http":
            request = httpx.Request("POST", f"{URL}/file_parse")
            response = httpx.Response(outcome[1], json={"error": "boom"}, request=request)
            resp.raise_for_status.side_effect = httpx.HTTPStatusError("boom", request=request, response=response)
            self.now += 2
            return resp
        self.now += self.work_s
        md, content_list, info = self._body(start, span, rung, "server")
        resp.json.return_value = {"results": {self.pdf.stem: {
            "md_content": md, "content_list": json.dumps(content_list),
            "middle_json": json.dumps({"pdf_info": info}),
        }}}
        return resp

    def get(self, url, timeout=None, **_kw):
        if not self.healthy:
            raise httpx.ConnectError("refused")
        return MagicMock(status_code=200)

    def popen(self, args, **kwargs):
        pdf, result_dir, start_arg, end_arg, formula = args[-5:]
        start = int(start_arg)
        end_incl = self.pages - 1 if end_arg == "none" else int(end_arg)
        span = min(end_incl, self.pages - 1) - start + 1
        rung = 1 if formula == "1" else 2
        world = self
        proc = MagicMock(pid=4321)

        def wait(timeout=None):
            if timeout is None:  # the reap after a kill
                return -9
            outcome = world.plan("sub", start, span, rung)
            if outcome == "timeout":
                world.now += timeout
                raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout)
            if isinstance(outcome, tuple) and outcome[0] == "exit":
                world.now += 10
                return outcome[1]
            world.now += world.work_s
            md, content_list, info = world._body(start, span, rung, "sub")
            base = Path(result_dir) / Path(pdf).name / "auto"
            base.mkdir(parents=True)
            name = Path(pdf).name
            (base / f"{name}.md").write_text(md, encoding="utf-8")
            (base / f"{name}_content_list.json").write_text(json.dumps(content_list), encoding="utf-8")
            (base / f"{name}_middle.json").write_text(json.dumps({"pdf_info": info}), encoding="utf-8")
            return 0

        # The event is stamped when the run starts, which is when the stand-in is built.
        self.events.append(("sub", self.now, start, span, rung, None))
        proc.wait.side_effect = wait
        return proc

    # -- the run ------------------------------------------------------------

    def beat(self) -> None:
        self.events.append(("beat", self.now))

    def fault_wait(self, seconds: float) -> None:
        self.events.append(("fault_wait", self.now, seconds))
        self.now += seconds

    def attempts(self) -> list[tuple]:
        return [e for e in self.events if e[0] in ("server", "sub")]

    def progress_times(self, kinds: tuple[str, ...] = ("beat", "page")) -> list[float]:
        return [self.t0] + [e[1] for e in self.events if e[0] in kinds] + [self.now]

    def longest_gap(self, kinds: tuple[str, ...] = ("beat", "page")) -> float:
        times = self.progress_times(kinds)
        return max(b - a for a, b in zip(times, times[1:]))

    @contextlib.contextmanager
    def active(
        self, *, batch: int = 1, page_timeout: int = 180, breaker: int = 3, platform: str = "linux",
        registered: dict | None = REGISTERED, url: str = URL, server: bool = True,
        monkeypatch: pytest.MonkeyPatch | None = None, autostart: bool = True,
        patch_config: bool = True,
    ) -> Iterator[None]:
        assert monkeypatch is not None
        monkeypatch.setenv("NX_MINERU_AUTOSTART", "1" if autostart else "0")
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("nexus.pdf_stack.require"))
            stack.enter_context(patch("nexus.pdf_extractor.do_parse", object()))
            if patch_config:
                stack.enter_context(patch("nexus.config.get_mineru_page_batch", return_value=batch))
                stack.enter_context(patch("nexus.config.get_mineru_page_timeout_s", return_value=page_timeout))
            stack.enter_context(patch("nexus.config.get_pdf_ladder_breaker", return_value=breaker))
            stack.enter_context(patch("nexus.config.get_mineru_server_url", return_value=url))
            stack.enter_context(patch("nexus.daemon.mineru_lifecycle._healthy", return_value=False))
            stack.enter_context(patch("nexus._mineru_pid.read_pid_file", return_value=dict(registered) if registered else None))
            stack.enter_context(patch("nexus._mineru_health.process_cwd_state", return_value="ok"))
            stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
            stack.enter_context(patch("nexus.util.process_group.safe_killpg_group"))
            stack.enter_context(patch_in("nexus.pdf_extractor", "sys.platform", platform))
            stack.enter_context(patch_in("nexus.pdf_extractor", "httpx.post", self.post))
            stack.enter_context(patch_in("nexus.pdf_extractor", "httpx.get", self.get))
            stack.enter_context(patch_in("nexus.pdf_extractor", "subprocess.Popen", self.popen))
            stack.enter_context(patch("nexus.pdf_extractor._fault_wait", self.fault_wait))
            if not server:
                stack.enter_context(patch.object(PDFExtractor, "_mineru_server_available", return_value=False))
            yield

    def extract(
        self, ext: PDFExtractor | None = None, *, on_heartbeat: Callable[[], None] | None = None,
        on_page: Callable[[int, str, dict], None] | None = None,
    ):
        ext = ext or PDFExtractor()

        def heartbeat() -> None:
            self.beat()
            if on_heartbeat is not None:
                on_heartbeat()

        def page(index: int, text: str, meta: dict) -> None:
            if on_page is not None:
                on_page(index, text, meta)
            self.events.append(("page", self.now, index))

        return ext.extract(
            self.pdf, extractor="mineru", on_page=page, on_heartbeat=heartbeat, allow_degraded=True,
        )


def run(tmp_path, monkeypatch, pages, plan=None, *, ext=None, **kw):
    """Extract a *pages*-page PDF in a fresh world; ``(result, world, ext)``."""
    world = World(tmp_path, pages, plan)
    ext = ext or PDFExtractor()
    with world.active(monkeypatch=monkeypatch, **kw):
        result = world.extract(ext)
    return result, world, ext


def server_events(world: World) -> list[tuple]:
    return [e for e in world.events if e[0] == "server"]


def sub_events(world: World) -> list[tuple]:
    return [e for e in world.events if e[0] == "sub"]


def times_out(*keys: tuple[str, int, int]) -> Callable:
    """A plan that times out the named ``(route, page 1-based, rung)`` attempts."""
    wanted = set(keys)

    def plan(route, start, span, rung):
        return "timeout" if (route, start + 1, rung) in wanted else "ok"

    return plan


# ── 1. the server request's timeout ──────────────────────────────────────────


@pytest.mark.parametrize("page_timeout,batch,pages,expected", [
    (180, 1, 2, [180, 180]),           # the default: one page per request
    (60, 4, 4, [240]),                 # the whole document in one batch: 4 x 60
    (60, 4, 5, [240, 60]),             # the short last batch is budgeted by ITS pages
    (30, 3, 6, [90, 90]),
])
def test_the_server_request_timeout_is_the_page_budget_times_the_pages_in_the_range(
    tmp_path, monkeypatch, page_timeout, batch, pages, expected,
) -> None:
    _, world, _ = run(tmp_path, monkeypatch, pages, page_timeout=page_timeout, batch=batch)
    assert [e[5] for e in server_events(world)] == expected, "never the old fixed 300 s"


def test_the_server_and_the_worker_budget_the_same_range_alike(tmp_path, monkeypatch) -> None:
    ext = PDFExtractor()
    ext._mineru_run_total_pages = 9
    assert ext._mineru_attempt_pages(2, 5) == 3
    assert ext._mineru_attempt_pages(2, None) == 7, "the whole-document range runs to the last page"
    assert PDFExtractor()._mineru_attempt_pages(0, None) == 1, "no page total known: one page"


# ── 2. a server timeout: next rung, subprocess, server untouched ─────────────


def test_a_server_timeout_on_a_three_slot_server_retries_the_page_on_the_subprocess_at_the_next_rung(
    tmp_path, monkeypatch,
) -> None:
    restart = MagicMock(return_value=True)
    ext = PDFExtractor()
    with patch.object(ext, "_restart_broken_mineru_server", restart):
        result, world, ext = run(
            tmp_path, monkeypatch, 3, times_out(("server", 2, 1)), ext=ext, page_timeout=60, platform="linux",
        )

    shape = [(e[0], e[2] + 1, e[4]) for e in world.attempts()]
    assert shape == [("server", 1, 1), ("server", 2, 1), ("sub", 2, 2), ("server", 3, 1)], (
        "page 2: server at rung 1, then the SUBPROCESS at rung 2, never the server again"
    )
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [2]}
    restart.assert_not_called()
    assert ext._mineru_server_up is True, "a timeout does not mark the server down"
    assert ext._mineru_server_checked is True


def test_a_server_timeout_is_a_timeout_failure_class_and_a_runtime_error() -> None:
    exc = MineruServerTimeout("x", timed_out=True)
    assert isinstance(exc, RuntimeError)
    assert _failure_class(exc) == "timeout", "the breaker counts it with the worker's timeouts"


def test_a_page_that_times_out_on_rungs_1_and_2_settles_on_its_text_layer_within_the_bound(
    tmp_path, monkeypatch,
) -> None:
    result, world, _ = run(
        tmp_path, monkeypatch, 3, times_out(("server", 2, 1), ("sub", 2, 2)), page_timeout=180,
    )

    assert result.metadata["extraction_pages"] == {"pymupdf-text": [2]}
    assert world.longest_gap() <= 180, f"gap {world.longest_gap()}: one attempt at most"
    assert world.longest_gap() < MINERU_ATTEMPT_BOUND_S


# ── 3. the restart rule ──────────────────────────────────────────────────────


def _restart_spy(world: World) -> MagicMock:
    def restart(info):
        world.events.append(("restart", world.now, info["pid"]))
        return True

    return MagicMock(side_effect=restart)


def test_the_first_rung_1_timeout_on_a_one_slot_server_restarts_it_once_with_the_old_pid(
    tmp_path, monkeypatch,
) -> None:
    world = World(tmp_path, 4, times_out(("server", 2, 1)))
    ext = PDFExtractor()
    spy = _restart_spy(world)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="darwin"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        world.extract(ext)

    spy.assert_called_once_with(REGISTERED)
    kinds = [e[0] for e in world.events if e[0] in ("server", "sub", "restart")]
    assert kinds.index("restart") == kinds.index("server", 1) + 1, "right after page 2's timed-out request"
    assert [(e[0], e[2] + 1) for e in world.attempts()][-1] == ("server", 4), "later pages use the server"


def test_one_timeout_on_a_three_slot_server_restarts_nothing_and_two_consecutive_restart_once(
    tmp_path, monkeypatch,
) -> None:
    world = World(tmp_path, 5, times_out(("server", 2, 1), ("server", 3, 1)))
    ext = PDFExtractor()
    spy = _restart_spy(world)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        world.extract(ext)

    spy.assert_called_once_with(REGISTERED)
    order = [(e[0], e[2] + 1 if e[0] != "restart" else None) for e in world.events if e[0] in ("server", "sub", "restart")]
    assert order.index(("restart", None)) > order.index(("server", 3)), "after page 3's timeout, not page 2's"
    assert order.index(("restart", None)) < order.index(("server", 4)), "before page 4 opens"


def test_a_page_that_does_not_time_out_ends_the_streak(tmp_path, monkeypatch) -> None:
    world = World(tmp_path, 6, times_out(("server", 2, 1), ("server", 4, 1)))
    ext = PDFExtractor()
    spy = _restart_spy(world)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        world.extract(ext)

    spy.assert_not_called()  # timeout, success, timeout: never two consecutive


def test_a_server_error_ends_the_streak_too(tmp_path, monkeypatch) -> None:
    def plan(route, start, span, rung):
        if route == "server" and rung == 1:
            return {1: "timeout", 2: ("http", 500), 3: "timeout"}.get(start)
        return "ok"

    world = World(tmp_path, 4, plan)
    ext = PDFExtractor()
    spy = MagicMock(return_value=True)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        world.extract(ext)

    spy.assert_not_called()


@pytest.mark.parametrize("platform,env,expected", [
    ("darwin", None, 1),
    ("darwin", "8", 1),      # MinerU pins macOS to one slot whatever the environment says
    ("linux", None, 3),
    ("linux", "5", 5),
    ("linux", "1", 1),
    ("linux", "0", 3),       # not positive: MinerU would refuse to start; the default is the estimate
    ("linux", "many", 3),
    ("win32", None, 3),
])
def test_the_slot_count_is_read_the_way_mineru_reads_it(monkeypatch, platform, env, expected) -> None:
    if env is None:
        monkeypatch.delenv("MINERU_API_MAX_CONCURRENT_REQUESTS", raising=False)
    else:
        monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", env)
    with patch_in("nexus.pdf_extractor", "sys.platform", platform):
        assert _mineru_server_slots() == expected


def test_a_five_slot_server_restarts_after_two_consecutive_timeouts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "5")
    world = World(tmp_path, 4, times_out(("server", 1, 1), ("server", 2, 1)))
    ext = PDFExtractor()
    spy = MagicMock(return_value=True)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        world.extract(ext)
    spy.assert_called_once()


def test_a_restart_starts_a_fresh_count(tmp_path, monkeypatch) -> None:
    """Timeouts on pages 2 to 5 of a three-slot server: the restart after page 3 spends the first
    two, and pages 4 and 5 are the next pair."""
    world = World(tmp_path, 6, times_out(("server", 2, 1), ("server", 3, 1), ("server", 4, 1), ("server", 5, 1)))
    ext = PDFExtractor()
    spy = _restart_spy(world)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        with capture_logs() as logs:
            world.extract(ext)

    assert spy.call_count == 2, "after page 3 and after page 5, not after page 4"
    pairs = [e["pages"] for e in logs if e.get("event") == "mineru_server_timeout_restart"]
    assert pairs == [["2", "3"], ["4", "5"]]


def test_a_restart_cooldown_held_by_another_process_means_no_restart_and_the_server_is_marked_down(
    tmp_path, monkeypatch,
) -> None:
    world = World(tmp_path, 5, times_out(("server", 2, 1)))
    ext = PDFExtractor()
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="darwin"), \
            patch("nexus._mineru_health.claim_broken_restart", return_value=False) as claim, \
            patch.object(ext, "_restart_mineru_server") as restart:
        world.extract(ext)

    claim.assert_called_once()
    restart.assert_not_called()
    assert ext._mineru_server_up is False
    later = [(e[0], e[2] + 1) for e in world.attempts() if e[2] + 1 >= 3]
    assert later and all(route == "sub" for route, _ in later), f"pages after the timeout: {later}"


def test_an_exhausted_restart_budget_marks_the_server_down(tmp_path, monkeypatch) -> None:
    world = World(tmp_path, 4, times_out(("server", 2, 1)))
    ext = PDFExtractor()
    ext._mineru_server_restarts = ext._MINERU_MAX_RESTARTS
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="darwin"), \
            patch("nexus._mineru_health.claim_broken_restart", return_value=True), \
            patch("nexus.daemon.mineru_lifecycle.spawn_policy_allows", return_value=True):
        world.extract(ext)

    assert ext._mineru_server_up is False
    assert all(e[0] == "sub" for e in world.attempts() if e[2] + 1 >= 3)


@pytest.mark.parametrize("label,autostart,url,registered", [
    ("autostart off", False, URL, REGISTERED),
    ("a configured remote URL", True, "http://gpu-box.example.com:8010", REGISTERED),
    ("a local server that is not the registered one", True, "http://127.0.0.1:9999", REGISTERED),
    ("a local server with nothing registered", True, URL, None),
])
def test_a_server_nexus_may_not_spawn_is_marked_down_and_never_restarted(
    tmp_path, monkeypatch, label, autostart, url, registered,
) -> None:
    world = World(tmp_path, 5, times_out(("server", 2, 1)))
    ext = PDFExtractor()
    with world.active(
        monkeypatch=monkeypatch, page_timeout=60, platform="darwin", autostart=autostart, url=url,
        registered=registered,
    ), patch.object(ext, "_restart_broken_mineru_server") as broken, \
            patch.object(ext, "_restart_mineru_server") as restart, \
            patch("nexus._mineru_health.claim_broken_restart") as claim:
        with capture_logs() as logs:
            world.extract(ext)

    broken.assert_not_called()
    restart.assert_not_called()
    claim.assert_not_called()  # the cooldown is not even spent on a server that is not ours
    assert ext._mineru_server_up is False, label
    assert [e for e in logs if e.get("event") == "mineru_server_timeout_not_restartable"], label
    assert all(e[0] == "sub" for e in world.attempts() if e[2] + 1 >= 3), "later pages: subprocess"


def test_the_restart_is_logged_with_the_page_numbers_and_the_old_pid(tmp_path, monkeypatch) -> None:
    world = World(tmp_path, 4, times_out(("server", 2, 1), ("server", 3, 1)))
    ext = PDFExtractor()
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", return_value=True):
        with capture_logs() as logs:
            world.extract(ext)

    (restart,) = [e for e in logs if e.get("event") == "mineru_server_timeout_restart"]
    assert restart["pages"] == ["2", "3"] and restart["old_pid"] == REGISTERED["pid"]


# ── 4. a dropped connection is a lost server, a timeout is not ───────────────


@pytest.mark.parametrize("failure", ["connect", "read_error", "connect_timeout"])
def test_a_dropped_connection_is_handled_as_a_lost_server_inside_the_dispatch(
    tmp_path, monkeypatch, failure,
) -> None:
    """``httpx.ReadError`` used to escape the dispatch's catch and reach the ladder as a dropped
    connection; ``ConnectTimeout`` is a ``TimeoutException`` that means nothing answered at all."""
    seen = {"n": 0}

    def plan(route, start, span, rung):
        if route == "server" and start == 1 and rung == 1:
            seen["n"] += 1
            return failure if seen["n"] == 1 else "ok"
        return "ok"

    ext = PDFExtractor()
    with patch.object(ext, "_restart_mineru_server", return_value=False):
        with capture_logs() as logs:
            result, world, ext = run(tmp_path, monkeypatch, 3, plan, ext=ext, page_timeout=60)

    assert [e for e in logs if e.get("event") == "mineru_server_lost"], "the lost-server branch handled it"
    assert not [e for e in logs if e.get("event") == "mineru_dropped_connection_retry"], "the ladder never saw it"
    assert not [e for e in logs if e.get("event") == "mineru_server_timeout"]
    assert result.metadata["extraction_pages"] == {}, "page 2 settled on rung 1 via the subprocess"


# ── 5. the bound and its check ───────────────────────────────────────────────


def test_a_product_above_240_s_refuses_before_any_page_and_names_both_settings(tmp_path, monkeypatch) -> None:
    world = World(tmp_path, 4)
    with world.active(monkeypatch=monkeypatch, page_timeout=100, batch=3):
        with pytest.raises(MineruBoundsError) as info:
            world.extract()

    message = str(info.value)
    assert "pdf.mineru_page_timeout_s" in message and "pdf.mineru_page_batch" in message
    assert "300" in message and "240" in message
    assert world.events == [], "nothing ran: no request, no worker, not even a heartbeat"


def test_the_refusal_is_not_wrapped_on_the_auto_path(tmp_path, monkeypatch) -> None:
    world = World(tmp_path, 4)
    ext = PDFExtractor()
    with world.active(monkeypatch=monkeypatch, page_timeout=100, batch=3), \
            patch("nexus.pdf_extractor._has_formulas_quick", return_value=10):
        with pytest.raises(MineruBoundsError) as info:
            ext.extract(world.pdf, extractor="auto")
    assert str(info.value).startswith("pdf.mineru_page_timeout_s"), "no 'contains formulas ... failed' prefix"


@pytest.mark.parametrize("timeout_s,batch,ok", [
    (180, 1, True),     # the defaults
    (60, 4, True),      # exactly the bound: allowed
    (240, 1, True),
    (61, 4, False),
    (181, 4, False),
    (1, 240, True),
    (1, 241, False),
])
def test_the_bound_is_at_most_240(timeout_s, batch, ok) -> None:
    with patch("nexus.config.get_mineru_page_timeout_s", return_value=timeout_s), \
            patch("nexus.config.get_mineru_page_batch", return_value=batch):
        assert (mineru_attempt_bound_problem() is None) is ok


def _config_with(**pdf):
    return patch("nexus.config.load_config", return_value={"pdf": pdf})


@pytest.mark.parametrize("key", ["mineru_page_timeout_s", "mineru_page_batch", "mineru_memory_ceiling_mb"])
@pytest.mark.parametrize("bad", ["abc", "2.5", "", None, [1]])
def test_a_non_integer_setting_is_named_not_a_bare_value_error(key, bad) -> None:
    with _config_with(**{key: bad}):
        with pytest.raises(PdfConfigError) as info:
            get_pdf_config()
    assert f"pdf.{key}" in str(info.value)
    assert "invalid literal" not in str(info.value)
    assert isinstance(info.value, ValueError), "callers that caught ValueError still do"


@pytest.mark.parametrize("key", ["mineru_page_timeout_s", "mineru_page_batch"])
def test_a_non_integer_value_is_reported_by_setting_name_at_the_start_of_a_run(
    tmp_path, monkeypatch, key,
) -> None:
    world = World(tmp_path, 2)
    with world.active(monkeypatch=monkeypatch, patch_config=False), _config_with(**{key: "abc"}):
        with pytest.raises(MineruBoundsError, match=f"pdf.{key}") as info:
            world.extract()
    assert "invalid literal" not in str(info.value)
    assert world.events == [], "nothing ran"


def test_the_defaults_and_numeric_strings_pass() -> None:
    with _config_with():
        assert mineru_attempt_bound_problem() is None
    with _config_with(mineru_page_timeout_s="60", mineru_page_batch="4"):
        assert mineru_attempt_bound_problem() is None
        cfg = get_pdf_config()
        assert (cfg.mineru_page_timeout_s, cfg.mineru_page_batch) == (60, 4)


def test_the_defaults_run_to_the_end(tmp_path, monkeypatch) -> None:
    result, world, _ = run(tmp_path, monkeypatch, 2)
    assert result.metadata["extraction_pages"] == {} and world.attempts()


# ── `nx config set` warns ────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def _set(*args: str):
    return CliRunner().invoke(main, ["config", "set", *args])


def test_config_set_warns_when_the_timeout_times_the_batch_would_exceed_240_s(fake_home) -> None:
    assert "Warning" not in _set("pdf.mineru_page_timeout_s=60").output
    assert "Warning" not in _set("pdf.mineru_page_batch=4").output, "4 x 60 s is exactly the bound"
    result = _set("pdf.mineru_page_timeout_s=300")
    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert "pdf.mineru_page_timeout_s" in result.output and "pdf.mineru_page_batch" in result.output
    cfg = yaml.safe_load((fake_home / ".config" / "nexus" / "config.yml").read_text())
    assert cfg["pdf"]["mineru_page_timeout_s"] == "300", "a warning, not a refusal: the value is stored"


def test_config_set_is_quiet_when_the_pair_fits(fake_home) -> None:
    result = _set("pdf.mineru_page_timeout_s=240")
    assert result.exit_code == 0 and "Warning" not in result.output, result.output


def test_config_set_warns_on_a_batch_that_overshoots_the_default_timeout(fake_home) -> None:
    result = _set("pdf.mineru_page_batch=2")
    assert "Warning" in result.output and "360" in result.output


def test_config_set_names_a_non_integer_value_instead_of_raising(fake_home) -> None:
    result = _set("pdf.mineru_page_timeout_s=soon")
    assert result.exit_code == 0 and "Warning" in result.output
    assert "pdf.mineru_page_timeout_s must be an integer" in result.output
    assert result.exception is None


def test_config_set_of_another_key_does_not_check_the_pair(fake_home) -> None:
    _set("pdf.mineru_page_batch=9")
    result = _set("pdf.extractor=mineru")
    assert "Warning" not in result.output


# ── 6. the heartbeat opens every attempt ─────────────────────────────────────


def _assert_every_attempt_opens_with_a_beat(world: World) -> None:
    kinds = [e[0] for e in world.events]
    for i, kind in enumerate(kinds):
        if kind in ("server", "sub"):
            assert kinds[i - 1] == "beat", f"attempt {world.events[i]} is not opened by a heartbeat: {kinds[max(0, i - 3):i + 1]}"


def test_a_failing_batch_of_four_at_60_s_opens_every_bisect_step_with_a_heartbeat(tmp_path, monkeypatch) -> None:
    def plan(route, start, span, rung):
        if (route, start, span) == ("server", 0, 4):
            return ("http", 500)
        if (route, start, span) == ("sub", 0, 4):
            return ("exit", 1)
        if (route, start, span) == ("server", 2, 2):
            return "timeout"
        return "ok"

    result, world, _ = run(tmp_path, monkeypatch, 4, plan, batch=4, page_timeout=60)

    steps = [(e[0], e[2], e[3]) for e in world.attempts()]
    assert steps == [
        ("server", 0, 4), ("sub", 0, 4),          # the batch: server error, then the worker, both fail
        ("server", 0, 2), ("server", 2, 2),       # the halves; the second times out (120 s)
        ("sub", 2, 1), ("sub", 3, 1),             # and is bisected to its pages on the SUBPROCESS
    ], steps
    _assert_every_attempt_opens_with_a_beat(world)
    assert world.longest_gap() < MINERU_ATTEMPT_BOUND_S
    assert result.metadata["extraction_pages"] == {}


def test_the_longest_attempt_the_bound_allows_leaves_a_gap_of_exactly_240_s(tmp_path, monkeypatch) -> None:
    result, world, _ = run(
        tmp_path, monkeypatch, 4, lambda r, s, n, g: "timeout" if (r, s, n) == ("server", 0, 4) else "ok",
        batch=4, page_timeout=60,
    )
    assert server_events(world)[0][5] == 240
    assert world.longest_gap() == 240 <= MINERU_ATTEMPT_BOUND_S
    _assert_every_attempt_opens_with_a_beat(world)


def test_a_subprocess_run_opens_with_a_heartbeat_even_with_no_server(tmp_path, monkeypatch) -> None:
    _, world, _ = run(tmp_path, monkeypatch, 3, server=False, page_timeout=60)
    assert [e[0] for e in world.attempts()] == ["sub"] * 3
    _assert_every_attempt_opens_with_a_beat(world)


def test_a_forced_failure_opens_with_a_heartbeat_and_waits_out_the_real_page_budget(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("NX_MINERU_FAULT_PAGES", "2:1,2:timeout")
    result, world, _ = run(tmp_path, monkeypatch, 3, page_timeout=77, server=False)

    waits = [e for e in world.events if e[0] == "fault_wait"]
    assert [w[2] for w in waits] == [77, 77], "each forced timeout waits the configured page budget"
    kinds = [e[0] for e in world.events]
    for i, kind in enumerate(kinds):
        if kind == "fault_wait":
            assert kinds[i - 1] == "beat", "a forced attempt is an attempt: the heartbeat opens it"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [2]}
    assert world.longest_gap() <= 77


def test_a_server_connection_error_then_a_restart_then_a_retry_keeps_the_gap_under_the_bound(
    tmp_path, monkeypatch,
) -> None:
    """The real ``_restart_mineru_server`` runs, with its spawn stubbed: the health wait it makes
    is an attempt of its own and opens with a heartbeat."""
    seen = {"n": 0}

    def plan(route, start, span, rung):
        if route == "server" and start == 1 and rung == 1:
            seen["n"] += 1
            return "connect" if seen["n"] == 1 else "ok"
        return "ok"

    world = World(tmp_path, 3, plan)

    def spawn(port):
        world.events.append(("spawn", world.now))
        world.now += 25.0  # the model load the health wait covers
        proc = MagicMock(pid=5151)
        proc.poll.return_value = None
        return proc

    ext = PDFExtractor()
    with world.active(monkeypatch=monkeypatch, page_timeout=60, registered=None), \
            patch("nexus._mineru_spawn.spawn_server_process", side_effect=spawn):
        world.extract(ext)

    kinds = [e[0] for e in world.events]
    lost = [i for i, e in enumerate(world.events) if e[0] == "server" and e[2] == 1][0]
    retry = [i for i, e in enumerate(world.events) if e[0] == "server" and e[2] == 1][1]
    assert "spawn" in kinds[lost:retry], "the restart ran between the failure and the retry"
    spawned = kinds.index("spawn", lost)
    before_spawn = [i for i in range(lost + 1, spawned) if kinds[i] == "beat"]
    after_spawn = [i for i in range(spawned + 1, retry) if kinds[i] == "beat"]
    assert before_spawn, "the restart opens with a heartbeat, before its election, stop and spawn"
    assert len(after_spawn) >= 2, "the health wait and the retry each open with a heartbeat after it"
    assert kinds[retry - 1] == "beat"
    assert world.longest_gap() < MINERU_ATTEMPT_BOUND_S
    assert ext._mineru_server_restarts == 1


def test_a_held_breaker_window_does_not_starve_the_heartbeat(tmp_path, monkeypatch) -> None:
    """The ladder holds up to threshold-1 pages back from ``on_page`` until the window resolves, so
    the gap between ``on_page`` calls grows by their attempts. Progress comes from the heartbeat."""
    bad = {2, 3}
    result, world, _ = run(
        tmp_path, monkeypatch, 5,
        lambda route, start, span, rung: "timeout" if start + 1 in bad else "ok",
        server=False, page_timeout=100, breaker=3,
    )

    assert result.metadata["extraction_pages"] == {"pymupdf-text": [2, 3]}
    assert world.longest_gap(("page",)) > MINERU_ATTEMPT_BOUND_S, (
        "the pages in the window are held, so on_page alone leaves a gap longer than the bound; "
        "without that this test shows nothing"
    )
    assert world.longest_gap(("beat",)) < MINERU_ATTEMPT_BOUND_S, "the heartbeat alone keeps the row alive"
    assert world.longest_gap(("beat",)) <= 100


def test_the_restarts_health_wait_opens_with_a_heartbeat_and_ticks_while_it_waits(
    tmp_path, monkeypatch,
) -> None:
    """The wait is up to 60 s; the interval is shortened here so a few polls show the tick."""
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
    polls = {"n": 0}

    def get(url, timeout=None, **_kw):
        polls["n"] += 1
        return MagicMock(status_code=200 if polls["n"] > 6 else 503)

    proc = MagicMock(pid=5151)
    proc.poll.return_value = None
    beats: list[int] = []
    ext = PDFExtractor()
    ext._heartbeat = lambda: beats.append(polls["n"])
    with patch("nexus.config.get_mineru_server_url", return_value=URL), \
            patch("nexus._mineru_pid.read_pid_file", return_value=None), \
            patch("nexus._mineru_spawn.spawn_server_process", return_value=proc), \
            patch("nexus._mineru_spawn._HEALTH_POLL_INTERVAL", 0.001), \
            patch("nexus.daemon.mineru_lifecycle.HEARTBEAT_EVERY_S", 0.0), \
            patch_in("nexus.pdf_extractor", "httpx.get", get):
        assert ext._restart_mineru_server() is True

    assert beats[0] == 0, "the wait opens with a heartbeat, before its first poll"
    assert len(beats) >= 4 and beats[-1] >= 4, f"and ticks while it polls: {beats}"


# ── 7. the two waits tick ────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _ensure_waiting_out_its_budget(monkeypatch, tmp_path, clock: FakeClock, heartbeat, wait_healthy_s: float = 120.0) -> None:
    """``ensure_mineru_running`` against a server that spawns and never turns healthy: the whole wait
    runs, on *clock*, which moves only when the wait sleeps."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    with patch("nexus.config.get_mineru_server_url", return_value=URL), \
            patch("nexus.daemon.mineru_lifecycle._healthy", return_value=False), \
            patch("nexus.config.get_pdf_config", return_value=MagicMock(mineru_autostart=True)), \
            patch("nexus.config.get_mineru_configured_fixed_port", return_value=None), \
            patch("nexus._mineru_spawn._find_free_port", return_value=8010), \
            patch("nexus._mineru_pid.read_pid_file", return_value=None), \
            patch("nexus._mineru_pid.is_process_alive", return_value=True), \
            patch("nexus._mineru_spawn.spawn_server_process", return_value=proc):
        result = ensure_mineru_running(
            wait_healthy_s=wait_healthy_s, _monotonic=clock.monotonic, _wall_time=clock.time,
            _sleep=clock.sleep, heartbeat=heartbeat,
        )
    assert result is None, "the server never came up: the full wait ran"


def test_the_autostart_wait_ticks_the_heartbeat_through_its_full_120_s(monkeypatch, tmp_path) -> None:
    clock = FakeClock()
    started = clock.t
    ticks: list[float] = []

    _ensure_waiting_out_its_budget(monkeypatch, tmp_path, clock, lambda: ticks.append(clock.t - started))

    assert clock.t - started >= 120.0, "the wait ran its full budget"
    assert ticks, "at least one heartbeat during the wait"
    assert ticks[0] == 0.0, "the wait opens with one"
    assert len(ticks) >= 2, "and it ticks again inside 120 s"
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert all(HEARTBEAT_EVERY_S <= g < HEARTBEAT_EVERY_S + 3 for g in gaps), f"every 60 s, no later: {gaps}"
    assert clock.t - started - ticks[-1] <= HEARTBEAT_EVERY_S + 3, "no silent stretch at the end of the wait"


def test_the_autostart_wait_without_a_heartbeat_is_unchanged(monkeypatch, tmp_path) -> None:
    clock = FakeClock()
    started = clock.t
    _ensure_waiting_out_its_budget(monkeypatch, tmp_path, clock, None, wait_healthy_s=10.0)
    assert clock.t - started >= 10.0


def test_the_extractors_autostart_wait_passes_its_heartbeat_to_the_lifecycle(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_ensure(**kwargs):
        seen.update(kwargs)
        kwargs["heartbeat"]()
        return None

    world = World(tmp_path, 2)
    world.healthy = False
    with world.active(monkeypatch=monkeypatch, page_timeout=60), \
            patch("nexus.daemon.mineru_lifecycle.ensure_mineru_running", side_effect=fake_ensure):
        world.extract()

    assert "heartbeat" in seen
    assert world.events[0][0] == "beat", "the wait's own heartbeat is the first progress of the run"


# ── 8. the heartbeat's errors are stops, never ladder failures ───────────────


def _fenced() -> PipelineRunFenced:
    return PipelineRunFenced("stale", pipeline_id=1, content_hash="h", run_epoch=1, current_epoch=2, remedy="")


def test_a_fenced_heartbeat_propagates_as_raised_and_no_page_is_degraded(tmp_path, monkeypatch) -> None:
    error = _fenced()
    delivered: list[int] = []
    calls = {"n": 0}

    def heartbeat() -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise error

    world = World(tmp_path, 5)
    with world.active(monkeypatch=monkeypatch, page_timeout=60):
        with pytest.raises(PipelineRunFenced) as info:
            world.extract(on_heartbeat=heartbeat, on_page=lambda i, t, m: delivered.append(i))

    assert info.value is error
    assert delivered == [0, 1], "pages 1 and 2 settled before the third attempt; nothing was degraded"
    assert not [e for e in world.events if e[0] == "sub"], "a stop never moved a page down a rung"


def test_a_heartbeat_error_is_never_swallowed_wherever_it_fires(tmp_path, monkeypatch) -> None:
    """Sweep: a connection error, a restart (with its health wait), a retry on the new server. The
    heartbeat raises at EACH of its calls in turn; every one of them must reach the caller as the
    same object, through the dispatch's ``except Exception`` arms and the lifecycle's catch-all."""

    def scenario(raise_at: int | None) -> tuple[int, BaseException | None]:
        seen = {"n": 0}

        def plan(route, start, span, rung):
            if route == "server" and start == 1 and rung == 1:
                seen["n"] += 1
                return "connect" if seen["n"] == 1 else "ok"
            return "ok"

        run_dir = tmp_path / f"w{raise_at}"
        run_dir.mkdir()
        world = World(run_dir, 3, plan)
        error = RuntimeError("heartbeat refused")  # a RuntimeError: the arm the ladder descends on
        count = {"n": 0}

        def heartbeat() -> None:
            count["n"] += 1
            if raise_at is not None and count["n"] == raise_at:
                raise error

        def spawn(port):
            proc = MagicMock(pid=5151)
            proc.poll.return_value = None
            return proc

        ext = PDFExtractor()
        caught: BaseException | None = None
        with world.active(monkeypatch=monkeypatch, page_timeout=60, registered=None), \
                patch("nexus._mineru_spawn.spawn_server_process", side_effect=spawn):
            try:
                world.extract(ext, on_heartbeat=heartbeat)
            except BaseException as exc:  # noqa: BLE001 — the sweep reads what arrived
                caught = exc
        if raise_at is not None:
            assert caught is error, f"heartbeat call {raise_at} of {total[0]}: arrived as {caught!r}"
        return count["n"], caught

    total = [0]
    n, caught = scenario(None)
    assert caught is None and n >= 5, f"the scenario has too few heartbeats to sweep ({n})"
    total[0] = n
    for k in range(1, n + 1):
        scenario(k)


def test_a_cancel_raised_by_on_page_propagates_unchanged(tmp_path, monkeypatch) -> None:
    error = PipelineCancelled("pipeline cancelled")

    def on_page(index, text, meta):
        raise error

    world = World(tmp_path, 3)
    with world.active(monkeypatch=monkeypatch, page_timeout=60):
        with pytest.raises(PipelineCancelled) as info:
            world.extract(on_page=on_page)
    assert info.value is error


def test_a_fenced_heartbeat_in_the_autostart_wait_is_not_swallowed_by_the_lifecycle_catch(
    tmp_path, monkeypatch,
) -> None:
    error = _fenced()
    world = World(tmp_path, 2)
    world.healthy = False

    def fake_ensure(**kwargs):
        kwargs["heartbeat"]()  # the wait's tick
        raise AssertionError("the wait went on after the heartbeat raised")

    def heartbeat() -> None:
        raise error

    with world.active(monkeypatch=monkeypatch, page_timeout=60), \
            patch("nexus.daemon.mineru_lifecycle.ensure_mineru_running", side_effect=fake_ensure):
        with pytest.raises(PipelineRunFenced) as info:
            world.extract(on_heartbeat=heartbeat)
    assert info.value is error


def test_the_heartbeat_is_cleared_when_the_extraction_ends_either_way(tmp_path, monkeypatch) -> None:
    _, _, ext = run(tmp_path, monkeypatch, 1, page_timeout=60)
    assert ext._heartbeat is None

    failing = tmp_path / "failing"
    failing.mkdir()
    world = World(failing, 1)
    ext2 = PDFExtractor()
    error = _fenced()

    def heartbeat() -> None:
        raise error

    with world.active(monkeypatch=monkeypatch, page_timeout=60):
        with pytest.raises(PipelineRunFenced):
            world.extract(ext2, on_heartbeat=heartbeat)
    assert ext2._heartbeat is None, "cleared on the failure path too"


# ── the shared lookups ───────────────────────────────────────────────────────


def test_is_registered_server_url_is_the_pid_files_own_loopback_port_only() -> None:
    assert is_registered_server_url(URL, REGISTERED)
    assert not is_registered_server_url("http://127.0.0.1:801", REGISTERED)
    assert not is_registered_server_url("http://gpu.example.com:8010", REGISTERED)
    assert not is_registered_server_url(None, REGISTERED)
    assert not is_registered_server_url(URL, None)


# ── 9. fix round 1 (reviews of a9c6cfffa): the restart streak, pinned rule by rule ───────────────


def _linux_streak_run(tmp_path, monkeypatch, pages, plan, *, fault=None, ext=None, restart=None):
    """Run on a three-slot server (a restart needs two consecutive rung-1 timeouts) with the broken
    restart replaced by a spy; ``(world, spy, ext, logs)``."""
    if fault is not None:
        monkeypatch.setenv("NX_MINERU_FAULT_PAGES", fault)
    world = World(tmp_path, pages, plan)
    ext = ext or PDFExtractor()
    spy = restart or _restart_spy(world)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        with capture_logs() as logs:
            world.extract(ext)
    return world, spy, ext, logs


def _server_attempts(world: World) -> list[tuple[int, int]]:
    """The server requests as ``(page 1-based, rung)`` (single-page ranges)."""
    return [(e[2] + 1, e[4]) for e in server_events(world)]


def test_a_rung_2_server_timeout_is_not_counted_toward_the_restart_streak(tmp_path, monkeypatch) -> None:
    """Page 2 times out at rung 1 (streak 1). Page 3's rung 1 is forced to fail without touching
    the server, so its rung 2 goes to the server and times out too. Only rung-1 requests count: no
    restart. (Counting it would make the two 'consecutive pages' the rule restarts on.)"""
    world, spy, _, logs = _linux_streak_run(
        tmp_path, monkeypatch, 4, times_out(("server", 2, 1), ("server", 3, 2)), fault="3:1",
    )

    timeouts = [(e["pages"], e["rung"]) for e in logs if e.get("event") == "mineru_server_timeout"]
    assert timeouts == [("2", 1), ("3", 2)], "both timeouts happened, one at each rung"
    assert (3, 1) not in _server_attempts(world), "page 3's rung 1 never reached the server"
    spy.assert_not_called()


def test_a_dropped_connection_ends_the_streak(tmp_path, monkeypatch) -> None:
    """Timeout on page 2, a dropped connection on page 3 (the lost-server branch retries it on a
    'restarted' server), timeout on page 4: the dropped connection ended the streak, so page 4 is
    the first of a new one."""
    calls = {"n": 0}

    def plan(route, start, span, rung):
        if route == "server" and rung == 1 and start == 2:
            calls["n"] += 1
            return "connect" if calls["n"] == 1 else "ok"
        return "timeout" if (route, start, rung) in {("server", 1, 1), ("server", 3, 1)} else "ok"

    world = World(tmp_path, 5, plan)
    ext = PDFExtractor()
    spy = _restart_spy(world)

    def fake_lost_server_restart(replace_pid=None) -> bool:
        ext._mineru_server_checked = True
        ext._mineru_server_up = True
        return True

    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", spy), \
            patch.object(ext, "_restart_mineru_server", side_effect=fake_lost_server_restart):
        with capture_logs() as logs:
            world.extract(ext)

    assert [e for e in logs if e.get("event") == "mineru_server_lost"], "page 3 took the lost-server branch"
    assert [(e["pages"], e["rung"]) for e in logs if e.get("event") == "mineru_server_timeout"] == [
        ("2", 1), ("4", 1),
    ]
    spy.assert_not_called()


def test_a_rung_2_server_answer_does_not_end_a_rung_1_streak(tmp_path, monkeypatch) -> None:
    """Timeout on page 2, then page 3's rung 1 is forced to fail and its rung 2 is ANSWERED by the
    server, then a timeout on page 4. The rung-2 answer is not a rung-1 request, so pages 2 and 4
    are consecutive rung-1 timeouts: one restart, after page 4."""
    world, spy, _, logs = _linux_streak_run(
        tmp_path, monkeypatch, 5, times_out(("server", 2, 1), ("server", 4, 1)), fault="3:1",
    )

    assert (3, 2) in _server_attempts(world), "page 3's rung 2 was answered by the server"
    spy.assert_called_once_with(REGISTERED)
    (restart,) = [e for e in logs if e.get("event") == "mineru_server_timeout_restart"]
    assert restart["pages"] == ["2", "4"]


@pytest.mark.parametrize("path", ["docling", "auto without formulas"])
def test_the_240_s_check_does_not_fire_on_a_path_that_never_reaches_mineru(tmp_path, monkeypatch, path) -> None:
    """A product above 240 s concerns MinerU only: a run the Docling path serves is untouched, and
    the check is not even asked."""
    world = World(tmp_path, 2)
    ext = PDFExtractor()
    clean = ExtractionResult(text="A clean page of ordinary prose, long enough to pass the gate. " * 6)
    with world.active(monkeypatch=monkeypatch, page_timeout=100, batch=3), \
            patch("nexus.config.mineru_attempt_bound_problem", wraps=mineru_attempt_bound_problem) as asked, \
            patch("nexus.pdf_extractor._has_formulas_quick", return_value=0), \
            patch.object(PDFExtractor, "_extract_with_docling", return_value=clean) as docling:
        result = ext.extract(
            world.pdf, extractor="docling" if path == "docling" else "auto", allow_degraded=True,
        )

    assert result is clean and docling.call_count == 1, "Docling served the run"
    asked.assert_not_called()
    assert world.events == [], "no MinerU request, worker or heartbeat"
    with world.active(monkeypatch=monkeypatch, page_timeout=100, batch=3):
        assert mineru_attempt_bound_problem() is not None, "non-vacuity: this pair is refused on MinerU"


# ── 10. a server timeout bisects on the subprocess (RDR-237 Bounds: never the same rung again) ───


def test_a_batch_that_times_out_on_the_server_bisects_on_the_subprocess_and_counts_once(
    tmp_path, monkeypatch,
) -> None:
    """The first half would time out on the server too (the abandoned parse of the batch still holds
    the slot), and a batch plus its half would read as two consecutive pages: a restart of a
    three-slot server for one bad region. The halves never go to the server."""
    def plan(route, start, span, rung):
        return "timeout" if (route, start, span) in {("server", 0, 4), ("server", 0, 2)} else "ok"

    world = World(tmp_path, 4, plan)
    ext = PDFExtractor()
    spy = _restart_spy(world)
    with world.active(monkeypatch=monkeypatch, page_timeout=60, batch=4, platform="linux"), \
            patch.object(ext, "_restart_broken_mineru_server", spy):
        world.extract(ext)

    assert [(e[0], e[2], e[3]) for e in world.attempts()] == [
        ("server", 0, 4), ("sub", 0, 2), ("sub", 2, 2),
    ]
    spy.assert_not_called()
    assert ext._server_timeout_pages == ["1-4"], "the batch is one entry in the streak"


def test_no_step_below_a_server_timeout_goes_back_to_the_server(tmp_path, monkeypatch) -> None:
    """The subtree stays on the subprocess: the quarters, and the page's own rung 2."""
    def plan(route, start, span, rung):
        if (route, start, span, rung) == ("server", 0, 4, 1):
            return "timeout"
        if (route, start, span, rung) in {("sub", 0, 2, 1), ("sub", 0, 1, 1)}:
            return ("exit", 1)
        return "ok"

    result, world, _ = run(tmp_path, monkeypatch, 4, plan, batch=4, page_timeout=60)

    assert [(e[0], e[2], e[3], e[4]) for e in world.attempts()] == [
        ("server", 0, 4, 1),
        ("sub", 0, 2, 1),    # first half: the worker fails it
        ("sub", 0, 1, 1),    # its first page: fails
        ("sub", 0, 1, 2),    # and rung 2 is the subprocess, not the server
        ("sub", 1, 1, 1),
        ("sub", 2, 2, 1),    # the second half
    ]
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [1]}
    _assert_every_attempt_opens_with_a_beat(world)


def test_a_range_that_failed_on_something_else_still_bisects_on_the_server(tmp_path, monkeypatch) -> None:
    """Only a server TIMEOUT moves the steps off the server: a server error leaves the server fine."""
    def plan(route, start, span, rung):
        return ("http", 500) if (route, start, span) == ("server", 0, 4) else (
            ("exit", 1) if (route, start, span) == ("sub", 0, 4) else "ok"
        )

    _, world, _ = run(tmp_path, monkeypatch, 4, plan, batch=4, page_timeout=60)

    assert [(e[0], e[2], e[3]) for e in world.attempts()] == [
        ("server", 0, 4), ("sub", 0, 4), ("server", 0, 2), ("server", 2, 2),
    ]


# ── 11. the restart: heartbeat first, budget before the machine-wide cooldown ────────────────────


def test_the_restart_opens_with_a_heartbeat_before_its_stop_and_spawn_so_the_gap_stays_under_240_s(
    tmp_path, monkeypatch,
) -> None:
    """A 4-page batch at 60 s times out after 240 s. The restart then stops the old server (the
    SIGTERM grace) and spawns a new one before its health wait beats: with the beat there, the
    silent gap is 240 + 10 + 5. The restart's own beat comes first."""
    world = World(tmp_path, 4, lambda route, start, span, rung: "timeout" if (route, start, span) == ("server", 0, 4) else "ok")
    ext = PDFExtractor()

    def stop(pid):
        world.events.append(("stop", world.now))
        world.now += 10.0
        return "terminated"

    def spawn(port):
        world.events.append(("spawn", world.now))
        world.now += 5.0
        proc = MagicMock(pid=5151)
        proc.poll.return_value = None
        return proc

    with world.active(monkeypatch=monkeypatch, page_timeout=60, batch=4, platform="darwin"), \
            patch("nexus._mineru_pid.is_process_alive", return_value=True), \
            patch("nexus._mineru_spawn.classify_server_pid", return_value="mineru"), \
            patch("nexus._mineru_spawn.stop_server_group", side_effect=stop), \
            patch("nexus._mineru_spawn.spawn_server_process", side_effect=spawn):
        world.extract(ext)

    kinds = [e[0] for e in world.events]
    assert "stop" in kinds and "spawn" in kinds, "the real restart ran, stop and spawn included"
    assert ext._mineru_server_restarts == 1
    first_server = kinds.index("server")
    stop_at = kinds.index("stop")
    assert "beat" in kinds[first_server + 1:stop_at], "a beat sits between the timed-out request and the stop"
    assert world.longest_gap() <= MINERU_ATTEMPT_BOUND_S, f"gap {world.longest_gap()}"
    assert world.longest_gap() == MINERU_ATTEMPT_BOUND_S, "the request's own 240 s is the longest silence"


def _restart_claim_spy():
    return patch("nexus._mineru_health.claim_broken_restart", return_value=True)


def test_a_process_whose_restart_budget_is_spent_does_not_claim_the_machine_wide_cooldown(
    tmp_path, monkeypatch,
) -> None:
    ext = PDFExtractor()
    ext._mineru_server_restarts = ext._MINERU_MAX_RESTARTS
    with _restart_claim_spy() as claim, patch.object(ext, "_restart_mineru_server") as restart:
        assert ext._restart_broken_mineru_server(REGISTERED) is False

    claim.assert_not_called()
    restart.assert_not_called()


def test_a_process_that_may_not_spawn_does_not_claim_the_machine_wide_cooldown(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "0")
    ext = PDFExtractor()
    with _restart_claim_spy() as claim, patch.object(ext, "_restart_mineru_server") as restart:
        assert ext._restart_broken_mineru_server(REGISTERED) is False

    claim.assert_not_called()
    restart.assert_not_called()


def test_a_process_that_can_restart_claims_the_cooldown_then_restarts(tmp_path, monkeypatch) -> None:
    """Non-vacuity for the two above: the same call with budget and policy open does claim."""
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
    ext = PDFExtractor()
    with _restart_claim_spy() as claim, patch.object(ext, "_restart_mineru_server", return_value=True) as restart:
        assert ext._restart_broken_mineru_server(REGISTERED) is True

    claim.assert_called_once()
    restart.assert_called_once_with(replace_pid=REGISTERED["pid"])


@pytest.mark.parametrize("budget_spent", [True, False])
def test_the_cooldown_stamp_is_written_only_by_a_process_that_can_restart(
    tmp_path, monkeypatch, budget_spent,
) -> None:
    """Through the real claim: the stamp next to the pid file exists after a restart the budget
    allowed (the control), and does not after one it refused."""
    world = World(tmp_path, 4, times_out(("server", 2, 1)))
    ext = PDFExtractor()
    if budget_spent:
        ext._mineru_server_restarts = ext._MINERU_MAX_RESTARTS
    with world.active(monkeypatch=monkeypatch, page_timeout=60, platform="darwin"), \
            patch.object(ext, "_restart_mineru_server", return_value=True):
        world.extract(ext)
        stamp = _restart_stamp_path()

    assert stamp.name == _RESTART_STAMP_NAME
    assert stamp.exists() is (not budget_spent)
    if budget_spent:
        assert ext._mineru_server_up is False, "the server is marked down, as before"


# ── 12. the restart log names a multi-page range ─────────────────────────────────────────────────


def test_the_restart_log_names_a_multi_page_range_as_first_last(tmp_path, monkeypatch) -> None:
    """Batch 2 over five pages: the second batch is pages 3 and 4. On a one-slot server its
    timeout restarts at once, and the log and the progress line say '3-4'."""
    world = World(tmp_path, 5, times_out(("server", 3, 1)))
    ext = PDFExtractor()
    with world.active(monkeypatch=monkeypatch, page_timeout=60, batch=2, platform="darwin"), \
            patch.object(ext, "_restart_broken_mineru_server", return_value=True):
        with capture_logs() as logs:
            world.extract(ext)

    (timeout,) = [e for e in logs if e.get("event") == "mineru_server_timeout"]
    assert timeout["pages"] == "3-4" and timeout["budget_s"] == 120
    (restart,) = [e for e in logs if e.get("event") == "mineru_server_timeout_restart"]
    assert restart["pages"] == ["3-4"]


# ── 13. a pdf integer setting is an integer ──────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["mineru_page_timeout_s", "mineru_page_batch", "mineru_memory_ceiling_mb"])
@pytest.mark.parametrize("bad", [True, False, 2.5, 0.1, float("inf"), float("nan"), "2.5"])
def test_a_boolean_or_a_fractional_number_is_not_an_integer_setting(key, bad) -> None:
    """``int(True)`` is 1 and ``int(2.5)`` is 2, and either would have been used without a word."""
    with _config_with(**{key: bad}):
        with pytest.raises(PdfConfigError) as info:
            get_pdf_config()
    assert f"pdf.{key}" in str(info.value)
    assert repr(bad) in str(info.value), "the value that was refused is named"


@pytest.mark.parametrize("value,expected", [(180.0, 180), (60, 60), ("60", 60), (" 60 ", 60), (4.0, 4)])
def test_an_integral_number_is_an_integer_setting(value, expected) -> None:
    with _config_with(mineru_page_timeout_s=value):
        assert get_pdf_config().mineru_page_timeout_s == expected
