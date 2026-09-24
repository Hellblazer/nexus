# SPDX-License-Identifier: AGPL-3.0-or-later
"""The version-lockstep hook must run under ANY installed CLI, or none.

Lockstep is the one hook whose job is to repair plugin-ahead skew: a plugin
update lands before the CLI does, the hook notices, and it dispatches the
detached reinstall. So it cannot depend on the CLI it is there to replace.
nexus-t9klx moved it to ``nx-hook version-lockstep``; an older ``nx-hook``
exits 2 on a verb it does not know, and the repair path died with the skew
it exists to repair (caught by tests/e2e/plugin-lockstep-gate.sh during the
7.58.0 battery, restored before release).

Two properties, both about the shipped plugin tree:

* hooks.json runs it as a plugin-resident script through ``python3``, never
  as an ``nx-hook`` verb;
* the script, its action and its interpreter helper import only the
  standard library and each other at module scope, so an absent or older
  ``nexus`` cannot stop the hook from starting.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "conexus" / "hooks" / "scripts"
_HOOK = "version_lockstep_hook.py"
_CHAIN = (_HOOK, "version_lockstep_action.py", "_interpreter.py")
_PLUGIN_LOCAL = {"_interpreter"}


def _session_start_entries() -> list[dict]:
    hooks = json.loads((_ROOT / "conexus" / "hooks" / "hooks.json").read_text())
    return [
        entry
        for group in hooks["hooks"].get("SessionStart", [])
        for entry in group.get("hooks", [])
    ]


def test_lockstep_is_a_plugin_script_not_an_nx_hook_verb() -> None:
    entries = _session_start_entries()
    verbs = [e for e in entries if e.get("command") == "nx-hook" and (e.get("args") or [""])[0] == "version-lockstep"]
    assert not verbs, (
        "hooks.json runs version lockstep as `nx-hook version-lockstep`; an "
        "older nx-hook exits 2 on that verb, so plugin-ahead skew can never "
        "repair itself. Keep it a plugin-resident python3 script."
    )
    script = f"${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/{_HOOK}"
    wired = [e for e in entries if e.get("command") == "python3" and e.get("args") == [script]]
    assert len(wired) == 1, f"expected exactly one SessionStart entry running {script}; got {entries}"
    assert (_SCRIPTS / _HOOK).is_file()


def test_lockstep_chain_imports_only_stdlib_at_module_scope() -> None:
    allowed = set(sys.stdlib_module_names) | _PLUGIN_LOCAL | {"__future__"}
    for name in _CHAIN:
        tree = ast.parse((_SCRIPTS / name).read_text())
        found: set[str] = set()
        for node in tree.body:
            for sub in ast.walk(node) if isinstance(node, (ast.Try, ast.If)) else [node]:
                if isinstance(sub, ast.Import):
                    found |= {a.name.split(".")[0] for a in sub.names}
                elif isinstance(sub, ast.ImportFrom) and sub.level == 0 and sub.module:
                    found.add(sub.module.split(".")[0])
        assert found, f"{name}: no module-scope imports found; the scan examined nothing"
        assert found <= allowed, f"{name} imports {sorted(found - allowed)} at module scope"
