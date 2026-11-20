# SPDX-License-Identifier: AGPL-3.0-or-later
"""The compaction-outcome gate's POSIX-only cases (the sibling of test_compact_outcome_harness.py).

They run the stub `nx`, a shebang script the scratch plugin's hook commands and the generation layout reach through
a symlinked `current` directory, or the `compact-outcome-gate.sh` wrapper under bash. Native Windows has neither a
shebang nor an unprivileged symlink, and the gate itself is POSIX-only (it drives a live `claude`). The module skips on
win32 at module level and the Windows test set leaves it out with `--ignore` in windows-pg-bundle-rehearsal.yml, so these
cases are not counted as skips against that job's ceiling (the test_session_facts_posix.py pattern, nexus-ct74m). The
cases that are plain Python (the decision rule, the scan of the hook's nx calls, the stub run as a Python script, the
child environment, the scenario script) stay in test_compact_outcome_harness.py and run on Windows.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.hooks.test_compact_outcome_harness import (  # noqa: F401 -- _no_claude is an autouse fixture
    CHECKPOINT, GATE_SH, HOOK_DIR, LIB, OPEN, SID, _no_claude, co, overlay, sc,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the gate is POSIX-only (shebang stub nx, bash wrapper, live claude)")


def _provisioned(tmp_path: Path, arm: str = "real", scenario=OPEN) -> tuple[dict, Path, Path]:
    """The live run's scratch layout and child env, with a fake Claude config dir naming the session."""
    iso, repo = tmp_path / "iso", tmp_path / "repo"
    sc.seed_repo(str(repo), scenario, sc.PLAN_SHORT)
    co.provision_iso(str(iso), SID, scenario)
    claude = tmp_path / "claude"
    (claude / "sessions").mkdir(parents=True)
    (claude / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": SID, "name": "demo_session"}), encoding="utf-8")
    env = co.child_env(str(iso), arm, dict(os.environ))
    env["CLAUDE_CONFIG_DIR"] = str(claude)
    return env, iso, repo


def _run_hook(mode: str, env: dict, repo: Path, extra: dict | None = None) -> subprocess.CompletedProcess:
    payload = {"session_id": SID, "cwd": str(repo), "trigger": "auto", "hook_event_name": "PreCompact", **(extra or {})}
    return subprocess.run([sys.executable, str(CHECKPOINT), mode], input=json.dumps(payload), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=90)


def _stub_calls(iso: Path) -> list[dict]:
    return [json.loads(line) for line in (iso / "stub" / "calls.jsonl").read_text(encoding="utf-8").splitlines()]



class TestStubDrivesTheRealHook:
    def test_full_stub_puts_the_board_the_checks_and_the_running_work_in_the_checkpoint(self, tmp_path: Path) -> None:
        env, iso, repo = _provisioned(tmp_path)
        assert _run_hook("pre", env, repo).returncode == 0
        ck = (iso / "nexus-config" / "compact-checkpoints" / f"{SID}.md").read_text(encoding="utf-8")
        assert "Close out the demo repo" in ck and "branch: main" in ck
        assert "## Board checked against the facts" in ck and "## Running for this session" in ck
        assert "## Background work this identity still has running" in ck and "watcher.sh" in ck
        out = _run_hook("inject", env, repo, {"hook_event_name": "SessionStart", "source": "compact"})
        assert "Close out the demo repo" in json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]

    @pytest.mark.parametrize("name", ["open", "meanwhile", "waiting", "bounded"])
    def test_every_scenarios_board_reaches_the_injection_and_every_call_was_answered(self, tmp_path: Path, name: str) -> None:
        scenario = sc.SCENARIOS[name]
        env, iso, repo = _provisioned(tmp_path, "real", scenario)
        assert _run_hook("pre", env, repo).returncode == 0
        out = _run_hook("inject", env, repo, {"hook_event_name": "SessionStart", "source": "compact"})
        text = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
        assert all(w in text for w in scenario.board_words), (scenario.board_words, text[:400])
        if scenario.indep_marker:
            assert scenario.indep_marker not in text, "the meanwhile item must live only in the summary"
        assert all(c["known"] for c in _stub_calls(iso)) and _stub_calls(iso)

    def test_the_board_is_found_by_its_identity_line_first_and_every_call_was_answered(self, tmp_path: Path) -> None:
        env, iso, repo = _provisioned(tmp_path)
        _run_hook("pre", env, repo)
        calls = _stub_calls(iso)
        argvs = [c["args"] for c in calls]
        assert all(c["known"] for c in calls)
        assert argvs[0][:3] == ["memory", "search", f"identity: {sc.IDENTITY}"], argvs
        for want in (["memory", "get"], ["command-context", "continuation"], ["identity", "work", "list"]):
            assert any(a[:len(want)] == want for a in argvs), (want, argvs)

    def test_empty_stub_is_the_planted_bad_checkpoint(self, tmp_path: Path) -> None:
        env, iso, repo = _provisioned(tmp_path, "bad")
        assert _run_hook("pre", env, repo).returncode == 0
        ck = (iso / "nexus-config" / "compact-checkpoints" / f"{SID}.md").read_text(encoding="utf-8")
        assert "Close out the demo repo" not in ck and "alpha" not in ck and "Background work" not in ck
        assert all(c["known"] for c in _stub_calls(iso))

    @pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv, which the wired hook command runs through")
    def test_the_wired_hook_commands_run_end_to_end_under_the_isolated_env(self, tmp_path: Path) -> None:
        """hooks.json runs the script through `uv tool run python`: the scratch plugin's own commands, with the scratch
        UV directories and no network, must write the checkpoint and inject it."""
        env, _iso, repo = _provisioned(tmp_path)
        plugin = tmp_path / "plugin"
        co.build_plugin(str(plugin))
        hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]

        def run(event: str, extra: dict) -> str:
            hook = hooks[event][0]["hooks"][0]
            argv = [hook["command"], *(a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)) for a in hook["args"])]
            payload = {"session_id": SID, "cwd": str(repo), "trigger": "auto", "hook_event_name": event, **extra}
            done = subprocess.run(argv, input=json.dumps(payload), env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
            assert done.returncode == 0, done.stderr
            return done.stdout

        assert run("PreCompact", {}) == ""
        out = run("SessionStart", {"source": "compact"})
        assert "Close out the demo repo" in json.loads(out)["hookSpecificOutput"]["additionalContext"]



class TestIsolationPosix:
    def test_the_only_nx_the_session_can_run_is_the_stub_and_the_config_dir_holds_no_secret(self, tmp_path: Path) -> None:
        env, iso, _repo = _provisioned(tmp_path)
        assert shutil.which("nx", path=env["PATH"]) == str(iso / "nx-tools" / "gen-1" / "bin" / "nx")
        config = iso / "nexus-config"
        files = sorted(p.relative_to(config).as_posix() for p in config.rglob("*") if p.is_file())
        assert len(files) == 1 and files[0].startswith("identity/") and files[0].endswith(".token"), files
        record = json.loads((config / files[0]).read_text(encoding="utf-8"))
        assert record["identity"] == sc.IDENTITY and record["token"] == "" and record["expires_at"] is None
        # an identity call the stub does not answer: nothing can bind
        done = subprocess.run(["nx", "identity", "bind"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert done.returncode != 0 and [c["known"] for c in _stub_calls(iso)] == [False]

    @pytest.mark.parametrize("name", ["open", "meanwhile", "waiting", "bounded"])
    def test_bd_and_gh_are_failing_stubs_and_no_board_names_a_fact_to_check(self, tmp_path: Path, name: str) -> None:
        """The hook resolves bd and gh from the PATH its generation layout puts first; today's boards name no bead, run or PR, so
        none is called, and if one ever does the stub fails instead of reaching the operator's real bd or gh."""
        env, iso, _repo = _provisioned(tmp_path, "real", sc.SCENARIOS[name])
        for tool in ("bd", "gh"):
            assert shutil.which(tool, path=env["PATH"]) == str(iso / "nx-tools" / "gen-1" / "bin" / tool)
            assert subprocess.run([tool, "list"], env=env, capture_output=True, timeout=30).returncode == 1
        spec = importlib.util.spec_from_file_location("_session_facts_under_test", HOOK_DIR / "_session_facts.py")
        sf = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = sf           # a dataclass in the module looks itself up here
        spec.loader.exec_module(sf)
        wants = sf.board_wants(sc.SCENARIOS[name].board("sid-1"))
        assert not any(dataclasses.astuple(wants)), wants


class TestGateShell:
    def test_help_prints_before_the_claude_check_and_before_anything_runs(self) -> None:
        """`-h` with no claude on the machine must print the header, not "claude is not on PATH"."""
        env = {**os.environ, "NX_GATE_CLAUDE": "/no/such/claude"}
        done = subprocess.run(["bash", str(GATE_SH), "-h"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert done.returncode == 64
        assert "compact-outcome-gate.sh" in done.stderr and "UNVERIFIED --" not in done.stdout + done.stderr
        assert "COST AND WALL TIME" in done.stderr

    def test_without_claude_the_run_is_unverified_exit_2(self) -> None:
        env = {**os.environ, "NX_GATE_CLAUDE": "/no/such/claude"}
        done = subprocess.run(["bash", str(GATE_SH), "--runs", "1"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert done.returncode == 2 and "UNVERIFIED" in done.stdout


class TestOverlayPosix:
    def test_the_overlay_files_sit_beside_the_real_checkpoint_without_either_touching_the_other(self, tmp_path: Path) -> None:
        """The real hook's prune takes only *.md, *.injected and .tmp-* files from the checkpoint directory; the overlay's
        <session>.openwork.json is none of them, and the overlay's inject deletes only its own file."""
        env, iso, repo = _provisioned(tmp_path, "fix")
        directory = iso / "nexus-config" / "compact-checkpoints"
        script = LIB / "compact_outcome_fix_overlay.py"
        payload = json.dumps({"session_id": SID, "cwd": str(repo), "transcript_path": ""})

        def overlay_run(verb: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(script), verb], input=payload, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)

        overlay_run("pre")
        assert (directory / f"{SID}.openwork.json").is_file()
        assert _run_hook("pre", env, repo).returncode == 0
        assert (directory / f"{SID}.openwork.json").is_file() and (directory / f"{SID}.md").is_file()
        text = json.loads(overlay_run("inject").stdout)["hookSpecificOutput"]["additionalContext"]
        assert "alpha, beta" in text and text.rstrip().endswith(overlay.BROAD_DIRECTIVE)
        assert (directory / f"{SID}.md").is_file() and not (directory / f"{SID}.openwork.json").exists()
