# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.12: every plugin skill, agent and command file has a byte ceiling that only falls.

Unit cases run the script on a throwaway git repo; the lint case checks this repo, which is how
CI's lint bucket enforces the table.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("context_budgets", _REPO / "scripts" / "context_budgets.py")
assert _spec and _spec.loader
cb = importlib.util.module_from_spec(_spec)
sys.modules["context_budgets"] = cb
_spec.loader.exec_module(cb)


def _write(root: Path, rel: str, size: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "scripts").mkdir()
    _write(tmp_path, "conexus/skills/a/SKILL.md", 1000)
    _write(tmp_path, "conexus/agents/dev.md", 2000)
    _write(tmp_path, "sn/commands/c.md", 500)
    _write(tmp_path, "prose-edit/agents/e.md", 300)
    _write(tmp_path, "tool-spill/skills/t/SKILL.md", 200)
    _write(tmp_path, "conexus/hooks/notes.md", 9999)  # not a context file
    _write(tmp_path, "conexus/skills/a/run.py", 9999)  # not markdown
    _track(tmp_path)
    return tmp_path


def _track(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "--", "conexus", "sn", "prose-edit", "tool-spill"], check=True)


def _ceil(root: Path, text: str) -> None:
    (root / cb.CEILINGS).write_text(text)


_EXACT = (
    "conexus/agents/dev.md 2000\nconexus/skills/a/SKILL.md 1000\nprose-edit/agents/e.md 300\n"
    "sn/commands/c.md 500\ntool-spill/skills/t/SKILL.md 200\n"
)


def test_scope_is_plugin_skills_agents_commands_markdown(repo: Path) -> None:
    assert cb.measure(repo) == {
        "conexus/agents/dev.md": 2000,
        "conexus/skills/a/SKILL.md": 1000,
        "prose-edit/agents/e.md": 300,
        "sn/commands/c.md": 500,
        "tool-spill/skills/t/SKILL.md": 200,
    }


def test_crlf_checkout_measures_like_lf(repo: Path) -> None:
    (repo / "sn/commands/c.md").write_bytes(b"line\r\n" * 100)  # 600 bytes on disk, 500 as stored
    assert cb.measure(repo)["sn/commands/c.md"] == 500


def test_exact_ceilings_pass(repo: Path) -> None:
    _ceil(repo, _EXACT)
    assert cb.check(repo) == []


def test_growth_past_a_ceiling_fails(repo: Path) -> None:
    _ceil(repo, _EXACT)
    _write(repo, "conexus/agents/dev.md", 2001)
    assert any("dev.md: 2001 bytes, over its ceiling of 2000" in e for e in cb.check(repo))


def test_a_file_with_no_ceiling_fails(repo: Path) -> None:
    _ceil(repo, _EXACT)
    _write(repo, "conexus/skills/b/SKILL.md", 10)
    _track(repo)
    assert any("skills/b/SKILL.md: 10 bytes and no ceiling" in e for e in cb.check(repo))


def test_slack_within_tolerance_passes_and_past_it_fails(repo: Path) -> None:
    _ceil(repo, _EXACT.replace("sn/commands/c.md 500", "sn/commands/c.md 756"))  # 256 of slack
    assert cb.check(repo) == []
    _ceil(repo, _EXACT.replace("sn/commands/c.md 500", "sn/commands/c.md 757"))
    assert any("lower it in this commit" in e for e in cb.check(repo))


def test_slack_fraction_applies_to_large_files(repo: Path) -> None:
    _write(repo, "conexus/agents/dev.md", 20000)  # 5% is 1000, above SLACK_MIN
    _ceil(repo, _EXACT.replace("dev.md 2000", "dev.md 21000"))
    assert cb.check(repo) == []
    _ceil(repo, _EXACT.replace("dev.md 2000", "dev.md 21001"))
    assert any("lower it in this commit" in e for e in cb.check(repo))


def test_a_stale_entry_fails(repo: Path) -> None:
    _ceil(repo, _EXACT + "conexus/skills/gone/SKILL.md 10\n")
    assert any("gone/SKILL.md: listed but not a tracked context file" in e for e in cb.check(repo))


def test_counting_nothing_is_an_error(tmp_path: Path) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    assert any("counted no skill" in e for e in cb.check(tmp_path))


def test_update_lowers_without_flag_and_refuses_raise_and_new(repo: Path) -> None:
    _ceil(repo, _EXACT.replace("dev.md 2000", "dev.md 5000"))
    assert cb.update_exceptions(repo, allow_raise=False) == []
    assert cb.read_ceilings(repo / cb.CEILINGS)["conexus/agents/dev.md"] == 2000
    _write(repo, "conexus/agents/dev.md", 2100)
    _write(repo, "conexus/skills/b/SKILL.md", 10)
    _track(repo)
    before = (repo / cb.CEILINGS).read_text()
    refusals = "\n".join(cb.update_exceptions(repo, allow_raise=False))
    assert "dev.md: raise 2000 -> 2100" in refusals
    assert "skills/b/SKILL.md: new entry at 10 bytes" in refusals
    assert (repo / cb.CEILINGS).read_text() == before, "a refused update writes nothing"
    assert cb.update_exceptions(repo, allow_raise=True) == []
    assert cb.check(repo) == []


def test_malformed_and_duplicate_lines_are_refused(repo: Path) -> None:
    _ceil(repo, "conexus/agents/dev.md lots\n")
    with pytest.raises(SystemExit, match="expected '<path> <bytes>'"):
        cb.read_ceilings(repo / cb.CEILINGS)
    _ceil(repo, "conexus/agents/dev.md 1\nconexus/agents/dev.md 2\n")
    with pytest.raises(SystemExit, match="listed twice"):
        cb.read_ceilings(repo / cb.CEILINGS)


def test_main_exit_codes(repo: Path) -> None:
    _ceil(repo, _EXACT)
    assert cb.main(["--root", str(repo), "check"]) == cb.EXIT_OK
    _write(repo, "sn/commands/c.md", 501)
    assert cb.main(["--root", str(repo), "check"]) == cb.EXIT_OVER
    assert cb.main(["--root", str(repo), "update-exceptions"]) == cb.EXIT_OVER
    assert cb.main(["--root", str(repo), "update-exceptions", "--allow-raise"]) == cb.EXIT_OK
    assert cb.check(repo) == []
    assert b"\r\n" not in (repo / cb.CEILINGS).read_bytes()


@pytest.mark.lint
def test_repo_context_files_are_within_their_ceilings() -> None:
    sizes = cb.measure(_REPO)
    # Non-vacuity: the globs must reach the skills, agents and commands trees.
    for kind in ("conexus/skills/", "conexus/agents/", "conexus/commands/", "prose-edit/"):
        assert any(p.startswith(kind) for p in sizes), kind
    errors = cb.check(_REPO)
    assert errors == [], "\n".join(errors)
