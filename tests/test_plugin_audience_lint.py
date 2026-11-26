# SPDX-License-Identifier: AGPL-3.0-or-later
"""The audience manifest lint (nexus-sby49.3, plan section 5 item 1; T9; reworked in nexus-sby49.4).

``conexus/hooks/audience.yaml`` declares who every hook registration, rule and injected constant is for: every user, or whoever
sets a named setting (``SETTING:<key>``), never a person. This lint is the
reason a new hook or a new line of injected text cannot reach every user without somebody deciding that: it fails on a
registration with no entry, an unknown value, a constant that does not exist or is not classified, a setting entry whose
module never reads the setting, a reader called outside a wrapped body, and an audience import whose fallback
widens a permission (T9). The checker is ``tests/_audience_manifest.py``; the behaviour leg uses it too.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import textwrap
from pathlib import Path

import pytest

from tests import _audience_manifest as am

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "conexus" / "hooks" / "audience.yaml"
HOOKS_JSON = ROOT / "conexus" / "hooks" / "hooks.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return am.load_manifest(MANIFEST)


@pytest.fixture(scope="module")
def hooks_json() -> dict:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def _problems(manifest: dict, hooks_json: dict) -> list[str]:
    return am.problems(manifest, hooks_json, ROOT)


# --- the real manifest ----------------------------------------------------------------------------


def test_the_manifest_is_true(manifest, hooks_json) -> None:
    found = _problems(manifest, hooks_json)
    assert found == [], "audience.yaml and the code disagree:\n  " + "\n  ".join(found)


def test_the_lint_examined_the_whole_plugin(manifest, hooks_json) -> None:
    """Non-vacuity: 23 registrations of 22 distinct argv, and every constant the census sees is classified."""
    regs = am.registrations(hooks_json)
    assert len(regs) == 23 and len(manifest["registrations"]) == 23
    assert len({tuple(r["argv"]) for r in regs}) == 22, "auto-approve is registered twice with one argv"
    assert len({r["key"] for r in regs}) == 23, "event plus argv is the key"
    injected = len(manifest["constants"])
    classified = sum(len(m.get("not_injected") or {}) for m in manifest["modules"].values())
    assert injected >= 45 and classified >= 20, (injected, classified)
    census_total = 0
    for path in manifest["modules"]:
        import ast

        census_total += len(am.census(ast.parse(am.module_file(ROOT, path).read_text(encoding="utf-8"))))
    declared = {c["id"] for c in manifest["constants"]}
    not_injected = {f"{p}:{n}" for p, m in manifest["modules"].items() for n in (m.get("not_injected") or {})}
    assert census_total >= 70, f"the census saw {census_total} constants: the lint is not looking at the hook modules"
    assert census_total <= len(declared | not_injected)


def test_every_registration_names_its_modules_audience_and_end_state(manifest) -> None:
    for r in manifest["registrations"]:
        assert r["modules"] and r["evidence"] and r["target_state"], r["key"]


def test_phase_3_left_no_row_that_is_not_live(manifest) -> None:
    """nexus-sby49.4: the long tail is wired, so no row is `live: false` and the behaviour leg's allowlist has no row it
    may name. A row added later as not live must still name the phase that wires it."""
    assert am.allowlist_rows(manifest) == []
    for kind in ("registrations", "constants", "rules"):
        for e in manifest[kind]:
            if not e.get("live", True):
                assert str(e["phase"]) in ("3", "after-2", "4"), e


def test_the_decisions_the_manifest_cites_are_answered(manifest) -> None:
    answered = set(manifest["decisions_answered"])
    for kind in ("registrations", "constants", "rules"):
        for e in manifest[kind]:
            if "decision" in e:
                assert e["decision"] in answered, e


def test_the_registry_and_the_advice_rows_carry_an_audience() -> None:
    import yaml

    registry = yaml.safe_load((ROOT / "conexus/hooks/scripts/routing/registry.yaml").read_text(encoding="utf-8"))
    for name, rule in registry["rules"].items():
        assert am.audience_kind(str(rule.get("audience"))) in am.AUDIENCES, name
    spec = importlib.util.spec_from_file_location("daa_lint", ROOT / "conexus/hooks/scripts/routing/destructive_action_advice.py")
    daa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daa)
    assert len(daa.ROWS) >= 5
    for row in daa.ROWS:
        assert am.audience_kind(row.audience) in am.AUDIENCES, row.id


def test_the_registry_audience_agrees_with_the_manifest(manifest) -> None:
    """``registry.yaml`` names each routing rule's audience for people reading the rule; the manifest is what the lint and the
    leg enforce. The two must say the same thing about the same hook."""
    import yaml

    registry = yaml.safe_load((ROOT / "conexus/hooks/scripts/routing/registry.yaml").read_text(encoding="utf-8"))
    by_script = {r["argv"][-1]: r for r in manifest["registrations"] if r["event"] == "PreToolUse"}
    seen = 0
    for name, rule in registry["rules"].items():
        reg = by_script.get(f"hooks/scripts/routing/{rule['hook_script']}")
        assert reg is not None, f"{name}: the manifest has no PreToolUse registration for {rule['hook_script']}"
        assert rule["audience"] == reg["audience"], f"{name}: registry.yaml says {rule['audience']}, audience.yaml says {reg['audience']}"
        seen += 1
    assert seen == 3, "the three routing rules"


def test_an_advice_row_may_name_a_setting_audience_only_in_a_module_that_reads_a_setting(manifest) -> None:
    """``Row.audience`` is read by nothing at run time (every row advises for everyone; a row blocks only through the
    ``advice_block`` setting), so a row that claimed a setting audience would look gated and be shown to every user. Every row
    is EVERY-USER."""
    spec = importlib.util.spec_from_file_location("daa_lint2", ROOT / "conexus/hooks/scripts/routing/destructive_action_advice.py")
    daa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daa)
    predicates = manifest["modules"]["hooks/scripts/routing/destructive_action_advice.py"].get("predicates") or []
    assert predicates == ["advice_blocked"]
    for row in daa.ROWS:
        assert row.audience == "EVERY-USER", f"advice row {row.id} claims {row.audience}"


# --- the census sees the value forms text is written in ------------------------------------------------

_LONG = "x" * 45


@pytest.mark.parametrize("source", [
    f'T = "{_LONG}"',
    f'T = "{"y" * 25}" + "{"z" * 25}"',
    f'T = f"{"a" * 30} {{1}} {"b" * 30}"',
    f'T = ["{_LONG}", "short"]',
    f'T = ("{_LONG}",)',
    f'T = {{"k": "{_LONG}"}}',
    f'T = Row("id", "{_LONG}")',
    f'T = Row("id", note="{_LONG}")',
    f'T = "{_LONG} {{}}".format(1)',
    f'T = "{_LONG}".strip()',
    f'T = "; ".join(["{_LONG}", "b"])',
    f'T = "{_LONG}" if FLAG else "short"',
    f'T = "short" if FLAG else "{_LONG}"',
    f'A, B = "short", "{_LONG}"',
    f'A, B = ("short", "{_LONG}")',
], ids=["literal", "concatenation", "f-string", "list", "tuple", "dict", "call-arg", "call-keyword", "format-receiver",
        "strip-receiver", "join-argument", "ifexp-body", "ifexp-orelse", "tuple-unpack", "tuple-unpack-parens"])
def test_the_census_counts_text_in_every_module_level_value_form(source: str) -> None:
    import ast

    assert am.census(ast.parse(source)), source


def test_a_tuple_unpack_names_only_the_target_that_holds_the_text() -> None:
    import ast

    found = am.census(ast.parse(f'A, B = "short", "{_LONG}"'))
    assert list(found) == ["B"], found


@pytest.mark.parametrize("source", [
    f'T = re.compile("{_LONG}")',
    f'T = frozenset(("{_LONG}",))',
    'T = "short"',
    'T = ("a", "b", "c")',
    f'def f():\n    return "{_LONG}"',
], ids=["re.compile", "frozenset", "short", "short-tuple", "inside-a-function"])
def test_the_census_leaves_matchers_short_text_and_function_text_alone(source: str) -> None:
    import ast

    assert am.census(ast.parse(source)) == {}


# --- the lint fails on what it is for ---------------------------------------------------------------


def _mutated(manifest: dict, fn) -> dict:
    m = copy.deepcopy(manifest)
    fn(m)
    return m


def test_a_registration_with_no_entry_fails(manifest, hooks_json) -> None:
    m = _mutated(manifest, lambda m: m["registrations"].pop(3))
    assert any("has no audience.yaml entry" in p for p in _problems(m, hooks_json))


def test_an_entry_for_a_registration_that_is_gone_fails(manifest, hooks_json) -> None:
    def add(m):
        extra = copy.deepcopy(m["registrations"][0])
        extra["key"] = "Stop hooks/scripts/removed.py"
        m["registrations"].append(extra)

    assert any("which hooks.json does not have" in p for p in _problems(_mutated(manifest, add), hooks_json))


def test_a_registration_whose_argv_drifted_fails(manifest, hooks_json) -> None:
    m = _mutated(manifest, lambda m: m["registrations"][0].__setitem__("matcher", "somethingelse"))
    assert any("differ from hooks.json" in p for p in _problems(m, hooks_json))


def test_the_same_argv_under_two_events_needs_two_entries(manifest, hooks_json) -> None:
    keys = [r["key"] for r in manifest["registrations"] if "auto-approve" in r["key"]]
    assert sorted(k.split(" ", 1)[0] for k in keys) == ["PermissionRequest", "PreToolUse"]


@pytest.mark.parametrize("field,value,needle", [
    ("audience", "EVERYONE", "unknown audience"),
    ("gate", "sometimes", "unknown gate"),
    ("phase", "9", "unknown phase"),
    ("evidence", "", "missing evidence"),
    ("target_state", None, "missing target_state"),
])
def test_an_unknown_or_missing_value_fails(manifest, hooks_json, field, value, needle) -> None:
    m = _mutated(manifest, lambda m: m["registrations"][0].__setitem__(field, value))
    assert any(needle in p for p in _problems(m, hooks_json)), field


def test_an_audience_that_disagrees_with_its_gate_fails(manifest, hooks_json) -> None:
    m = _mutated(manifest, lambda m: m["constants"][0].update({"audience": "SETTING:telemetry", "gate": "none"}))
    assert any("cannot carry gate" in p for p in _problems(m, hooks_json))
    m = _mutated(manifest, lambda m: m["constants"][0].update({"audience": "EVERY-USER", "gate": "setting"}))
    assert any("cannot carry gate" in p for p in _problems(m, hooks_json))


def test_a_setting_audience_must_name_a_setting_the_checker_knows(manifest, hooks_json) -> None:
    m = _mutated(manifest, lambda m: m["constants"][0].update({"audience": "SETTING:who_is_sam", "gate": "setting"}))
    assert any("names no setting the checker knows" in p for p in _problems(m, hooks_json))
    for retired in ("SAM-WIDE", "NEXUS-REPO"):
        m = _mutated(manifest, lambda m, a=retired: m["constants"][0].update({"audience": a, "gate": "none"}))
        assert any("unknown audience" in p for p in _problems(m, hooks_json)), retired


def test_a_decision_outside_d1_to_d13_fails(manifest, hooks_json) -> None:
    m = _mutated(manifest, lambda m: m["constants"][0].__setitem__("decision", "D14"))
    assert any("is not D1..D13" in p for p in _problems(m, hooks_json))


def test_a_constant_that_does_not_exist_fails(manifest, hooks_json) -> None:
    def add(m):
        e = copy.deepcopy(m["constants"][0])
        e["id"] = e["id"].rsplit(":", 1)[0] + ":_NO_SUCH_CONSTANT"
        m["constants"].append(e)

    assert any("does not exist" in p and "_NO_SUCH_CONSTANT" in p for p in _problems(_mutated(manifest, add), hooks_json))


def test_a_new_string_constant_nobody_classified_fails(manifest, hooks_json) -> None:
    """The census: drop a declared constant and the lint names it as unclassified."""
    def drop(m):
        m["constants"] = [c for c in m["constants"] if not c["id"].endswith(":_ORCH_GIT")]

    found = _problems(_mutated(manifest, drop), hooks_json)
    assert any("_ORCH_GIT" in p and "does not classify" in p for p in found), found


def test_a_live_setting_entry_in_a_module_that_never_reads_the_setting_fails(manifest, hooks_json) -> None:
    def gate_the_setup_notice(m):
        m["rules"].append({
            "id": "bogus", "module": "hooks/scripts/nx_setup_notice.py", "audience": "SETTING:ci_wait", "gate": "setting",
            "evidence": "x", "target_state": "x", "phase": "2",
        })

    found = _problems(_mutated(manifest, gate_the_setup_notice), hooks_json)
    assert any("never calls project_text()" in p for p in found), found


def test_a_setting_entry_that_is_not_live_may_have_no_read_yet(manifest, hooks_json) -> None:
    """A row whose setting is declared and not wired (`live: false`) need not be read by its module, where a live one must."""
    def add_unwired(m):
        m["rules"].append({
            "id": "unwired", "module": "hooks/scripts/nx_setup_notice.py", "audience": "SETTING:ci_wait", "gate": "setting",
            "evidence": "x", "target_state": "x", "phase": "3", "live": False,
        })

    assert _problems(_mutated(manifest, add_unwired), hooks_json) == []


def test_a_module_that_declares_the_wrong_predicates_fails(manifest, hooks_json) -> None:
    m = _mutated(manifest, lambda m: m["modules"]["nexus.hooks.subagent_start"].__setitem__("predicates", ["guard_mode"]))
    assert any("declares predicates" in p for p in _problems(m, hooks_json))


def test_a_module_with_text_and_no_entry_fails(manifest, hooks_json) -> None:
    m = _mutated(manifest, lambda m: m["modules"].pop("nexus.hooks.board_due"))
    found = _problems(m, hooks_json)
    assert any("nexus.hooks.board_due" in p and "not in the modules table" in p for p in found), found


# --- T9: the wrapper, the guarded import and the fallback -------------------------------------------------------

_GOOD_GUARD = textwrap.dedent('''
    import sys
    try:
        from _audience import guard_mode as _mode, telemetry_enabled as _telemetry, remote_test_hosts as _hosts, note_hook_error as _note, project_text as _text
    except BaseException:
        def _mode(payload):
            return "strict"

        def _telemetry():
            return False

        def _text(payload, key):
            return None

        def _hosts():
            return frozenset()

        def _note(payload, what):
            return None

    def body(payload):
        if _mode(payload) == "strict":
            return 1

    if __name__ == "__main__":
        _lib.run_hook(body)
''')

_PREDICATES = ["guard_mode", "note_hook_error", "project_text", "remote_test_hosts", "telemetry_enabled"]


def _check(source: str, *, predicates=None, wrapper="run_hook", **kw) -> list[str]:
    return am.module_problems("fixture.py", source, declared_predicates=_PREDICATES if predicates is None else predicates,
                              wrapper=wrapper, **kw)


def test_a_guard_shaped_module_passes() -> None:
    assert _check(_GOOD_GUARD) == []


def test_an_unguarded_import_fails() -> None:
    src = _GOOD_GUARD.replace("try:\n    from", "if True:\n    from").replace("except BaseException:", "else:")
    assert any("not inside a try" in p for p in _check(src))


def test_a_bare_module_import_fails() -> None:
    src = "import _audience\n\ndef body(payload):\n    return _audience.guard_mode(payload)\n\n_lib.run_hook(body)\n"
    assert any("imports the audience helper as a module" in p for p in _check(src, predicates=[]))


def test_a_tightening_reader_whose_fallback_loosens_fails() -> None:
    src = _GOOD_GUARD.replace('def _mode(payload):\n        return "strict"', 'def _mode(payload):\n        return "seatbelt"')
    assert any("the fallback for _mode returns" in p for p in _check(src))


def test_a_loosening_reader_whose_fallback_answers_true_fails() -> None:
    src = _GOOD_GUARD.replace("def _telemetry():\n        return False", "def _telemetry():\n        return True")
    assert any("the fallback for _telemetry returns 'True'" in p for p in _check(src))


def test_a_project_text_fallback_that_names_text_fails() -> None:
    src = _GOOD_GUARD.replace("def _text(payload, key):\n        return None", 'def _text(payload, key):\n        return "run this"')
    assert any("the fallback for _text returns" in p for p in _check(src))


def test_a_remote_hosts_fallback_that_names_a_host_fails() -> None:
    src = _GOOD_GUARD.replace("return frozenset()", "return frozenset({'chas'})")
    assert any("_hosts" in p for p in _check(src))


def test_an_import_with_no_fallback_defined_fails() -> None:
    src = _GOOD_GUARD.replace("    def _note(payload, what):\n        return None\n", "    pass\n")
    assert any("defines no fallback named _note" in p for p in _check(src))


def test_a_handler_that_catches_nothing_broad_fails() -> None:
    src = _GOOD_GUARD.replace("except BaseException:", "except KeyError:")
    assert any("no handler for Exception or BaseException" in p for p in _check(src))


def test_a_predicate_call_at_import_time_fails() -> None:
    src = _GOOD_GUARD + "\nSTARTUP = _mode({})\n"
    assert any("runs at import time" in p for p in _check(src))


def test_a_predicate_call_in_a_class_body_fails() -> None:
    src = _GOOD_GUARD + "\nclass K:\n    on = _mode({})\n"
    assert any("runs at import time" in p for p in _check(src))


def test_a_run_hook_module_with_no_run_hook_fails() -> None:
    src = _GOOD_GUARD.replace("_lib.run_hook(body)", "body({})")
    assert any("wrapper shape 'run_hook' is not present" in p for p in _check(src))


def test_a_never_fail_module_passes_with_never_fail_and_fails_without() -> None:
    src = _GOOD_GUARD.replace("_lib.run_hook(body)", "never_fail(lambda: body({}), 'x')")
    assert _check(src, wrapper="never_fail") == []
    assert any("never_fail" in p for p in _check(_GOOD_GUARD, wrapper="never_fail"))


def test_a_main_try_module_needs_a_main_with_a_try() -> None:
    ok = _GOOD_GUARD.replace('if __name__ == "__main__":\n    _lib.run_hook(body)', textwrap.dedent('''
        def main():
            try:
                body({})
            except Exception:
                return 0
    '''))
    assert _check(ok, wrapper="main_try") == []
    assert any("main_try" in p for p in _check(_GOOD_GUARD, wrapper="main_try"))


def test_an_entry_module_must_be_a_registered_verb_and_an_mcp_module_a_hook_tool() -> None:
    src = _GOOD_GUARD.replace("_lib.run_hook(body)", "pass")
    assert _check(src, wrapper="entry", dotted="nexus.hooks.x", verb_modules={"nexus.hooks.x"}) == []
    assert any("entry" in p for p in _check(src, wrapper="entry", dotted="nexus.hooks.x", verb_modules=set()))
    assert _check(src, wrapper="mcp", dotted="nexus.hooks.x", mcp_modules={"nexus.hooks.x"}) == []
    assert any("mcp" in p for p in _check(src, wrapper="mcp", dotted="nexus.hooks.x", mcp_modules=set()))


def test_a_local_try_wrapper_needs_the_call_inside_a_try_that_catches() -> None:
    inside = textwrap.dedent('''
        try:
            from _audience import telemetry_enabled as _telemetry
        except BaseException:
            def _telemetry():
                return False

        def record(payload):
            try:
                if not _telemetry():
                    return
            except Exception:
                pass
    ''')
    assert _check(inside, predicates=["telemetry_enabled"], wrapper="local_try") == []
    outside = inside.replace("    try:\n        if not _telemetry():\n            return\n    except Exception:\n        pass\n",
                             "    if not _telemetry():\n        return\n")
    assert outside != inside
    assert any("outside a try/except" in p for p in _check(outside, predicates=["telemetry_enabled"], wrapper="local_try"))


def test_the_real_guard_and_the_real_lib_pass_t9() -> None:
    """The three real shapes, against the real files: the guard (run_hook), the routing library (local_try) and the src hooks."""
    for rel, wrapper, preds, dotted in (
        ("conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py", "run_hook",
         ["forced_strict_clause", "guard_mode", "note_hook_error", "remote_test_hosts"], None),
        ("conexus/hooks/scripts/routing/_lib.py", "local_try", ["telemetry_enabled"], None),
        ("src/nexus/hooks/subagent_stop_t1_nudge.py", "never_fail", ["t1_nudge_enabled", "telemetry_enabled"], "nexus.hooks.subagent_stop_t1_nudge"),
    ):
        found = am.module_problems(rel, (ROOT / rel).read_text(encoding="utf-8"), declared_predicates=preds, wrapper=wrapper, dotted=dotted)
        assert found == [], found
