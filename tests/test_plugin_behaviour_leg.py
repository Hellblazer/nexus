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


def _row(manifest: dict) -> str:
    return next(r for r in am.allowlist_rows(manifest) if r.startswith("conexus/hooks/scripts/routing/credential_print_guard.py:"))


def test_the_checked_in_allowlist_is_well_formed_and_names_only_rows_not_live_yet(manifest) -> None:
    entries = leg.parse_allowlist(leg.DEFAULT_ALLOWLIST.read_text(encoding="utf-8"))
    assert entries, "Phase 2 ships with its Phase 3 worklist"
    assert leg.allowlist_problems(entries, manifest) == []
    assert all(e.until == "phase-3" for e in entries)


def test_an_allowlist_entry_for_a_live_row_is_refused(manifest) -> None:
    entries = leg.parse_allowlist("conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py:RULE_NAME\tSam\tuntil: phase-3\twhy\n")
    assert any("not a row that is not live yet" in p for p in leg.allowlist_problems(entries, manifest))
    entries = leg.parse_allowlist("U1a\tSam\tuntil: phase-3\twhy\n")
    assert any("not a row that is not live yet" in p for p in leg.allowlist_problems(entries, manifest))


def test_an_allowlist_entry_past_its_condition_is_refused(manifest) -> None:
    row = _row(manifest)
    expired = leg.parse_allowlist(f"{row}\tSam\tuntil: phase-2\tbecause\n")
    assert any("has passed" in p for p in leg.allowlist_problems(expired, manifest))
    answered = leg.parse_allowlist(f"{row}\tSam\tuntil: D6\tbecause\n")
    assert any("is answered" in p for p in leg.allowlist_problems(answered, manifest))
    unknown = leg.parse_allowlist(f"{row}\tSam\tuntil: someday\tbecause\n")
    assert any("unknown condition" in p for p in leg.allowlist_problems(unknown, manifest))


def test_a_malformed_allowlist_line_raises() -> None:
    with pytest.raises(ValueError):
        leg.parse_allowlist("only\ttwo fields\n")


def test_an_allowlisted_finding_is_let_through_and_counted_and_the_rest_fail(manifest) -> None:
    row = _row(manifest)
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


# --- presence ------------------------------------------------------------------------------------


def _synthetic(tmp_path: Path):
    mod = tmp_path / "m.py"
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
    (tmp_path / "m.py").write_text("X = 1\n", encoding="utf-8")
    problems, examined = leg.presence_problems(manifest, tmp_path, {})
    assert examined == 0 and "could not be read" in problems[0]


def test_the_real_manifests_constants_are_all_readable_from_source(manifest) -> None:
    for c in manifest["constants"]:
        if c["audience"] in ("SAM-WIDE", "NEXUS-REPO") and c.get("live", True):
            text = leg.constant_text(ROOT, c["id"])
            assert text and text.strip(), c["id"]


# --- a real run, in both directions ----------------------------------------------------------------


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="the leg runs the hooks' own uv argv")


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
