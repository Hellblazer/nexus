# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A hook that returns a VERDICT must be wired on the command tier (nexus-17i1n).

An ``mcp_tool`` hook cannot decide anything. Claude Code's hooks guide
names four hook types that carry a decision -- ``prompt``, ``agent``,
``command`` and ``http`` -- and ``mcp_tool`` is not among them; the only
posture it states for ``mcp_tool`` is "non-blocking error", and its
output is read for context, never for a verdict.

RDR-215 bead nexus-q02nx.21 rewired 21 ``hooks.json`` entries across the
two tiers and moved three verdict-returning hooks to ``mcp_tool`` with
everything else. conexus 7.55.0 shipped with all three inert. Measured
2026-09-20 on CLI 2.1.278 against that pin: a ``bd close`` naming a bead
with NO review-completed marker reached ``bd`` and closed it, while the
identical payload handed to ``pre_close_verification.run()`` returned a
correct, fully-worded deny. Every part was individually right -- the
logic, the T1 marker store, the matcher, the registration -- and the gate
still did nothing, because the tier it was wired on discards verdicts.

Nothing caught it, and the reason is worth stating: a hook that declines
to speak and a hook whose verdict is thrown away produce the same
observable, which is nothing at all. Every existing test drove ``run()``
and asserted the envelope, which was correct and stayed correct
throughout. The untested proposition was that the wire carries it.

So these tests are about the WIRE, and deliberately not about the
envelope. They read the real ``conexus/hooks/hooks.json`` rather than a
fixture, because the shipped file is the artifact that was wrong.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests._hook_wiring import command_verb, events_for
from nexus._hook_runtime.entry import VERB_TABLE
from nexus.mcp.hooks import DECIDING_HOOKS, HOOK_TOOLS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOKS_JSON = _REPO_ROOT / "conexus" / "hooks" / "hooks.json"


def _entries() -> list[tuple[str, str, dict]]:
    """Every hook entry in the shipped hooks.json, as (event, matcher, hook)."""
    data = json.loads(_HOOKS_JSON.read_text())
    out: list[tuple[str, str, dict]] = []
    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                out.append((event, group.get("matcher", ""), hook))
    return out


def test_the_wiring_file_is_where_we_think_it_is() -> None:
    """Non-vacuity for every test below: they all parse this one file.

    Without this, a renamed or moved hooks.json turns the whole module
    into a set of assertions over an empty list, which passes.
    """
    assert _HOOKS_JSON.is_file(), f"hooks.json not found at {_HOOKS_JSON}"
    entries = _entries()
    assert len(entries) > 20, f"only {len(entries)} hook entries parsed; the shape changed"
    assert any(h.get("type") == "mcp_tool" for _, _, h in entries), (
        "no mcp_tool entries at all — either the tier was retired (delete this "
        "module and say so) or the parse is wrong"
    )


@pytest.mark.parametrize("hook_name", sorted(DECIDING_HOOKS))
def test_a_deciding_hook_is_never_wired_as_an_mcp_tool(hook_name: str) -> None:
    """The regression itself. An mcp_tool entry for these is a silent no-op."""
    tool = f"hook_{hook_name}"
    wired_as_tool = [
        (event, matcher)
        for event, matcher, hook in _entries()
        if hook.get("type") == "mcp_tool" and hook.get("tool") == tool
    ]
    assert not wired_as_tool, (
        f"{tool} returns a verdict, and an mcp_tool hook cannot return one — "
        f"wired as mcp_tool at {wired_as_tool}. Wire it as a command tier "
        f"entry instead: {{'type': 'command', 'command': 'nx-hook', 'args': "
        f"['{hook_name.replace('_', '-')}']}}. See nexus.mcp.hooks.DECIDING_HOOKS."
    )


@pytest.mark.parametrize("hook_name", sorted(DECIDING_HOOKS))
def test_a_deciding_hook_has_a_command_tier_verb_to_be_wired_as(hook_name: str) -> None:
    """Refusing the tool tier is only half an answer; there must be a tier to take."""
    verb = hook_name.replace("_", "-")
    assert verb in VERB_TABLE, (
        f"{hook_name} may not be wired as an mcp_tool, but nx-hook has no "
        f"{verb!r} verb for it to be wired as instead. Add it to VERB_TABLE."
    )
    assert VERB_TABLE[verb] == f"nexus.hooks.{hook_name}", (
        f"nx-hook verb {verb!r} resolves to {VERB_TABLE[verb]}, not the "
        f"nexus.hooks.{hook_name} module the tool tier registers."
    )


@pytest.mark.parametrize("hook_name", sorted(DECIDING_HOOKS))
def test_a_deciding_hook_is_actually_wired_somewhere(hook_name: str) -> None:
    """A gate nobody wires is as inert as one wired on the wrong tier.

    This is the other half of the bug's shape. Moving the entry off
    ``mcp_tool`` satisfies the test above by DELETING it, which would
    leave the gate exactly as dead as it was and the suite exactly as
    green. So assert the command-tier entry exists and names the verb.
    """
    verb = hook_name.replace("_", "-")
    wired = [
        (event, matcher)
        for event, matcher, hook in _entries()
        if command_verb(hook) == verb
    ]
    assert wired, (
        f"no hooks.json entry runs `nx-hook {verb}`, directly or through the "
        f"nx-hook shim — {hook_name} is "
        f"registered and ported but fires on no event."
    )


