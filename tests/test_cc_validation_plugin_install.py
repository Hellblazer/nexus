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
    assert "grep -qxF \"$CC_PLUGIN_VERSION\"" in fn, "the Version: value must match exactly, not as a substring"
    assert 'grep -qF "Version:' not in fn
    assert "failed to load" in fn


# --- a hook-asserting scenario reads the debug log, not the transcript --------


@pytest.mark.parametrize("num,helper", [("12", "cc_mcp_hook_ran"), ("13", "cc_hook_ran"), ("18", "cc_hook_ran")])
def test_hook_assertions_use_the_debug_log(num: str, helper: str) -> None:
    code = _code(_dependents()[num])
    assert helper in code, f"scenario {num} asserts a hook ran without {helper}"
    assert ".jsonl" not in code and "claude/projects" not in code, (
        f"scenario {num} reads the session transcript, which omits a hook that printed nothing"
    )


def test_scenario_12_names_the_plugin_in_its_mcp_hook_assertion() -> None:
    assert 'cc_mcp_hook_ran "$CC_PLUGIN_ID" "hook_subagent_start"' in _code(_dependents()["12"])


def test_scenario_18_also_needs_the_context_to_have_been_delivered() -> None:
    code = _code(_dependents()["18"])
    assert 'cc_hook_context_injected "SubagentStart" "subagent_start.py"' in code


def test_scenario_12_judges_the_reply_against_the_id_the_hook_emitted() -> None:
    """A fixed probe phrase is satisfiable from a prompt; a per-dispatch id the
    debug log recorded is not. The verdict must compare the two."""
    code = _code(_dependents()["12"])
    assert "Claimant id: [0-9a-f]" in code and "CLAIM-12K7-$id" in code
    assert "check before duplicating" not in code
    # the reply is the subagent's own, never the main agent's paraphrase on the pane
    assert 'REPLIES12="$(cc_subagent_replies || true)"' in code
    assert "cc_wait_subagent_reply" in code
    assert 'capture -500 | grep' not in code and 'PANE12' not in code


