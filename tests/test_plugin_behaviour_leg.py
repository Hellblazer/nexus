# SPDX-License-Identifier: AGPL-3.0-or-later
"""The behaviour leg's own tests (nexus-sby49.3, plan section 5 item 2).

``tests/e2e/lib/plugin_behaviour.py`` runs the plugin's hooks and judges what a user and the maintainer see. This file pins
its judgement (the lexicon, the output classifier, the allowlist rules, the presence rules) with synthetic input, and proves
it fails in both directions on a real run: a plugin copy whose helper calls everyone the maintainer fails (a user got the
maintainer's deny), and one whose helper calls nobody the maintainer fails (nexus's own checkout lost its guard). The full
leg, both postures, is the release battery's ``plugins`` leg and ``plugin-out-of-repo-gate.sh``.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _audience_manifest as am
from tests._tmp_root import child_env_with_tmpdir

ROOT = Path(__file__).resolve().parent.parent
LEG = ROOT / "tests" / "e2e" / "lib" / "plugin_behaviour.py"
PLUGIN = ROOT / "conexus"


def _load():
    spec = importlib.util.spec_from_file_location("plugin_behaviour_under_test", LEG)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


leg = _load()


@pytest.fixture(scope="module")
def manifest() -> dict:
    return am.load_manifest(PLUGIN / "hooks" / "audience.yaml")


# --- the lexicon -------------------------------------------------------------------------------


@pytest.mark.parametrize("text,hit", [
    ("see nexus-abc12 for details", "nexus-abc12"),
    ("RDR-184 scopes this", "RDR-184"),
    ("run bd ready first", "bd"),
    ("ask Sam", "Sam"),
    ("ask Hal", "Hal"),
    ("the Orchestrator commits", "Orchestrator"),
    ("develop/main", "develop/main"),
    ("a lab-remote run", "lab-remote"),
    ("on lab-run hosts", "lab-run"),
    ("copy it to chas", "chas"),
    ("qwent is a pool host", "qwent"),
    ("hellmini too", "hellmini"),
    ("the Hellblazer/nexus wrapper", "Hellblazer/nexus"),
    ("hook-ENFORCED", "hook-ENFORCED"),
    ("behaviour in local AND cloud mode", "local AND cloud"),
    ("send it to halcyon", "halcyon"),
    ("ping Wren", "Wren"),
])
def test_the_lexicon_catches_what_a_user_must_never_see(text: str, hit: str) -> None:
    assert leg.lexicon_hits(text) == [hit]


@pytest.mark.parametrize("text", [
    "A sample of the halting problem, in a purchase order",
    "nexus-x is too short to be an id, and a nextbd or beads is no tracker command",
    "T1 survives compaction; a /clear, /resume or fresh restart",
    "Ship when <X>; each finding is a blocker or a bead",
])
def test_the_lexicon_leaves_ordinary_text_alone(text: str) -> None:
    assert leg.lexicon_hits(text) == []


def test_the_legs_own_scratch_names_never_look_like_an_id() -> None:
    """The scratch config dir was once called nexus-config, which the id pattern matched in a path the hook printed."""
    assert leg.lexicon_hits("/tmp/plugin-behaviour.x/iso/cfg/checkpoints") == []
    assert leg.lexicon_hits("/tmp/x/nexus-config/checkpoints") == ["nexus-config"]


def test_the_peer_names_are_the_stop_guards() -> None:
    """The lexicon's person names are the stop guard's own ``_PEER_NAMES`` (the rest of that tuple is ordinary English)."""
    import ast

    tree = ast.parse((PLUGIN / "hooks" / "scripts" / "stop_wake_guard.py").read_text(encoding="utf-8"))
    names: tuple = ()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_PEER_NAMES" for t in node.targets):
            names = ast.literal_eval(node.value)
    assert names, "the stop guard no longer has _PEER_NAMES: update the lexicon"
    assert set(leg.PEER_NAMES) <= set(names), set(leg.PEER_NAMES) - set(names)


# --- what an output is -----------------------------------------------------------------------------


def test_classify_reads_the_hook_envelopes() -> None:
    deny = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": "no", "reason": "no"}, "systemMessage": "short"})
    assert leg.classify(deny) == ("deny", "no\nno\nshort") or leg.classify(deny)[0] == "deny"
    assert leg.classify("")[0] == "none"
    assert leg.classify("## T1: text\nmore")[0] == "text"
    advice = json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "do it"}})
    assert leg.classify(advice) == ("advice", "Stop\ndo it")
    assert leg.classify(json.dumps({"decision": "block", "reason": "x"}))[0] == "block"
    allow = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})
    assert leg.classify(allow)[0] == "allow"
    assert leg.classify("[1, 2]")[0] == "text"


# --- the allowlist -------------------------------------------------------------------------------


ROW = "hooks/scripts/routing/credential_print_guard.py:_REDIRECT"


@pytest.fixture()
def unwired(manifest) -> dict:
    """The manifest with one row (the credential redirect) switched to ``live: false``: Phase 3 wired every real one, so the
    allowlist's rules are exercised on a synthetic row."""
    import copy

    m = copy.deepcopy(manifest)
    for c in m["constants"]:
        if c["id"] == ROW:
            c["live"] = False
    assert am.allowlist_rows(m) == [ROW]
    return m


