# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-spwp2: no cc-validation scenario hand-writes a plugin install, and every
scenario that depends on a plugin goes through the one shared helper.

Claude Code 2.1.302 does not load a plugin from a hand-written
``installed_plugins.json`` entry: ``claude plugin list`` says "failed to load:
Marketplace nexus-plugins not found" and no hook, agent or server of the plugin
exists in the session. Scenarios 12 and 18 (and the fake-plugin parts 13b and
14b) wrote that file, so they ran with NO plugin and passed or failed on a
plugin-less session. The fix is ``cc_plugin_install`` / ``cc_plugin_require`` in
``tests/cc-validation/runner.sh`` (a local-directory marketplace, then a check
that ``claude plugin list`` shows the plugin enabled at the version its
plugin.json declares). These tests pin the census, the helper's behaviour against
a fake ``claude`` that reproduces both the loaded and the not-loaded answers, and
that a hook-asserting scenario reads the debug log, not the transcript.

Nothing here starts Claude Code. The live runs are recorded in the bead.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.lint

REPO = pathlib.Path(__file__).resolve().parent.parent
CCV = REPO / "tests" / "cc-validation"
SCENARIOS = sorted((CCV / "scenarios").glob("[0-9]*.sh"))
RUNNER = CCV / "runner.sh"

#: The scenarios that install or depend on a plugin, found by the census in the
#: bead's commit (grep installed_plugins / enabledPlugins / plugin install /
#: marketplace over every scenario). The census count is the non-vacuity floor.
CENSUS = {"12", "13", "14", "18", "32"}
#: Scenarios in tests/cc-validation/scenarios when the census was taken.
SCENARIO_COUNT = 30

#: What marks a scenario as plugin-dependent: it installs one through the helper,
#: or carries any of the hand-written install's markers.
_DEPENDENT = re.compile(
    r"cc_plugin_(install|require)|installed_plugins\.json|enabledPlugins|installPath"
    r"|extraKnownMarketplaces|claude plugin |--plugin-dir"
)


def _code(path: pathlib.Path) -> str:
    """The file without comment-only lines."""
    return "\n".join(
        ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")
    )


def _num(path: pathlib.Path) -> str:
    return path.name.split("_", 1)[0].lstrip("0") or "0"


def _bash_function(path: pathlib.Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"{name}() not found in {path}"
    return m.group(0)


def _dependents() -> dict[str, pathlib.Path]:
    return {_num(p): p for p in SCENARIOS if _DEPENDENT.search(_code(p))}


# --- the census ---------------------------------------------------------------


def test_the_scenario_set_is_the_one_the_census_examined() -> None:
    assert len(SCENARIOS) == SCENARIO_COUNT, (
        f"{len(SCENARIOS)} scenarios, the census examined {SCENARIO_COUNT}: a new scenario must be "
        "classified (plugin-dependent or not) and CENSUS/SCENARIO_COUNT updated"
    )


def test_the_plugin_dependent_scenarios_are_the_census() -> None:
    found = set(_dependents())
    assert found == CENSUS, f"plugin-dependent scenarios {sorted(found)} != census {sorted(CENSUS)}"
    assert len(found) >= 5, "non-vacuity floor: the census found fewer than five plugin-dependent scenarios"


# --- no scenario hand-writes an install ---------------------------------------


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.name)
def test_no_scenario_hand_writes_installed_plugins_or_enablement(path: pathlib.Path) -> None:
    code = _code(path)
    writes = [
        ln for ln in code.splitlines()
        if re.search(r">{1,2}\s*\"?[^\s\"]*installed_plugins\.json", ln) or "tee " in ln and "installed_plugins" in ln
    ]
    assert not writes, f"{path.name} writes installed_plugins.json: {writes}"
    assert '"installPath"' not in code, f"{path.name} hand-writes a plugin install entry"
    assert "enabledPlugins" not in code, (
        f"{path.name} hand-writes enabledPlugins; `claude plugin install` writes it"
    )


