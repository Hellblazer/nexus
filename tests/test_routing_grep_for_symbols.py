# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 Phase 2 hook 1: grep_for_symbols_redirects_to_serena.

Detects identifier-shaped patterns in grep / rg calls against code
files and denies with a redirect to Serena's symbol-navigation MCP
tools (find_symbol, find_referencing_symbols).

Matcher shapes (allowed identifier patterns):
- Single identifier:       MyClass
- Dotted-id chain:         Module.Class.method
- Pipe-alternation of ids: MyClass|YourClass

Disqualifiers (allow through):
- All-uppercase short tokens: TODO, FIXME, XXX, HACK
- Whitespace inside the pattern (text search, not symbol)
- Regex metachars elsewhere: *, +, ?, brackets, anchors, etc.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent

# RDR-125: the grep_for_symbols hook moved from nx to sn (the plugin
# that ships Serena, the redirect target). The nx-side path must no
# longer exist; see test_hook_not_in_nx below.
HOOK_SCRIPT = (
    PROJECT_ROOT
    / "sn"
    / "hooks"
    / "scripts"
    / "routing"
    / "grep_for_symbols_redirects_to_serena.py"
)

_LEGACY_NX_PATH = (
    PROJECT_ROOT
    / "nx"
    / "hooks"
    / "scripts"
    / "routing"
    / "grep_for_symbols_redirects_to_serena.py"
)


def _run(payload: dict, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=10, env=env,
    )


def _decision(proc) -> dict:
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))


def test_script_exists():
    assert HOOK_SCRIPT.exists()


def test_hook_not_in_nx():
    """RDR-125: the hook lives in sn now; the nx-side copy must be gone."""
    assert not _LEGACY_NX_PATH.exists(), (
        f"Hook still present at legacy nx path {_LEGACY_NX_PATH}. "
        "RDR-125 migrated this rule to sn; the nx copy must be deleted."
    )


# ---------------------------------------------------------------------------
# Positive: identifier-shaped patterns on code files -> deny
# ---------------------------------------------------------------------------


def test_grep_single_identifier_on_py_file_denies():
    proc = _run(_bash("grep MyClass src/foo.py"))
    d = _decision(proc)
    assert d["permissionDecision"] == "deny"
    assert "jet_brains_find_symbol" in d["reason"] or "Serena" in d["reason"]


def test_rg_single_identifier_on_ts_file_denies():
    proc = _run(_bash("rg MyHandler src/handler.ts"))
    assert _decision(proc)["permissionDecision"] == "deny"


def test_grep_dotted_identifier_chain_denies():
    proc = _run(_bash("grep Module.Class.method src/lib.py"))
    assert _decision(proc)["permissionDecision"] == "deny"


def test_grep_pipe_alternation_of_identifiers_denies():
    proc = _run(_bash("grep -E 'MyClass|YourClass' src/foo.py"))
    assert _decision(proc)["permissionDecision"] == "deny"


def test_rg_swift_file_denies():
    proc = _run(_bash("rg AppDelegate Sources/App/Main.swift"))
    assert _decision(proc)["permissionDecision"] == "deny"