def _s12_verdict(tmp_path: pathlib.Path, debug_log: str, replies: str, hook_ok: bool) -> str:
    """Run scenario 12's own verdict block under the runner's errexit/pipefail with
    stubbed helpers; return the one pass/fail line it printed."""
    text = (CCV / "scenarios" / "12_real_nx_subagent.sh").read_text(encoding="utf-8")
    start = text.index("    # The ids the hook emitted")
    end = text.index("    claude_exit")
    dbg = tmp_path / "dbg12.log"
    dbg.write_text(debug_log)
    script = (
        "set -euo pipefail\n"
        'pass() { echo "PASS:$*"; }\nfail() { echo "FAIL:$*"; }\ncapture() { echo PANE; }\n'
        f"cc_subagent_replies() {{ printf '%s\\n' {replies!r}; }}\n"
        f"cc_mcp_hook_ran() {{ {'true' if hook_ok else 'false'}; }}\n"
        f'CC_DEBUG_FILE="{dbg}"\nCC_PLUGIN_ID=conexus@nexus-plugins\n' + text[start:end] + '\necho REACHED_END\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "REACHED_END" in r.stdout, f"the verdict block died before its end: {r.stdout}{r.stderr}"
    return next(ln for ln in r.stdout.splitlines() if ln.startswith(("PASS:", "FAIL:")))


_S12_LOG = "[DEBUG] Hook SubagentStart: Claimant id: 0123456789abcdef \\u2014 mailbox\n"


def test_scenario_12_verdicts(tmp_path: pathlib.Path) -> None:
    good = "CLAIM-12K7-0123456789abcdef"
    assert _s12_verdict(tmp_path, _S12_LOG, good, True).startswith("PASS:")
    # right id, but no clean plugin hook call in the debug log
    assert _s12_verdict(tmp_path, _S12_LOG, good, False).startswith("FAIL:")
    # the subagent says absent
    assert "not injecting" in _s12_verdict(tmp_path, _S12_LOG, "ABSENT-12K7", True)
    # a claim of an id the hook never emitted (a guess or a leak from elsewhere)
    assert "not one the hook emitted" in _s12_verdict(tmp_path, _S12_LOG, "CLAIM-12K7-ffffffffffffffff", True)
    # the hook emitted nothing, so the debug log holds no id (errexit must not end the block)
    assert "not injecting" in _s12_verdict(tmp_path, "[DEBUG] nothing\n", "ABSENT-12K7", True)
    assert "not one the hook emitted" in _s12_verdict(tmp_path, "[DEBUG] nothing\n", good, True)
    # no reply at all
    assert "indeterminate" in _s12_verdict(tmp_path, _S12_LOG, "", True)


def test_scenario_18_reads_the_subagents_own_reply() -> None:
    code = _code(_dependents()["18"])
    assert "OUT=$(cc_subagent_replies)" in code and "cc_wait_subagent_reply" in code
    assert "OUT=$(capture" not in code


def test_a_plugin_session_always_writes_a_debug_log() -> None:
    fn = _bash_function(RUNNER, "claude_start")
    assert '--debug-file ${CC_DEBUG_FILE}' in fn


# --- prompt-based scenarios pin their permission mode -------------------------


def test_every_scenario_pins_a_default_mode() -> None:
    missing = [p.name for p in SCENARIOS if '"defaultMode"' not in _code(p) and "--permission-mode" not in _code(p)]
    assert not missing, f"scenarios that leave the permission mode to Claude Code's default: {missing}"
    assert len(SCENARIOS) >= SCENARIO_COUNT


#: Settings modes under which a permission prompt (or its absence) is part of
#: what a scenario measures. acceptEdits/bypassPermissions only keep a session
#: from stalling and mean the same thing with the skip flag.
_PERMISSION_SENSITIVE = re.compile(r'"defaultMode":\s*"(default|auto|plan)"')
#: lib.sh's claude_start, which always launches --dangerously-skip-permissions
#: (not claude_start_auto / claude_start_s32, which pick their own mode).
_LIB_START = re.compile(r"(?<![\w])claude_start(?![\w])")
#: Scenarios that pin a permission-sensitive mode and still launch through the
#: skip-permissions claude_start: known, tracked, each carrying the reason in its
#: own header. A NEW scenario doing this fails the test below.
KNOWN_SKIP_PERMISSIONS = {
    "5": "nexus-qsmc5: pins defaultMode default but launches --dangerously-skip-permissions",
    "6": "nexus-qsmc5: pins defaultMode default but launches --dangerously-skip-permissions",
    "7": "nexus-qsmc5: pins defaultMode default but launches --dangerously-skip-permissions",
}


def _relies_on_mode_under_skip_flag(path: pathlib.Path) -> bool:
    code = _code(path)
    return bool(_PERMISSION_SENSITIVE.search(code)) and bool(_LIB_START.search(code))


def test_no_scenario_relies_on_a_permission_mode_the_launch_flag_overrides() -> None:
    assert "--dangerously-skip-permissions" in (REPO / "tests" / "e2e" / "lib.sh").read_text(encoding="utf-8"), (
        "lib.sh claude_start no longer launches with the skip flag: this census has lost its premise"
    )
    found = {_num(p) for p in SCENARIOS if _relies_on_mode_under_skip_flag(p)}
    assert found == set(KNOWN_SKIP_PERMISSIONS), (
        f"scenarios that pin a permission-sensitive defaultMode but launch with --dangerously-skip-permissions: "
        f"{sorted(found)}; known: {sorted(KNOWN_SKIP_PERMISSIONS)}. Launch without the flag, or list the new one "
        "with a reason tied to nexus-qsmc5"
    )
    assert found, "non-vacuity: the census found no such scenario, so it examined nothing"


@pytest.mark.parametrize("num", sorted(KNOWN_SKIP_PERMISSIONS))
def test_each_known_exception_says_so_in_its_own_header(num: str) -> None:
    path = next(p for p in SCENARIOS if _num(p) == num)
    head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:8])
    assert "nexus-qsmc5" in head and "--dangerously-skip-permissions" in head


