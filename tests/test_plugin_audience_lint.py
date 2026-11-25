# SPDX-License-Identifier: AGPL-3.0-or-later
"""The audience manifest lint (nexus-sby49.3, plan section 5 item 1; T9).

``conexus/hooks/audience.yaml`` declares who every hook registration, rule and injected constant is for. This lint is the
reason a new hook or a new line of injected text cannot reach every user without somebody deciding that: it fails on a
registration with no entry, an unknown value, a constant that does not exist or is not classified, a gated entry whose
module never calls its predicate, a predicate called outside a wrapped body, and an audience import whose fallback
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


def test_the_not_live_rows_are_the_phase_3_rows(manifest) -> None:
    """The behaviour leg's allowlist is exactly these. Every one names the phase that wires it."""
    rows = am.allowlist_rows(manifest)
    assert rows, "a Phase 2 manifest with no Phase 3 rows would mean the long tail was done"
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
        assert rule.get("audience") in am.AUDIENCES, name
    spec = importlib.util.spec_from_file_location("daa_lint", ROOT / "conexus/hooks/scripts/routing/destructive_action_advice.py")
    daa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daa)
    assert len(daa.ROWS) >= 5
    for row in daa.ROWS:
        assert row.audience in am.AUDIENCES, row.id


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
    m = _mutated(manifest, lambda m: m["constants"][0].update({"audience": "SAM-WIDE", "gate": "none"}))
    assert any("cannot carry gate" in p for p in _problems(m, hooks_json))


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


def test_a_live_gated_entry_in_a_module_that_never_calls_its_predicate_fails(manifest, hooks_json) -> None:
    def gate_the_setup_notice(m):
        m["rules"].append({
            "id": "bogus", "module": "hooks/scripts/nx_setup_notice.py", "audience": "SAM-WIDE", "gate": "maintainer",
            "evidence": "x", "target_state": "x", "phase": "2",
        })

    found = _problems(_mutated(manifest, gate_the_setup_notice), hooks_json)
    assert any("never calls maintainer()" in p for p in found), found


def test_a_gated_entry_that_is_not_live_may_have_no_call_yet(manifest, hooks_json) -> None:
    """The Phase 3 rows: their gate is declared and not wired, so the module need not call it (and the leg allowlists them)."""
    assert any(not e.get("live", True) and e["gate"] in am.PREDICATE_GATES for k in ("constants", "rules") for e in manifest[k])
    assert _problems(manifest, hooks_json) == []


def test_a_module_that_declares_the_wrong_predicates_fails(manifest, hooks_json) -> None:
    m = _mutated(manifest, lambda m: m["modules"]["nexus.hooks.subagent_start"].__setitem__("predicates", ["maintainer"]))
    assert any("declares predicates" in p for p in _problems(m, hooks_json))


def test_a_module_with_text_and_no_entry_fails(manifest, hooks_json) -> None:
    m = _mutated(manifest, lambda m: m["modules"].pop("nexus.hooks.board_due"))
    found = _problems(m, hooks_json)
    assert any("nexus.hooks.board_due" in p and "not in the modules table" in p for p in found), found


# --- T9: the wrapper, the guarded import and the fallback -------------------------------------------------------

_GOOD_GUARD = textwrap.dedent('''
    import sys
    try:
        from _audience import maintainer as _maintainer, account_flag as _flag, remote_test_hosts as _hosts, note_hook_error as _note
    except BaseException:
        def _maintainer(payload):
            return True

        def _flag():
            return False

        def _hosts():
            return frozenset()

        def _note(payload, what):
            return None

    def body(payload):
        if _maintainer(payload):
            return 1

    if __name__ == "__main__":
        _lib.run_hook(body)
''')

_PREDICATES = ["account_flag", "maintainer", "note_hook_error", "remote_test_hosts"]


def _check(source: str, *, predicates=None, wrapper="run_hook", **kw) -> list[str]:
    return am.module_problems("fixture.py", source, declared_predicates=_PREDICATES if predicates is None else predicates,
                              wrapper=wrapper, **kw)


def test_a_guard_shaped_module_passes() -> None:
    assert _check(_GOOD_GUARD) == []


def test_an_unguarded_import_fails() -> None:
    src = _GOOD_GUARD.replace("try:\n    from", "if True:\n    from").replace("except BaseException:", "else:")
    assert any("not inside a try" in p for p in _check(src))


def test_a_bare_module_import_fails() -> None:
    src = "import _audience\n\ndef body(payload):\n    return _audience.maintainer(payload)\n\n_lib.run_hook(body)\n"
    assert any("imports the audience helper as a module" in p for p in _check(src, predicates=[]))


def test_a_tightening_predicate_whose_fallback_answers_false_fails() -> None:
    src = _GOOD_GUARD.replace("def _maintainer(payload):\n        return True", "def _maintainer(payload):\n        return False")
    assert any("the fallback for _maintainer returns 'False'" in p for p in _check(src))


def test_a_loosening_predicate_whose_fallback_answers_true_fails() -> None:
    src = _GOOD_GUARD.replace("def _flag():\n        return False", "def _flag():\n        return True")
    assert any("the fallback for _flag returns 'True'" in p for p in _check(src))


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
    src = _GOOD_GUARD + "\nSTARTUP = _maintainer({})\n"
    assert any("runs at import time" in p for p in _check(src))


def test_a_predicate_call_in_a_class_body_fails() -> None:
    src = _GOOD_GUARD + "\nclass K:\n    on = _maintainer({})\n"
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
            from _audience import maintainer as _maintainer
        except BaseException:
            def _maintainer(payload):
                return True

        def record(payload):
            try:
                if not _maintainer(payload):
                    return
            except Exception:
                pass
    ''')
    assert _check(inside, predicates=["maintainer"], wrapper="local_try") == []
    outside = inside.replace("    try:\n        if not _maintainer(payload):\n            return\n    except Exception:\n        pass\n",
                             "    if not _maintainer(payload):\n        return\n")
    assert outside != inside
    assert any("outside a try/except" in p for p in _check(outside, predicates=["maintainer"], wrapper="local_try"))


def test_the_real_guard_and_the_real_lib_pass_t9() -> None:
    """The three real shapes, against the real files: the guard (run_hook), the routing library (local_try) and the src hooks."""
    for rel, wrapper, preds, dotted in (
        ("conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py", "run_hook",
         ["maintainer", "note_hook_error", "note_marker_only", "remote_test_hosts"], None),
        ("conexus/hooks/scripts/routing/_lib.py", "local_try", ["maintainer"], None),
        ("src/nexus/hooks/subagent_stop_t1_nudge.py", "never_fail", ["maintainer"], "nexus.hooks.subagent_stop_t1_nudge"),
    ):
        found = am.module_problems(rel, (ROOT / rel).read_text(encoding="utf-8"), declared_predicates=preds, wrapper=wrapper, dotted=dotted)
        assert found == [], found
