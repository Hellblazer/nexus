# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hypothesis tests for T1 session-ID propagation across agent spawns."""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

_HANDOFF_TTL = 3.0
_STICKY_TTL = 5.0


# ── Approach A helpers ───────────────────────────────────────────────────────

def write_handoff(path: Path, session_id: str) -> None:
    path.write_text(session_id)


def adopt_handoff(path: Path, ttl_seconds: float = _HANDOFF_TTL) -> str | None:
    try:
        stat = path.stat()
        if time.time() - stat.st_mtime > ttl_seconds:
            return None
        tmp = path.with_name(path.name + f".adopting.{os.getpid()}")
        path.rename(tmp)
        content = tmp.read_text().strip()
        tmp.unlink()
        return content or None
    except (FileNotFoundError, OSError):
        return None


_ADOPT_SCRIPT_TEMPLATE = """\
import os, time, sys
from pathlib import Path
path = Path({path!r})
try:
    stat = path.stat()
    if time.time() - stat.st_mtime > 3.0:
        print("STALE"); sys.exit(0)
    tmp = path.with_name(path.name + '.adopting.' + str(os.getpid()))
    path.rename(tmp)
    print(tmp.read_text().strip())
    tmp.unlink()
except FileNotFoundError:
    print("MISSING")
"""


class TestApproachA:
    def test_adopted_within_ttl(self, tmp_path):
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "session-parent-001")
        assert adopt_handoff(hf) == "session-parent-001"

    def test_file_consumed_on_adoption(self, tmp_path):
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "session-parent-002")
        adopt_handoff(hf)
        assert not hf.exists()

    def test_second_reader_gets_none(self, tmp_path):
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "session-parent-003")
        assert adopt_handoff(hf) == "session-parent-003"
        assert adopt_handoff(hf) is None

    def test_stale_handoff_not_adopted(self, tmp_path):
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "stale-session")
        old = time.time() - (_HANDOFF_TTL + 2)
        os.utime(hf, (old, old))
        assert adopt_handoff(hf) is None and hf.exists()

    def test_subprocess_adopts_within_ttl(self, tmp_path):
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "session-sub-001")
        script = _ADOPT_SCRIPT_TEMPLATE.format(path=str(hf))
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        assert r.stdout.strip() == "session-sub-001"

    def test_concurrent_subprocesses_only_one_wins(self, tmp_path):
        hf = tmp_path / "t1_handoff"
        write_handoff(hf, "shared-session")
        script = _ADOPT_SCRIPT_TEMPLATE.format(path=str(hf))
        procs = [
            subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
            for _ in range(5)
        ]
        outputs = [p.communicate()[0].strip() for p in procs]
        winners = [o for o in outputs if o == "shared-session"]
        assert len(winners) == 1 and len([o for o in outputs if o == "MISSING"]) == 4


# ── Approach B ───────────────────────────────────────────────────────────────

_ENV_SCRIPT = "import os; print(os.environ.get('NX_SESSION_ID', 'MISSING'))"


class TestApproachB:
    @pytest.mark.parametrize("set_var,expected", [
        (True, "env-session-42"),
        (False, "MISSING"),
    ])
    def test_subprocess_env_inheritance(self, set_var, expected):
        env = {k: v for k, v in os.environ.items() if k != "NX_SESSION_ID"}
        if set_var:
            env["NX_SESSION_ID"] = "env-session-42"
        r = subprocess.run([sys.executable, "-c", _ENV_SCRIPT], env=env,
                           capture_output=True, text=True)
        assert r.stdout.strip() == expected

    def test_env_var_survives_two_levels(self):
        env = {**os.environ, "NX_SESSION_ID": "deep-session-99"}
        inner = _ENV_SCRIPT
        outer = textwrap.dedent(f"""\
            import subprocess, sys
            r = subprocess.run([sys.executable, '-c', {inner!r}], capture_output=True, text=True)
            print(r.stdout.strip())
        """)
        r = subprocess.run([sys.executable, "-c", outer], env=env,
                           capture_output=True, text=True)
        assert r.stdout.strip() == "deep-session-99"


# ── Approach C helpers ───────────────────────────────────────────────────────

def maybe_adopt_session(session_file: Path, ttl_seconds: float = _STICKY_TTL) -> str | None:
    try:
        stat = session_file.stat()
        if time.time() - stat.st_mtime <= ttl_seconds:
            content = session_file.read_text().strip()
            return content or None
    except (FileNotFoundError, OSError):
        pass
    return None


class TestApproachC:
    def test_recent_file_adopted(self, tmp_path):
        sf = tmp_path / "current_session"
        sf.write_text("parent-session-sticky")
        assert maybe_adopt_session(sf) == "parent-session-sticky"

    def test_file_not_consumed(self, tmp_path):
        sf = tmp_path / "current_session"
        sf.write_text("parent-sticky-002")
        maybe_adopt_session(sf)
        assert sf.exists()

    @pytest.mark.parametrize("setup,expected", [
        ("old", None), ("missing", None),
    ])
    def test_old_or_missing_returns_none(self, tmp_path, setup, expected):
        sf = tmp_path / "current_session"
        if setup == "old":
            sf.write_text("old-session")
            old = time.time() - (_STICKY_TTL + 2)
            os.utime(sf, (old, old))
        assert maybe_adopt_session(sf) is expected

    def test_concurrent_subprocesses_all_adopt_same(self, tmp_path):
        sf = tmp_path / "current_session"
        parent_id = "sticky-shared-session"
        sf.write_text(parent_id)
        script = textwrap.dedent(f"""\
            import time, sys
            from pathlib import Path
            sf = Path({str(sf)!r})
            try:
                stat = sf.stat()
                if time.time() - stat.st_mtime <= {_STICKY_TTL}:
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
        assert all(r == parent_id for r in results)

    def test_ttl_expiry_prevents_collision(self, tmp_path):
        sf = tmp_path / "current_session"
        sf.write_text("window-a-session")
        past = time.time() - (_STICKY_TTL + 1)
        os.utime(sf, (past, past))
        assert maybe_adopt_session(sf) is None


def test_approach_summary():
    """Trade-off summary — always passes; documentation only."""
    assert True
