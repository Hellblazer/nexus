# SPDX-License-Identifier: AGPL-3.0-or-later
"""run-scenario.sh points a session at the stand-in MarkupEditor server only under PROSE_EDIT_APP_RUNNER=1 (nexus-scmcs.3).

The runners run a copy of the acceptance directory in a scratch tree, with a stub in place of the credentials helper
and a stub `claude` that records its argv, so no session starts and nothing costs money. The argv is asserted whole:
without the switch it is what it was before the stand-in existed; with it, it is that argv plus the MCP config and
the tool allowances, and nothing else.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

from tests.prose_edit.test_brief import ROOT

ACC = ROOT / "tests" / "prose_edit" / "acceptance"
F = "tests/prose_edit/fixtures"
PATH_RUN = f"/prose-edit {F}/qualifiers-a.md --genre reference-doc --budget 5"
STDIN_RUN = "/prose-edit - --genre commit-message\n\nfix: make the queue drain in order basically"
TOOLS = ("open_document", "get_document", "propose_edits", "list_suggestions", "undo")


def _tree(tmp_path: Path) -> Path:
    """A scratch tree laid out like the checkout: tests/prose_edit/acceptance and a credentials stub."""
    wt = Path(os.path.realpath(tmp_path)) / "wt"
    acc = wt / "tests" / "prose_edit" / "acceptance"
    acc.mkdir(parents=True)
    for name in ("run-scenario.sh", "runner_guard.bash", "record-run.sh", "markup_standin.py", "markup_standin_decisions.json"):
        shutil.copy(ACC / name, acc / name)
    lib = wt / "tests" / "e2e" / "lib"
    lib.mkdir(parents=True)
    (lib / "claude_credentials.py").write_text(
        "import os, sys\nassert sys.argv[1:3] == ['run', '--'], sys.argv\nos.execvp(sys.argv[3], sys.argv[3:])\n", encoding="utf-8")
    return wt


def _run(wt: Path, tmp_path: Path, name: str, prompt: str, **env: str) -> tuple[list[str], Path]:
    """Run run-scenario.sh with a stub claude; the argv the stub saw, and the out dir."""
    out = Path(os.path.realpath(tmp_path)) / "out"
    argv_file = tmp_path / "argv.txt"
    stub = tmp_path / "claude"
    stub.write_text(f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\0\' "$a"; done > {argv_file}\nexit 0\n', encoding="utf-8")
    stub.chmod(0o755)
    e = {**os.environ, "OUT_DIR": str(out), "CLAUDE_BIN": str(stub), "TMPDIR": str(tmp_path)}
    for k in ("PROSE_EDIT_APP_RUNNER", "PROSE_EDIT_STANDIN_PYTHON", "PROSE_EDIT_STANDIN_DECISIONS", "RESUME_FROM",
              "PROSE_EDIT_ISOLATED_SESSION"):
        e.pop(k, None)
    if env.get("PROSE_EDIT_APP_RUNNER") == "1" and "PROSE_EDIT_STANDIN_PYTHON" not in env:
        e["PROSE_EDIT_STANDIN_PYTHON"] = sys.executable   # this interpreter has mcp; the preflight imports it
    e.update(env)
    proc = subprocess.run(["bash", str(wt / "tests/prose_edit/acceptance/run-scenario.sh"), name, prompt],
                          capture_output=True, text=True, env=e, timeout=60)
    assert argv_file.exists(), proc.stderr
    return argv_file.read_bytes().decode("utf-8").split("\0")[:-1], out


def _baseline(wt: Path, tmp_path: Path, prompt: str) -> list[str]:
    """The argv run-scenario.sh built before the stand-in existed."""
    scripts = f"{wt}/prose-edit/skills/prose-edit/scripts"
    return ["-p", prompt, "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
            "--plugin-dir", f"{wt}/prose-edit",
            "--allowedTools", f"Bash(python3 {scripts}/brief.py:*)", f"Bash(python3 {scripts}/memory.py:*)",
            f"Bash(python3 {scripts}/review.py:*)", "Read", "Write", "Agent",
            "--disallowedTools", "Bash(nx:*)", "Bash(uv:*)", "Bash(bd:*)", "Bash(curl:*)",
            "--add-dir", str(tmp_path), "--max-turns", "40"]


@pytest.mark.parametrize(("name", "prompt"), [("c1", STDIN_RUN), ("canary-fail", PATH_RUN), ("a2", "Accept edit 1."),
                                              ("canary-nx", "Run the shell command `nx --version`.")])
def test_without_the_switch_the_session_gets_the_argv_it_always_had(tmp_path: Path, name: str, prompt: str) -> None:
    wt = _tree(tmp_path)
    argv, out = _run(wt, tmp_path, name, prompt)
    assert argv == _baseline(wt, tmp_path, prompt)
    assert not list(out.glob("*.mcp.json")) and not (out / "markup-standin-state.json").exists()


def _split(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """The argv without the stand-in's additions, and the additions."""
    rest = list(argv)
    i = rest.index("--mcp-config")
    config = rest[i + 1]
    del rest[i:i + 2]
    rest.remove("--strict-mcp-config")
    allowed = {t: f"mcp__markupeditor__{t}" for t in TOOLS}
    for tool in allowed.values():
        rest.remove(tool)
    return rest, {"config": config}