#: Events on which a hook's verdict can change what happens next. A
#: PostToolUse verdict cannot: the tool has already run, so the ``allow``
#: ``divergence_language_guard`` emits is decorative and losing it costs
#: nothing. Stop and SubagentStop are here because a ``block`` there is
#: real, even though the only hook currently wired on Stop can just
#: approve.
_EVENTS_WHERE_A_VERDICT_MATTERS = frozenset(
    {"PreToolUse", "PermissionRequest", "Stop", "SubagentStop", "UserPromptSubmit"}
)

#: The three ``_io`` helpers that write a verdict, plus the two envelope
#: keys a module can build by hand. :func:`_verdicts_emitted` reads only
#: these, via ``ast``, so a verdict word appearing in a docstring or as
#: some other vocabulary's value is not mistaken for one.
#:
#: That mistake is the reason this is parsed rather than grepped. A
#: first cut searched each module's whole text for ``"block"`` and
#: flagged ``agent_dispatch_expect``, whose ``"block"`` is a
#: ``stop_guard_mode`` value, and ``stop_verification``, whose is prose.
#: Both would have been "fixed" by adding them to DECIDING_HOOKS, which
#: would have made the list mean nothing.
_EMITTER_CALLS = frozenset({"permission_decision", "permission_request", "stop_decision"})
_VERDICT_KEYS = frozenset({"permissionDecision", "decision", "behavior"})

#: Stands in for a verdict this reader cannot evaluate statically — a
#: variable, a named constant, an f-string. It is treated as non-neutral
#: on purpose, so an unreadable verdict forces the hook into
#: DECIDING_HOOKS rather than being counted as no verdict at all. A
#: reader that answers "nothing to see" when it cannot see is the exact
#: shape of the defect this module exists to catch.
_NEEDS_MANUAL_CLASSIFICATION = "deny"

#: The Claude Code release the tier rule was read and measured against.
#: This module asserts a fact about a FILE in order to enforce a fact
#: about the HARNESS, and only the first half is checked here. If a
#: later CLI lets `mcp_tool` carry a verdict, or changes which events
#: read one, these tests keep enforcing a rule that has stopped being
#: true and nothing says so. Re-read the hooks guide when this is far
#: behind the CLI in use; `tests/hooks/test_deciding_verbs_end_to_end.py`
#: is the nearest thing to a behavioural check and it still only proves
#: the command tier works, never that the tool tier does not.
_VERIFIED_AGAINST_CLI = "2.1.278"  # 2026-09-20

#: ``allow`` is neutral on most events and NOT neutral on these two,
#: where it skips a permission prompt the user would otherwise see. That
#: is why ``auto_approve`` counts and ``divergence_language_guard`` does
#: not, though both emit nothing but ``allow``.
_ALLOW_IS_A_DECISION_ON = frozenset({"PreToolUse", "PermissionRequest"})


def test_every_registered_hook_whose_verdict_matters_is_declared_deciding() -> None:
    """DECIDING_HOOKS is hand-kept; this is what keeps it honest.

    A new hook whose verdict matters and is NOT listed is free to be
    wired as an ``mcp_tool``, and the parametrized tests above would
    never look at it — which is the exact route this defect took to a
    release. So go the other way: read the ported hook modules, work out
    whose verdict can change an outcome, and require each to be declared.

    "Can change an outcome" is two things, and an earlier draft of this
    test had only the first. A hook must emit a non-neutral verdict
    (``deny``/``ask``/``block``, or an ``allow`` on an event where allow
    skips a prompt), AND be wired on an event where verdicts are read at
    all. With only the first half it flagged
    ``divergence_language_guard``, which emits ``permissionDecision:
    allow`` on PostToolUse — after the tool has run, against nothing.
    Adding it to DECIDING_HOOKS to quiet the test would have been the
    wrong fix: the list would then mean "emits a verdict token" and stop
    meaning "must be wired on the command tier".

    Matching on source text rather than behaviour is deliberate. The
    alternative is calling every hook's ``run()`` with a synthetic
    payload and reading the envelope, which needs a plausible payload per
    hook and returns the neutral answer for most of them on most inputs —
    a check whose domain would not reliably contain the claim.
    """
    hooks_dir = _REPO_ROOT / "src" / "nexus" / "hooks"
    assert hooks_dir.is_dir(), f"hook package not found at {hooks_dir}"

    events_by_hook = _events_by_registered_hook()
    registered = _registered_hook_names()

    matters: set[str] = set()
    for path in sorted(hooks_dir.glob("*.py")):
        if path.name.startswith("_") or path.stem not in registered:
            continue
        events = events_by_hook.get(path.stem, set()) & _EVENTS_WHERE_A_VERDICT_MATTERS
        if not events:
            continue
        verdicts = _verdicts_emitted(path)
        if verdicts & {"deny", "ask", "block"}:
            matters.add(path.stem)
        elif "allow" in verdicts and events & _ALLOW_IS_A_DECISION_ON:
            matters.add(path.stem)

    assert matters, (
        "found no registered hook whose verdict matters — either the verdict "
        "markers are stale or no deciding hook is wired at all"
    )

    undeclared = matters - DECIDING_HOOKS
    assert not undeclared, (
        f"these hooks return a verdict on an event that reads one, but are not "
        f"in DECIDING_HOOKS, so nothing stops hooks.json wiring them as an "
        f"mcp_tool where the verdict is discarded: {sorted(undeclared)}. "
        f"(The tier rule was read and measured against Claude Code CLI "
        f"{_VERIFIED_AGAINST_CLI}; if that is far behind the CLI in use, "
        f"re-read the hooks guide before assuming this rule still holds.)"
    )


