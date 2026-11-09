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
    monkeypatch.setattr(fsr, "SLACK_MIN", 3)
    _git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    _write(tmp_path, "tests/test_seed.py", 1)  # check refuses a repo with no test tree
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
    assert "big.py" not in joined, "headroom within tolerance is the seeded norm, not a finding"
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
    assert fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS) == {"src/nexus/big.py": 20}, (
        "an accepted ceiling is kept, not churned"
    )
    assert fsr.check(repo).notes == []
    (repo / fsr.SIZE_EXCEPTIONS).write_text("src/nexus/big.py 30\n")  # past two slacks
    assert fsr.update_exceptions(repo, allow_raise=False) == []
    assert fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS) == {"src/nexus/big.py": 21}, "18 + 3"


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
        "src/nexus/big.py": 25,
        "src/nexus/new.py": 19,
    }, "a raise or new entry is written at size plus one slack"
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


def test_exactly_at_each_cap_passes(repo: Path) -> None:
    _write(repo, "src/nexus/hard.py", 15)
    _write(repo, "src/nexus/soft.py", 8)
    _write(repo, "tests/test_cap.py", 10)
    for m in ("a", "b"):
        _write(repo, f"src/nexus/pkg/{m}.py", 1)
    _track(repo)
    f = fsr.check(repo)
    assert f.errors == []
    assert not any("soft.py" in w for w in f.warnings)
    assert any("hard.py: 15 lines, over the soft cap" in w for w in f.warnings)


def test_line_count_handles_no_final_newline_and_empty(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x\ny")
    (tmp_path / "b").write_bytes(b"")
    (tmp_path / "c").write_bytes(b"x\n")
    assert [fsr._count_lines(tmp_path / n) for n in "abc"] == [2, 0, 1]


def test_tracked_but_deleted_file_is_skipped(repo: Path) -> None:
    _write(repo, "src/nexus/a.py", 1)
    _write(repo, "src/nexus/gone.py", 99)
    _track(repo)
    (repo / "src/nexus/gone.py").unlink()
    assert fsr.check(repo).errors == []


def test_slack_past_tolerance_fails(repo: Path) -> None:
    _write(repo, "src/nexus/big.py", 16)
    _track(repo)
    (repo / fsr.SIZE_EXCEPTIONS).write_text("src/nexus/big.py 22\n")
    assert fsr.check(repo).errors == [], "two slacks of SLACK_MIN (6 lines) are tolerated"
    (repo / fsr.SIZE_EXCEPTIONS).write_text("src/nexus/big.py 23\n")
    assert any("lower it in this commit" in e for e in fsr.check(repo).errors)


def test_listed_directory_growth_warns_and_unlisted_fails(repo: Path) -> None:
    for m in ("a", "b", "c", "d", "e"):
        _write(repo, f"src/nexus/pkg/{m}.py", 1)
    _track(repo)
    (repo / fsr.SHAPE_EXCEPTIONS).write_text("dir src/nexus/pkg 4\n")
    f = fsr.check(repo)
    assert f.errors == []
    assert any(
        "src/nexus/pkg/: 5 modules, over its ceiling of 4" in w for w in f.warnings
    )
    (repo / fsr.SHAPE_EXCEPTIONS).write_text("dir src/nexus/pkg 6\n")
    assert any("below its ceiling of 6" in n for n in fsr.check(repo).notes)


def test_dissolved_family_and_dir_are_noted(repo: Path) -> None:
    _write(repo, "src/nexus/a.py", 1)
    _track(repo)
    (repo / fsr.SHAPE_EXCEPTIONS).write_text(
        "dir src/nexus 9\nfamily src/nexus/session\n"
    )
    notes = "\n".join(fsr.check(repo).notes)
    assert "src/nexus/: within the module cap" in notes
    assert "family src/nexus/session: no longer a family" in notes


def test_update_refuses_family_and_dir_raise_and_writes_no_shape_file(
    repo: Path,
) -> None:
    for m in ("a", "b", "c", "d", "e"):
        _write(repo, f"src/nexus/pkg/{m}.py", 1)
    _write(repo, "src/nexus/session.py", 1)
    _write(repo, "src/nexus/session_store.py", 1)
    _track(repo)
    shape_before = "dir src/nexus/pkg 4\n"
    (repo / fsr.SHAPE_EXCEPTIONS).write_text(shape_before)
    refusals = "\n".join(fsr.update_exceptions(repo, allow_raise=False))
    assert "pkg" not in refusals, (
        "a grown listed directory warns in check, so update keeps it"
    )
    assert "family src/nexus/session: new family exception" in refusals
    assert (repo / fsr.SHAPE_EXCEPTIONS).read_text() == shape_before
    assert not (repo / fsr.SIZE_EXCEPTIONS).exists()


def test_update_keeps_a_grown_listed_directory_at_its_ceiling(repo: Path) -> None:
    for m in ("a", "b", "c", "d", "e"):
        _write(repo, f"src/nexus/pkg/{m}.py", 1)
    _write(repo, "src/nexus/big.py", 16)
    _track(repo)
    (repo / fsr.SHAPE_EXCEPTIONS).write_text("dir src/nexus/pkg 4\n")
    (repo / fsr.SIZE_EXCEPTIONS).write_text("src/nexus/big.py 20\n")
    assert fsr.update_exceptions(repo, allow_raise=False) == []
    assert fsr._read_shape(repo / fsr.SHAPE_EXCEPTIONS)[0] == {"src/nexus/pkg": 4}
    assert fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS) == {"src/nexus/big.py": 20}
    assert fsr.update_exceptions(repo, allow_raise=True) == []
    assert fsr._read_shape(repo / fsr.SHAPE_EXCEPTIONS)[0] == {"src/nexus/pkg": 5}