def _transcript(out: Path, name: str, *, standin: bool) -> None:
    """What an earlier turn of the run left: its stream-json (with a session id) and, for a stand-in turn, its config."""
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.jsonl").write_text(json.dumps({"session_id": f"sid-{name}"}) + "\n", encoding="utf-8")
    if standin:
        (out / f"{name}.mcp.json").write_text("{}", encoding="utf-8")


def test_with_the_switch_a_path_run_gets_the_mcp_config_and_the_tools_and_nothing_else(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    argv, out = _run(wt, tmp_path, "a1", PATH_RUN, PROSE_EDIT_APP_RUNNER="1")
    rest, found = _split(argv)
    assert rest == _baseline(wt, tmp_path, PATH_RUN)
    assert found["config"] == str(out / "a1.mcp.json")
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    assert all(f"mcp__markupeditor__{t}" in allowed for t in TOOLS)
    # the skill never saves or replaces: those stay refused (dontAsk) so a run that tries shows as a denied call
    assert not any("save_document" in a or "replace_blocks" in a for a in argv)
    assert "--strict-mcp-config" in argv  # the machine's own MarkupEditor servers stay out of a scenario
    assert not any(a.startswith("mcp__markupeditor__") for a in argv[:argv.index("--allowedTools")])


@pytest.mark.parametrize(("name", "prompt"), [
    ("c1", STDIN_RUN),                                                                  # a stdin run has no app to drive
    ("cutonly-1", f"/conexus-prose-edit:prose-cut {F}/review-scenario.md --mode cut-only --voice-card c.txt "
                  "--word-budget 40 --genre how-to"),                                   # run-cut.sh
    ("canary-nx", "Run the shell command `nx --version` with the Bash tool."),          # run-canaries.sh
    ("a2", "Accept edit 1."),                                                           # an answer turn with nothing to resume
])
def test_with_the_switch_a_session_that_does_not_drive_the_app_keeps_the_argv_it_always_had(tmp_path: Path, name: str, prompt: str) -> None:
    wt = _tree(tmp_path)
    argv, out = _run(wt, tmp_path, name, prompt, PROSE_EDIT_APP_RUNNER="1")
    assert argv == _baseline(wt, tmp_path, prompt)
    assert not list(out.glob("*.mcp.json"))


def test_with_the_switch_an_answer_turn_resumed_from_a_stand_in_turn_gets_the_stand_in_and_one_resumed_from_a_stdin_run_does_not(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    out = Path(os.path.realpath(tmp_path)) / "out"
    _transcript(out, "a1", standin=True)
    _transcript(out, "c1", standin=False)
    argv, _ = _run(wt, tmp_path, "a2", "reviewed", PROSE_EDIT_APP_RUNNER="1", RESUME_FROM="a1")
    rest, found = _split(argv)
    assert found["config"] == str(out / "a2.mcp.json")
    assert rest[:2] == ["-p", "reviewed"] and "--resume" in rest and rest[rest.index("--resume") + 1] == "sid-a1"
    argv, _ = _run(wt, tmp_path, "c2", "Accept edit 1.", PROSE_EDIT_APP_RUNNER="1", RESUME_FROM="c1")
    assert "--mcp-config" not in argv and "--strict-mcp-config" not in argv
    assert not any(a.startswith("mcp__markupeditor__") for a in argv)
    assert not (out / "c2.mcp.json").exists()


def test_the_config_names_the_standin_under_markupeditor_with_its_state_decisions_and_scenario(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    _, out = _run(wt, tmp_path, "a1", PATH_RUN, PROSE_EDIT_APP_RUNNER="1")
    _transcript(out, "a1", standin=False)   # the jsonl with a session id; a1.mcp.json is the run's own
    _, out = _run(wt, tmp_path, "a2", "reviewed", PROSE_EDIT_APP_RUNNER="1", RESUME_FROM="a1")
    acc = wt / "tests" / "prose_edit" / "acceptance"
    one = json.loads((out / "a1.mcp.json").read_text(encoding="utf-8"))
    two = json.loads((out / "a2.mcp.json").read_text(encoding="utf-8"))
    assert list(one["mcpServers"]) == ["markupeditor"]
    server = one["mcpServers"]["markupeditor"]
    assert server["command"] == sys.executable and server["args"] == [str(acc / "markup_standin.py")]
    assert server["env"] == {"MARKUP_STANDIN_STATE": str(out / "markup-standin-state.json"),
                             "MARKUP_STANDIN_SCENARIO": "a1",
                             "MARKUP_STANDIN_CALLS": str(out / "a1.calls.jsonl"),
                             "MARKUP_STANDIN_DECISIONS": str(acc / "markup_standin_decisions.json")}
    # the sessions of one run share the state file (a resumed session is a new process), and differ in the scenario
    assert two["mcpServers"]["markupeditor"]["env"]["MARKUP_STANDIN_STATE"] == server["env"]["MARKUP_STANDIN_STATE"]
    assert two["mcpServers"]["markupeditor"]["env"]["MARKUP_STANDIN_SCENARIO"] == "a2"


def _stub_python(where: Path, *, exit_code: int = 0) -> Path:
    """An interpreter stand-in: logs its argv to ARGV.txt beside it and exits with `exit_code` (the import check)."""
    where.mkdir(parents=True, exist_ok=True)
    py = where / "python"
    py.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{where}/ARGV.txt"\nexit {exit_code}\n', encoding="utf-8")
    py.chmod(0o755)
    return py


def test_the_caller_can_name_the_interpreter_and_the_decision_table(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    table = tmp_path / "mine.json"
    table.write_text("{}", encoding="utf-8")
    py = _stub_python(tmp_path / "opt")
    _, out = _run(wt, tmp_path, "a1", PATH_RUN, PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_STANDIN_PYTHON=str(py),
                  PROSE_EDIT_STANDIN_DECISIONS=str(table))
    server = json.loads((out / "a1.mcp.json").read_text(encoding="utf-8"))["mcpServers"]["markupeditor"]
    assert server["command"] == str(py) and server["env"]["MARKUP_STANDIN_DECISIONS"] == str(table)


def test_the_checkouts_own_venv_is_the_interpreter_when_it_has_one(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    py = wt / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")   # `-c 'import mcp...'` succeeds
    py.chmod(0o755)
    _, out = _run(wt, tmp_path, "a1", PATH_RUN, PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_STANDIN_PYTHON="")  # unset: the default
    assert json.loads((out / "a1.mcp.json").read_text(encoding="utf-8"))["mcpServers"]["markupeditor"]["command"] == str(py)


def test_the_written_config_starts_a_server_that_answers_the_markupeditor_tools(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    _, out = _run(wt, tmp_path, "a1", PATH_RUN, PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_STANDIN_PYTHON=sys.executable)
    server = json.loads((out / "a1.mcp.json").read_text(encoding="utf-8"))["mcpServers"]["markupeditor"]
    doc = tmp_path / "d.md"
    doc.write_text("# T\n\nA basically fine line.\n", encoding="utf-8")

    async def go() -> tuple[set[str], str]:
        env = {**get_default_environment(), **server["env"]}
        params = StdioServerParameters(command=server["command"], args=server["args"], env=env)
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            await session.call_tool("open_document", {"path": str(doc)})
            reply = await session.call_tool("get_document", {"from": 0, "limit": 1})
            return names, reply.content[0].text  # type: ignore[union-attr]

    names, text = asyncio.run(go())
    assert names >= set(TOOLS) | {"save_document"}
    assert json.loads(text)["blocks"][0]["markdown"] == "# T"


def test_the_guard_reason_names_the_standin_and_no_longer_says_none_exists() -> None:
    proc = subprocess.run(["bash", "-c", '. "$1"; printf %s "$RUNNER_NO_APP_REASON"', "x", str(ACC / "runner_guard.bash")],
                          capture_output=True, text=True, timeout=30)
    reason = proc.stdout
    assert "none exists yet" not in reason
    assert "PROSE_EDIT_APP_RUNNER=1" in reason and "markup_standin.py" in reason and "MarkupEditor" in reason


def test_the_readme_says_how_to_run_against_the_standin() -> None:
    text = (ACC / "README.md").read_text(encoding="utf-8")
    assert "markup_standin.py" in text and "markup_standin_decisions.json" in text and "PROSE_EDIT_STANDIN_PYTHON" in text


# ---------------------------------------------------------------------------
# The preflight: a stand-in that cannot start is refused before any session (code review r1, blocker 1)
# ---------------------------------------------------------------------------


def test_an_interpreter_without_mcp_stops_the_run_before_any_session_and_names_the_variable(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    py = _stub_python(tmp_path / "bare", exit_code=1)   # as a system python3 without the mcp package answers
    out = Path(os.path.realpath(tmp_path)) / "out"
    argv_file = tmp_path / "argv.txt"
    stub = tmp_path / "claude"
    stub.write_text(f"#!/bin/sh\ntouch {argv_file}\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {**os.environ, "OUT_DIR": str(out), "CLAUDE_BIN": str(stub), "TMPDIR": str(tmp_path),
           "PROSE_EDIT_APP_RUNNER": "1", "PROSE_EDIT_STANDIN_PYTHON": str(py)}
    proc = subprocess.run(["bash", str(wt / "tests/prose_edit/acceptance/run-scenario.sh"), "a1", PATH_RUN],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 1, proc.stderr
    assert "PROSE_EDIT_STANDIN_PYTHON" in proc.stderr and "mcp.server.mcpserver" in proc.stderr and str(py) in proc.stderr
    assert not argv_file.exists()  # no session started, no tokens spent
    assert not (out / "a1.mcp.json").exists() and not (out / "not-run.txt").exists()  # and it is not recorded as NOT-RUN
    assert (tmp_path / "bare" / "ARGV.txt").read_text(encoding="utf-8").strip() == "-c import mcp.server.mcpserver"


def test_the_default_interpreter_is_checked_too(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    py = wt / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    py.chmod(0o755)
    out = Path(os.path.realpath(tmp_path)) / "out"
    env = {**os.environ, "OUT_DIR": str(out), "CLAUDE_BIN": "/nonexistent/claude", "TMPDIR": str(tmp_path), "PROSE_EDIT_APP_RUNNER": "1"}
    env.pop("PROSE_EDIT_STANDIN_PYTHON", None)
    proc = subprocess.run(["bash", str(wt / "tests/prose_edit/acceptance/run-scenario.sh"), "a1", PATH_RUN],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 1 and str(py) in proc.stderr


def test_a_session_that_does_not_need_the_standin_is_not_held_up_by_the_check(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    py = _stub_python(tmp_path / "bare", exit_code=1)
    argv, _ = _run(wt, tmp_path, "c1", STDIN_RUN, PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_STANDIN_PYTHON=str(py))
    assert argv == _baseline(wt, tmp_path, STDIN_RUN)
    assert not (tmp_path / "bare" / "ARGV.txt").exists()


# ---------------------------------------------------------------------------
# NAME.app: a stand-in run says so
# ---------------------------------------------------------------------------


def _git_tree(tmp_path: Path) -> Path:
    wt = _tree(tmp_path)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True)
    return wt


def test_record_run_writes_standin_when_the_session_had_a_stand_in_config_and_none_otherwise(tmp_path: Path) -> None:
    wt = _git_tree(tmp_path)
    out = tmp_path / "out"
    _transcript(out, "a1", standin=True)
    _transcript(out, "c1", standin=False)
    for name in ("a1", "c1"):
        subprocess.run(["bash", str(wt / "tests/prose_edit/acceptance/record-run.sh"), str(out), name], check=True,
                       capture_output=True, text=True, timeout=60)
    assert (out / "a1.app").read_text(encoding="utf-8").strip() == "standin"
    assert (out / "c1.app").read_text(encoding="utf-8").strip() == "none"


def test_a_session_run_through_run_scenario_with_the_switch_leaves_its_app_marker(tmp_path: Path) -> None:
    wt = _git_tree(tmp_path)
    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\necho '{\"session_id\": \"s\"}'\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    out = tmp_path / "out"
    env = {**os.environ, "OUT_DIR": str(out), "CLAUDE_BIN": str(stub), "TMPDIR": str(tmp_path),
           "PROSE_EDIT_APP_RUNNER": "1", "PROSE_EDIT_STANDIN_PYTHON": sys.executable}
    for name, prompt in (("a1", PATH_RUN), ("c1", STDIN_RUN)):
        subprocess.run(["bash", str(wt / "tests/prose_edit/acceptance/run-scenario.sh"), name, prompt],
                       capture_output=True, text=True, env=env, timeout=60, check=True)
    assert (out / "a1.app").read_text(encoding="utf-8").strip() == "standin"
    assert (out / "c1.app").read_text(encoding="utf-8").strip() == "none"


ISO = ["--setting-sources", "project,local", "--strict-mcp-config"]


def _without_iso(argv: list[str]) -> list[str]:
    rest = list(argv)
    i = rest.index("--setting-sources")
    assert rest[i + 1] == "project,local"
    del rest[i:i + 2]
    return rest


@pytest.mark.parametrize(("name", "prompt"), [("c1", STDIN_RUN), ("a2", "Accept edit 1."),
                                              ("canary-nx", "Run the shell command `nx --version`.")])
def test_an_isolated_session_loads_no_user_settings_and_no_user_mcp_servers(tmp_path: Path, name: str, prompt: str) -> None:
    wt = _tree(tmp_path)
    argv, _ = _run(wt, tmp_path, name, prompt, PROSE_EDIT_ISOLATED_SESSION="1")
    base = _baseline(wt, tmp_path, prompt)
    i = base.index("--disallowedTools")
    assert argv == base[:i] + ISO + base[i:]          # the plugin still comes from --plugin-dir; nothing else moves


def test_an_isolated_path_run_under_the_switch_names_strict_mcp_config_once(tmp_path: Path) -> None:
    wt = _tree(tmp_path)
    argv, out = _run(wt, tmp_path, "a1", PATH_RUN, PROSE_EDIT_APP_RUNNER="1", PROSE_EDIT_ISOLATED_SESSION="1")
    assert argv.count("--strict-mcp-config") == 1 and argv.count("--setting-sources") == 1
    rest, found = _split(_without_iso(argv))
    assert rest == _baseline(wt, tmp_path, PATH_RUN) and found["config"] == str(out / "a1.mcp.json")


@pytest.mark.parametrize("value", ["0", ""])
def test_without_the_isolation_mark_the_argv_is_unchanged(tmp_path: Path, value: str) -> None:
    wt = _tree(tmp_path)
    argv, _ = _run(wt, tmp_path, "c1", STDIN_RUN, PROSE_EDIT_ISOLATED_SESSION=value)
    assert argv == _baseline(wt, tmp_path, STDIN_RUN)