def test_the_verdict_criterion_excludes_a_decorative_allow() -> None:
    """Non-vacuity for the test above, from the case that taught it.

    ``divergence_language_guard`` emits ``permissionDecision: allow`` and
    is registered, so a criterion of "emits a verdict token" sweeps it
    in. If this stops holding, either that hook moved to an event where
    its verdict matters — in which case it belongs in DECIDING_HOOKS —
    or the criterion above quietly widened back out.
    """
    events = _events_by_registered_hook().get("divergence_language_guard", set())
    assert events, "divergence_language_guard is not wired at all; this guard is now vacuous"
    assert not (events & _EVENTS_WHERE_A_VERDICT_MATTERS), (
        f"divergence_language_guard is now wired on {sorted(events)}, where a "
        f"verdict is read — add it to DECIDING_HOOKS and rewrite this test"
    )
    text = (_REPO_ROOT / "src" / "nexus" / "hooks" / "divergence_language_guard.py").read_text()
    assert '"allow"' in text, "it no longer emits allow; this guard no longer guards anything"
    assert "divergence_language_guard" not in DECIDING_HOOKS


def _verdicts_emitted(path: Path) -> set[str]:
    """Every verdict value *path*'s module can put on the wire.

    Two sources, both read from the parse tree rather than the text: a
    literal string argument to one of the ``_io`` verdict helpers, and a
    literal value under a verdict key in a dict literal (the shape a
    module that builds its own envelope uses). A non-literal argument is
    invisible here, which is a real limit — see the accompanying
    non-vacuity test, which pins that this does find the verdicts of the
    hooks we know emit them.
    """
    found: set[str] = set()
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in _EMITTER_CALLS:
                # Positional AND keyword. Every call site today passes
                # the verdict positionally, so reading only node.args
                # happened to work — and a future
                # stop_decision(decision="block") would have been
                # invisible to a test whose entire job is catching a
                # silently-missing verdict. Found in review, not by a
                # failure, which is the same way this whole class hides.
                for arg in (*node.args, *(kw.value for kw in node.keywords)):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
                    elif not isinstance(arg, ast.Constant):
                        # A verdict this reader CANNOT evaluate — a name,
                        # a constant referenced by name, an f-string.
                        # Returning nothing for it would be the silent
                        # gap this whole module exists to prevent, one
                        # level up, so it is surfaced as a value that
                        # forces the hook into DECIDING_HOOKS and makes
                        # a human classify it.
                        found.add(_NEEDS_MANUAL_CLASSIFICATION)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in _VERDICT_KEYS
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.add(value.value)
    return found


def test_the_verdict_reader_finds_the_verdicts_we_know_are_there() -> None:
    """Non-vacuity for :func:`_verdicts_emitted`.

    It reads literal arguments and literal dict values only, so a module
    that computed its verdict would read as emitting none — and a silent
    empty result is exactly how this whole class of gate fails. Pin the
    three known emitters by value.
    """
    hooks_dir = _REPO_ROOT / "src" / "nexus" / "hooks"
    assert "deny" in _verdicts_emitted(hooks_dir / "pre_close_verification.py")
    assert "block" in _verdicts_emitted(hooks_dir / "subagent_stop.py")
    assert "allow" in _verdicts_emitted(hooks_dir / "auto_approve.py")
    # And the discrimination that matters: a mode value named "block" is
    # not a verdict.
    assert "block" not in _verdicts_emitted(hooks_dir / "agent_dispatch_expect.py")


def _events_by_registered_hook() -> dict[str, set[str]]:
    """Which hooks.json events each registered hook fires on, across both tiers."""
    return {name: set(events_for(name)) for name in _registered_hook_names()}


def _registered_hook_names() -> set[str]:
    return {spec.name for spec in HOOK_TOOLS}


def test_deciding_hooks_all_name_real_registered_hooks() -> None:
    """A typo in DECIDING_HOOKS silently protects nothing."""
    unknown = DECIDING_HOOKS - _registered_hook_names()
    assert not unknown, (
        f"DECIDING_HOOKS names hooks that are not registered in HOOK_TOOLS: {sorted(unknown)}"
    )