def test_the_checked_in_allowlist_is_empty_and_every_manifest_row_is_live(manifest) -> None:
    """Phase 3 (nexus-sby49.4) ships when the allowlist has no entry and no row is `live: false`."""
    entries = leg.parse_allowlist(leg.DEFAULT_ALLOWLIST.read_text(encoding="utf-8"))
    assert entries == []
    assert leg.allowlist_problems(entries, manifest) == []
    assert am.allowlist_rows(manifest) == []


def test_an_allowlist_entry_for_a_live_row_is_refused(manifest) -> None:
    entries = leg.parse_allowlist("hooks/scripts/routing/subagent_git_write_requires_orchestrator.py:RULE_NAME\tSam\tuntil: phase-3\twhy\n")
    assert any("not a row that is not live yet" in p for p in leg.allowlist_problems(entries, manifest))
    entries = leg.parse_allowlist("U1a\tSam\tuntil: phase-3\twhy\n")
    assert any("not a row that is not live yet" in p for p in leg.allowlist_problems(entries, manifest))


def test_an_allowlist_entry_for_a_row_that_is_not_live_is_accepted_until_its_phase(unwired) -> None:
    entries = leg.parse_allowlist(f"{ROW}\tHellblazer/nexus\tuntil: phase-4\tbecause\n")
    assert leg.allowlist_problems(entries, unwired) == []


def test_an_allowlist_entry_past_its_condition_is_refused(unwired) -> None:
    manifest = unwired
    row = ROW
    expired = leg.parse_allowlist(f"{row}\tSam\tuntil: phase-2\tbecause\n")
    assert any("has passed" in p for p in leg.allowlist_problems(expired, manifest))
    answered = leg.parse_allowlist(f"{row}\tSam\tuntil: D6\tbecause\n")
    assert any("is answered" in p for p in leg.allowlist_problems(answered, manifest))
    unknown = leg.parse_allowlist(f"{row}\tSam\tuntil: someday\tbecause\n")
    assert any("unknown condition" in p for p in leg.allowlist_problems(unknown, manifest))


def test_a_malformed_allowlist_line_raises() -> None:
    with pytest.raises(ValueError):
        leg.parse_allowlist("only\ttwo fields\n")


def test_an_allowlisted_finding_is_let_through_and_counted_and_the_rest_fail(unwired) -> None:
    manifest = unwired
    row = ROW
    entries = leg.parse_allowlist(f"{row}\tHellblazer/nexus\tuntil: phase-3\treason\n")
    key = "PreToolUse hooks/scripts/routing/credential_print_guard.py"
    findings = [
        {"reg_key": key, "hit": "Hellblazer/nexus", "probe": "a"},
        {"reg_key": key, "hit": "RDR-219", "probe": "b"},
        {"reg_key": "Stop hooks/scripts/stop_wake_guard.py", "hit": "Hellblazer/nexus", "probe": "c"},
    ]
    failures, allowed = leg.apply_allowlist(findings, entries, manifest)
    assert [f["probe"] for f in allowed] == ["a"] and entries[0].used == 1
    assert sorted(f["probe"] for f in failures) == ["b", "c"], "another hit, and the same hit in another registration, still fail"


#: The allowlist may only shrink (nexus-sby49.3, critic I3/I4). Phase 3 removes entries; nothing adds one. A new entry
#: needs a decision of record, and then this set changes in the same commit.
ALLOWLIST_PINNED: set[tuple[str, str]] = set()  # emptied by Phase 3 (nexus-sby49.4)
#: Every manifest row whose end state is not wired yet. Phase 3 (and D6's flip) empties this; a new `live: false` row is a
#: decision, not a way around the leg.
NOT_LIVE_PINNED: set[str] = set()  # emptied by Phase 3 (nexus-sby49.4)