def test_rg_java_file_denies():
    proc = _run(_bash("rg HashMap src/main/java/Foo.java"))
    assert _decision(proc)["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Negative: non-symbol searches and non-code files allow through
# ---------------------------------------------------------------------------


def test_grep_text_phrase_with_spaces_allows():
    """Text search with spaces is not a symbol; allow."""
    proc = _run(_bash("grep 'hello world' src/foo.py"))
    assert _decision(proc)["permissionDecision"] == "allow"


def test_grep_with_regex_metachars_allows():
    """Pattern containing regex metachars is text-search, not a symbol."""
    proc = _run(_bash("grep 'foo.*bar' src/foo.py"))
    assert _decision(proc)["permissionDecision"] == "allow"


def test_grep_todo_allows():
    """TODO/FIXME/XXX/HACK are not symbols."""
    proc = _run(_bash("grep TODO src/foo.py"))
    assert _decision(proc)["permissionDecision"] == "allow"


def test_grep_fixme_allows():
    proc = _run(_bash("rg FIXME src/foo.py"))
    assert _decision(proc)["permissionDecision"] == "allow"


def test_grep_on_markdown_allows():
    """Non-code file: text search, not a symbol lookup."""
    proc = _run(_bash("grep Handler README.md"))
    assert _decision(proc)["permissionDecision"] == "allow"


def test_grep_on_yaml_allows():
    proc = _run(_bash("grep MyClass config.yaml"))
    assert _decision(proc)["permissionDecision"] == "allow"


def test_grep_without_code_file_extension_allows():
    """No file argument or only non-code files: allow."""
    proc = _run(_bash("grep MyClass"))
    assert _decision(proc)["permissionDecision"] == "allow"


def test_non_grep_bash_allows():
    proc = _run(_bash("ls src/"))
    assert _decision(proc)["permissionDecision"] == "allow"


def test_non_bash_tool_allows():
    proc = _run({"tool_name": "Edit", "tool_input": {"file_path": "x"}})
    assert _decision(proc)["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# Escape token
# ---------------------------------------------------------------------------


def test_escape_token_allows():
    proc = _run(_bash(
        "grep MyClass src/foo.py  # routing-allow: searching test fixture refs"
    ))
    assert _decision(proc)["permissionDecision"] == "allow"


def test_escape_token_too_short_still_denies():
    proc = _run(_bash("grep MyClass src/foo.py  # routing-allow: x"))
    assert _decision(proc)["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


def test_empty_stdin_allows():
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="", capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    d = json.loads(proc.stdout)["hookSpecificOutput"]
    assert d["permissionDecision"] == "allow"


def test_non_json_stdin_allows():
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="{not json", capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Registry + hooks.json wiring
# ---------------------------------------------------------------------------


def test_registry_has_rule():
    """RDR-125: rule lives in sn's registry, not nx's."""
    yaml = pytest.importorskip("yaml")
    sn_reg = PROJECT_ROOT / "sn" / "hooks" / "scripts" / "routing" / "registry.yaml"
    parsed = yaml.safe_load(sn_reg.read_text()) or {}
    rule = (parsed.get("rules") or {}).get("grep_for_symbols_redirects_to_serena")
    assert rule is not None, "Expected the rule in sn's registry.yaml after RDR-125 migration"

    # And it must NOT be in nx's registry anymore.
    nx_reg = PROJECT_ROOT / "nx" / "hooks" / "scripts" / "routing" / "registry.yaml"
    nx_parsed = yaml.safe_load(nx_reg.read_text()) or {}
    assert "grep_for_symbols_redirects_to_serena" not in (nx_parsed.get("rules") or {}), (
        "Rule still present in conexus/hooks/scripts/routing/registry.yaml; "
        "RDR-125 migrated it to sn — nx entry must be removed."
    )


def test_hooks_json_registers():
    """RDR-125: PreToolUse:Bash hook is registered in sn, not nx."""
    sn_hooks_json = PROJECT_ROOT / "sn" / "hooks" / "hooks.json"
    sn_data = json.loads(sn_hooks_json.read_text())
    sn_bash_hooks = sn_data["hooks"].get("PreToolUse", [])
    sn_found = any(
        "grep_for_symbols_redirects_to_serena.py" in h.get("command", "")
        for entry in sn_bash_hooks if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    )
    assert sn_found, "Expected sn/hooks/hooks.json PreToolUse:Bash to register the script"

    nx_hooks_json = PROJECT_ROOT / "nx" / "hooks" / "hooks.json"
    nx_data = json.loads(nx_hooks_json.read_text())
    nx_bash_hooks = nx_data["hooks"].get("PreToolUse", [])
    nx_found = any(
        "grep_for_symbols_redirects_to_serena.py" in h.get("command", "")
        for entry in nx_bash_hooks if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    )
    assert not nx_found, (
        "Hook still registered in conexus/hooks/hooks.json after RDR-125 migration; "
        "nx PreToolUse:Bash entry must be removed."
    )
