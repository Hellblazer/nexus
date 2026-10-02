# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/develop-freeze.sh (nexus-eusu6): set / clear / status for the
board/develop-freeze topic. A fake ``nx`` stands in for the tuple space
(tests/scripts/_fake_nx.py); nothing here touches the live board."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _fake_nx import calls as fake_nx_calls
from _fake_nx import fake_env, install_fake_nx, run
from _fake_nx import post as fake_post

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "develop-freeze.sh"


@pytest.fixture()
def fx(tmp_path):
    bindir, state = install_fake_nx(tmp_path)
    env = fake_env(bindir, state)
    env["NX_SESSION_ID"] = "sess-abc123"
    work = tmp_path / "cwd"
    work.mkdir()
    return work, state, env


def _sh(fx, *args: str, env_extra: dict | None = None):
    work, _state, env = fx
    return run([str(SCRIPT), *args], cwd=work, env={**env, **(env_extra or {})})


def _rows(state: Path) -> list[dict]:
    p = state / "rows.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def test_script_is_executable() -> None:
    assert SCRIPT.exists() and os.access(SCRIPT, os.X_OK)


def test_status_with_no_posts_is_open_exit_0(fx) -> None:
    proc = _sh(fx, "status")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "open" in proc.stdout.lower()
    assert "board/develop-freeze" in fake_nx_calls(fx[1]), "status must actually read the board"


def test_set_then_status_then_clear_round_trip(fx) -> None:
    _work, state, _env = fx

    proc = _sh(fx, "set", "--reason", "7.67.0 release", "--holder", "nexus_d97")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    posted = _rows(state)
    assert len(posted) == 1
    body = json.loads(posted[0]["body"])
    assert body["state"] == "frozen"
    assert body["holder"] == "nexus_d97"
    assert body["reason"] == "7.67.0 release"
    assert body["set_at"]
    assert posted[0]["keys"] == {"topic": "develop-freeze"}
    assert posted[0]["dims"].get("from"), "the board template requires the from dimension"
    assert posted[0]["nonce"], "the board template is keys+nonce"

    st = _sh(fx, "status")
    assert st.returncode == 10, st.stdout + st.stderr
    assert "frozen" in st.stdout.lower()
    assert "nexus_d97" in st.stdout and "7.67.0 release" in st.stdout
    assert "age" in st.stdout.lower()

    cl = _sh(fx, "clear", "--reason", "tag cut and back-merged")
    assert cl.returncode == 0, cl.stdout + cl.stderr
    assert json.loads(_rows(state)[-1]["body"])["state"] == "open"

    st2 = _sh(fx, "status")
    assert st2.returncode == 0, st2.stdout + st2.stderr
    assert "open" in st2.stdout.lower()


def test_holder_defaults_to_the_session_identity(fx) -> None:
    proc = _sh(fx, "set", "--reason", "release")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    body = json.loads(_rows(fx[1])[0]["body"])
    assert "sess-abc123" in body["holder"]


def test_set_requires_a_reason(fx) -> None:
    proc = _sh(fx, "set")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not _rows(fx[1]), "a refused set must post nothing"


def test_an_overlong_reason_is_refused_before_posting(fx) -> None:
    """board/<topic> caps a body at 1024 bytes; the engine would refuse it as
    TooLarge, but a clear local message beats a raw engine error."""
    proc = _sh(fx, "set", "--reason", "x" * 2000)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not _rows(fx[1])


@pytest.mark.parametrize(
    "label,reason",
    [
        # 590 quotes / backslashes are 590 raw bytes but escape to 1180 in JSON.
        ("quote-heavy", '"' * 590),
        ("backslash-heavy", "\\" * 590),
    ],
)
def test_a_reason_that_encodes_over_the_board_cap_is_refused_before_posting(fx, label, reason) -> None:
    """The binding check is the ENCODED body, not the raw reason (nexus-eusu6
    review): the fake nx refuses >1024-byte bodies as the real template does,
    so an unchecked body would surface here as exit 5, not 2."""
    proc = _sh(fx, "set", "--reason", reason)
    assert proc.returncode == 2, f"{label}: " + proc.stdout + proc.stderr
    assert "caps a post" in proc.stderr
    assert not _rows(fx[1])


def test_a_multibyte_reason_at_the_raw_cap_posts_at_utf8_size(fx) -> None:
    """ensure_ascii=False: 200 CJK chars (600 bytes, the raw cap) post, and the
    body carries the characters. With the old default \\uXXXX escaping the same
    reason encoded to 1283 bytes and the engine refused it (review finding)."""
    proc = _sh(fx, "set", "--reason", "冻" * 200)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rows = _rows(fx[1])
    assert len(rows) == 1 and "冻" in rows[0]["body"]


def test_an_overlong_holder_is_refused_before_posting(fx) -> None:
    proc = _sh(fx, "set", "--reason", "release", "--holder", "h" * 200)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not _rows(fx[1])


def test_a_stderr_warning_on_a_successful_read_does_not_break_status(fx) -> None:
    """Only stdout is parsed: a warning on stderr with rc 0 used to be merged into
    the JSON and read as a malformed board (review finding)."""
    assert _sh(fx, "set", "--reason", "release").returncode == 0
    proc = _sh(fx, "status", env_extra={"FAKE_NX_RD_STDERR": "note: something the CLI wanted to say"})
    assert proc.returncode == 10, proc.stdout + proc.stderr


def test_a_hung_board_read_times_out_as_unreadable(fx) -> None:
    """The read is bounded (perl alarm): a hung tuple space must not hang callers."""
    proc = _sh(fx, "status", env_extra={"FAKE_NX_RD_SLEEP": "30", "FREEZE_READ_TIMEOUT": "2"})
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "timed out" in proc.stderr


def test_unknown_subcommand_is_a_usage_error(fx) -> None:
    assert _sh(fx, "bogus").returncode == 2
    assert _sh(fx).returncode == 2


def test_status_on_an_unreadable_board_is_a_distinct_nonzero(fx) -> None:
    proc = _sh(fx, "status", env_extra={"FAKE_NX_FAIL_RD": "1"})
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert proc.returncode not in (0, 10)


def test_a_failed_post_is_reported_not_swallowed(fx) -> None:
    proc = _sh(fx, "set", "--reason", "release", env_extra={"FAKE_NX_FAIL_OUT": "1"})
    assert proc.returncode == 5, proc.stdout + proc.stderr


def test_status_reports_the_age_of_the_newest_post(fx) -> None:
    import datetime

    then = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2, minutes=5))
    fake_post(fx[1], body={"state": "frozen", "holder": "h", "reason": "r"},
              created_at=then.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    proc = _sh(fx, "status")
    assert proc.returncode == 10, proc.stdout + proc.stderr
    assert "2h05m" in proc.stdout or "2h04m" in proc.stdout, proc.stdout


def test_no_installed_nx_is_a_distinct_failure(fx, tmp_path) -> None:
    """Only a dev-checkout/venv nx on PATH: refuse distinctly, as
    git-push-develop.sh does, never read the board through it."""
    work, _state, env = fx
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub = venv_bin / "nx"
    stub.write_text("#!/usr/bin/env bash\necho stub-nx-should-never-run\nexit 1\n")
    stub.chmod(0o755)
    kept = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not (Path(d) / "nx").exists()]
    env = {**env, "PATH": os.pathsep.join([str(venv_bin), *kept])}
    proc = run([str(SCRIPT), "status"], cwd=work, env=env)
    assert proc.returncode == 4, proc.stdout + proc.stderr
