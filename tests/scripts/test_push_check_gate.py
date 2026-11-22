# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/push_check_gate.py (nexus-igea9): selection, proofs and the pieces that need no repository.

The gate end to end (a real push, the five replays of 2026-11-22's reds, the cleanup) is
tests/scripts/test_git_push_develop_sh_checks.py. Here: which checks a diff selects, what makes a status file a proof, and
that the write-seam path list is ci.yml's own.
"""
from __future__ import annotations

import importlib.util
import os
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


def _ctx(path: str) -> bool:
    """What scripts/context_budgets.py covers: .md under <plugin>/<kind>/, its ceilings file and itself."""
    return (path.endswith(".md") and path.startswith(tuple(f"{p}/{k}/" for p in ("conexus", "sn", "prose-edit", "tool-spill")
                                                           for k in ("skills", "agents", "commands", "resources")))
            or path in ("tests/fixtures/context-budgets.txt", "scripts/context_budgets.py"))


# ── selection ───────────────────────────────────────────────────────────────

def test_a_diff_that_touches_nothing_selectable_still_selects_the_always_on_checks() -> None:
    assert list(gate.select({"README.md", "src/nexus/cli.py"}, _wire, _ctx)) == ["file-size", "changelog"]


def test_an_empty_diff_selects_the_always_on_checks_too() -> None:
    assert list(gate.select(set(), _wire, _ctx)) == ["file-size", "changelog"]


@pytest.mark.parametrize(("path", "extra"), [
    ("service/src/main/java/A.java", ["wire-ledger"]),
    ("src/nexus/db/http_thing_client.py", ["wire-ledger"]),
    ("docs/wire-contract-pending.md", ["wire-ledger", "cloud-leg", "docs-readers"]),
    ("docs/catalog.md", ["cloud-leg", "docs-readers"]),
    ("conexus/skills/x/SKILL.md", ["plugin-ledger", "context-budget"]),
    ("conexus/agents/a.md", ["plugin-ledger", "context-budget"]),
    ("sn/commands/c.md", ["plugin-ledger", "context-budget"]),
    ("tool-spill/resources/r.md", ["plugin-ledger", "context-budget"]),
    ("prose-edit/skills/b/SKILL.md", ["plugin-ledger", "context-budget"]),
    ("tests/fixtures/context-budgets.txt", ["context-budget"]),
    ("scripts/context_budgets.py", ["context-budget"]),
    ("sn/hooks/y.sh", ["plugin-ledger"]),
    ("tool-spill/a.py", ["plugin-ledger"]),
    ("prose-edit/b.md", ["plugin-ledger"]),
    (".claude-plugin/marketplace.json", ["plugin-ledger"]),
    ("src/nexus/catalog/http_catalog_client.py", ["wire-ledger", "write-seam"]),
    ("src/nexus/corpus.py", ["write-seam"]),
    ("service/src/main/resources/db/changelog/x.xml", ["wire-ledger", "write-seam"]),
])
def test_each_path_class_selects_its_checks(path: str, extra: list[str]) -> None:
    chosen = gate.select({path}, _wire, _ctx)
    assert set(chosen) == {"file-size", "changelog", *extra}, chosen
    assert chosen["file-size"] == "always"
    for name in extra:
        assert chosen[name] == path, "the selecting path is named"


def test_selection_follows_the_fixed_order() -> None:
    chosen = gate.select({"conexus/skills/a.md", "docs/a.md", "service/b.java", "pyproject.toml"}, _wire, _ctx)
    assert list(chosen) == ["file-size", "changelog", "wire-ledger", "context-budget", "cloud-leg", "plugin-ledger",
                            "docs-readers", "write-seam"]


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
        summary = "1600 passed in 100.0s"
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
    ({"summary": "3 failed, 1600 passed in 1s"}, "failures"),
    ({"summary": "2 passed in 1s"}, "the floor"),
    ({"command": ["uv", "run", "pytest", "-m", gate.FAST_MARKERS, *DOCS_FILES]}, "addopts"),
    ({"command": ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, DOCS_FILES[0]]}, "does not name 1 of the 2"),
    ({"command": ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, *DOCS_FILES, "-k", "docs"]}, "narrows"),
    *[({"command": ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, *DOCS_FILES, arg]}, "narrows")
      for arg in ("-kdocs", "--keyword=docs", "--ignore=tests/test_a.py", "--ignore", "--ignore-glob=tests/*", "--deselect=tests/test_a.py::t",
                  "--lf", "--last-failed", "--sw", "--stepwise", "--maxfail=1", "--maxfail", "-x", "-xvs", "--exitfirst", "--co", "--collect-only")],
    ({"command": ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, *DOCS_FILES, "-m", "integration"]}, "exactly one -m"),
    ({"command": ["uv", "run", "pytest", "-o", "addopts=", "-m", "integration", "-m", gate.FAST_MARKERS, *DOCS_FILES]}, "exactly one -m"),
    ({"command": ["uv", "run", "pytest", "-o", "addopts=", f"-m{gate.FAST_MARKERS}", *DOCS_FILES]}, "exactly one -m"),
    ({"command": ["uv", "run", "pytest", "-o", "addopts=", "-o", "addopts=-k nothing", "-m", gate.FAST_MARKERS, *DOCS_FILES]}, "addopts"),
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


def test_options_that_merely_start_like_a_narrowing_one_are_not_narrowing() -> None:
    cmd = ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, *DOCS_FILES, "--color=yes", "-p", "no:cacheprovider", "-rf"]
    assert gate.proof_problem(_proof("docs-readers", command=cmd), "docs-readers", TREE, DOCS_FILES) is None


# ── what a proof says about itself, the re-push line, the floors ─────────────

def test_an_accepted_proof_is_summarised_with_its_tree_start_duration_and_host() -> None:
    proof = _proof("docs-readers", started_at=1795000000.0, elapsed_s=159.6, host="chas")
    line = gate.proof_line("docs-readers", proof, Path("/x/docs.json"))
    assert line == "PUSH_PROOF_ACCEPTED docs-readers tree=tttttttttttt started_at=2026-11-18T11:06:40Z elapsed_s=160 host=chas file=docs.json"


def test_a_proof_without_a_host_or_start_says_unknown_rather_than_inventing_one() -> None:
    line = gate.proof_line("write-seam", _proof("write-seam"), Path("seam.json"))
    assert "host=unknown" in line and "started_at=unknown" in line and "elapsed_s=unknown" in line


def test_the_repush_line_carries_every_missing_proof_between_the_flags_and_the_shas() -> None:
    line = gate.repush_line("scripts/git-push-develop.sh --source HEAD", "abc123 def456", ["docs-readers", "write-seam"])
    assert line == "scripts/git-push-develop.sh --source HEAD --proof docs.json --proof seam.json abc123 def456"


@pytest.mark.parametrize("leg", ["docs-readers", "write-seam"])
def test_both_proof_commands_build_the_jar_as_their_own_step_and_never_wrap_anything_in_lab_run(leg: str) -> None:
    """AGENTS.md rule 13: an engine build goes through `build-gate-jar.sh --lab-run <class>`, never inside lab-run (and
    lab-remote runs its command under lab-run), so the jar is step 1 and pytest is step 2, on the pool host itself."""
    text = gate.proof_commands(leg, _files(leg), "a" * 40)
    assert "1) scripts/build-gate-jar.sh --lab-run lab-m" in text and "2) " in text
    assert text.index("1) scripts/build-gate-jar.sh") < text.index("2) ")
    assert "lab-run -c" not in text.replace("--lab-run", "") and "lab-remote" not in text and "sh -c" not in text
    assert "python3 scripts/watched_pytest.py --status" in text and f"--status {'docs' if leg == 'docs-readers' else 'seam'}.json" in text
    assert "pool host" in text and "clean checkout" in text


def test_the_write_seam_proof_runs_integration_tests_with_the_jar_required_and_the_docs_one_with_ci_s_marker() -> None:
    seam = gate.proof_commands("write-seam", list(gate.WRITE_SEAM_TESTS), "a" * 40)
    assert "NX_REQUIRE_SERVICE_JAR=1" in seam and "-m integration" in seam
    docs = gate.proof_commands("docs-readers", DOCS_FILES, "a" * 40)
    assert f'-m "{gate.FAST_MARKERS}"' in docs and " ".join(DOCS_FILES) in docs and "NX_REQUIRE_SERVICE_JAR" not in docs


def test_the_floors_are_below_what_a_real_run_collects_and_well_above_a_mass_skip() -> None:
    """Collected on 2026-11-22: test_cloud_leg.py 102 (101 pass, 1 opt-in skip), test_plugin_release_drift_ledger.py 30."""
    assert gate.SPECS["docs-readers"].min_passed == 1500
    assert gate.SPECS["plugin-ledger"].min_passed == 20 and gate.SPECS["cloud-leg"].min_passed == 80


# ── the child environment ────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "GIT_DIR", "GIT_INDEX_FILE"])
def test_the_child_environment_drops_what_could_change_a_run(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "-k nothing")
    assert name not in gate._env()


# ── pytest results: skips are vacuous unless named ───────────────────────────

CLOUD_SKIP = "SKIPPED [1] tests/scripts/test_cloud_leg.py:932: set NX_CLOUDLEG_REAL_RUN=1 (needs the gate jar, uv and the network)"


def test_the_one_named_cloud_leg_skip_is_allowed_and_nothing_else_is() -> None:
    out_ok = f"{CLOUD_SKIP}\n101 passed, 1 skipped in 12s\n"
    assert gate.skip_problem("cloud-leg", out_ok) is None
    assert gate.skip_problem("cloud-leg", "SKIPPED [1] tests/x.py:1: no jar\n100 passed, 1 skipped in 1s\n") is not None
    assert gate.skip_problem("cloud-leg", f"{CLOUD_SKIP}\nSKIPPED [1] tests/x.py:1: other\n100 passed, 2 skipped in 1s\n") is not None
    assert gate.skip_problem("plugin-ledger", "27 passed, 3 skipped in 1s\n") is not None
    assert gate.skip_problem("plugin-ledger", "30 passed in 1s\n") is None


# ── a timeout stops only the check that timed out ────────────────────────────

def test_a_timeout_kills_only_its_own_check_and_what_it_started(tmp_path: Path) -> None:
    import concurrent.futures  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    marker = tmp_path / "grandchild.pid"
    slow = [sys.executable, "-c", f"import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"open({str(marker)!r},'w').write(str(p.pid)); time.sleep(60)"]
    sibling = [sys.executable, "-c", "import time; time.sleep(3); print('sibling finished')"]
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        slow_future = pool.submit(gate._run_child, slow, tmp_path, dict(os.environ), 1)
        sibling_future = pool.submit(gate._run_child, sibling, tmp_path, dict(os.environ), 30)
        with pytest.raises(subprocess.TimeoutExpired):
            slow_future.result(timeout=30)
        code, out = sibling_future.result(timeout=30)
    assert code == 0 and "sibling finished" in out, "a sibling check is not stopped by another's timeout"
    pid = int(marker.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the timed-out check's own child survived it")


# ── skips ───────────────────────────────────────────────────────────────────

def test_parse_skip_takes_a_known_check_and_a_reason() -> None:
    assert gate.parse_skip("file-size=ceiling raise,  approved\nby sam") == ("file-size", "ceiling raise, approved by sam")
    assert gate.parse_skip("check-gate=no uv") == ("check-gate", "no uv")


@pytest.mark.parametrize("bad", ["file-size", "file-size=", "file-size= ", "nonsense=reason", "=reason"])
def test_parse_skip_refuses_an_unknown_check_or_a_blank_reason(bad: str) -> None:
    import argparse  # noqa: PLC0415

    with pytest.raises(argparse.ArgumentTypeError):
        gate.parse_skip(bad)


# ── the uv fallback never touches the lock or the environment ─────────────────

def _fake_tool(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_the_uv_fallback_is_frozen_and_no_sync_and_is_checked_before_it_is_trusted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "uv.log"
    bare = _fake_tool(tmp_path / "bare-python", 'exit 1\n')                       # a python with no pytest
    _fake_tool(tmp_path / "uv", f'echo "$@" >> {log}\nexit 0\n')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    cmd = gate.pytest_python(tmp_path / "checkout", str(bare))
    assert cmd[1:6] == ["run", "--frozen", "--no-sync", "--project", str(tmp_path / "checkout")] and cmd[-1] == "python"
    assert "--frozen --no-sync" in log.read_text(), "the import probe went through the same frozen, no-sync command"


def test_a_uv_environment_without_pytest_is_a_gate_error_not_a_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bare = _fake_tool(tmp_path / "bare-python", 'exit 1\n')
    _fake_tool(tmp_path / "uv", 'exit 1\n')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    with pytest.raises(gate.GateError, match="uv sync"):
        gate.pytest_python(tmp_path / "checkout", str(bare))



@pytest.mark.parametrize("leg", ["docs-readers", "write-seam"])
def test_the_printed_proof_commands_call_python3_because_a_pool_host_has_no_bare_python(leg: str) -> None:
    """Found by running the printed docs-readers command on a pool host (WSL): `sh: 1: python: not found`."""
    text = gate.proof_commands(leg, _files(leg), "a" * 40)
    assert "python3 scripts/watched_pytest.py --status" in text and " python scripts/" not in text
