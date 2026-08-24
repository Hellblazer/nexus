# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A long-lived daemon must not depend on the directory it was launched from.

nexus-yg70j. The aspect-worker inherits the cwd of whatever spawned it — and
RDR-173 requires that parent to be an interactive session holding `claude -p`
credentials. When that directory is later removed, `os.getcwd()` raises
FileNotFoundError inside every `os.path.abspath()` call, and the worker dies
PERMANENTLY and SILENTLY: 16 consecutive batches raised at WARNING with nothing
alerting, zero successes, ~40 minutes on a production install (2026-08-24).

    aspect_readers.py:95   uri_for -> "file://" + os.path.abspath(source_path)
    FileNotFoundError: [Errno 2] No such file or directory

Confirmed by lsof: the wedged worker's cwd was a `git worktree` checkout under
/private/tmp that had been removed after its branch merged.

THE TRIGGER IS NOT THE BUG, and this test exists partly to say so. Removing a
finished worktree is correct, and this project's own guidance routes sessions
into exactly such directories — so a short-lived launch directory is the NORMAL
case here, not an edge case. A daemon that cannot survive it is the defect.

Same family as nexus-ws67k (1338c7624): a short-lived /private/tmp worktree
reaching into long-lived production state.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_getcwd_raises_once_the_launch_dir_is_deleted() -> None:
    """The mechanism itself, pinned. If a future Python or platform stops
    raising here, the guard below is protecting against nothing and this test
    says so rather than passing quietly."""
    probe = textwrap.dedent(
        """
        import os, sys, tempfile, shutil
        d = tempfile.mkdtemp()
        os.chdir(d)
        shutil.rmtree(d)
        try:
            os.path.abspath("relative/path.md")
        except FileNotFoundError:
            sys.exit(42)
        sys.exit(0)
        """
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True)
    assert r.returncode == 42, (
        "os.path.abspath on a relative path no longer raises when the cwd is "
        f"deleted (exit {r.returncode}) — the premise of nexus-yg70j changed"
    )


def test_daemon_chdirs_off_the_launch_directory(tmp_path: Path) -> None:
    """The fix: after startup the process must no longer be standing in the
    directory it was launched from.

    Drives the real `run_aspect_worker_daemon` prologue in a subprocess whose
    cwd is a doomed directory, stubbing everything after the chdir so the test
    exercises the ordering without booting a daemon.
    """
    launch = tmp_path / "doomed-worktree"
    launch.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    repo = Path(__file__).parent.parent

    probe = textwrap.dedent(
        f"""
        import os, sys, shutil
        sys.path.insert(0, {str(repo / "src")!r})
        launch = {str(launch)!r}
        os.chdir(launch)

        import nexus.daemon.aspect_worker_daemon as m
        # Stub everything AFTER the chdir: we are testing the prologue only.
        m.configure_logging = lambda *a, **k: None
        m._require_extraction_credentials = lambda *a, **k: None
        class _D:
            def __init__(self, **k): pass
            def start(self): raise SystemExit(0)
            def run_until_signal(self): pass
            def stop(self): pass
        m.AspectWorkerDaemon = _D

        try:
            m.run_aspect_worker_daemon(config_dir=__import__("pathlib").Path({str(cfg)!r}), tenant="t")
        except SystemExit:
            pass

        # The load-bearing assertion: we are no longer in the launch dir, and
        # getcwd still works after it is destroyed.
        moved = os.getcwd() != os.path.realpath(launch)
        shutil.rmtree(launch)
        try:
            os.path.abspath("x/y.md")
            survived = True
        except FileNotFoundError:
            survived = False
        sys.exit(0 if (moved and survived) else 1)
        """
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert r.returncode == 0, (
        "daemon did not detach from its launch directory, or did not survive "
        f"its deletion.\nstdout={r.stdout}\nstderr={r.stderr}"
    )


def test_the_anchor_is_the_config_dir_not_an_arbitrary_place(tmp_path: Path) -> None:
    """Positive control on WHERE it lands. A chdir to somewhere useless would
    pass the test above while leaving relative-path resolution nonsensical."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    repo = Path(__file__).parent.parent
    probe = textwrap.dedent(
        f"""
        import os, sys, pathlib
        sys.path.insert(0, {str(repo / "src")!r})
        import nexus.daemon.aspect_worker_daemon as m
        m.configure_logging = lambda *a, **k: None
        m._require_extraction_credentials = lambda *a, **k: None
        class _D:
            def __init__(self, **k): pass
            def start(self): raise SystemExit(0)
            def run_until_signal(self): pass
            def stop(self): pass
        m.AspectWorkerDaemon = _D
        try:
            m.run_aspect_worker_daemon(config_dir=pathlib.Path({str(cfg)!r}), tenant="t")
        except SystemExit:
            pass
        sys.exit(0 if os.getcwd() == os.path.realpath({str(cfg)!r}) else 1)
        """
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert r.returncode == 0, (
        f"daemon should anchor on config_dir.\nstdout={r.stdout}\nstderr={r.stderr}"
    )
