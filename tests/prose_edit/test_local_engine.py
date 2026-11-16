# SPDX-License-Identifier: AGPL-3.0-or-later
"""The acceptance runners write T2 to a throwaway local engine, never to the operator's install (nexus-scmcs.3).

tests/prose_edit/acceptance/local_engine.bash starts a local-mode engine in a sandbox HOME and exports PROSE_EDIT_NX as
an `env -i ...` wrapper aimed at it; run-review.sh, run-teach.sh and run-memory-gate.sh source it. Here a stub `nx`
in a scratch tree's .venv/bin stands in for the real one (it records each call's arguments and environment), so the
shape of the wrapper, the init and teardown calls, the opt-outs and the exit-trap composition are pinned without an
engine. The real engine is exercised once by hand (the bead's proof), not by pytest.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.prose_edit.test_brief import ROOT

ACC = ROOT / "tests" / "prose_edit" / "acceptance"
GUARD = ACC / "runner_guard.bash"
HELPER = ACC / "local_engine.bash"

STUB_NX = r"""#!/bin/sh
CTRL="__CTRL__"
mkdir -p "$CTRL/calls"
n=$(ls "$CTRL/calls" | grep -c args)
n=$(printf '%04d' "$n")
echo "$*" > "$CTRL/calls/$n.args"
env | sort > "$CTRL/calls/$n.env"
case "$1" in
  init)
    mkdir -p "$HOME/.cache/nexus"
    [ -f "$HOME/.cache/nexus/model.bin" ] && echo seeded > "$CTRL/seeded.txt"
    echo model > "$HOME/.cache/nexus/model.bin"
    [ -f "$CTRL/config_yml" ] && mkdir -p "$NEXUS_CONFIG_DIR" && cp "$CTRL/config_yml" "$NEXUS_CONFIG_DIR/config.yml"
    [ -f "$CTRL/init_noserving" ] || echo "nx init --service complete — the service backend is serving."
    exit "$(cat "$CTRL/init_rc" 2>/dev/null || echo 0)" ;;
  daemon)
    [ "$2" = service ] && exit "$(cat "$CTRL/stop_rc" 2>/dev/null || echo 0)" ;;
esac
exit 0
"""

STUB_MEMORY = r"""import json, os, sys
open(os.path.join("__CTRL__", "memory-nx.txt"), "a").write(os.environ.get("PROSE_EDIT_NX", "<unset>") + "\n")
verb = sys.argv[1] if len(sys.argv) > 1 else ""
print(json.dumps({"voice-card": {"voice_card": None}, "rejections": {"rejections": []}}.get(verb, {})))
sys.exit(1 if verb == "promote" else 0)
"""

STUB_CLAUDE = """#!/bin/sh
printf '%s\\n' "${PROSE_EDIT_NX-<unset>}" >> "__CTRL__/claude-nx.txt"
printf '%s\\n' "$2" | head -1 >> "__CTRL__/prompts.txt"
echo '{"session_id": "stub"}'
if [ "$2" = reviewed ] && [ -f "__CTRL__/apply_on_reviewed" ]; then
  echo '{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": "python3 /w/prose-edit/skills/prose-edit/scripts/review.py apply --work /w --accept 1"}}]}}'