def test_the_census_regexes_see_the_shapes_they_are_for() -> None:
    assert _PERMISSION_SENSITIVE.search('"permissions": { "defaultMode": "default" }')
    assert _PERMISSION_SENSITIVE.search('"defaultMode": "auto"')
    assert not _PERMISSION_SENSITIVE.search('"defaultMode": "acceptEdits"')
    assert _LIB_START.search("claude_start")
    assert not _LIB_START.search("claude_start_auto") and not _LIB_START.search("claude_start_s32")


def test_scenario_32_runs_in_default_mode_not_auto() -> None:
    code = _code(CCV / "scenarios" / "32_pretooluse_no_blanket_allow.sh")
    assert 'S32_PERMISSION_MODE="${CC_VAL_S32_MODE:-default}"' in code
    assert '--permission-mode=${S32_PERMISSION_MODE}' in code


# --- the container carries what a loaded plugin needs -------------------------


def test_the_container_stages_sn_and_builds_the_cli_from_the_staged_tree() -> None:
    run = _code(CCV / "container" / "run.sh")
    assert re.search(r"tests/e2e/lib conexus sn\b", run), "scenario 18 installs the sn plugin"
    # The CLI comes from the same tree as the plugins under test.
    assert re.search(r"git ls-files .* src .*pyproject\.toml", run), "run.sh must stage the CLI source (src/, pyproject.toml)"
    assert 'cli-src' in run and 'docker build -q -t "$IMAGE" "$CTX"' in run
    assert "NEXUS_CLI_VERSION" not in run and "--build-arg" not in run, "no version-keyed PyPI install"
    docker = _code(CCV / "container" / "Dockerfile")
    assert "COPY cli-src /tmp/cli-src" in docker
    assert "uv tool install /tmp/cli-src" in docker
    assert "conexus==" not in docker and "NEXUS_CLI_VERSION" not in docker, "the CLI must not be fetched from PyPI by version"
    assert "test -x /usr/local/bin/nx-mcp" in docker
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
      suffixversion) printf '  ❯ %s\n    Version: %s1\n    Scope: user\n    Status: ✔ enabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
      prefixversion) printf '  ❯ %s\n    Version: 1%s\n    Scope: user\n    Status: ✔ enabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
      loaderror) printf '  ❯ %s\n    Version: %s\n    Scope: user\n    Status: ✔ enabled\n    Error: hooks/hooks.json failed to load\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
      disabled) printf '  ❯ %s\n    Version: %s\n    Scope: user\n    Status: ✘ disabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
      *) printf '  ❯ %s\n    Version: %s\n    Scope: user\n    Status: ✔ enabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
    esac
    exit 0 ;;