def test_the_allowlist_is_pinned_and_may_only_shrink() -> None:
    entries = leg.parse_allowlist(leg.DEFAULT_ALLOWLIST.read_text(encoding="utf-8"))
    assert {(e.row, e.hit) for e in entries} <= ALLOWLIST_PINNED, "a new allowlist entry needs a decision of record, and this pin"


def test_the_not_live_rows_are_pinned_and_may_only_shrink(manifest) -> None:
    assert set(am.allowlist_rows(manifest)) <= NOT_LIVE_PINNED, "a new live: false row is a decision, not a way around the leg"
    assert not NOT_LIVE_PINNED and am.allowlist_rows(manifest) == []


def test_every_advisory_rows_text_is_clean_or_allowlisted(manifest) -> None:
    """The leg drives three advice commands; this holds EVERY row of the table to the lexicon, from the rows themselves."""
    spec = importlib.util.spec_from_file_location("daa_for_leg", PLUGIN / "hooks" / "scripts" / "routing" / "destructive_action_advice.py")
    daa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daa)
    allowed = {e.hit for e in leg.parse_allowlist(leg.DEFAULT_ALLOWLIST.read_text(encoding="utf-8"))
               if e.row == "PreToolUse hooks/scripts/routing/destructive_action_advice.py"}
    assert allowed == set(), "U8a: every advice row's text is neutral, so nothing is allowlisted"
    assert len(daa.ROWS) >= 8
    for row in daa.ROWS:
        extra = set(leg.lexicon_hits(row.advice)) - allowed
        assert not extra, f"advice row {row.id} names {sorted(extra)} to a user, and no allowlist entry covers it"


def test_the_leg_expects_the_subagent_stop_nudge_by_audience() -> None:
    """U5a and U5b: general-purpose is nudged only for the maintainer; a conexus agent for everyone."""
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    reg = next(r for r in am.registrations(hooks) if r["event"] == "SubagentStop")
    seen: dict[tuple[str, str], str] = {}
    for name in ("plain", "marked", "flagged"):
        fx = leg.Fixture(name, Path("/x") / name, Path("/h"), name == "flagged")
        for sc in leg._scenarios(reg, fx):
            seen[(name, sc["name"])] = sc["expect"]
    assert seen[("plain", "general-purpose")] == "none"
    assert seen[("marked", "general-purpose")] == seen[("flagged", "general-purpose")] == "advice"
    assert all(seen[(n, "conexus:developer")] == "advice" for n in ("plain", "marked", "flagged"))


# --- presence ------------------------------------------------------------------------------------


def _synthetic(tmp_path: Path):
    mod = tmp_path / "conexus" / "m.py"
    mod.parent.mkdir(exist_ok=True)
    mod.write_text('SAM = "the maintainers own row of text"\nREPO = "a row only the nexus checkout gets"\n', encoding="utf-8")
    manifest = {"constants": [
        {"id": "m.py:SAM", "audience": "SAM-WIDE", "gate": "maintainer", "live": True},
        {"id": "m.py:REPO", "audience": "NEXUS-REPO", "gate": "nexus_repo", "live": True},
        {"id": "m.py:NOT_LIVE", "audience": "SAM-WIDE", "gate": "maintainer", "live": False},
    ]}
    return manifest


def test_presence_passes_when_each_constant_is_where_its_audience_says(tmp_path) -> None:
    texts = {"plain": "hello", "plain-dirty": "", "marked": "the maintainers  own row of text\nand a row only the nexus checkout gets",
             "flagged": "the maintainers own row of text"}
    problems, examined = leg.presence_problems(_synthetic(tmp_path), tmp_path, texts)
    assert problems == [] and examined == 2


def test_presence_fails_on_a_leak_to_a_user_a_missing_row_and_a_nexus_row_in_a_flagged_repo(tmp_path) -> None:
    texts = {"plain": "a row only the nexus checkout gets", "plain-dirty": "", "marked": "the maintainers own row of text",
             "flagged": "the maintainers own row of text and a row only the nexus checkout gets"}
    problems, _ = leg.presence_problems(_synthetic(tmp_path), tmp_path, texts)
    joined = "\n".join(problems)
    assert "REPO (NEXUS-REPO) never appeared in the marked fixture" in joined
    assert "REPO (NEXUS-REPO) appeared in the flagged fixture" in joined
    assert "REPO (NEXUS-REPO) appeared in a user's output" in joined
    assert "SAM (SAM-WIDE)" not in joined


