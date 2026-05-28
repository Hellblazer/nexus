# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-vg6d4: ``t2_prefix_scan.py`` must be stdlib-only.

The plugin's ``_run_python_hook.sh`` wrapper probes bare
``python3.13`` / ``python3.12`` to invoke ``session_start_hook.py``,
which calls ``t2_prefix_scan.py``. On a ``uv tool install conexus``
deployment the wrapper's resolved interpreter cannot import the
``nexus`` package (it lives in conexus's own venv). Before the fix,
``t2_prefix_scan.py`` did ``from nexus.db.t2 import T2Database`` at the
top, the import failed silently, and the entire
``## T2 Memory (Active Project)`` section was omitted from the
session-start context. This pinned the regression: the script must run
under a vanilla Python with only stdlib available.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "t2_prefix_scan.py"
)


def _seed_db(db_path: Path) -> None:
    """Create a minimal memory.db with rows for two namespaces."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE memory (
                id        INTEGER PRIMARY KEY,
                project   TEXT NOT NULL,
                title     TEXT NOT NULL,
                session   TEXT,
                agent     TEXT,
                content   TEXT NOT NULL,
                tags      TEXT,
                timestamp TEXT NOT NULL,
                ttl       INTEGER,
                access_count  INTEGER DEFAULT 0 NOT NULL,
                last_accessed TEXT DEFAULT ''
            )
            """
        )
        rows = [
            ("nexus", "release-5-3-0-validation",
             "## Header\n\nValidated 5.3.0 release pipeline end-to-end.",
             "2026-05-28T03:00:00"),
            ("nexus", "rdr-memory-audit",
             "Audited 17 RDR memories; 9 stale; refreshed.",
             "2026-05-28T02:30:00"),
            ("nexus_rdr", "rdr-129",
             "RDR-129 closed 2026-05-27; T2 daemon write-path hardening.",
             "2026-05-27T22:00:00"),
        ]
        conn.executemany(
            "INSERT INTO memory (project, title, content, timestamp) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _run(project_name: str, db_path: Path, *, interpreter: str = sys.executable) -> str:
    """Invoke the script with NEXUS_CONFIG_DIR pointed at *db_path*'s parent."""
    env = os.environ.copy()
    env["NEXUS_CONFIG_DIR"] = str(db_path.parent)
    # Strip PYTHONPATH so the test runner's venv site-packages are not on
    # sys.path; this approximates the bare-interpreter invocation that
    # _run_python_hook.sh produces on a uv-tool-install deployment.
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [interpreter, str(SCRIPT), project_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"script exit {result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result.stdout


def test_runs_under_bare_python_and_surfaces_entries(tmp_path: Path) -> None:
    """The headline regression: must not print 'T2 not available'."""
    db = tmp_path / "memory.db"
    _seed_db(db)
    out = _run("nexus", db)
    # Must surface the bare-namespace entries.
    assert "### T2 Memory" in out
    assert "release-5-3-0-validation" in out
    assert "rdr-memory-audit" in out
    # Must surface the suffixed namespace separately.
    assert "### T2 Memory (rdr)" in out
    assert "rdr-129" in out
    # Must not have leaked the pre-fix import-error message anywhere.
    assert "T2 not available" not in out
    assert "No module named" not in out


def test_recency_ordering_within_namespace(tmp_path: Path) -> None:
    """Entries inside a namespace appear in DESC timestamp order."""
    db = tmp_path / "memory.db"
    _seed_db(db)
    out = _run("nexus", db)
    # release-5-3-0-validation (03:00) is newer than rdr-memory-audit (02:30);
    # the newer entry's title appears first.
    pos_release = out.index("release-5-3-0-validation")
    pos_audit = out.index("rdr-memory-audit")
    assert pos_release < pos_audit


def test_no_namespaces_means_no_output(tmp_path: Path) -> None:
    """Querying a prefix with no matches returns clean empty output."""
    db = tmp_path / "memory.db"
    _seed_db(db)
    out = _run("unknown_project_xyz", db)
    assert out == ""


def test_missing_db_is_silent_noop(tmp_path: Path) -> None:
    """A fresh install with no T2 yet should not error or emit noise."""
    # tmp_path has no memory.db.
    env = os.environ.copy()
    env["NEXUS_CONFIG_DIR"] = str(tmp_path)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "nexus"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_like_metacharacters_in_prefix_are_escaped(tmp_path: Path) -> None:
    """Prefix containing ``_`` must not glob-match other namespaces.

    The pre-fix MemoryStore.get_projects_with_prefix already escapes
    LIKE metacharacters; the stdlib port preserves that behavior.
    """
    db = tmp_path / "memory.db"
    _seed_db(db)
    # The seeded DB has 'nexus' and 'nexus_rdr'. A query for 'nexus_'
    # (with literal underscore) must match only 'nexus_rdr', not
    # 'nexusX...'. Add a dummy collision to make the test bite.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memory (project, title, content, timestamp) VALUES "
        "('nexusXrdr', 'should-not-appear', 'glob-match canary', '2026-05-28T01:00:00')"
    )
    conn.commit()
    conn.close()
    out = _run("nexus_", db)
    assert "rdr-129" in out  # legitimate nexus_rdr match
    assert "should-not-appear" not in out  # underscore is literal, not a glob
