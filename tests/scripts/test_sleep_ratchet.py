# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.11: the sleep-count ratchet fails a test tree with more ``time.sleep`` calls than its
ceiling, fails a ceiling left far above the count, and ``update`` lowers the ceiling but refuses to
raise it without ``--allow-raise``.

The unit cases run the script against a throwaway git repo; the lint case runs it against this repo,
so the committed ceiling is what CI's lint bucket enforces.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "sleep_ratchet.py"
_spec = importlib.util.spec_from_file_location("sleep_ratchet", _SCRIPT)
assert _spec and _spec.loader
sr = importlib.util.module_from_spec(_spec)
sys.modules["sleep_ratchet"] = sr  # dataclass resolution looks the module up
_spec.loader.exec_module(sr)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _sleeps(root: Path, rel: str, n: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "import time\n" + "".join("time.sleep(0.01)\n" for _ in range(n)),
        encoding="utf-8",
    )


def _track(root: Path) -> None:
    _git(root, "add", "-A")


def _ceiling(root: Path, value: int) -> None:
    (root / sr.CEILING_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / sr.CEILING_FILE).write_text(f"{sr.HEADER}tests {value}\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _sleeps(tmp_path, "tests/test_a.py", 20)
    _track(tmp_path)
    return tmp_path


def test_counts_time_sleep_calls_and_a_bare_sleep_from_time_not_strings_or_other_sleeps(
    repo: Path,
) -> None:
    (repo / "tests/test_b.py").write_text(
        "from time import sleep\nimport asyncio\n"
        "sleep(1)\n"
        "asyncio.sleep(1)\n"  # not a blocking wall-clock wait
        "CHILD = 'import time; time.sleep(120)'\n"  # a child process's payload, not a wait here
        "def f(clock):\n    clock.sleep(1)\n",  # a virtual clock's sleep
        encoding="utf-8",
    )
    _track(repo)
    assert sr.count_sleeps(repo) == {"tests/test_a.py": 20, "tests/test_b.py": 1}


def test_within_the_ceiling_passes_and_over_it_fails(repo: Path) -> None:
    _ceiling(repo, 30)
    assert sr.check(repo).errors == []
    _sleeps(repo, "tests/test_c.py", 11)
    _track(repo)
    errors = sr.check(repo).errors
    assert any("31 time.sleep calls" in e and "ceiling 30" in e for e in errors), errors


def test_a_ceiling_more_than_two_slacks_above_the_count_fails(repo: Path) -> None:
    _ceiling(repo, 20 + 2 * 10 + 1)  # slack at 20 calls is the floor, 10
    errors = sr.check(repo).errors
    assert any("lower it" in e for e in errors), errors
    _ceiling(repo, 20 + 2 * 10)
    assert sr.check(repo).errors == []


def test_slack_is_the_larger_of_ten_calls_or_five_percent() -> None:
    assert sr.slack(20) == 10
    assert sr.slack(700) == 35
    assert sr.ceiling_for(700) == 735


def test_update_lowers_the_ceiling_and_refuses_a_raise(repo: Path) -> None:
    _ceiling(repo, 40)
    assert sr.update(repo, allow_raise=False) == sr.EXIT_OK
    assert sr.read_ceiling(repo) == 30
    _sleeps(repo, "tests/test_c.py", 15)
    _track(repo)
    assert sr.update(repo, allow_raise=False) == sr.EXIT_OVER
    assert sr.read_ceiling(repo) == 30, "a refused raise writes nothing"
    assert sr.update(repo, allow_raise=True) == sr.EXIT_OK
    assert sr.read_ceiling(repo) == 35 + 10


def test_a_missing_ceiling_file_and_an_empty_tree_are_errors(repo: Path) -> None:
    assert any("no ceiling" in e for e in sr.check(repo).errors)
    _ceiling(repo, 30)
    _git(repo, "rm", "-qf", "tests/test_a.py")
    assert any("counted no test files" in e for e in sr.check(repo).errors)


@pytest.mark.lint
def test_repo_is_within_its_sleep_ceiling() -> None:
    counts = sr.count_sleeps(_REPO)
    # Non-vacuity: the glob must reach the real tree, or "no errors" means nothing was counted.
    assert len(counts) > 100 and sum(counts.values()) > 300, (
        len(counts),
        sum(counts.values()),
    )
    f = sr.check(_REPO)
    assert f.errors == [], "\n".join(f.errors)
