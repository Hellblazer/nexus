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
fake worker that exits 0, records every signal the kill primitive sends instead of delivering
it, and requires that nothing reaches the stand-in."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_extractor import PDFExtractor
from nexus.util import process_group as pg
from tests._module_seam import patch_in, setattr_in

posix_only = pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")


def _exiting_worker(pid: int):
    """Rung 2's subprocess: reports *pid*, exits 0 at once and leaves a parse behind."""

    def popen(args, **_kwargs):
        pdf, result_dir, start, _end, _formula = args[-5:]
        proc = MagicMock(pid=pid)

        def wait(timeout=None):
            base = Path(result_dir) / Path(pdf).name / "auto"
            base.mkdir(parents=True, exist_ok=True)
            name = Path(pdf).name
            (base / f"{name}.md").write_text(f"page {int(start) + 1}", encoding="utf-8")
            (base / f"{name}_content_list.json").write_text("[]", encoding="utf-8")
            (base / f"{name}_middle.json").write_text(json.dumps({"pdf_info": []}), encoding="utf-8")
            return 0

        proc.wait.side_effect = wait
        proc.returncode = 0
        return proc

    return popen


@posix_only
def test_an_exited_workers_pid_is_never_resolved_to_a_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stand_in = subprocess.Popen(  # noqa: S603 — fixed argv, test-owned
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
    )
    try:
        stand_in_group = os.getpgid(stand_in.pid)
        sent: list[tuple[str, int, int]] = []
        setattr_in(monkeypatch, pg, "os.kill", lambda pid, sig: sent.append(("kill", pid, sig)))
        setattr_in(monkeypatch, pg, "os.killpg", lambda pgid, sig: sent.append(("killpg", pgid, sig)))
        with patch_in("nexus.pdf_extractor", "subprocess.Popen", _exiting_worker(stand_in.pid)), \
                patch("nexus.util.process_group.safe_killpg_group") as group_sweep:
            md, _content, _info = PDFExtractor()._mineru_run_subprocess(tmp_path / "x.pdf", 0, 1)
        assert md == "page 1", "the fake worker's parse was read: the normal-exit path ran"
        reached = [s for s in sent if s[1] in (stand_in.pid, stand_in_group, -stand_in_group)]
        assert reached == [], (
            f"a reaped worker's pid was signalled through the kill primitive: {reached}. Once the "
            "kernel recycles that pid this is SIGKILL to an unrelated process group"
        )
        group_sweep.assert_called_once_with(stand_in.pid)  # the recorded-group sweep still runs
        assert stand_in.poll() is None
    finally:
        stand_in.kill()
        stand_in.wait(timeout=10)
