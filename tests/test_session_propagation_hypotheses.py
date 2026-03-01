# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hypothesis tests for T1 session-ID propagation across agent spawns.

Problem
-------
The SessionStart hook always calls write_claude_session_id() with a fresh UUID,
so every spawned subagent gets its own isolated T1 namespace.  Three ambient
propagation mechanisms are compared here — no ChromaDB involved; each test
validates *only* the propagation primitive.

Approach A — Handoff file with TTL
    Parent hook writes the session ID to a well-known file immediately before
    (or as) the agent spawn occurs.  Child's hook reads the file; if it is
    younger than a TTL window it atomically renames the file (POSIX-safe) and
    adopts the session ID.  Only the first reader wins.

    Hypothesis: reliable, race-safe, zero extra dependencies.

Approach B — Environment variable inheritance
    Parent process sets NX_SESSION_ID in its environment.  If the Agent-tool
    subprocess inherits the environment (as normal POSIX fork/exec children do),
    the child's SessionStart hook can read the var and skip UUID generation.

    Hypothesis: works if and only if Claude Code's Agent-tool spawn propagates
    the parent's environment.  Simple to implement; fails silently if env is
    stripped.

Approach C — Sticky current_session (read-before-write with mtime)
    SessionStart hook reads current_session before deciding whether to write a
    new ID.  If the file is younger than STICKY_TTL, the hook adopts the existing
    ID rather than overwriting it.  The first SessionStart "owns" the file;
    subsequent SessionStarts within the window "join" it.

    Hypothesis: zero extra files/env vars; works automatically for any spawn that
    happens within the TTL window.  Risk: two unrelated Claude Code windows started
    close together accidentally share a session (mitigated by short TTL, e.g. 5 s).