def test_the_runner_writes_only_the_empty_registry() -> None:
    fn = _bash_function(RUNNER, "_cc_plugins_baseline")
    assert '{"version": 2, "plugins": {}}' in fn
    code = _code(RUNNER)
    hits = [ln for ln in code.splitlines() if re.search(r">\s*\"?[^\s\"]*installed_plugins\.json", ln)]
    assert len(hits) == 1, f"the runner must write installed_plugins.json in exactly one place: {hits}"


# --- every dependent scenario goes through the shared helper ------------------


def test_every_plugin_dependent_scenario_calls_the_shared_helper() -> None:
    deps = _dependents()
    assert len(deps) >= 5
    for num, path in deps.items():
        assert re.search(r"\bcc_plugin_(require|install)\b", _code(path)), (
            f"scenario {num} depends on a plugin but never calls cc_plugin_require/cc_plugin_install"
        )


@pytest.mark.parametrize("num", ["12", "13", "14", "18"])
def test_the_plain_scenarios_use_the_failing_precondition_and_tear_down(num: str) -> None:
    code = _code(_dependents()[num])
    assert re.search(r"if cc_plugin_require \S+ \S+ \S+; then", code), (
        f"scenario {num} must start its session only inside `if cc_plugin_require ...; then`"
    )
    assert "cc_plugin_uninstall_all" in code
    assert not re.search(r"^\s*skip\b.*(plugin (is )?not (loaded|installed)|did not load)", code, re.M), "an unloaded plugin must fail, not skip"


def test_scenario_32_installs_through_the_helper_and_fails_the_leg_when_unloaded() -> None:
    code = _code(CCV / "scenarios" / "32_pretooluse_no_blanket_allow.sh")
    assert "cc_plugin_install" in code
    assert not re.search(r"^\s*skip\b", code, re.M)
    assert "claude plugin marketplace add" not in code, "scenario 32 must not carry its own copy of the install"


def test_the_helper_installs_through_a_marketplace_and_checks_the_list() -> None:
    fn = _bash_function(RUNNER, "cc_plugin_install")
    assert 'claude plugin marketplace add "$mkt"' in fn
    assert 'claude plugin install "$plugin@$mkt_name" --scope user' in fn
    assert 'grep -qF "Status: ✔ enabled"' in fn
    assert 'grep -qF "Version: $CC_PLUGIN_VERSION"' in fn
    assert "failed to load" in fn


# --- a hook-asserting scenario reads the debug log, not the transcript --------


@pytest.mark.parametrize("num,helper", [("12", "cc_mcp_hook_ran"), ("13", "cc_hook_ran"), ("18", "cc_hook_ran")])
def test_hook_assertions_use_the_debug_log(num: str, helper: str) -> None:
    code = _code(_dependents()[num])
    assert helper in code, f"scenario {num} asserts a hook ran without {helper}"
    assert ".jsonl" not in code and "claude/projects" not in code, (
        f"scenario {num} reads the session transcript, which omits a hook that printed nothing"
    )


def test_a_plugin_session_always_writes_a_debug_log() -> None:
    fn = _bash_function(RUNNER, "claude_start")
    assert '--debug-file ${CC_DEBUG_FILE}' in fn


# --- prompt-based scenarios pin their permission mode -------------------------


def test_every_scenario_pins_a_default_mode() -> None:
    missing = [p.name for p in SCENARIOS if '"defaultMode"' not in _code(p) and "--permission-mode" not in _code(p)]
    assert not missing, f"scenarios that leave the permission mode to Claude Code's default: {missing}"
    assert len(SCENARIOS) >= SCENARIO_COUNT


def test_scenario_32_runs_in_default_mode_not_auto() -> None:
    code = _code(CCV / "scenarios" / "32_pretooluse_no_blanket_allow.sh")
    assert 'S32_PERMISSION_MODE="${CC_VAL_S32_MODE:-default}"' in code
    assert '--permission-mode=${S32_PERMISSION_MODE}' in code


# --- the container carries what a loaded plugin needs -------------------------


