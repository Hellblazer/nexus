# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.1: the file-size and folder-shape ratchet fails growth past a cap or a ceiling, and
``update-exceptions`` lowers ceilings but refuses to raise or add one without ``--allow-raise``.

The unit cases run the script against a throwaway git repo with small caps; the lint case runs it
against this repo, so the committed exceptions files are what CI's lint bucket enforces.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "file_size_ratchet.py"
_spec = importlib.util.spec_from_file_location("file_size_ratchet", _SCRIPT)
assert _spec and _spec.loader
fsr = importlib.util.module_from_spec(_spec)
sys.modules["file_size_ratchet"] = fsr  # dataclass resolution looks the module up
_spec.loader.exec_module(fsr)


def _write(root: Path, rel: str, lines: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"x = {i}\n" for i in range(lines)))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(fsr, "SOURCE_SOFT_CAP", 8)
    monkeypatch.setattr(fsr, "SOURCE_HARD_CAP", 15)
    monkeypatch.setattr(fsr, "TEST_CAP", 10)
    monkeypatch.setattr(fsr, "DIR_MODULE_CAP", 3)
    _git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    return tmp_path


def _track(root: Path) -> None:
    _git(root, "add", "--", *[d for d in ("src", "tests") if (root / d).exists()])


def test_source_over_hard_cap_without_exception_fails(repo: Path) -> None:
    _write(repo, "src/nexus/big.py", 16)
    _track(repo)
    f = fsr.check(repo)
    assert any(
        "src/nexus/big.py: 16 lines, over the source cap of 15" in e for e in f.errors
    )


def test_soft_cap_warns_only(repo: Path) -> None:
    _write(repo, "src/nexus/mid.py", 9)
    _track(repo)
    f = fsr.check(repo)
    assert f.errors == []
    assert any("mid.py: 9 lines, over the soft cap of 8" in w for w in f.warnings)


def test_test_cap(repo: Path) -> None:
    _write(repo, "src/nexus/a.py", 1)
    _write(repo, "tests/test_big.py", 11)
    _track(repo)
    assert any("over the test cap of 10" in e for e in fsr.check(repo).errors)


def test_untracked_file_is_not_counted(repo: Path) -> None:
    _write(repo, "src/nexus/a.py", 1)
    _track(repo)
    _write(repo, "src/nexus/draft.py", 99)
    assert fsr.check(repo).errors == []


def test_ceiling_holds_and_growth_past_it_fails(repo: Path) -> None:
    _write(repo, "src/nexus/big.py", 20)
    _track(repo)
    (repo / fsr.SIZE_EXCEPTIONS).write_text("src/nexus/big.py 20\n")
    assert fsr.check(repo).errors == []
    _write(repo, "src/nexus/big.py", 21)
    assert any("over its ceiling of 20" in e for e in fsr.check(repo).errors)


def test_slack_and_stale_entries_are_noted(repo: Path) -> None:
    _write(repo, "src/nexus/big.py", 18)
    _write(repo, "src/nexus/small.py", 5)
    _track(repo)
    (repo / fsr.SIZE_EXCEPTIONS).write_text(
        "src/nexus/big.py 20\nsrc/nexus/small.py 30\nsrc/nexus/gone.py 40\n"
    )
    f = fsr.check(repo)
    assert f.errors == []
    joined = "\n".join(f.notes)
    assert "big.py: 18 lines, below its ceiling of 20" in joined
    assert "small.py: now within its cap" in joined
    assert "gone.py: listed with ceiling 40 but not a tracked" in joined


def test_dir_cap_and_family_rule(repo: Path) -> None:
    for m in ("a", "b", "c", "d"):
        _write(repo, f"src/nexus/pkg/{m}.py", 1)
    _write(repo, "src/nexus/session.py", 1)
    _write(repo, "src/nexus/session_store.py", 1)
    _track(repo)
    errs = "\n".join(fsr.check(repo).errors)
    assert "src/nexus/pkg/: 4 modules, over the cap of 3" in errs
    assert "src/nexus/session.py sits beside src/nexus/session_*.py" in errs
    (repo / fsr.SHAPE_EXCEPTIONS).write_text(
        "dir src/nexus/pkg 4\nfamily src/nexus/session\n"
    )
    assert fsr.check(repo).errors == []


def test_private_module_prefix_is_not_a_family(repo: Path) -> None:
    _write(repo, "src/nexus/_io.py", 1)
    _write(repo, "src/nexus/_io_extra.py", 1)
    _track(repo)
    assert fsr.check(repo).errors == []


def test_update_lowers_and_drops_without_flag(repo: Path) -> None:
    _write(repo, "src/nexus/big.py", 18)
    _write(repo, "src/nexus/small.py", 5)
    _track(repo)
    (repo / fsr.SIZE_EXCEPTIONS).write_text(
        "src/nexus/big.py 20\nsrc/nexus/small.py 30\n"
    )
    assert fsr.update_exceptions(repo, allow_raise=False) == []
    assert fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS) == {"src/nexus/big.py": 18}
    assert fsr.check(repo).notes == []


def test_update_refuses_raise_and_new_entries_without_flag(repo: Path) -> None:
    _write(repo, "src/nexus/big.py", 22)
    _write(repo, "src/nexus/new.py", 16)
    for m in ("a", "b", "c", "d"):
        _write(repo, f"src/nexus/pkg/{m}.py", 1)
    _track(repo)
    before = "src/nexus/big.py 20\n"
    (repo / fsr.SIZE_EXCEPTIONS).write_text(before)
    refusals = "\n".join(fsr.update_exceptions(repo, allow_raise=False))
    assert "big.py: raise 20 -> 22" in refusals
    assert "new.py: new exception at 16 lines" in refusals
    assert "src/nexus/pkg/: new directory exception" in refusals
    assert (repo / fsr.SIZE_EXCEPTIONS).read_text() == before, (
        "a refused update writes nothing"
    )
    assert fsr.update_exceptions(repo, allow_raise=True) == []
    assert fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS) == {
        "src/nexus/big.py": 22,
        "src/nexus/new.py": 16,
    }
    assert fsr.check(repo).errors == []


def test_malformed_exception_line_is_refused(repo: Path) -> None:
    (repo / fsr.SIZE_EXCEPTIONS).write_text("src/nexus/big.py twenty\n")
    with pytest.raises(SystemExit, match="expected '<path> <ceiling>'"):
        fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS)


def test_main_exit_codes(repo: Path) -> None:
    _write(repo, "src/nexus/a.py", 1)
    _track(repo)
    assert fsr.main(["--root", str(repo), "check"]) == fsr.EXIT_OK
    _write(repo, "src/nexus/a.py", 16)
    assert fsr.main(["--root", str(repo), "check"]) == fsr.EXIT_OVER


@pytest.mark.lint
def test_repo_is_within_its_caps_and_ceilings() -> None:
    source, tests = fsr.measure_sizes(_REPO)
    # Non-vacuity: the globs must reach the real trees, or "no errors" means nothing was counted.
    assert len(source) > 500 and len(tests) > 500, (len(source), len(tests))
    assert any(p.startswith("service/src/main/") for p in source)
    f = fsr.check(_REPO)
    assert f.errors == [], "\n".join(f.errors)