def test_presence_fails_when_a_constants_text_cannot_be_read(tmp_path) -> None:
    manifest = {"constants": [{"id": "m.py:GONE", "audience": "SAM-WIDE", "gate": "maintainer", "live": True}]}
    (tmp_path / "conexus").mkdir(exist_ok=True)
    (tmp_path / "conexus" / "m.py").write_text("X = 1\n", encoding="utf-8")
    problems, examined = leg.presence_problems(manifest, tmp_path, {})
    assert examined == 0 and "could not be read" in problems[0]


def test_the_real_manifests_constants_are_all_readable_from_source(manifest) -> None:
    for c in manifest["constants"]:
        if c["audience"] in ("SAM-WIDE", "NEXUS-REPO") and c.get("live", True):
            text = leg.constant_text(ROOT, c["id"])
            assert text and text.strip(), c["id"]


# --- a real run, in both directions ----------------------------------------------------------------


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="the leg runs the hooks' own uv argv")


def test_the_real_runs_are_not_skipped_in_ci() -> None:
    """Non-vacuity: the two direction tests below skip without uv, and every CI runner has it, so a skip there is a failure."""
    import os

    if os.environ.get("CI"):
        assert shutil.which("uv") is not None, "uv is missing on a CI runner: the leg's real-run tests would skip and report green"


def _run_leg(plugin: Path, only: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LEG), "run", "--plugin", str(plugin), "--posture", "P2", "--account", "harness", "--only", only, "--jobs", "2"],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT),
    )


def _plugin_copy(tmp_path: Path, helper_returns: bool) -> Path:
    dst = tmp_path / "plugin"
    shutil.copytree(PLUGIN, dst, ignore=shutil.ignore_patterns("__pycache__", "evals", "*.pyc"))
    helper = dst / "hooks" / "scripts" / "_audience.py"
    src = helper.read_text(encoding="utf-8")
    src = src.replace(
        "def maintainer(payload: object) -> bool:\n    \"\"\"The account flag OR the repo marker; True on any error.\"\"\"\n    try:\n        return account_flag() or nexus_repo(payload)",
        f"def maintainer(payload: object) -> bool:\n    \"\"\"mutated\"\"\"\n    try:\n        return {helper_returns!r}",
    )
    assert f"return {helper_returns!r}" in src and "mutated" in src
    helper.write_text(src, encoding="utf-8")
    return dst