def test_the_container_stages_sn_and_installs_the_cli_for_the_plugin_version() -> None:
    run = _code(CCV / "container" / "run.sh")
    assert re.search(r"tests/e2e/lib conexus sn\b", run), "scenario 18 installs the sn plugin"
    assert '--build-arg "NEXUS_CLI_VERSION=$NEXUS_CLI_VERSION"' in run
    docker = _code(CCV / "container" / "Dockerfile")
    assert "ARG NEXUS_CLI_VERSION" in docker
    assert 'uv tool install "conexus==${NEXUS_CLI_VERSION}"' in docker
    assert "mcp==" in docker, "the stub scenarios need the mcp package"


# --- the helper's behaviour, against a fake `claude` --------------------------

_FAKE_CLAUDE = r"""#!/usr/bin/env bash
# fake claude: just the three `plugin` subcommands the helper calls.
mode="${FAKE_CLAUDE_MODE:-ok}"
state="$HOME/.fake-claude"; mkdir -p "$state"
case "${2:-}" in
  marketplace) echo "$4" > "$state/mkt_dir"; echo "Added"; exit 0 ;;
  install)
    id="$3"; plugin="${id%@*}"; mkt="${id#*@}"; dir="$(cat "$state/mkt_dir")"
    [ "$mode" = noinstall ] && { echo "install failed" >&2; exit 1; }
    ver="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$dir/.claude-plugin/marketplace.json")"
    dest="$HOME/.claude/plugins/cache/$mkt/$plugin/$ver"; mkdir -p "$(dirname "$dest")"; cp -R "$dir/$plugin" "$dest"
    python3 - "$HOME/.claude/plugins/installed_plugins.json" "$id" "$dest" "$ver" <<'PY'
import json, sys
path, pid, dest, ver = sys.argv[1:]
json.dump({"version": 2, "plugins": {pid: [{"scope": "user", "installPath": dest, "version": ver}]}}, open(path, "w"))
PY
    echo "Installed $id"; exit 0 ;;
  list)
    id="$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"]),""))' "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null)"
    printf 'Installed plugins:\n\n'
    case "$mode" in
      unloaded) printf '  ❯ %s\n    Version: dev\n    Scope: user\n    Status: ✘ failed to load\n    Error: Marketplace nexus-plugins not found\n' "${id:-conexus@nexus-plugins}" ;;
      wrongversion) printf '  ❯ %s\n    Version: 0.0.0\n    Scope: user\n    Status: ✔ enabled\n' "$id" ;;
      disabled) printf '  ❯ %s\n    Version: %s\n    Scope: user\n    Status: ✘ disabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
      *) printf '  ❯ %s\n    Version: %s\n    Scope: user\n    Status: ✔ enabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
    esac
    exit 0 ;;
esac
exit 2
"""


def _helpers() -> str:
    names = ["_cc_plugins_baseline", "cc_plugin_install", "cc_plugin_require", "cc_keep_evidence",
             "cc_plugin_uninstall_all", "cc_hook_ran", "cc_mcp_hook_ran"]
    return "\n".join(_bash_function(RUNNER, n) for n in names)


def _plugin(root: pathlib.Path, name: str = "conexus", version: str = "7.82.0") -> pathlib.Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": version}))
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text("{}")
    return root


def _run(tmp_path: pathlib.Path, body: str, mode: str = "ok", **extra: str) -> subprocess.CompletedProcess[str]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    claude = fakebin / "claude"
    claude.write_text(_FAKE_CLAUDE)
    claude.chmod(0o755)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": f"{fakebin}:{os.environ['PATH']}", "HOME": str(tmp_path / "realhome"),
        "TEST_HOME": str(home), "FAKE_CLAUDE_MODE": mode, **extra,
    }
    prelude = (
        'set -u\nfail() { echo "FAILCALL:$*"; }\nskip() { echo "SKIPCALL:$*"; }\n_SCENARIO="t scenario"\n'
        + _helpers() + "\n"
    )
    return subprocess.run(["bash", "-c", prelude + body], capture_output=True, text=True, env=env)


