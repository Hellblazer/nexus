# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hypothesis: PPID chain walk for T1 session propagation.

The idea
--------
Instead of any handoff file or env var, use the OS process tree:

1. SessionStart writes the session ID to sessions/{os.getpid()}.session
   (keyed by the Claude Code process PID, not getsid which changes per Bash call).

2. When a subagent's SessionStart fires, instead of generating a fresh UUID,
   walk up the PPID chain looking for any sessions/{ppid}.session file.
   If one is found, adopt that session ID.  If the chain reaches PID 1, generate
   fresh.

Properties:
- No extra hooks, no handoff files, no env var threading
- Works automatically for any depth of nesting (agent spawning agent)
- Multiple concurrent sessions stay isolated: each has its own PID chain
- Chain walk is O(depth) — typically 3-5 hops

The tests here validate that each part of the mechanism works at the OS level,
without involving ChromaDB or any nexus storage.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers: the mechanism under test
# ---------------------------------------------------------------------------

def _ppid_of(pid: int) -> int | None:
    """Return the parent PID of *pid* via ps, or None if the process is gone."""
    try:
        out = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        val = int(out)
        return val if val > 1 else None
    except (subprocess.CalledProcessError, ValueError):
        return None


def find_ancestor_session(sessions_dir: Path, start_pid: int | None = None) -> str | None:
    """Walk the PPID chain from *start_pid* (default: os.getpid()) looking for
    a sessions/{pid}.session file.  Returns the first session ID found, or None.
    """
    pid = start_pid if start_pid is not None else os.getpid()
    seen: set[int] = set()
    while pid and pid not in seen:
        seen.add(pid)
        candidate = sessions_dir / f"{pid}.session"
        if candidate.exists():
            content = candidate.read_text().strip()
            if content:
                return content
        pid = _ppid_of(pid)
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPpidChainMechanism:

    def test_getppid_returns_actual_parent(self) -> None:
        """os.getppid() in a subprocess equals the parent's os.getpid()."""
        parent_pid = os.getpid()
        script = "import os; print(os.getppid())"
        r = subprocess.run([sys.executable, "-c", script],
                           capture_output=True, text=True)
        child_ppid = int(r.stdout.strip())
        assert child_ppid == parent_pid

    def test_ppid_of_helper_works(self) -> None:
        """_ppid_of(os.getpid()) returns os.getppid() — ps gives the same answer."""
        my_ppid = os.getppid()
        discovered = _ppid_of(os.getpid())
        assert discovered == my_ppid

    def test_ppid_of_nonexistent_pid_returns_none(self) -> None:
        """_ppid_of() on a non-existent PID returns None, doesn't raise."""
        assert _ppid_of(999999999) is None

    def test_chain_walk_finds_immediate_parent_file(self, tmp_path: Path) -> None:
        """Subprocess walking PPID chain finds a file written by its direct parent."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        parent_pid = os.getpid()
        (sessions / f"{parent_pid}.session").write_text("parent-session-via-pid")

        script = textwrap.dedent(f"""\
            import os, subprocess, sys
            from pathlib import Path

            sessions = Path({str(sessions)!r})

            pid = os.getpid()
            seen = set()
            result = None
            while pid and pid not in seen:
                seen.add(pid)
                candidate = sessions / f"{{pid}}.session"
                if candidate.exists():
                    result = candidate.read_text().strip()
                    break
                try:
                    out = subprocess.check_output(
                        ["ps", "-o", "ppid=", "-p", str(pid)],
                        text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                    pid = int(out)
                    if pid <= 1:
                        break
                except Exception:
                    break
            print(result or "NOT_FOUND")
        """)
        r = subprocess.run([sys.executable, "-c", script],
                           capture_output=True, text=True)
        assert r.stdout.strip() == "parent-session-via-pid"

    def test_chain_walk_finds_grandparent_file(self, tmp_path: Path) -> None:
        """Two-level subprocess chain: grandchild finds grandparent's session file.

        Simulates: parent Claude Code → child Claude Code → hook script.
        The hook script (grandchild) walks up and finds the grandparent's session.
        """
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        grandparent_pid = os.getpid()
        (sessions / f"{grandparent_pid}.session").write_text("grandparent-session")

        # Child spawns grandchild; grandchild walks chain
        grandchild_script = textwrap.dedent(f"""\
            import os, subprocess, sys
            from pathlib import Path
            sessions = Path({str(sessions)!r})
            pid = os.getpid()
            seen = set()
            result = None
            while pid and pid not in seen:
                seen.add(pid)
                candidate = sessions / f"{{pid}}.session"
                if candidate.exists():
                    result = candidate.read_text().strip()
                    break
                try:
                    out = subprocess.check_output(
                        ["ps", "-o", "ppid=", "-p", str(pid)],
                        text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                    pid = int(out)
                    if pid <= 1:
                        break
                except Exception:
                    break
            print(result or "NOT_FOUND")
        """)
        child_script = textwrap.dedent(f"""\
            import subprocess, sys
            r = subprocess.run(
                [sys.executable, "-c", {grandchild_script!r}],
                capture_output=True, text=True,
            )
            print(r.stdout.strip())
        """)
        r = subprocess.run([sys.executable, "-c", child_script],
                           capture_output=True, text=True)
        assert r.stdout.strip() == "grandparent-session"

    def test_chain_stops_at_pid_1(self, tmp_path: Path) -> None:
        """Chain walk terminates and returns None when no session file exists."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        # No session files written — walk should reach PID 1 and give up
        result = find_ancestor_session(sessions)
        assert result is None

    def test_nearest_ancestor_wins(self, tmp_path: Path) -> None:
        """When multiple ancestors have session files, the nearest one wins.

        Relevant when an agent itself spawns a further subagent: the immediate
        parent's session is adopted, not the grandparent's.
        """
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        grandparent_pid = os.getpid()
        (sessions / f"{grandparent_pid}.session").write_text("grandparent-session")

        # Child writes its own session file, then grandchild walks chain
        grandchild_script = textwrap.dedent(f"""\
            import os, subprocess, sys
            from pathlib import Path
            sessions = Path({str(sessions)!r})
            pid = os.getpid()
            seen = set()
            result = None
            while pid and pid not in seen:
                seen.add(pid)
                candidate = sessions / f"{{pid}}.session"
                if candidate.exists():
                    result = candidate.read_text().strip()
                    break
                try:
                    out = subprocess.check_output(
                        ["ps", "-o", "ppid=", "-p", str(pid)],
                        text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                    pid = int(out)
                    if pid <= 1:
                        break
                except Exception:
                    break
            print(result or "NOT_FOUND")
        """)
        child_script = textwrap.dedent(f"""\
            import os, subprocess, sys
            from pathlib import Path
            sessions = Path({str(sessions)!r})
            # Child writes its own session
            (sessions / f"{{os.getpid()}}.session").write_text("child-session")
            r = subprocess.run(
                [sys.executable, "-c", {grandchild_script!r}],
                capture_output=True, text=True,
            )
            print(r.stdout.strip())
        """)
        r = subprocess.run([sys.executable, "-c", child_script],
                           capture_output=True, text=True)
        # Grandchild's nearest ancestor with a session file is the child
        assert r.stdout.strip() == "child-session"

    def test_sibling_sessions_stay_isolated(self, tmp_path: Path) -> None:
        """Two subprocesses with different parents cannot see each other's sessions.

        Models two concurrent top-level Claude Code windows: each has its own
        PID-keyed session; no cross-contamination via PPID chain.
        """
        sessions = tmp_path / "sessions"
        sessions.mkdir()

        # Each 'sibling' writes its own session file and tries to find one
        # starting from its own PID.  There is no shared ancestor file.
        script = textwrap.dedent(f"""\
            import os, subprocess, sys
            from pathlib import Path
            sessions = Path({str(sessions)!r})
            # Write our own session
            (sessions / f"{{os.getpid()}}.session").write_text("sibling-session-" + str(os.getpid()))
            # Walk chain — will only ever find our own file, not the other sibling's
            pid = os.getpid()
            seen = set()
            results = []
            while pid and pid not in seen:
                seen.add(pid)
                c = sessions / f"{{pid}}.session"
                if c.exists():
                    results.append(c.read_text().strip())
                try:
                    out = subprocess.check_output(
                        ["ps", "-o", "ppid=", "-p", str(pid)],
                        text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                    pid = int(out)
                    if pid <= 1:
                        break
                except Exception:
                    break
            # Should find exactly its own session, not the other sibling's
            assert len(results) == 1
            assert results[0].startswith("sibling-session-")
            assert results[0].endswith(str(os.getpid()))
            print("OK")
        """)
        p1 = subprocess.Popen([sys.executable, "-c", script],
                               stdout=subprocess.PIPE, text=True)
        p2 = subprocess.Popen([sys.executable, "-c", script],
                               stdout=subprocess.PIPE, text=True)
        o1 = p1.communicate()[0].strip()
        o2 = p2.communicate()[0].strip()
        assert o1 == "OK"
        assert o2 == "OK"