"""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Approach A — Handoff file helpers
# ---------------------------------------------------------------------------

_HANDOFF_TTL = 3.0  # seconds


def write_handoff(path: Path, session_id: str) -> None:
    """Write session_id to the handoff file (parent side)."""
    path.write_text(session_id)


def adopt_handoff(path: Path, ttl_seconds: float = _HANDOFF_TTL) -> str | None:
    """Attempt to atomically adopt the handoff file (child side).

    Returns the session_id if the file is fresh and this process wins the
    rename race.  Returns None if the file is absent, stale, or already claimed.
    """
    try:
        stat = path.stat()
        if time.time() - stat.st_mtime > ttl_seconds:
            return None
        # POSIX rename is atomic: exactly one racing process wins
        tmp = path.with_name(path.name + f".adopting.{os.getpid()}")
        path.rename(tmp)
        content = tmp.read_text().strip()
        tmp.unlink()
        return content or None
    except (FileNotFoundError, OSError):
        return None


class TestApproachA:
    """Handoff file with TTL."""

    def test_adopted_within_ttl(self, tmp_path: Path) -> None:
        """File written just now is adopted by the calling process."""
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "session-parent-001")
        result = adopt_handoff(hf)
        assert result == "session-parent-001"

    def test_file_consumed_on_adoption(self, tmp_path: Path) -> None:
        """Handoff file is deleted after a successful adoption."""
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "session-parent-002")
        adopt_handoff(hf)
        assert not hf.exists()

    def test_second_reader_gets_none(self, tmp_path: Path) -> None:
        """Once adopted, a second call returns None (file is gone)."""
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "session-parent-003")
        first = adopt_handoff(hf)
        second = adopt_handoff(hf)
        assert first == "session-parent-003"
        assert second is None

    def test_stale_handoff_not_adopted(self, tmp_path: Path) -> None:
        """File older than TTL is not adopted; file is left in place."""
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "stale-session")
        old_time = time.time() - (_HANDOFF_TTL + 2)
        os.utime(hf, (old_time, old_time))
        result = adopt_handoff(hf)
        assert result is None
        assert hf.exists()  # not consumed

    def test_subprocess_adopts_within_ttl(self, tmp_path: Path) -> None:
        """A freshly-launched subprocess sees and adopts the handoff file."""
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "session-sub-001")
        script = textwrap.dedent(f"""\
            import os, time, sys
            from pathlib import Path
            path = Path({str(hf)!r})
            try:
                stat = path.stat()
                if time.time() - stat.st_mtime > 3.0:
                    print("STALE")
                    sys.exit(0)
                tmp = path.with_name(path.name + '.adopting.' + str(os.getpid()))
                path.rename(tmp)
                print(tmp.read_text().strip())
                tmp.unlink()
            except FileNotFoundError:
                print("MISSING")
        """)
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        assert r.stdout.strip() == "session-sub-001"

    def test_concurrent_subprocesses_only_one_wins(self, tmp_path: Path) -> None:
        """When N subprocesses race to adopt the handoff file, exactly one wins."""
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "shared-session")
        script = textwrap.dedent(f"""\
            import os, time, sys
            from pathlib import Path
            path = Path({str(hf)!r})
            try:
                stat = path.stat()
                if time.time() - stat.st_mtime > 3.0:
                    print("STALE")
                    sys.exit(0)
                tmp = path.with_name(path.name + '.adopting.' + str(os.getpid()))
                path.rename(tmp)
                print(tmp.read_text().strip())
                tmp.unlink()
            except FileNotFoundError:
                print("MISSING")
        """)
        procs = [
            subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
            for _ in range(5)
        ]
        outputs = [p.communicate()[0].strip() for p in procs]
        winners = [o for o in outputs if o == "shared-session"]
        losers = [o for o in outputs if o == "MISSING"]
        assert len(winners) == 1, f"Expected exactly one winner; got {outputs}"
        assert len(losers) == 4


# ---------------------------------------------------------------------------
# Approach B — Environment variable inheritance
# ---------------------------------------------------------------------------

class TestApproachB:
    """NX_SESSION_ID env var is set by parent; child subprocess inherits it."""

    def test_subprocess_reads_nx_session_id(self) -> None:
        """Subprocess launched with NX_SESSION_ID in env sees the value."""
        env = {**os.environ, "NX_SESSION_ID": "env-session-42"}
        script = "import os; print(os.environ.get('NX_SESSION_ID', 'MISSING'))"
        r = subprocess.run([sys.executable, "-c", script], env=env,
                           capture_output=True, text=True)
        assert r.stdout.strip() == "env-session-42"

    def test_subprocess_without_var_sees_missing(self) -> None:
        """Subprocess launched without NX_SESSION_ID cannot read a value."""
        env = {k: v for k, v in os.environ.items() if k != "NX_SESSION_ID"}
        script = "import os; print(os.environ.get('NX_SESSION_ID', 'MISSING'))"
        r = subprocess.run([sys.executable, "-c", script], env=env,
                           capture_output=True, text=True)
        assert r.stdout.strip() == "MISSING"

    def test_env_var_survives_two_levels(self) -> None:
        """NX_SESSION_ID propagates through subprocess → sub-subprocess chain."""
        env = {**os.environ, "NX_SESSION_ID": "deep-session-99"}
        # outer script spawns inner script
        inner = "import os; print(os.environ.get('NX_SESSION_ID', 'MISSING'))"
        outer = textwrap.dedent(f"""\
            import subprocess, sys, os
            r = subprocess.run(
                [sys.executable, '-c', {inner!r}],
                capture_output=True, text=True
            )
            print(r.stdout.strip())
        """)
        r = subprocess.run([sys.executable, "-c", outer], env=env,
                           capture_output=True, text=True)
        assert r.stdout.strip() == "deep-session-99"

    def test_env_var_not_set_at_shell_level_by_default(self) -> None:
        """Baseline: NX_SESSION_ID is not present unless explicitly set.

        This test documents the risk: if Claude Code strips env vars between
        Bash invocations (which it does — each Bash call is a fresh subprocess),
        env-var approach only works if some hook writes NX_SESSION_ID to a
        persistent shell rc file or the parent *re-exports* it on every call.
        """
        env = {k: v for k, v in os.environ.items() if k != "NX_SESSION_ID"}
        script = "import os; print('SET' if 'NX_SESSION_ID' in os.environ else 'UNSET')"
        r = subprocess.run([sys.executable, "-c", script], env=env,
                           capture_output=True, text=True)
        assert r.stdout.strip() == "UNSET"


# ---------------------------------------------------------------------------
# Approach C — Sticky current_session (read-before-write with mtime)
# ---------------------------------------------------------------------------

_STICKY_TTL = 5.0  # seconds


def maybe_adopt_session(session_file: Path, ttl_seconds: float = _STICKY_TTL) -> str | None:
    """Return existing session ID if the file is fresh, else None.

    Child SessionStart calls this before generating a new ID.  If a non-None
    value is returned, the child adopts it (skips write).  If None, the child
    generates a fresh ID and writes it.
    """
    try:
        stat = session_file.stat()
        if time.time() - stat.st_mtime <= ttl_seconds:
            content = session_file.read_text().strip()
            return content or None
    except (FileNotFoundError, OSError):
        pass
    return None


class TestApproachC:
    """Sticky session: child adopts parent's current_session if it is fresh."""

    def test_recent_file_is_adopted(self, tmp_path: Path) -> None:
        """Session file written < TTL ago is returned as-is."""
        sf = tmp_path / "current_session"
        sf.write_text("parent-session-sticky")
        result = maybe_adopt_session(sf)
        assert result == "parent-session-sticky"

    def test_file_not_consumed_on_adoption(self, tmp_path: Path) -> None:
        """Sticky adoption reads but does NOT delete the file (all children share it)."""
        sf = tmp_path / "current_session"
        sf.write_text("parent-sticky-002")
        maybe_adopt_session(sf)
        assert sf.exists()

    def test_old_file_returns_none(self, tmp_path: Path) -> None:
        """Session file older than TTL returns None → child generates fresh ID."""
        sf = tmp_path / "current_session"
        sf.write_text("old-session")
        old_time = time.time() - (_STICKY_TTL + 2)
        os.utime(sf, (old_time, old_time))
        result = maybe_adopt_session(sf)
        assert result is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """No session file → child generates fresh ID."""
        sf = tmp_path / "current_session"
        result = maybe_adopt_session(sf)
        assert result is None

    def test_multiple_concurrent_subprocesses_all_adopt_same_session(
        self, tmp_path: Path
    ) -> None:
        """All children reading a fresh current_session get the same value.

        Unlike Approach A, the file is NOT deleted — all children win.
        """
        sf = tmp_path / "current_session"
        parent_id = "sticky-shared-session"
        sf.write_text(parent_id)
        script = textwrap.dedent(f"""\
            import time, sys
            from pathlib import Path
            sf = Path({str(sf)!r})
            ttl = {_STICKY_TTL}
            try:
                stat = sf.stat()
                if time.time() - stat.st_mtime <= ttl:
                    print(sf.read_text().strip())
                else:
                    print("STALE")
            except FileNotFoundError:
                print("MISSING")
        """)
        results = [
            subprocess.run([sys.executable, "-c", script],
                           capture_output=True, text=True).stdout.strip()
            for _ in range(5)
        ]
        assert all(r == parent_id for r in results), f"Not all adopted: {results}"

    def test_sticky_ttl_window_is_tight_enough_to_avoid_collisions(
        self, tmp_path: Path
    ) -> None:
        """After TTL expires, a new invocation generates its own fresh ID.

        This documents the collision-avoidance bound: if two independent Claude
        Code windows start more than STICKY_TTL seconds apart, they do NOT share
        a session. The shorter the TTL, the safer.
        """
        sf = tmp_path / "current_session"
        sf.write_text("window-a-session")
        # expire it
        past = time.time() - (_STICKY_TTL + 1)
        os.utime(sf, (past, past))
        # Simulate 'Window B' starting after TTL
        adopted = maybe_adopt_session(sf)
        assert adopted is None  # B starts fresh; A's session is not contaminated