def test_a_loaded_plugin_passes_the_precondition(tmp_path: pathlib.Path) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    r = _run(tmp_path, f'cc_plugin_install "{root}" conexus nexus-plugins; echo "rc=$? id=$CC_PLUGIN_ID v=$CC_PLUGIN_VERSION dbg=$CC_DEBUG_FILE"')
    assert "rc=0 id=conexus@nexus-plugins v=7.82.0" in r.stdout, r.stdout + r.stderr
    assert "cc-debug-conexus.log" in r.stdout
    assert (tmp_path / "home" / "plugin-list.txt").read_text().count("✔ enabled") == 1


def test_the_hand_written_registry_shape_is_refused(tmp_path: pathlib.Path) -> None:
    """The 2.1.302 answer for a plugin that was never installed through a
    marketplace: 'failed to load: Marketplace ... not found'. The precondition
    must FAIL (a fail() call, never skip()) and return non-zero."""
    root = _plugin(tmp_path / "src" / "conexus")
    r = _run(tmp_path, f'cc_plugin_require "{root}" conexus nexus-plugins; echo "rc=$?"', mode="unloaded")
    assert "rc=1" in r.stdout, r.stdout + r.stderr
    assert "FAILCALL:t scenario: plugin conexus@nexus-plugins is NOT loaded" in r.stdout
    assert "SKIPCALL" not in r.stdout
    assert "Marketplace nexus-plugins not found" in r.stderr, "the list output must reach the log"


@pytest.mark.parametrize("mode", ["unloaded", "wrongversion", "disabled", "noinstall"])
def test_every_not_loaded_answer_fails_the_install(tmp_path: pathlib.Path, mode: str) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    r = _run(tmp_path, f'cc_plugin_install "{root}" conexus nexus-plugins; echo "rc=$? id=[$CC_PLUGIN_ID] dbg=[$CC_DEBUG_FILE]"', mode=mode)
    assert "rc=1 id=[] dbg=[]" in r.stdout, f"{mode}: {r.stdout}{r.stderr}"


def test_a_plugin_without_a_version_fails(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "src" / "conexus"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}")
    r = _run(tmp_path, f'cc_plugin_install "{root}" conexus nexus-plugins; echo "rc=$?"')
    assert "rc=1" in r.stdout


def test_install_replaces_prior_plugin_state_and_teardown_empties_it(tmp_path: pathlib.Path) -> None:
    a = _plugin(tmp_path / "src" / "a", name="conexus", version="1.0.0")
    b = _plugin(tmp_path / "src" / "b", name="sn", version="2.0.0")
    body = (
        f'cc_plugin_install "{a}" conexus nexus-plugins || echo A_FAILED\n'
        f'cc_plugin_install "{b}" sn nexus-plugins || echo B_FAILED\n'
        'python3 -c \'import json,os;print(sorted(json.load(open(os.environ["TEST_HOME"]+"/.claude/plugins/installed_plugins.json"))["plugins"]))\'\n'
        'cc_plugin_uninstall_all\n'
        'python3 -c \'import json,os;print(json.load(open(os.environ["TEST_HOME"]+"/.claude/plugins/installed_plugins.json")))\'\n'
        'echo "after: id=[$CC_PLUGIN_ID] dbg=[$CC_DEBUG_FILE]"'
    )
    r = _run(tmp_path, body)
    assert "A_FAILED" not in r.stdout and "B_FAILED" not in r.stdout, r.stdout + r.stderr
    assert "['sn@nexus-plugins']" in r.stdout
    assert "{'version': 2, 'plugins': {}}" in r.stdout
    assert "after: id=[] dbg=[]" in r.stdout


def test_the_prepare_callback_runs_on_the_copy_before_the_install(tmp_path: pathlib.Path) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    body = (
        'mark() { echo prepared > "$1/hooks/PREPARED"; }\n'
        f'cc_plugin_install "{root}" conexus nexus-plugins mark; echo "rc=$?"\n'
        'ls "$CC_PLUGIN_INSTALLED_ROOT/hooks"'
    )
    r = _run(tmp_path, body)
    assert "rc=0" in r.stdout and "PREPARED" in r.stdout, r.stdout + r.stderr
    assert not (root / "hooks" / "PREPARED").exists(), "the source tree must stay untouched"