esac
exit 2
"""


def _helpers() -> str:
    names = ["_cc_plugins_baseline", "cc_plugin_install", "cc_plugin_require", "_cc_evidence_copy",
             "cc_keep_evidence", "cc_plugin_uninstall_all", "cc_hook_ran", "cc_mcp_hook_ran",
             "cc_hook_context_injected", "cc_subagent_replies", "cc_wait_subagent_reply"]
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


@pytest.mark.parametrize(
    "mode", ["unloaded", "wrongversion", "suffixversion", "prefixversion", "loaderror", "disabled", "noinstall"]
)
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
    ok = 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_subagent_start"'
    assert _hook_ran(tmp_path, _MCP_CALL, ok)
    assert not _hook_ran(tmp_path, _MCP_CALL + _MCP_ERR, ok)
    assert not _hook_ran(tmp_path, _MCP_ERR, ok)
    assert not _hook_ran(tmp_path, _MCP_CALL, 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_other"')
    assert not _hook_ran(tmp_path, None, ok)


def test_cc_mcp_hook_ran_matches_only_the_named_plugins_call(tmp_path: pathlib.Path) -> None:
    """Another plugin's server exposing the same tool name must not count."""
    other = "2026-10-17T21:30:28.203Z [DEBUG] Hooks: mcp_tool calling plugin:sn:nexus/hook_subagent_start with 4 arg(s)\n"
    assert not _hook_ran(tmp_path, other, 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_subagent_start"')
    assert _hook_ran(tmp_path, other, 'cc_mcp_hook_ran "sn@nexus-plugins" "hook_subagent_start"')
    # the plugin name is matched whole, not as a prefix of a longer plugin name
    longer = "2026-10-17T21:30:28.203Z [DEBUG] Hooks: mcp_tool calling plugin:conexus-extra:nexus/hook_subagent_start with 1 arg(s)\n"
    assert not _hook_ran(tmp_path, longer, 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_subagent_start"')
    # both present: the named plugin's call is enough
    assert _hook_ran(tmp_path, other + _MCP_CALL, 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_subagent_start"')


_CTX_OK = (
    "2026-10-17T21:31:37.346Z [DEBUG] Hook SubagentStart (uv tool run --directory ${CLAUDE_PLUGIN_ROOT} --no-config "
    "--quiet --python >=3.12 python ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/subagent_start.py) provided additionalContext (3462 chars)\n"
)


def test_cc_hook_context_injected_needs_accepted_nonempty_context(tmp_path: pathlib.Path) -> None:
    call = 'cc_hook_context_injected "SubagentStart" "subagent_start.py"'
    assert _hook_ran(tmp_path, _CTX_OK, call)
    assert _hook_ran(
        tmp_path,
        "Hook SubagentStart (plugin:conexus:nexus/hook_subagent_start) provided additionalContext (4388 chars)\n",
        'cc_hook_context_injected "SubagentStart" "hook_subagent_start"',
    )
    assert not _hook_ran(tmp_path, _CTX_OK.replace("(3462 chars)", "(0 chars)"), call)
    assert not _hook_ran(tmp_path, _CTX_OK, 'cc_hook_context_injected "SubagentStart" "other_script.py"')
    assert not _hook_ran(tmp_path, _CTX_OK, 'cc_hook_context_injected "SessionStart" "subagent_start.py"')
    assert not _hook_ran(tmp_path, _LOG_OK, call), "a hook that finished but delivered nothing is not injection"
    assert not _hook_ran(tmp_path, None, call)


# --- the subagent's own reply ---------------------------------------------------


def _agent_transcripts(tmp_path: pathlib.Path) -> pathlib.Path:
    proj = tmp_path / "home" / ".claude" / "projects" / "-home-x" / "sess"
    (proj / "subagents").mkdir(parents=True)
    def line(role: str, blocks: list[dict]) -> str:
        return json.dumps({"type": role, "message": {"role": role, "content": blocks}}) + "\n"
    (proj / "subagents" / "agent-abc.jsonl").write_text(
        line("user", [{"type": "text", "text": "PROMPT-ECHO-12K7"}])
        + line("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "TOOLCALL-12K7"}}])
        + "not json\n"
        + line("assistant", [{"type": "text", "text": "CLAIM-12K7-0123456789abcdef"}])
    )
    # the main session's own transcript: the main agent's paraphrase, not evidence
    (proj.parent / "main.jsonl").write_text(line("assistant", [{"type": "text", "text": "MAIN-PARAPHRASE-12K7"}]))
    return proj


def test_cc_subagent_replies_returns_only_the_subagents_assistant_text(tmp_path: pathlib.Path) -> None:
    _agent_transcripts(tmp_path)
    r = _run(tmp_path, "cc_subagent_replies")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CLAIM-12K7-0123456789abcdef", r.stdout + r.stderr


def test_cc_wait_subagent_reply_finds_the_token_or_times_out(tmp_path: pathlib.Path) -> None:
    _agent_transcripts(tmp_path)
    r = _run(tmp_path, 'cc_wait_subagent_reply "CLAIM-12K7-[0-9a-f]{8,}" 2; echo "rc=$?"')
    assert "rc=0" in r.stdout, r.stdout + r.stderr
    r = _run(tmp_path, 'cc_wait_subagent_reply "MAIN-PARAPHRASE-12K7" 2; echo "rc=$?"')
    assert "rc=1" in r.stdout and "TIMEOUT" in r.stdout, "the main transcript must not count as the subagent's reply"
    r = _run(tmp_path, 'cc_wait_subagent_reply "PROMPT-ECHO-12K7|TOOLCALL-12K7" 2; echo "rc=$?"')
    assert "rc=1" in r.stdout, "a user turn or tool call is not a reply"


# --- evidence never carries a credential ---------------------------------------


def test_evidence_refuses_files_holding_a_credential_shaped_string(tmp_path: pathlib.Path) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    ev = tmp_path / "evidence"
    home = tmp_path / "home"
    body = (
        f'cc_plugin_install "{root}" conexus nexus-plugins || echo INSTALL_FAILED\n'
        'echo "debug line with sk-ant-oat01-FAKEFAKE inside" > "$CC_DEBUG_FILE"\n'
        'mkdir -p "$TEST_HOME/.claude/projects/p"\n'
        'echo "{\\"clean\\": true}" > "$TEST_HOME/.claude/projects/p/clean.jsonl"\n'
        'echo "{\\"env\\": \\"sk-ant-api03-FAKEFAKE\\"}" > "$TEST_HOME/.claude/projects/p/dirty.jsonl"\n'
        'cc_plugin_uninstall_all\n'
    )
    r = _run(tmp_path, body, CC_VAL_EVIDENCE_DIR=str(ev))
    assert "INSTALL_FAILED" not in r.stdout, r.stdout + r.stderr
    kept = {p.name for p in (ev / "t").iterdir()}
    assert "cc-debug-conexus.log" not in kept, "a debug log holding sk-ant- must not be copied"
    assert "dirty.jsonl" not in kept
    assert {"clean.jsonl", "plugin-list.txt", "plugin-install.log", "installed_plugins.json"} <= kept
    assert "refused to keep cc-debug-conexus.log" in r.stderr and "refused to keep dirty.jsonl" in r.stderr
    assert "FAKEFAKE" not in r.stderr and "FAKEFAKE" not in r.stdout, "the refusal must not echo the content"
    for f in (ev / "t").iterdir():
        assert "sk-ant-" not in f.read_text(), f"{f.name} carries a credential-shaped string"
    assert home.exists()


# --- scenario 14b needs 14a's positive control ----------------------------------


def _14b_verdict(tmp_path: pathlib.Path, ctrl: int, agent_ran: int, mcp_in_inv: int, log_called: int) -> str:
    """Run scenario 14's own 14b verdict block with the given measurements."""
    text = (CCV / "scenarios" / "14_inline_mcpservers_variations.sh").read_text(encoding="utf-8")
    start = text.index("if [[ $mcp_in_inv -eq 1 && $log_called -eq 1 ]]; then\n    pass \"14b:")
    end = text.index("\nfi\n", start) + len("\nfi\n")
    block = text[start:end]
    script = (
        'pass() { echo "PASS:$*"; }\nskip() { echo "SKIP:$*"; }\nfail() { echo "FAIL:$*"; }\n'
        f"CTRL_14A_LOADED={ctrl}\nagent_ran={agent_ran}\nmcp_in_inv={mcp_in_inv}\nlog_called={log_called}\n" + block
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().split(":", 1)[0]


def test_14b_does_not_load_verdict_is_a_pass_only_with_the_positive_control(tmp_path: pathlib.Path) -> None:
    assert _14b_verdict(tmp_path, ctrl=1, agent_ran=1, mcp_in_inv=0, log_called=0) == "PASS"
    assert _14b_verdict(tmp_path, ctrl=0, agent_ran=1, mcp_in_inv=0, log_called=0) == "SKIP"
    # the other readings do not depend on the control
    assert _14b_verdict(tmp_path, ctrl=0, agent_ran=1, mcp_in_inv=1, log_called=1) == "PASS"
    assert _14b_verdict(tmp_path, ctrl=0, agent_ran=1, mcp_in_inv=1, log_called=0) == "SKIP"
    assert _14b_verdict(tmp_path, ctrl=1, agent_ran=0, mcp_in_inv=0, log_called=0) == "SKIP"


def test_14a_is_the_only_thing_that_sets_the_control() -> None:
    code = _code(CCV / "scenarios" / "14_inline_mcpservers_variations.sh")
    assert code.count("CTRL_14A_LOADED=1") == 1
    assert "CTRL_14A_LOADED=0" in code
    assert code.index("CTRL_14A_LOADED=1") < code.index('scenario "14b ')


# --- no probe phrase is written whole in the prompt that asks for it ----------------

#: What each injection-probing scenario judges the reply on, besides the reply
#: tokens (found from the scenario's own poll_for/grep patterns). None may
#: appear whole in any claude_prompt of the scenario: the pane shows the prompt
#: and the subagent sees it, so a probe written into it is satisfiable without
#: any injection (nexus-spwp2 review).
PROBES = {
    "12": ["Claimant id:", "check before duplicating sibling work"],
    "13": ["UNIQUE-PROJECT-A-MARKER-KQX73", "UNIQUE-PLUGIN-B-MARKER-WYZ91", "UNIQUE-CONTROL-C-MARKER-RJF44"],
    "18": ["project-from-cwd", "fetch current docs"],
}
_PROMPT = re.compile(r'claude_prompt\s+"((?:[^"\\]|\\.)*)"')
_PATTERN_ARG = re.compile(r'(?:poll_for|grep\s+-[A-Za-z]*)\s+"([^"]+)"')


def _prompts(path: pathlib.Path) -> list[str]:
    return _PROMPT.findall(_code(path))


def _reply_tokens(path: pathlib.Path) -> set[str]:
    out: set[str] = set()
    for pat in _PATTERN_ARG.findall(_code(path)):
        for alt in pat.split("|"):
            if re.fullmatch(r"[A-Za-z0-9_-]{6,}", alt):
                out.add(alt)
    return out


def _whole_in_prompts(prompts: list[str], probes: list[str] | set[str]) -> list[tuple[str, str]]:
    squashed = [(p, " ".join(p.split())) for p in prompts]
    return [(probe, p[:60]) for probe in sorted(probes) for p, sq in squashed if probe in p or probe in sq]


@pytest.mark.parametrize("num", sorted(PROBES))
def test_no_prompt_contains_its_own_probe_phrase_whole(num: str) -> None:
    path = _dependents()[num]
    prompts = _prompts(path)
    tokens = _reply_tokens(path)
    assert len(prompts) >= 1, f"scenario {num}: found no claude_prompt, so the census examined nothing"
    assert len(tokens) >= 2, f"scenario {num}: found {sorted(tokens)} reply tokens, expected at least two"
    hits = _whole_in_prompts(prompts, set(PROBES[num]) | tokens)
    assert not hits, f"scenario {num} writes a probe whole in its own prompt: {hits}"


def test_the_prompt_census_counts() -> None:
    """Non-vacuity: how many prompts and tokens the three scenarios hand the lint."""
    total_prompts = sum(len(_prompts(_dependents()[n])) for n in PROBES)
    total_tokens = sum(len(_reply_tokens(_dependents()[n])) for n in PROBES)
    assert total_prompts >= 5, total_prompts   # 12:1, 13:3, 18:1
    assert total_tokens >= 10, total_tokens


def test_the_prompt_lint_catches_a_whole_probe() -> None:
    prompts = ['Does your context contain fetch  current docs? reply FOUND-13Z', 'ask SERENA-OK-9F2K']
    hits = _whole_in_prompts(prompts, ["fetch current docs", "SERENA-OK-9F2K", "NOT-THERE"])
    assert {h[0] for h in hits} == {"fetch current docs", "SERENA-OK-9F2K"}
    assert not _whole_in_prompts(["the words fetch current followed by one space and the word docs"], ["fetch current docs"])