# ---------------------------------------------------------------------------
# Comparison summary (printed when running with -v)
# ---------------------------------------------------------------------------

def test_approach_summary() -> None:
    """Summary of trade-offs — always passes; exists for documentation only.

    Approach A (Handoff file + TTL)
        + Race-safe (exactly one adopter via POSIX rename)
        + Works even if env vars are stripped
        + Short adoption window (3 s) limits cross-window collisions
        - Requires a PreToolUse / SubagentStart hook to write the file
        - Single-adopter: parallel agent spawns each need their own handoff file
          (or the handoff key is written to a per-spawn file in a directory)

    Approach B (NX_SESSION_ID env var)
        + No extra files; purely in-memory / environment
        + Two-level propagation works (env is inherited)
        - Depends on Claude Code NOT stripping the environment between spawns
          (Bash subprocesses are fresh; Agent tool spawn behaviour is unknown)
        - Requires the parent to explicitly export the var somewhere visible to
          the child's process environment — non-trivial without a hook writing
          it to a shell init file

    Approach C (Sticky current_session mtime)
        + Zero extra mechanism: no new files, no hooks, no env vars
        + All children share automatically within TTL window
        - If two unrelated Claude Code windows start within TTL (< 5 s apart),
          they accidentally share a T1 namespace
        - Parent must write current_session BEFORE children start (it does — the
          SessionStart hook runs first in the parent conversation)
        - Children must NOT overwrite current_session if they adopt an existing ID
          (requires a conditional write in session.py)
    """
    assert True
