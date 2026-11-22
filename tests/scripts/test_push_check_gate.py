# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/push_check_gate.py (nexus-igea9): selection, proofs and the pieces that need no repository.

The gate end to end (a real push, the five replays of 2026-11-22's reds, the cleanup) is
tests/scripts/test_git_push_develop_sh_checks.py. Here: which checks a diff selects, what makes a status file a proof, and
that the write-seam path list is ci.yml's own.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("push_check_gate", REPO_ROOT / "scripts" / "push_check_gate.py")
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
sys.modules["push_check_gate"] = gate
_spec.loader.exec_module(gate)

TREE = "t" * 40


def _wire(path: str) -> bool:
    return path.startswith("service/") or Path(path).name.startswith("http_")


# ── selection ───────────────────────────────────────────────────────────────

def test_a_diff_that_touches_nothing_selectable_still_selects_the_always_on_checks() -> None:
    assert list(gate.select({"README.md", "src/nexus/cli.py"}, _wire)) == ["file-size", "changelog"]


def test_an_empty_diff_selects_the_always_on_checks_too() -> None:
    assert list(gate.select(set(), _wire)) == ["file-size", "changelog"]


@pytest.mark.parametrize(("path", "extra"), [
    ("service/src/main/java/A.java", ["wire-ledger"]),
    ("src/nexus/db/http_thing_client.py", ["wire-ledger"]),
    ("docs/wire-contract-pending.md", ["wire-ledger", "cloud-leg", "docs-readers"]),
    ("docs/catalog.md", ["cloud-leg", "docs-readers"]),
    ("conexus/skills/x/SKILL.md", ["plugin-ledger"]),
    ("sn/hooks/y.sh", ["plugin-ledger"]),
    ("tool-spill/a.py", ["plugin-ledger"]),
    ("prose-edit/b.md", ["plugin-ledger"]),
    (".claude-plugin/marketplace.json", ["plugin-ledger"]),
    ("src/nexus/catalog/http_catalog_client.py", ["wire-ledger", "write-seam"]),
    ("src/nexus/corpus.py", ["write-seam"]),
    ("service/src/main/resources/db/changelog/x.xml", ["wire-ledger", "write-seam"]),
])
def test_each_path_class_selects_its_checks(path: str, extra: list[str]) -> None:
    chosen = gate.select({path}, _wire)
    assert set(chosen) == {"file-size", "changelog", *extra}, chosen
    assert chosen["file-size"] == "always"
    for name in extra:
        assert chosen[name] == path, "the selecting path is named"


def test_selection_follows_the_fixed_order() -> None:
    chosen = gate.select({"conexus/a.md", "docs/a.md", "service/b.java", "pyproject.toml"}, _wire)
    assert list(chosen) == ["file-size", "changelog", "wire-ledger", "cloud-leg", "plugin-ledger", "docs-readers", "write-seam"]


def test_the_write_seam_paths_are_ci_ymls_own_filter() -> None:
    """The gate mirrors the `seam` filter of ci.yml's write-seam-gate job; a path added to one and not the other is a drift."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci[ci.index("\n  write-seam-gate:"):]
    block = job[job.index("seam:\n"):job.index("- name: Verify the seam predicate")]
    listed = [m.group(1) for m in re.finditer(r"^\s*- '([^']+)'\s*$", block, re.M)]
    assert len(listed) >= 15, f"read {len(listed)} paths from the seam filter: the parse moved"
    exact = {p for p in listed if not p.endswith("**")}
    prefixes = {p[:-2] for p in listed if p.endswith("**")}
    assert exact == set(gate.WRITE_SEAM_EXACT)
    assert prefixes == set(gate.WRITE_SEAM_PREFIXES)


def test_the_write_seam_pytest_files_are_the_ones_ci_runs() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    run = ci[ci.index("uv run pytest tests/db/test_write_seam_gate_integration.py"):]
    run = run[:run.index("-o addopts")]
    assert set(re.findall(r"tests/db/\S+\.py", run)) == set(gate.WRITE_SEAM_TESTS)


def test_the_docs_readers_command_is_the_one_ci_runs() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f'-o addopts="" -m "{gate.FAST_MARKERS}"' in ci, "ci.yml's docs-readers marker moved: update FAST_MARKERS"


# ── summary counts ──────────────────────────────────────────────────────────

def test_summary_counts_read_pytests_final_line() -> None:
    assert gate.summary_counts("131 passed, 1 skipped, 3 warnings in 12.63s") == {"passed": 131, "skipped": 1}
    assert gate.summary_counts("1 failed, 2 errors, 5 passed in 1s") == {"failed": 1, "error": 2, "passed": 5}
    assert gate.summary_counts("no tests ran in 0.01s") == {}
    assert gate.summary_counts("") == {}


# ── proofs ──────────────────────────────────────────────────────────────────

DOCS_FILES = ["tests/test_a.py", "tests/test_b.py"]


def _proof(leg: str = "docs-readers", **over: object) -> dict:
    if leg == "docs-readers":
        command = ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, *DOCS_FILES, "-v", "-o", "faulthandler_timeout=180"]
        summary = "700 passed in 100.0s"
    else:
        command = ["uv", "run", "pytest", "-o", "addopts=", "-m", "integration", *gate.WRITE_SEAM_TESTS, "-v"]
        summary = "8 passed in 90.0s"
    base = {"state": "passed", "exit_code": 0, "command": command, "tree": TREE, "tree_dirty": False, "summary": summary}
    return {**base, **over}


def _files(leg: str) -> list[str]:
    return DOCS_FILES if leg == "docs-readers" else list(gate.WRITE_SEAM_TESTS)


@pytest.mark.parametrize("leg", ["docs-readers", "write-seam"])
def test_a_proof_of_the_same_tree_passes(leg: str) -> None:
    assert gate.proof_problem(_proof(leg), leg, TREE, _files(leg)) is None


@pytest.mark.parametrize(("over", "fragment"), [
    ({"tree": "u" * 40}, "tip's tree is"),
    ({"tree": None}, "no tree sha"),
    ({"tree_dirty": True}, "not clean"),
    ({"tree_dirty": None}, "not clean"),
    ({"state": "failed", "exit_code": 1}, "did not pass"),
    ({"state": "stalled", "exit_code": 124}, "did not pass"),
    ({"summary": None}, "no pytest summary"),
    ({"summary": "3 failed, 700 passed in 1s"}, "failures"),
    ({"summary": "2 passed in 1s"}, "the floor"),
    ({"command": ["uv", "run", "pytest", "-m", gate.FAST_MARKERS, *DOCS_FILES]}, "addopts"),
    ({"command": ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, DOCS_FILES[0]]}, "does not name 1 of the 2"),
    ({"command": ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, *DOCS_FILES, "-k", "docs"]}, "narrows"),
    ({"command": ["uv", "run", "pytest", "-o", "addopts=", "-m", "integration", *DOCS_FILES]}, "marker"),
    ({"command": ["uv", "run", "python", "scripts/x.py", *DOCS_FILES]}, "not a pytest run"),
])
def test_a_proof_that_does_not_prove_the_docs_readers_leg_is_refused_with_the_reason(over: dict, fragment: str) -> None:
    problem = gate.proof_problem(_proof("docs-readers", **over), "docs-readers", TREE, DOCS_FILES)
    assert problem is not None and fragment in problem, problem


def test_a_write_seam_proof_with_any_skip_is_vacuous_not_green() -> None:
    problem = gate.proof_problem(_proof("write-seam", summary="7 passed, 1 skipped in 90s"), "write-seam", TREE, _files("write-seam"))
    assert problem is not None and "skipped" in problem


def test_a_write_seam_proof_needs_the_integration_marker() -> None:
    cmd = ["uv", "run", "pytest", "-o", "addopts=", *gate.WRITE_SEAM_TESTS]
    problem = gate.proof_problem(_proof("write-seam", command=cmd), "write-seam", TREE, _files("write-seam"))
    assert problem is not None and "integration" in problem


def test_a_leading_python_dash_m_is_not_taken_for_the_marker_option() -> None:
    cmd = ["python", "-m", "pytest", "-o", "addopts=", "-m", "integration", *gate.WRITE_SEAM_TESTS]
    assert gate.proof_problem(_proof("write-seam", command=cmd), "write-seam", TREE, _files("write-seam")) is None


# ── skips ───────────────────────────────────────────────────────────────────

def test_parse_skip_takes_a_known_check_and_a_reason() -> None:
    assert gate.parse_skip("file-size=ceiling raise,  approved\nby sam") == ("file-size", "ceiling raise, approved by sam")
    assert gate.parse_skip("check-gate=no uv") == ("check-gate", "no uv")


@pytest.mark.parametrize("bad", ["file-size", "file-size=", "file-size= ", "nonsense=reason", "=reason"])
def test_parse_skip_refuses_an_unknown_check_or_a_blank_reason(bad: str) -> None:
    import argparse  # noqa: PLC0415

    with pytest.raises(argparse.ArgumentTypeError):
        gate.parse_skip(bad)