def test_slack_fraction_applies_to_large_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fsr, "SOURCE_HARD_CAP", 100)
    _write(repo, "src/nexus/big.py", 200)
    _track(repo)
    # slack is taken from the ceiling: 5% of 222 is 11, two of them reach 222 exactly
    (repo / fsr.SIZE_EXCEPTIONS).write_text("src/nexus/big.py 222\n")
    assert fsr.check(repo).errors == []
    (repo / fsr.SIZE_EXCEPTIONS).write_text("src/nexus/big.py 223\n")
    assert any("lower it in this commit" in e for e in fsr.check(repo).errors)


def test_directory_exactly_at_the_cap_passes(repo: Path) -> None:
    for m in ("a", "b", "c"):
        _write(repo, f"src/nexus/pkg/{m}.py", 1)
    _track(repo)
    assert fsr.check(repo).errors == []


def test_cli_update_refusal_exits_nonzero(repo: Path) -> None:
    _write(repo, "src/nexus/new.py", 16)
    _track(repo)
    assert fsr.main(["--root", str(repo), "update-exceptions"]) == fsr.EXIT_OVER


def test_update_seeds_at_the_hard_cap_not_the_soft_cap(repo: Path) -> None:
    _write(repo, "src/nexus/mid.py", 12)
    _track(repo)
    assert fsr.update_exceptions(repo, allow_raise=False) == []
    assert fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS) == {}


def test_seeded_ceilings_carry_headroom_so_a_small_growth_passes(repo: Path) -> None:
    _write(repo, "src/nexus/big.py", 20)
    _track(repo)
    assert fsr.update_exceptions(repo, allow_raise=True) == []
    assert fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS) == {"src/nexus/big.py": 23}
    _write(repo, "src/nexus/big.py", 23)  # an ordinary edit
    assert fsr.check(repo).errors == []
    assert fsr.update_exceptions(repo, allow_raise=False) == [], "an accepted entry is no raise"
    assert fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS) == {"src/nexus/big.py": 23}
    _write(repo, "src/nexus/big.py", 24)
    assert any("over its ceiling of 23" in e for e in fsr.check(repo).errors)


def test_a_shrink_by_a_whole_headroom_passes_at_every_size() -> None:
    for size in (1600, 2100, 5000, 20000):
        assert fsr.accepted(size - fsr.slack_allowed(size), fsr.ceiling_for(size)), size


def test_duplicate_entries_are_refused(repo: Path) -> None:
    (repo / fsr.SIZE_EXCEPTIONS).write_text("src/nexus/a.py 20\nsrc/nexus/a.py 30\n")
    with pytest.raises(SystemExit, match="listed twice"):
        fsr._read_pairs(repo / fsr.SIZE_EXCEPTIONS)
    (repo / fsr.SHAPE_EXCEPTIONS).write_text("family src/nexus/x\nfamily src/nexus/x\n")
    with pytest.raises(SystemExit, match="expected one"):
        fsr._read_shape(repo / fsr.SHAPE_EXCEPTIONS)
    (repo / fsr.SHAPE_EXCEPTIONS).write_text("dir src/nexus 50\ndir src/nexus 60\n")
    with pytest.raises(SystemExit, match="expected one"):
        fsr._read_shape(repo / fsr.SHAPE_EXCEPTIONS)


def test_counting_nothing_is_an_error(repo: Path) -> None:
    (repo / "tests/test_seed.py").unlink()
    _write(repo, "src/nexus/a.py", 1)
    _track(repo)
    assert any("counted 1 source and 0 test files" in e for e in fsr.check(repo).errors)


@pytest.mark.lint
def test_repo_is_within_its_caps_and_ceilings() -> None:
    source, tests = fsr.measure_sizes(_REPO)
    # Non-vacuity: the globs must reach the real trees, or "no errors" means nothing was counted.
    assert len(source) > 500 and len(tests) > 500, (len(source), len(tests))
    assert any(p.startswith("service/src/main/") for p in source)
    assert any(p.startswith("service/src/test/") for p in tests)
    f = fsr.check(_REPO)
    assert f.errors == [], "\n".join(f.errors)