def test_evidence_is_kept_when_a_directory_is_mounted(tmp_path: pathlib.Path) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    ev = tmp_path / "evidence"
    body = f'cc_plugin_install "{root}" conexus nexus-plugins; echo x > "$CC_DEBUG_FILE"; cc_plugin_uninstall_all'
    r = _run(tmp_path, body, CC_VAL_EVIDENCE_DIR=str(ev))
    assert r.returncode == 0, r.stderr
    kept = {p.name for p in (ev / "t").iterdir()}
    assert {"plugin-list.txt", "plugin-install.log", "cc-debug-conexus.log", "installed_plugins.json"} <= kept


# --- the debug-log matchers ---------------------------------------------------

_LOG_OK = (
    "2026-10-17T21:31:37.343Z [DEBUG] Hooks: SubagentStart:general-purpose [uv tool run python /x/hooks/scripts/subagent_start.py]"
    " (plugin sn@nexus-plugins) finished with status 0\n"
)


def _hook_ran(tmp_path: pathlib.Path, log: str | None, call: str) -> bool:
    dbg = tmp_path / "dbg.log"
    dbg.unlink(missing_ok=True)
    if log is not None:
        dbg.write_text(log)
    r = _run(tmp_path, f'CC_DEBUG_FILE="{dbg}"; {call} && echo YES || echo NO')
    assert r.stdout.strip() in {"YES", "NO"}, r.stdout + r.stderr
    return r.stdout.strip() == "YES"


def test_cc_hook_ran_reads_the_debug_log(tmp_path: pathlib.Path) -> None:
    assert _hook_ran(tmp_path, _LOG_OK, 'cc_hook_ran "SubagentStart" "sn@nexus-plugins"')
    assert _hook_ran(tmp_path, _LOG_OK, 'cc_hook_ran "SubagentStart" "sn@nexus-plugins" "subagent_start.py"')


@pytest.mark.parametrize(
    "log,call",
    [
        (_LOG_OK, 'cc_hook_ran "SubagentStart" "conexus@nexus-plugins"'),
        (_LOG_OK, 'cc_hook_ran "PreToolUse" "sn@nexus-plugins"'),
        (_LOG_OK, 'cc_hook_ran "SubagentStart" "sn@nexus-plugins" "other_script.py"'),
        (_LOG_OK.replace("status 0", "status 1"), 'cc_hook_ran "SubagentStart" "sn@nexus-plugins"'),
        ("", 'cc_hook_ran "SubagentStart" "sn@nexus-plugins"'),
        (None, 'cc_hook_ran "SubagentStart" "sn@nexus-plugins"'),
    ],
)
def test_cc_hook_ran_does_not_match_what_did_not_run(tmp_path: pathlib.Path, log: str | None, call: str) -> None:
    assert not _hook_ran(tmp_path, log, call)


_MCP_CALL = "2026-10-17T21:30:28.203Z [DEBUG] Hooks: mcp_tool calling plugin:conexus:nexus/hook_subagent_start with 4 arg(s)\n"
_MCP_ERR = "2026-10-17T21:21:26.714Z [ERROR] Hooks: mcp_tool hook error: Unknown tool: 'hook_subagent_start'. Nexus is not installed.\n"


def test_cc_mcp_hook_ran_needs_the_call_and_no_error(tmp_path: pathlib.Path) -> None:
    assert _hook_ran(tmp_path, _MCP_CALL, 'cc_mcp_hook_ran "hook_subagent_start"')
    assert not _hook_ran(tmp_path, _MCP_CALL + _MCP_ERR, 'cc_mcp_hook_ran "hook_subagent_start"')
    assert not _hook_ran(tmp_path, _MCP_ERR, 'cc_mcp_hook_ran "hook_subagent_start"')
    assert not _hook_ran(tmp_path, _MCP_CALL, 'cc_mcp_hook_ran "hook_other"')
    assert not _hook_ran(tmp_path, None, 'cc_mcp_hook_ran "hook_subagent_start"')
