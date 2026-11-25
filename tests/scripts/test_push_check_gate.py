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
import subprocess
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
    assert list(gate.select({"README.md", "src/nexus/cli.py"}, _wire, _ctx)) == ["file-size", "changelog", "context-budget"]


def test_an_empty_diff_selects_the_always_on_checks_too() -> None:
    assert list(gate.select(set(), _wire, _ctx)) == ["file-size", "changelog", "context-budget"]


@pytest.mark.parametrize(("path", "extra"), [
    ("service/src/main/java/A.java", ["wire-ledger"]),
    ("src/nexus/db/http_thing_client.py", ["wire-ledger"]),
    ("docs/wire-contract-pending.md", ["wire-ledger", "cloud-leg", "docs-readers"]),
    ("docs/catalog.md", ["cloud-leg", "docs-readers"]),
    ("conexus/skills/x/SKILL.md", ["plugin-ledger"]),
    ("conexus/agents/a.md", ["plugin-ledger"]),
    ("sn/commands/c.md", ["plugin-ledger"]),
    ("tool-spill/resources/r.md", ["plugin-ledger"]),
    ("prose-edit/skills/b/SKILL.md", ["plugin-ledger"]),
    ("tests/fixtures/context-budgets.txt", []),
    ("scripts/context_budgets.py", []),
    ("tests/_engine_substrate.py", ["pg-cluster"]),
    ("tests/_tenant_drop.py", ["pg-cluster"]),
    ("tests/_child_process.py", ["pg-cluster"]),
    ("tests/_ddl_cluster_sites.py", ["pg-cluster"]),
    ("tests/_tmp_root.py", ["pg-cluster"]),
    ("tests/test_integration_pg_cluster.py", ["pg-cluster"]),
    ("tests/test_x.py", []),
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
    assert set(chosen) == {"file-size", "changelog", "context-budget", *extra}, chosen
    assert chosen["file-size"] == "always"
    for name in extra:
        assert chosen[name] == path, "the selecting path is named"


@pytest.mark.parametrize("path", ["conexus/skills/x/SKILL.md", "conexus/agents/a.md", "tests/fixtures/context-budgets.txt", "scripts/context_budgets.py"])
def test_the_context_budget_is_always_on_and_names_the_path_that_reached_it(path: str) -> None:
    """Always on (it takes under a second), so a tree already over a ceiling is caught on any push; a covered path is named."""
    assert gate.select({path}, _wire, _ctx)["context-budget"] == path
    assert gate.select({"README.md"}, _wire, _ctx)["context-budget"] == "always"


def test_selection_follows_the_fixed_order() -> None:
    chosen = gate.select({"conexus/skills/a.md", "docs/a.md", "service/b.java", "pyproject.toml"}, _wire, _ctx)
    chosen = gate.select({"conexus/skills/a.md", "docs/a.md", "service/b.java", "pyproject.toml", "tests/_engine_substrate.py"}, _wire, _ctx)
    assert list(chosen) == ["file-size", "changelog", "wire-ledger", "context-budget", "cloud-leg", "plugin-ledger", "pg-cluster",
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
    cmd = ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, *DOCS_FILES, "--color=yes", "-rf", "-v",
           "-o", "faulthandler_timeout=180"]
    assert gate.proof_problem(_proof("docs-readers", command=cmd), "docs-readers", TREE, DOCS_FILES) is None


@pytest.mark.parametrize("extra", [
    ["-o", "cache_dir=/x"], ["-ocache_dir=x"], ["--override-ini", "cache_dir=x"], ["--override-ini=cache_dir=x"], ["-o", "faulthandler_timeout=abc"],
    ["-o", "addopts=-k x"], ["-oaddopts="],
    ["-c", "other.ini"], ["-cother.ini"], ["--config-file", "other.ini"], ["--config-file=other.ini"],
    ["--rootdir", "/x"], ["--rootdir=/x"], ["-p", "no:cacheprovider"], ["-pxdist"], ["-p", "my_plugin"]])
def test_options_that_swap_the_configuration_a_run_reads_are_refused(extra: list[str]) -> None:
    """-o other than addopts= (and watched_pytest's own faulthandler_timeout), -c, --rootdir and -p change what pytest loads."""
    cmd = ["uv", "run", "pytest", "-o", "addopts=", "-m", gate.FAST_MARKERS, *DOCS_FILES, *extra]
    problem = gate.proof_problem(_proof("docs-readers", command=cmd), "docs-readers", TREE, DOCS_FILES)
    assert problem is not None and ("configuration" in problem or "addopts" in problem), problem


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
def test_both_proof_commands_follow_the_three_step_form_of_rule_13(leg: str) -> None:
    """AGENTS.md rule 13: only an engine build runs unwrapped (`build-gate-jar.sh --lab-run <class>`); the Python run goes
    through lab-remote, under lab-run. So: (a) warm the copy, (b) build the jar in it over ssh, (c) run pytest through lab-remote."""
    text = gate.proof_commands(leg, _files(leg), "a" * 40)
    status = "docs" if leg == "docs-readers" else "seam"
    a = text.index("(a) ")
    b = text.index("(b) ")
    c = text.index("(c) ")
    assert a < b < c
    assert "lab-remote --host chas -c lab-s -- true" in text[a:b] and "clean worktree" in text[a:b] and "prints" in text[a:b]
    assert "ssh nxtest@chas" in text[b:c] and "scripts/build-gate-jar.sh --lab-run lab-m" in text[b:c]
    assert "lab-remote" not in text[b:c] and "lab-run -c" not in text[b:c].replace("--lab-run", "")
    assert f"lab-remote --host chas -c lab-m --fetch {status}.json -- " in text[c:]
    assert f"python3 scripts/watched_pytest.py --status {status}.json -- uv run pytest" in text[c:]
    assert "sh -c" not in text


def test_the_write_seam_proof_runs_integration_tests_with_the_jar_required_and_the_docs_one_with_ci_s_marker() -> None:
    seam = gate.proof_commands("write-seam", list(gate.WRITE_SEAM_TESTS), "a" * 40)
    assert "env NX_REQUIRE_SERVICE_JAR=1 python3 scripts/watched_pytest.py" in seam and "-m integration" in seam
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


# ── the throwaway-cluster conf census (halcyon, after the develop red on 2776c549e3) ──

def test_a_cluster_path_selected_by_content_is_selected_too() -> None:
    chosen = gate.select({"tests/db/test_x.py"}, _wire, _ctx, is_cluster=lambda p: p == "tests/db/test_x.py")
    assert chosen["pg-cluster"] == "tests/db/test_x.py"


def test_the_cluster_check_runs_the_conf_census_with_the_integration_marker_included_and_needs_no_engine() -> None:
    spec = gate.SPECS["pg-cluster"]
    assert spec.token == "PUSH_REFUSED_PG_CLUSTER" and spec.kind == "run" and spec.needs == ("tests/test_integration_pg_cluster.py",)
    cmd = gate.command_for("pg-cluster", ["py"], ["py"])
    assert cmd[-3:] == ["-m", "not slow", "tests/test_integration_pg_cluster.py"], "integration is not excluded: it pins the conf lines"
    assert spec.min_passed == 80 and spec.skip_ok == "", "99 tests, no skips, no engine: a pass with skips or a mass skip is vacuous"


# ── outbound_paths: what a merge contributes (nexus-igea9) ──

def _run_git(repo: Path, *args: str, check: bool = True) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=check)
    return proc.stdout.strip()


def _commit(repo: Path, path: str, text: str, msg: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    _run_git(repo, "add", path)
    _run_git(repo, "commit", "-q", "-m", msg)
    return _run_git(repo, "rev-parse", "HEAD")


def _stale_branch_repo(tmp_path: Path) -> Path:
    """Base B on develop; branch ``feature`` from B touches notes/unrelated.md; develop then advances past B touching paths
    that select checks (files the push never changes relative to develop). HEAD is left on develop."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "develop")
    _commit(repo, "README.md", "base\n", "base")
    _run_git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "notes/unrelated.md", "feature\n", "feature work")
    _run_git(repo, "checkout", "-q", "develop")
    _commit(repo, "service/src/main/java/dev/nexus/service/db/CatalogRepository.java", "// develop\n", "develop advances")
    _commit(repo, "docs/agents/catalog-t3-split.md", "develop\n", "develop docs")
    return repo


def test_a_merge_of_a_stale_branch_onto_develop_contributes_only_what_it_brings_to_develop(tmp_path: Path) -> None:
    repo = _stale_branch_repo(tmp_path)
    base = _run_git(repo, "rev-parse", "HEAD")            # origin/develop: the push's base
    _run_git(repo, "merge", "-q", "--no-ff", "-m", "merge stale feature", "feature")   # first parent = develop
    tip = _run_git(repo, "rev-parse", "HEAD")
    assert gate.outbound_paths(repo, base, tip) == {"notes/unrelated.md"}


def test_a_merge_of_develop_into_the_pushed_branch_contributes_only_what_it_brings_to_develop(tmp_path: Path) -> None:
    """The other parent order: the pusher merged develop INTO the stale branch (first parent = the branch)."""
    repo = _stale_branch_repo(tmp_path)
    base = _run_git(repo, "rev-parse", "HEAD")
    _run_git(repo, "checkout", "-q", "feature")
    _run_git(repo, "merge", "-q", "--no-ff", "-m", "merge develop into feature", "develop")   # first parent = feature
    tip = _run_git(repo, "rev-parse", "HEAD")
    assert gate.outbound_paths(repo, base, tip) == {"notes/unrelated.md"}


def test_a_path_touched_and_reverted_in_separate_commits_still_selects_its_checks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "develop")
    _commit(repo, "service/Seam.java", "// one\n", "base")
    base = _run_git(repo, "rev-parse", "HEAD")
    _commit(repo, "service/Seam.java", "// two\n", "touch")
    _commit(repo, "service/Seam.java", "// one\n", "revert")
    tip = _run_git(repo, "rev-parse", "HEAD")
    assert _run_git(repo, "diff", "--name-only", base, tip) == "", "the net diff is empty"
    assert gate.outbound_paths(repo, base, tip) == {"service/Seam.java"}


def _init_with_base(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "develop")
    _commit(repo, "shared.txt", "one\n", "base")
    _commit(repo, "service/Seam.java", "// one\n", "base seam")
    return repo


def _head(repo: Path) -> str:
    return _run_git(repo, "rev-parse", "HEAD")


def test_a_merges_own_resolution_selects_a_file_a_later_merge_removes_again(tmp_path: Path) -> None:
    """Guards the merge's own diff: the file the resolution adds is in no non-merge commit, and a later merge hides it from
    the net diff (a later plain commit would list it itself)."""
    repo = _init_with_base(tmp_path)
    _run_git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "shared.txt", "feature\n", "feature edits shared")
    _run_git(repo, "checkout", "-q", "-b", "feature2", "develop")
    _commit(repo, "notes/f2.md", "f2", "feature two")
    _run_git(repo, "checkout", "-q", "develop")
    _commit(repo, "shared.txt", "develop\n", "develop edits shared")
    base = _head(repo)
    _run_git(repo, "merge", "--no-ff", "--no-commit", "feature", check=False)       # conflicts on shared.txt
    (repo / "shared.txt").write_text("resolved\n")
    (repo / "resolution-only.txt").write_text("added by the merge\n")
    _run_git(repo, "add", "shared.txt", "resolution-only.txt")
    _run_git(repo, "commit", "-q", "-m", "merge with resolution")
    _run_git(repo, "merge", "--no-ff", "--no-commit", "feature2")
    _run_git(repo, "rm", "-q", "resolution-only.txt")
    _run_git(repo, "commit", "-q", "-m", "merge that removes the added file")
    tip = _head(repo)
    assert "resolution-only.txt" not in _run_git(repo, "diff", "--name-only", base, tip).split()
    assert gate.outbound_paths(repo, base, tip) == {"shared.txt", "notes/f2.md", "resolution-only.txt"}


def test_an_octopus_merge_selects_what_it_brings_and_not_what_develop_changed_since_the_branches(tmp_path: Path) -> None:
    """Guards picking the in-base parent among N parents (a two-parent assumption would crash or pick the wrong side)."""
    repo = _init_with_base(tmp_path)
    for name in ("one", "two"):
        _run_git(repo, "checkout", "-q", "-b", f"f-{name}", "develop")
        _commit(repo, f"notes/{name}.md", name, f"feature {name}")
        _run_git(repo, "checkout", "-q", "develop")
    _commit(repo, "service/src/main/java/dev/nexus/service/db/CatalogRepository.java", "// develop\n", "develop advances")
    base = _head(repo)
    _run_git(repo, "merge", "-q", "--no-ff", "-m", "octopus", "f-one", "f-two")
    tip = _head(repo)
    assert len(_run_git(repo, "rev-list", "--parents", "-n1", tip).split()) == 4, "three parents"
    assert gate.outbound_paths(repo, base, tip) == {"notes/one.md", "notes/two.md"}


def test_a_path_touched_and_reverted_on_the_merged_branch_still_selects_its_checks(tmp_path: Path) -> None:
    """Guards walking the merged-in branch's own commits (a first-parent-only traversal would lose them)."""
    repo = _init_with_base(tmp_path)
    _run_git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "service/Seam.java", "// two\n", "feature touches the seam")
    _commit(repo, "service/Seam.java", "// one\n", "feature reverts it")
    _run_git(repo, "checkout", "-q", "develop")
    _commit(repo, "docs/develop-only.md", "develop\n", "develop advances")
    base = _head(repo)
    _run_git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    tip = _head(repo)
    assert _run_git(repo, "diff", "--name-only", base, tip) == "", "net and merge diffs are both empty"
    assert gate.outbound_paths(repo, base, tip) == {"service/Seam.java"}


def test_an_empty_range_has_no_outbound_paths(tmp_path: Path) -> None:
    """Pins the no-commits case: no merge lines, no diff, an empty set and no crash."""
    repo = _init_with_base(tmp_path)
    tip = _head(repo)
    assert gate.outbound_paths(repo, tip, tip) == set()


def test_a_merge_with_neither_parent_in_base_diffs_against_the_side_with_fewer_range_commits(tmp_path: Path) -> None:
    """Guards the fallback: develop has an unpushed commit (1 range commit) and the stale branch has two, and the merge lists
    the branch first. The first parent would select X, which develop changed after the branch's base and the push never does."""
    repo = _init_with_base(tmp_path)
    _run_git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "notes/f1.md", "f1", "feature one")
    _commit(repo, "notes/f2.md", "f2", "feature two")
    _run_git(repo, "checkout", "-q", "develop")
    _commit(repo, "service/X.java", "// develop after the branch's base\n", "develop advances")
    base = _head(repo)                                         # origin/develop
    _commit(repo, "notes/unpushed.md", "p", "unpushed develop commit")
    _run_git(repo, "checkout", "-q", "feature")
    _run_git(repo, "merge", "-q", "--no-ff", "-m", "merge develop into feature", "develop")   # parents: feature, develop
    tip = _head(repo)
    parents = _run_git(repo, "rev-list", "--parents", "-n1", tip).split()[1:]
    assert all(subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", p, base]).returncode == 1
               for p in parents), "neither parent is in base"
    assert gate.outbound_paths(repo, base, tip) == {"notes/unpushed.md", "notes/f1.md", "notes/f2.md"}
