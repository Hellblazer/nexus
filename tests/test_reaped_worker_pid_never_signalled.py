# SPDX-License-Identifier: AGPL-3.0-or-later
"""A MinerU worker that has exited and been reaped is never killed by its pid.

Develop lost runner at 7eb199896 shard 1 (labrun alloc 519d15ab, 2026-10-30): the runner's
main process took SIGKILL at 19:30:44Z, about four minutes after chas's pid counter wrapped
(the job's step pids were 4,133,457 at 19:22:51Z against a pid_max of 4,194,304), so low pids
were being handed to fresh processes again. Shards 2 and 6 of the same run, on the same host
and uid, ran ``tests/test_rdr237_restart_real.py``, whose fake per-page worker reports
``pid=4321`` and exits 0. ``_mineru_run_subprocess`` then swept the exited worker with
``kill_tree(proc)``, which resolves the group from the pid (``os.getpgid(4321)``) and SIGKILLs
every member but the resource tracker. Once pid 4321 belongs to someone else, that is their
whole process group; the own-group guard does not help, because the victim is another job's
group. A dry probe on chas (the fabricated pid answered as alive, every signal recorded and
dropped) recorded 24 SIGKILLs from those two tests.

The same sweep in production does the same thing to a reaped worker whose pid the kernel has
recycled. After the reap the only safe handle is the group id recorded at spawn
(``safe_killpg_group``, nexus-5ny9r); on Windows the job handle.

The pin stands a live process in a group of its own (a stand-in for the recycled pid) behind a
fake worker, records every signal the kill primitive sends instead of delivering it, and
requires that nothing reaches the stand-in. It covers every sweep that runs after the reap: a
worker that exits 0, one that exits non-zero, one killed by SIGKILL, and a
``stop_mineru_workers`` that lands after the reap while the worker is still registered (the
live-worker kill, then the stopped path's own sweep)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus import pdf_extractor
from nexus.pdf_extractor import PDFExtractor
from nexus.util import process_group as pg
from tests._module_seam import patch_in, setattr_in

posix_only = pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")


def _reaped_worker(pid: int, rc: int, *, stop_after_reap: bool = False):
    """Rung 2's subprocess: reports *pid*, leaves a parse behind and is reaped with *rc*.

    ``returncode`` is ``None`` until the reap, as on a real ``Popen``. With *stop_after_reap*,
    ``stop_mineru_workers`` runs once the returncode is set and before the wait returns, the
    window in which the stage thread has reaped the worker but not yet unregistered it."""

    def popen(args, **_kwargs):
        pdf, result_dir, start, _end, _formula = args[-5:]
        proc = MagicMock(pid=pid)
        proc.returncode = None

        def wait(timeout=None):
            base = Path(result_dir) / Path(pdf).name / "auto"
            base.mkdir(parents=True, exist_ok=True)
            name = Path(pdf).name
            (base / f"{name}.md").write_text(f"page {int(start) + 1}", encoding="utf-8")
            (base / f"{name}_content_list.json").write_text("[]", encoding="utf-8")
            (base / f"{name}_middle.json").write_text(json.dumps({"pdf_info": []}), encoding="utf-8")
            proc.returncode = rc
            if stop_after_reap:
                assert pdf_extractor.stop_mineru_workers() == 1, "the reaped worker is still registered"
            return rc

        proc.wait.side_effect = wait
        return proc

    return popen


@pytest.fixture
def stand_in() -> Iterator[subprocess.Popen]:
    """A live process in a group of its own: the process the kernel gave the reaped pid to."""
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
    )
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_in: subprocess.Popen, popen):
    """Run the per-batch worker path; return (outcome, recorded signals that reached the stand-in,
    the recorded-group sweep mock)."""
    stand_in_group = os.getpgid(stand_in.pid)
    sent: list[tuple[str, int, int]] = []
    setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: sent.append(("kill", pid, sig)))
    setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: sent.append(("killpg", pgid, sig)))
    with patch_in("nexus.pdf_extractor", "subprocess.Popen", popen), \
            patch("nexus.util.process_group.safe_killpg_group") as group_sweep:
        try:
            outcome: object = PDFExtractor()._mineru_run_subprocess(tmp_path / "x.pdf", 0, 1)
        except BaseException as exc:  # noqa: BLE001 — MineruWorkerStopped is a BaseException
            outcome = exc
    reached = [s for s in sent if s[1] in (stand_in.pid, stand_in_group, -stand_in_group)]
    return outcome, reached, group_sweep


_NO_SIGNAL = (
    "a reaped worker's pid was signalled through the kill primitive: {}. Once the kernel "
    "recycles that pid this is SIGKILL to an unrelated process group"
)


@posix_only
@pytest.mark.parametrize(
    ("rc", "raised"),
    [(0, None), (1, pdf_extractor.MineruWorkerError), (-signal.SIGKILL, pdf_extractor.MineruMemoryError)],
    ids=["exit-0", "exit-1", "killed-9"],
)
def test_an_exited_workers_pid_is_never_resolved_to_a_process_group(
    rc: int, raised: type[BaseException] | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    stand_in: subprocess.Popen,
) -> None:
    outcome, reached, group_sweep = _run(tmp_path, monkeypatch, stand_in, _reaped_worker(stand_in.pid, rc))
    if raised is None:
        assert isinstance(outcome, tuple) and outcome[0] == "page 1", outcome  # the exit-0 path ran
    else:
        assert isinstance(outcome, raised), outcome  # the non-zero exit path ran
    assert reached == [], _NO_SIGNAL.format(reached)
    group_sweep.assert_called_once_with(stand_in.pid)  # the recorded-group sweep still runs
    assert stand_in.poll() is None


@posix_only
def test_a_stop_that_lands_after_the_reap_never_resolves_the_pid_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_in: subprocess.Popen,
) -> None:
    outcome, reached, group_sweep = _run(
        tmp_path, monkeypatch, stand_in,
        _reaped_worker(stand_in.pid, -signal.SIGKILL, stop_after_reap=True),
    )
    assert isinstance(outcome, pdf_extractor.MineruWorkerStopped), outcome  # the stopped path ran
    assert reached == [], _NO_SIGNAL.format(reached)
    # Once from the live-worker kill, once from the stopped path's own sweep.
    assert group_sweep.call_args_list == [((stand_in.pid,),), ((stand_in.pid,),)], group_sweep.call_args_list
    assert stand_in.poll() is None