fi
exit 0
"""


class Box:
    """A scratch checkout: the acceptance directory, a stub nx in .venv/bin, stub scripts, a git repository."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = Path(os.path.realpath(tmp_path))
        self.wt = self.root / "wt"
        self.ctrl = self.root / "ctrl"
        self.tmp = self.root / "tmp"
        self.out = self.root / "out"
        self.ctrl.mkdir()
        self.tmp.mkdir()
        acc = self.wt / "tests" / "prose_edit" / "acceptance"
        shutil.copytree(ACC, acc, ignore=shutil.ignore_patterns("__pycache__"))
        fixtures = self.wt / "tests" / "prose_edit" / "fixtures"
        fixtures.mkdir(parents=True)
        for name in ("review-scenario.md", "user-entry.md"):
            shutil.copy(ROOT / "tests" / "prose_edit" / "fixtures" / name, fixtures / name)
        lib = self.wt / "tests" / "e2e" / "lib"
        lib.mkdir(parents=True)
        (lib / "claude_credentials.py").write_text(
            "import os, sys\nassert sys.argv[1:3] == ['run', '--'], sys.argv\nos.execvp(sys.argv[3], sys.argv[3:])\n", encoding="utf-8")
        scripts = self.wt / "prose-edit" / "skills" / "prose-edit" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "memory.py").write_text(STUB_MEMORY.replace("__CTRL__", str(self.ctrl)), encoding="utf-8")
        self.nx = self.wt / ".venv" / "bin" / "nx"
        self.write_nx()
        self.claude = self.root / "claude"
        self.claude.write_text(STUB_CLAUDE.replace("__CTRL__", str(self.ctrl)), encoding="utf-8")
        self.claude.chmod(0o755)
        docs = self.wt / "docs"
        docs.mkdir()
        (docs / "keep.md").write_text("kept\n", encoding="utf-8")
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(self.wt), *args], check=True, capture_output=True)
        self.lock = self.root / "lock"

    def write_nx(self) -> None:
        self.nx.parent.mkdir(parents=True, exist_ok=True)
        self.nx.write_text(STUB_NX.replace("__CTRL__", str(self.ctrl)), encoding="utf-8")
        self.nx.chmod(0o755)

    def control(self, name: str, text: str = "1") -> None:
        (self.ctrl / name).write_text(text, encoding="utf-8")

    def calls(self) -> list[str]:
        return [p.read_text(encoding="utf-8").strip() for p in sorted((self.ctrl / "calls").glob("*.args"))] \
            if (self.ctrl / "calls").is_dir() else []

    def call_env(self, index: int) -> dict[str, str]:
        text = sorted((self.ctrl / "calls").glob("*.env"))[index].read_text(encoding="utf-8")
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def sandboxes(self) -> list[Path]:
        return sorted(self.tmp.glob("prose-engine-*"))

    def env(self, **extra: str) -> dict[str, str]:
        e = {**os.environ, "TMPDIR": str(self.tmp), "OUT_DIR": str(self.out), "PROSE_EDIT_RUNNER_LOCK": str(self.lock),
             "CLAUDE_BIN": str(self.claude), "PROSE_EDIT_STANDIN_PYTHON": sys.executable}
        for k in ("PROSE_EDIT_NX", "PROSE_EDIT_LOCAL_ENGINE", "PROSE_EDIT_ENGINE_CACHE", "PROSE_EDIT_APP_RUNNER",
                  "PROSE_EDIT_ENGINE_STOP_WAIT", "RESUME_FROM", "SCENARIOS", "GATE_JOBS", "ANSWER",
                  "PROSE_EDIT_STANDIN_DECISIONS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "PROSE_EDIT_ISOLATED_SESSION"):
            e.pop(k, None)
        e.update(extra)
        return e


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def drive(box: Box, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Source the guard and the helper in a bash that has taken the runner lock, then run `body`."""
    script = (f'set -u\nWT="{box.wt}"\nOUT="{box.out}"\nexport TMPDIR="{box.tmp}"\n'
              f'. "{GUARD}"\n. "{HELPER}"\nrunner_lock test-runner\n{body}\n')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=box.env(**env), timeout=120)


def words(out: str) -> list[str]:
    return shlex.split(out.strip())


# ---------------------------------------------------------------------------
# The wrapper string
# ---------------------------------------------------------------------------


def test_the_wrapper_is_an_env_i_allowlist_aimed_at_a_sandbox_under_tmpdir(box: Box) -> None:
    proc = drive(box, 'local_engine_start t\nprintf "%s" "$PROSE_EDIT_NX" > "$TMPDIR/../wrapper.txt"\nprintf "%s" "$LOCAL_ENGINE_DIR" > "$TMPDIR/../sandbox.txt"',
                 UV_TOOL_DIR="/leak/uv", UV_TOOL_BIN_DIR="/leak/bin", XDG_DATA_HOME="/leak/xdg", VOYAGE_API_KEY="secret",
                 NX_SERVICE_URL="https://cloud.example.invalid")
    assert proc.returncode == 0, proc.stderr
    tokens = words((box.root / "wrapper.txt").read_text(encoding="utf-8"))
    sandbox = Path((box.root / "sandbox.txt").read_text(encoding="utf-8"))
    assert tokens[:2] == ["env", "-i"]
    assert tokens[-1] == str(box.nx)                       # the checkout's own nx, never the operator's
    pairs = dict(t.split("=", 1) for t in tokens[2:-1])
    assert pairs["HOME"] == f"{sandbox}/home" and pairs["NEXUS_CONFIG_DIR"] == f"{sandbox}/home/.config/nexus"
    assert sandbox.parent == box.tmp and sandbox.name.startswith("prose-engine-")
    assert pairs["NX_LOCAL"] == "1" and pairs["NX_NO_TELEMETRY"] == "1"
    assert pairs["PATH"] == f"{box.wt}/.venv/bin:/usr/bin:/bin"
    assert pairs["PYTHONIOENCODING"] == "utf-8" and pairs["PYTHONUTF8"] == "1"   # env -i would drop what memory.py sets
    assert "throwaway" in pairs["NX_ALLOW_PROD_WRITE"] and str(sandbox) in pairs["NX_ALLOW_PROD_WRITE"]
    assert not [k for k in pairs if k.startswith(("UV_", "XDG_", "VOYAGE", "NX_SERVICE", "PIP_"))]
    assert pairs["NO_PROXY"] == "127.0.0.1,localhost"      # a proxy never intercepts the loopback engine call
    assert set(pairs) == {"HOME", "NEXUS_CONFIG_DIR", "PATH", "TERM", "NX_LOCAL", "NX_NO_TELEMETRY", "NX_ALLOW_PROD_WRITE",
                          "PYTHONIOENCODING", "PYTHONUTF8", "NO_PROXY"}


@pytest.mark.parametrize(("operators", "expected"), [
    ({}, "127.0.0.1,localhost"),
    ({"NO_PROXY": "internal.invalid,10.0.0.0/8"}, "127.0.0.1,localhost,internal.invalid,10.0.0.0/8")])
def test_the_wrapper_keeps_loopback_out_of_a_proxy_and_appends_the_operators_no_proxy(
        box: Box, operators: dict[str, str], expected: str) -> None:
    body = 'local_engine_start t\neval "$PROSE_EDIT_NX memory list -p zzprose_x" > /dev/null'
    proc = drive(box, body, HTTPS_PROXY="http://proxy.invalid:3128", **operators)
    assert proc.returncode == 0, proc.stderr
    seen = box.call_env(box.calls().index("memory list -p zzprose_x"))
    assert seen["NO_PROXY"] == expected and seen["HTTPS_PROXY"] == "http://proxy.invalid:3128"


def test_init_runs_through_the_same_scrubbed_environment_and_nothing_ambient_reaches_it(box: Box) -> None:
    proc = drive(box, 'local_engine_start t', UV_TOOL_DIR="/leak/uv", XDG_DATA_HOME="/leak/xdg", VOYAGE_API_KEY="secret",
                 NX_SERVICE_URL="https://cloud.example.invalid", NX_SERVICE_TOKEN="tok", UV_INDEX_URL="https://mirror.invalid")
    assert proc.returncode == 0, proc.stderr
    assert box.calls()[0] == "init -y --no-autostart"       # never registers a launchd or systemd unit
    seen = box.call_env(0)
    assert not [k for k in seen if k.startswith(("UV_", "XDG_", "VOYAGE", "NX_SERVICE", "PIP_"))], sorted(seen)
    assert seen["NX_LOCAL"] == "1" and seen["NX_NO_TELEMETRY"] == "1"
    assert seen["HOME"].startswith(str(box.tmp)) and seen["NEXUS_CONFIG_DIR"] == f"{seen['HOME']}/.config/nexus"
    assert seen["HOME"] != os.environ.get("HOME")
    assert "secret" not in "".join(seen.values()) and "tok" not in seen.values()


def test_the_wrapper_runs_a_command_in_the_sandbox_and_proxies_pass_through_only_when_set(box: Box) -> None:
    body = 'local_engine_start t\neval "$PROSE_EDIT_NX memory list -p zzprose_x" > /dev/null'
    proc = drive(box, body, HTTPS_PROXY="http://proxy.invalid:3128")
    assert proc.returncode == 0, proc.stderr
    assert "memory list -p zzprose_x" in box.calls()
    index = box.calls().index("memory list -p zzprose_x")
    seen = box.call_env(index)
    assert seen["HTTPS_PROXY"] == "http://proxy.invalid:3128" and "HTTP_PROXY" not in seen
    assert not [k for k in seen if k.startswith(("UV_", "XDG_"))]


def test_a_path_with_a_space_still_splits_into_the_same_words(tmp_path: Path) -> None:
    (tmp_path / "a b").mkdir()
    box = Box(tmp_path / "a b")
    proc = drive(box, 'local_engine_start t\nprintf "%s" "$PROSE_EDIT_NX" > "$TMPDIR/../wrapper.txt"')
    assert proc.returncode == 0, proc.stderr
    tokens = words((box.root / "wrapper.txt").read_text(encoding="utf-8"))
    assert tokens[-1] == str(box.nx) and any(t.startswith("HOME=") and "/prose-engine-" in t for t in tokens)


# ---------------------------------------------------------------------------
# Which nx the run uses: the opt-out, the explicit command, a missing venv, a failed init
# ---------------------------------------------------------------------------


def test_the_opt_out_changes_nothing_and_starts_no_engine(box: Box) -> None:
    proc = drive(box, 'local_engine_start t\nprintf "%s" "${PROSE_EDIT_NX-<unset>}" > "$TMPDIR/../nx.txt"', PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    assert (box.root / "nx.txt").read_text(encoding="utf-8") == "<unset>"      # today's behaviour: the operator's nx
    assert box.calls() == [] and box.sandboxes() == []
    assert "PROSE_EDIT_LOCAL_ENGINE=0" in (box.out / "local-engine.txt").read_text(encoding="utf-8")


def test_the_opt_out_keeps_a_command_the_caller_set(box: Box) -> None:
    proc = drive(box, 'local_engine_start t\nprintf "%s" "$PROSE_EDIT_NX" > "$TMPDIR/../nx.txt"', PROSE_EDIT_LOCAL_ENGINE="0",
                 PROSE_EDIT_NX="ssh box nx")
    assert proc.returncode == 0 and (box.root / "nx.txt").read_text(encoding="utf-8") == "ssh box nx"
    assert box.calls() == [] and box.sandboxes() == []


def test_an_explicit_prose_edit_nx_wins_and_no_engine_starts(box: Box) -> None:
    proc = drive(box, 'local_engine_start t\nprintf "%s" "$PROSE_EDIT_NX" > "$TMPDIR/../nx.txt"', PROSE_EDIT_NX="/opt/my/nx --flag")
    assert proc.returncode == 0, proc.stderr
    assert (box.root / "nx.txt").read_text(encoding="utf-8") == "/opt/my/nx --flag"
    assert box.calls() == [] and box.sandboxes() == []
    assert "PROSE_EDIT_NX is set and wins" in (box.out / "local-engine.txt").read_text(encoding="utf-8")


def test_any_value_but_zero_leaves_the_engine_on(box: Box) -> None:
    proc = drive(box, 'local_engine_start t', PROSE_EDIT_LOCAL_ENGINE="1")
    assert proc.returncode == 0 and box.calls()[0].startswith("init")


def test_a_checkout_without_a_venv_nx_stops_the_run_naming_the_fix_and_never_falls_back(box: Box) -> None:
    box.nx.unlink()
    proc = drive(box, 'local_engine_start t\necho REACHED\nprintf "%s" "${PROSE_EDIT_NX-<unset>}"')
    assert proc.returncode == 1
    assert "REACHED" not in proc.stdout and "<unset>" not in proc.stdout
    assert str(box.nx) in proc.stderr and "uv sync" in proc.stderr
    assert "PROSE_EDIT_LOCAL_ENGINE=0" in proc.stderr and "PROSE_EDIT_NX" in proc.stderr
    assert box.sandboxes() == [] and not box.lock.exists()


def test_a_failed_init_stops_the_run_names_the_log_and_leaves_no_sandbox(box: Box) -> None:
    box.control("init_rc", "1")
    proc = drive(box, 'local_engine_start t\necho REACHED')
    assert proc.returncode == 1 and "REACHED" not in proc.stdout
    log = box.out / "local-engine-init.log"
    assert str(log) in proc.stderr and "did not initialise" in proc.stderr and log.is_file()
    assert box.sandboxes() == [] and not box.lock.exists()
    assert any(c.startswith("daemon service stop --with-pg") for c in box.calls())   # a half-built engine is stopped too


def test_an_init_that_exits_zero_without_a_serving_backend_is_a_failure(box: Box) -> None:
    box.control("init_noserving")
    proc = drive(box, 'local_engine_start t\necho REACHED')
    assert proc.returncode == 1 and "REACHED" not in proc.stdout
    assert "serving" in proc.stderr and str(box.out / "local-engine-init.log") in proc.stderr
    assert box.sandboxes() == []


# ---------------------------------------------------------------------------
# Teardown, composed with the guard's exit trap
# ---------------------------------------------------------------------------


def _stops(box: Box) -> list[str]:
    return [c for c in box.calls() if c.startswith("daemon ")]


@pytest.mark.parametrize(("body", "rc"), [("true", 0), ("exit 7", 7), ('kill -TERM $$\nsleep 5', 143)])
def test_the_exit_trap_stops_the_worker_then_the_service_and_removes_the_sandbox(box: Box, body: str, rc: int) -> None:
    proc = drive(box, f'local_engine_start t\n{body}')
    assert proc.returncode == rc, proc.stderr
    assert box.sandboxes() == []
    stops = _stops(box)
    assert len(stops) == 2, stops
    assert stops[0].startswith("daemon aspect-worker stop --config-dir ") and "/home/.config/nexus" in stops[0]
    assert stops[1] == "daemon service stop --with-pg"
    # both go through the scrubbed environment, aimed at the sandbox
    for i in (len(box.calls()) - 2, len(box.calls()) - 1):
        seen = box.call_env(i)
        assert seen["NX_LOCAL"] == "1" and "/prose-engine-" in seen["HOME"] and not [k for k in seen if k.startswith("UV_")]


def test_teardown_composes_with_the_guards_trap_lock_copies_and_jobs(box: Box) -> None:
    body = ('local_engine_start t\nmkdir -p "$WT/docs/zz-x"; runner_track docs/zz-x\n'
            'sleep 60 &\nprintf "%s" "$!" > "$TMPDIR/../job.pid"\nexit 0')
    proc = drive(box, body)
    assert proc.returncode == 0, proc.stderr
    assert box.sandboxes() == [] and not (box.wt / "docs" / "zz-x").exists() and not box.lock.exists()
    job = int((box.root / "job.pid").read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(job, 0)                                    # the background job was stopped by the guard's trap first
    assert len(_stops(box)) == 2


def test_a_failing_service_stop_still_removes_the_sandbox_and_leaves_the_runs_exit_code(box: Box) -> None:
    box.control("stop_rc", "1")
    proc = drive(box, 'local_engine_start t\nexit 0')
    assert proc.returncode == 0 and box.sandboxes() == []   # `daemon service stop` failed; the run's rc and the removal stand


def test_a_run_with_the_local_engine_off_calls_no_nx_at_all(box: Box) -> None:
    proc = drive(box, 'local_engine_start t', PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0 and box.calls() == []


def test_a_process_still_naming_the_sandbox_keeps_it_and_is_reported_not_killed(box: Box) -> None:
    body = ('local_engine_start t\nbash -c \'sleep 20; :\' prose-survivor "$LOCAL_ENGINE_DIR" >/dev/null 2>&1 &\n'
            'printf "%s" "$!" > "$TMPDIR/../survivor.pid"\nprintf "%s" "$LOCAL_ENGINE_DIR" > "$TMPDIR/../sandbox.txt"\n'
            'disown\nsleep 0.3\nlocal_engine_stop\necho DONE')
    proc = drive(box, body, PROSE_EDIT_ENGINE_STOP_WAIT="1")
    pid = int((box.root / "survivor.pid").read_text(encoding="utf-8"))
    try:
        assert proc.returncode == 0, proc.stderr
        sandbox = Path((box.root / "sandbox.txt").read_text(encoding="utf-8"))
        assert sandbox.is_dir()                          # kept: a data directory under a live process orphans it
        assert "KEPT" in proc.stderr and str(sandbox) in proc.stderr and str(pid) in proc.stderr
        os.kill(pid, 0)                                  # and nothing killed it
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(0.2)
        shutil.rmtree(box.sandboxes()[0] if box.sandboxes() else box.tmp / "none", ignore_errors=True)


# ---------------------------------------------------------------------------
# The model cache
# ---------------------------------------------------------------------------


def test_the_cache_is_seeded_from_and_saved_to_the_cache_directory(box: Box) -> None:
    cache = box.root / "cache"
    assert drive(box, 'local_engine_start t', PROSE_EDIT_ENGINE_CACHE=str(cache)).returncode == 0
    assert not (box.ctrl / "seeded.txt").exists()                  # first run: nothing to seed
    assert (cache / "nexus" / "model.bin").read_text(encoding="utf-8") == "model\n"   # and saved for the next
    (cache / "nexus" / "model.bin").write_text("kept\n", encoding="utf-8")
    assert drive(box, 'local_engine_start t', PROSE_EDIT_ENGINE_CACHE=str(cache)).returncode == 0
    assert (box.ctrl / "seeded.txt").exists()                      # second run: init found the seeded model
    assert (cache / "nexus" / "model.bin").read_text(encoding="utf-8") == "kept\n"   # a cache that has one is not overwritten


def test_without_a_cache_directory_nothing_is_seeded_or_saved(box: Box) -> None:
    assert drive(box, 'local_engine_start t').returncode == 0
    assert not (box.ctrl / "seeded.txt").exists()


# ---------------------------------------------------------------------------
# The runners: they source the helper, start the engine after the lock and not-run decisions, tear it down on exit
# ---------------------------------------------------------------------------

RUNNERS = ("run-review.sh", "run-teach.sh", "run-memory-gate.sh", "run-cut.sh")


@pytest.mark.parametrize("name", RUNNERS)
def test_each_runner_sources_the_helper_and_starts_the_engine_only_after_the_lock(name: str) -> None:
    sh = (ACC / name).read_text(encoding="utf-8")
    assert '. "$HERE/local_engine.bash"' in sh and f"local_engine_start {name}" in sh
    assert sh.index(f"runner_lock {name}") < sh.index(f"local_engine_start {name}")      # nothing starts without the lock
    assert not re.search(r"^trap ", sh, re.MULTILINE)                                     # the guard keeps the only EXIT trap
    assert "local_engine_stop" not in sh                                                  # registered by the helper, not by the runner
    assert sh.index(f"local_engine_start {name}") < sh.index("run() {")                      # before the first session
    assert "PROSE_EDIT_LOCAL_ENGINE=0" in sh and "PROSE_EDIT_NX" in sh                    # the runner's header says how to opt out


def test_the_helper_registers_its_teardown_with_the_guard_and_the_guard_runs_the_hooks_before_the_lock_is_released() -> None:
    helper, guard = HELPER.read_text(encoding="utf-8"), GUARD.read_text(encoding="utf-8")
    assert "runner_at_exit local_engine_stop" in helper and not re.search(r"^\s*trap ", helper, re.MULTILINE)
    cleanup = guard[guard.index("runner_cleanup() {"):guard.index("# Every acquisition decision")]
    assert cleanup.index("_runner_stop_jobs") < cleanup.index("RUNNER_EXIT_HOOKS") < cleanup.index("RUNNER_LOCK_PATH")


def _claude_nx(box: Box) -> list[str]:
    path = box.ctrl / "claude-nx.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _run_runner(box: Box, name: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(box.wt / "tests/prose_edit/acceptance" / name), *args], capture_output=True, text=True,
                          cwd=box.wt, env=box.env(**env), timeout=180)


def test_run_review_gives_its_sessions_the_wrapper_and_tears_the_engine_down(box: Box) -> None:
    proc = _run_runner(box, "run-review.sh", SCENARIOS="c")
    assert proc.returncode == 0, proc.stderr
    seen = _claude_nx(box)
    assert seen and all(words(v)[:2] == ["env", "-i"] and words(v)[-1] == str(box.nx) for v in seen)
    assert box.sandboxes() == [] and not box.lock.exists()
    assert box.calls()[0] == "init -y --no-autostart" and _stops(box)[-1] == "daemon service stop --with-pg"
    assert "started" in (box.out / "local-engine.txt").read_text(encoding="utf-8")


def test_run_review_with_the_opt_out_runs_as_it_always_did(box: Box) -> None:
    proc = _run_runner(box, "run-review.sh", SCENARIOS="c", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    assert _claude_nx(box) and set(_claude_nx(box)) == {"<unset>"}     # the sessions inherit no PROSE_EDIT_NX
    assert box.calls() == [] and box.sandboxes() == []


def test_run_review_with_its_own_command_leaves_it_alone(box: Box) -> None:
    proc = _run_runner(box, "run-review.sh", SCENARIOS="c", PROSE_EDIT_NX="/opt/my/nx")
    assert proc.returncode == 0, proc.stderr
    assert set(_claude_nx(box)) == {"/opt/my/nx"} and box.calls() == [] and box.sandboxes() == []


def test_run_review_that_has_nothing_to_run_starts_no_engine(box: Box) -> None:
    proc = _run_runner(box, "run-review.sh", SCENARIOS="a")           # a path run, NOT-RUN without the switch
    assert proc.returncode == 3, proc.stderr
    assert box.calls() == [] and box.sandboxes() == [] and not (box.out / "local-engine-init.log").exists()


@pytest.mark.parametrize(("name", "args"), [("run-teach.sh", []), ("run-memory-gate.sh", ["x", "tests/prose_edit/fixtures/review-scenario.md", "1"])])
def test_teach_and_the_gate_are_not_run_without_the_switch_and_start_no_engine(box: Box, name: str, args: list[str]) -> None:
    proc = _run_runner(box, name, *args)
    assert proc.returncode == 3, proc.stderr
    assert box.calls() == [] and box.sandboxes() == []


def test_run_memory_gate_under_the_switch_gives_its_sessions_the_wrapper_and_tears_down(box: Box) -> None:
    proc = _run_runner(box, "run-memory-gate.sh", "g", "tests/prose_edit/fixtures/review-scenario.md", "1", PROSE_EDIT_APP_RUNNER="1")
    assert proc.returncode == 0, proc.stderr
    seen = _claude_nx(box)
    assert len(seen) >= 3 and all(words(v)[:2] == ["env", "-i"] for v in seen)
    assert box.sandboxes() == [] and not box.lock.exists() and _stops(box)[-1] == "daemon service stop --with-pg"


def test_run_teach_under_the_switch_points_its_own_memory_calls_at_the_engine_and_tears_down(box: Box) -> None:
    proc = _run_runner(box, "run-teach.sh", PROSE_EDIT_APP_RUNNER="1")
    assert proc.returncode == 0, proc.stderr
    mem = (box.ctrl / "memory-nx.txt").read_text(encoding="utf-8").splitlines()
    assert mem and all(words(v)[:2] == ["env", "-i"] for v in mem)    # the script's own memory.py calls, not only the sessions
    assert box.sandboxes() == [] and not box.lock.exists() and _stops(box)[-1] == "daemon service stop --with-pg"


def test_the_runners_remove_a_leftover_standin_state_file_at_start(box: Box) -> None:
    box.out.mkdir()
    (box.out / "markup-standin-state.json").write_text('{"counter": 9}', encoding="utf-8")
    proc = _run_runner(box, "run-review.sh", SCENARIOS="c", PROSE_EDIT_LOCAL_ENGINE="0")
    assert proc.returncode == 0, proc.stderr
    assert not (box.out / "markup-standin-state.json").exists()


def test_the_memory_gate_refuses_more_than_one_job_under_the_switch_before_any_engine(box: Box) -> None:
    proc = _run_runner(box, "run-memory-gate.sh", "g", "tests/prose_edit/fixtures/review-scenario.md", "2",
                       PROSE_EDIT_APP_RUNNER="1", GATE_JOBS="2")
    assert proc.returncode == 1 and "GATE_JOBS=2" in proc.stderr and "PROSE_EDIT_APP_RUNNER=1" in proc.stderr
    assert box.calls() == [] and box.sandboxes() == [] and _claude_nx(box) == [] and not box.lock.exists()


def test_the_memory_gate_refuses_a_custom_answer_without_a_decision_table_under_the_switch(box: Box) -> None:
    args = ("g", "tests/prose_edit/fixtures/review-scenario.md", "1")
    proc = _run_runner(box, "run-memory-gate.sh", *args, PROSE_EDIT_APP_RUNNER="1", ANSWER="Reject the edit that cuts basically.")
    assert proc.returncode == 1 and "ANSWER" in proc.stderr and "PROSE_EDIT_STANDIN_DECISIONS" in proc.stderr
    assert box.calls() == [] and _claude_nx(box) == []
    table = box.root / "mine.json"
    table.write_text("{}", encoding="utf-8")
    ok = _run_runner(box, "run-memory-gate.sh", *args, PROSE_EDIT_APP_RUNNER="1", ANSWER="Reject the edit that cuts basically.",
                     PROSE_EDIT_STANDIN_DECISIONS=str(table))
    assert ok.returncode == 0, ok.stderr


def test_the_readme_documents_the_local_engine_its_switches_and_the_standin_caveats() -> None:
    text = (ACC / "README.md").read_text(encoding="utf-8")
    for needle in ("PROSE_EDIT_LOCAL_ENGINE=0", "PROSE_EDIT_NX=<command>", "PROSE_EDIT_ENGINE_CACHE", "local_engine.bash",
                   "NX_ALLOW_PROD_WRITE", "never stops a process by pattern", "uv sync",
                   "not yet confirmed on the app", "FIRST occurrence", "NAME.calls.jsonl", "`NAME.app`",
                   "harness measurement", "not a run of the preregistered gate", "GATE_JOBS", "import mcp.server.mcpserver",
                   "`reviewed`", "same turn"):
        assert needle in text, needle


def test_run_canaries_starts_the_engine_after_the_lock_and_before_its_first_session() -> None:
    sh = (ACC / "run-canaries.sh").read_text(encoding="utf-8")
    assert '. "$HERE/local_engine.bash"' in sh
    assert sh.index("runner_lock run-canaries.sh") < sh.index("local_engine_start run-canaries.sh") \
        < sh.index('"$HERE/run-scenario.sh"')
    assert not re.search(r"^trap ", sh, re.MULTILINE) and "local_engine_stop" not in sh
    # canary-fail names its own fake nx inline, so it wins over the engine's wrapper for that session only
    assert 'PROSE_EDIT_NX="python3 $HERE/fake_nx_unavailable.py"' in sh


# ---------------------------------------------------------------------------
# Session isolation, the endpoint check and the stop-wait check
# ---------------------------------------------------------------------------

def test_a_started_engine_marks_the_sessions_isolated(box: Box) -> None:
    proc = drive(box, 'local_engine_start t\necho "ISO=${PROSE_EDIT_ISOLATED_SESSION:-unset}"\n'
                      'bash -c \'echo "CHILD=${PROSE_EDIT_ISOLATED_SESSION:-unset}"\'')
    assert proc.returncode == 0, proc.stderr
    assert "ISO=1" in proc.stdout and "CHILD=1" in proc.stdout     # exported, so run-scenario.sh sees it


@pytest.mark.parametrize("env", [{"PROSE_EDIT_LOCAL_ENGINE": "0"}, {"PROSE_EDIT_NX": "python3 /x/fake.py"}])
def test_no_engine_no_isolation(box: Box, env: dict[str, str]) -> None:
    proc = drive(box, 'local_engine_start t\necho "ISO=${PROSE_EDIT_ISOLATED_SESSION:-unset}"', **env)
    assert proc.returncode == 0 and "ISO=unset" in proc.stdout     # an opt-out changes nothing from before


def test_a_sandbox_config_naming_a_remote_service_url_stops_the_run(box: Box) -> None:
    box.control("config_yml", "service_url: https://api.example.invalid/v1\n")
    proc = drive(box, 'local_engine_start t\necho REACHED')
    assert proc.returncode == 1 and "REACHED" not in proc.stdout
    assert "not loopback" in proc.stderr
    assert "PROSE_EDIT_NX" not in proc.stdout and box.sandboxes() == []   # torn down by the guard's exit hook


def test_a_sandbox_config_naming_a_loopback_service_url_is_accepted(box: Box) -> None:
    box.control("config_yml", "service_url: http://127.0.0.1:41234\n")
    proc = drive(box, 'local_engine_start t\necho REACHED')
    assert proc.returncode == 0 and "REACHED" in proc.stdout, proc.stderr


@pytest.mark.parametrize("value", ["abc", "5s", "-1"])
def test_a_stop_wait_that_is_not_whole_seconds_stops_the_run_before_any_engine(box: Box, value: str) -> None:
    proc = drive(box, 'local_engine_start t\necho REACHED', PROSE_EDIT_ENGINE_STOP_WAIT=value)
    assert proc.returncode == 1 and "REACHED" not in proc.stdout
    assert "PROSE_EDIT_ENGINE_STOP_WAIT" in proc.stderr and box.calls() == [] and box.sandboxes() == []