@needs_uv
def test_the_leg_passes_on_the_real_plugin_for_the_commit_probe(tmp_path) -> None:
    proc = _run_leg(PLUGIN, "git commit -am x", tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PLUGIN BEHAVIOUR LEG PASSED" in proc.stdout
    assert "P2: 1 registrations driven, 4 payloads" in proc.stdout, proc.stdout  # plain, marked, flagged, plain-dirty


@needs_uv
def test_the_leg_fails_when_a_user_gets_the_maintainers_deny(tmp_path) -> None:
    """A helper that calls everyone the maintainer: the plain fixture's commit is denied, and the leg says so."""
    proc = _run_leg(_plugin_copy(tmp_path, True), "git commit -am x", tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "P2/plain/" in proc.stdout and "expected the call to pass, got deny" in proc.stdout


@needs_uv
def test_the_leg_fails_when_nexus_loses_its_guard(tmp_path) -> None:
    """A helper that calls nobody the maintainer: the marked checkout's commit passes, and the leg says so."""
    proc = _run_leg(_plugin_copy(tmp_path, False), "git commit -am x", tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "P2/marked/" in proc.stdout and "expected deny, got none" in proc.stdout


def test_a_plugin_with_no_manifest_is_unverified(tmp_path) -> None:
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text('{"hooks": {}}')
    proc = subprocess.run([sys.executable, str(LEG), "run", "--plugin", str(tmp_path)], capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert proc.returncode == 2 and "UNVERIFIED" in proc.stdout and "audience.yaml" in proc.stdout


# --- Phase 3's expectations (nexus-sby49.4) ----------------------------------------------------------------------


def _reg(event: str, tail_end: str) -> dict:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return next(r for r in am.registrations(hooks) if r["event"] == event and r["argv"][-1].endswith(tail_end))


def _scenarios_for(reg: dict, name: str) -> dict[str, dict]:
    fx = leg.Fixture(name, Path("/x") / name, Path("/h"), name == "flagged")
    return {sc["name"]: sc for sc in leg._scenarios(reg, fx)}


def test_the_leg_expects_the_prompt_for_the_seven_tools_for_everyone_but_the_flagged_account() -> None:
    """U2b: the seven are silent for a user and for the marked checkout (the marker never loosens), allowed for the flag."""
    reg = _reg("PreToolUse", "auto-approve")
    for name in ("plain", "plain-dirty", "plain-beads", "marked", "flagged"):
        sc = _scenarios_for(reg, name)
        assert sc["search"]["expect"] == "allow"
        for tool in leg.PROMPTED_FOR_USERS:
            assert sc[tool]["expect"] == ("allow" if name == "flagged" else "none"), (name, tool)
    assert len(leg.PROMPTED_FOR_USERS) == 7
    assert "answer_plan_delete" in leg.PROMPTED_FOR_USERS and "plan_delete" in leg.PROMPTED_FOR_USERS


def test_the_leg_expects_neutral_stop_text_for_users_and_the_ci_text_only_in_the_marked_checkout() -> None:
    reg = _reg("Stop", "stop_wake_guard.py")
    marked = _scenarios_for(reg, "marked")
    assert marked["ci wait"]["must"] == ("nx ci wait",) and marked["ci wait"]["private_cfg"] is True
    for name in ("plain", "plain-dirty", "plain-beads", "flagged"):
        sc = _scenarios_for(reg, name)
        assert set(leg.CI_TEXT) <= set(sc["ci wait"]["forbid"]) and set(leg.CI_TEXT) <= set(sc["closing promise"]["forbid"]), name
        assert sc["ci wait"]["private_cfg"] is True, "no config directory may appear (U6c)"


def test_the_leg_expects_the_compaction_words_by_the_projects_data() -> None:
    inject = _reg("SessionStart", "inject")
    user, beads, marked = (_scenarios_for(inject, n) for n in ("plain", "plain-beads", "marked"))
    assert "board" in user["compact, no checkpoint"]["forbid"] and "bd list" in user["compact, no checkpoint"]["forbid"]
    assert "bd list --status=in_progress" in beads["compact, no checkpoint"]["must"]
    assert "board" in beads["compact, no checkpoint"]["forbid"], "a beads project is not a board project"
    assert "threads board entry" in marked["compact, no checkpoint"]["must"], "the maintainer keeps the board"


def test_the_beads_fixture_runs_only_the_compaction_registrations(manifest) -> None:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    regs = {r["key"]: r for r in am.registrations(hooks)}
    kept = [k for k, r in regs.items() if leg._is_compaction(r)]
    assert sorted(regs[k]["event"] for k in kept) == ["PostCompact", "PreCompact", "SessionStart"]
    assert "plain-beads" in leg.USER_FIXTURES and leg.BEADS_FIXTURES == ("plain-beads",)


def test_the_bd_word_is_a_hit_unless_the_project_has_beads() -> None:
    assert leg.lexicon_hits("run `bd list` now") == ["bd"]
    assert leg.lexicon_hits("run `bd list` now", beads=True) == []
    assert leg.lexicon_hits("nexus-abc12 and bd", beads=True) == ["nexus-abc12"]


def test_evaluate_fails_a_missing_required_word_a_forbidden_one_and_a_leaked_config_dir(manifest) -> None:
    def outcome(text: str, *, must=(), forbid=(), leaked=()):
        probe = leg.Probe("P2/plain/x/y", "k", "plain", "P2", "script", [], None, None, must=must, forbid=forbid)
        return leg.Outcome(probe, 0, text, "", "advice", text, "", leaked)

    _f, hard, _t, _c = leg.evaluate([outcome("hello", must=("nx ci wait",))], manifest, ROOT)
    assert any("does not carry 'nx ci wait'" in h for h in hard)
    _f, hard, _t, _c = leg.evaluate([outcome("the board", forbid=("board",))], manifest, ROOT)
    assert any("carries 'board'" in h for h in hard)
    _f, hard, _t, _c = leg.evaluate([outcome("ok", leaked=("logs",))], manifest, ROOT)
    assert any("left ['logs']" in h for h in hard)
    _f, hard, _t, _c = leg.evaluate([outcome("fine")], manifest, ROOT)
    assert hard == []


def test_a_private_config_probe_reports_what_a_hook_leaves_in_it(tmp_path) -> None:
    probe = leg.Probe("p", "k", "plain", "P2", "script",
                      [sys.executable, "-c", "import os; os.makedirs(os.path.join(os.environ['NEXUS_CONFIG_DIR'], 'logs'))"],
                      {}, None, private_cfg=True)
    env = child_env_with_tmpdir({}, tmp_path)
    out = leg.run_probe(probe, env, tmp_path, 30)
    assert out.leaked == ("logs",)
    clean = leg.Probe("p", "k", "plain", "P2", "script", [sys.executable, "-c", "pass"], {}, None, private_cfg=True)
    assert leg.run_probe(clean, env, tmp_path, 30).leaked == ()
