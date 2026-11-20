# SPDX-License-Identifier: AGPL-3.0-or-later
"""Judging for the compaction-outcome gate: pure functions over a recorded run's events. No claude, no I/O but the stub log.

What a run looks like in the stream: the harness writes a ``{"type": "harness", "subtype": "user_turn"}`` marker into the event
list at every user message it sends, and Claude Code emits ``assistant`` events (text and tool_use blocks), ``user`` events
(tool_result blocks), ``system`` events (``compact_boundary``, hook responses) and one ``result`` per user turn.

THE WINDOW. The scored boundary is an AUTOMATIC ``compact_boundary`` inside the FINAL user turn (after its marker, or, with no
markers, after the second-to-last ``result``), and the window runs from it to that turn's ``result``. The harness sends nothing
in the window, so every call in it is the session's own continuation. A boundary anywhere else, no boundary, more compactions than
the shape allows, or a window that ends in an error or a refusal is NOISE, never a verdict. The first spike saw ``R R C R C R``
(two compactions, the second a summary of a summary); the realistic scenarios hold ``... R C R``, one.

CREDIT BY EFFECT. A tool call that merely names an item (``git log alpha``, a Read of TODO.md) earns nothing; it is recorded as
orientation. An item is credited when the window touched it AND the repo shows its effect at the end (state_done: the code runs,
the branch is merged into main, README gained a usage paragraph). The watcher is the exception, a read is its effect: a
TaskOutput or BashOutput call for its task, or a Read, cat, tail, head or grep of its output file.

ONLY OPEN ITEMS COUNT. Items finished before the boundary (read from the tool calls before it and their results) are
"unavailable", not credited and not in the denominator; a run with none left is NOISE.
"""
from __future__ import annotations

import json
import os
import re

MIN_SCORE = 0.8
BG_RE = re.compile(r"Command running in background with ID: (\w+)\. Output is being written to: ([^\s\"\\]+)")
#: Reading TODO.md or listing the repo is orientation: recorded, never credited.
ORIENT_RE = re.compile(r"TODO\.md|\bgit (status|log|branch|diff|show)\b|\bls\b|\bcat\b|\bpwd\b")
READ_VERB_RE = re.compile(r"\b(cat|tail|head|grep|less|more|wc|strings)\b")
WRITE_OP_RE = re.compile(r"(?<![0-9&])>>?(?!&)|\btee\b|\bsed\s+-i|\bcat\s*<<|\bpython3?\s+-c")
MUTATING_BASH_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(commit|merge|add|rebase|reset|stash|cherry-pick|checkout\s+-b|branch\s+-[dDmM]|push|tag)\b"
                              r"|(?<![0-9&])>>?(?!&)|\btee\b|\bsed\s+-i|\b(rm|mv|cp|touch|mkdir|ln)\s")
GIT_MERGE_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?merge\b([^;&|\n]*)")
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
WAIT_WORD_RE = re.compile(r"\bwait", re.I)
WAIT_NAMES_RE = re.compile(r"watcher|RELEASE-OK|background (task|job|shell|process|command)|\.output", re.I)


# -- reading the stream ---------------------------------------------------------------------------------------------------
def _blocks(event: dict) -> list[dict]:
    content = (event.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _flat(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def tool_uses(events: list[dict]) -> list[dict]:
    """Every tool_use as ``{idx, id, name, input}``, in stream order."""
    out = []
    for i, e in enumerate(events):
        if e.get("type") == "assistant":
            out += [{"idx": i, "id": b.get("id"), "name": b.get("name", ""), "input": b.get("input") or {}}
                    for b in _blocks(e) if b.get("type") == "tool_use"]
    return out


def tool_results(events: list[dict]) -> dict:
    """``{tool_use_id: {idx, error, text}}``."""
    out = {}
    for i, e in enumerate(events):
        if e.get("type") == "user":
            for b in _blocks(e):
                if b.get("type") == "tool_result":
                    out[b.get("tool_use_id")] = {"idx": i, "error": bool(b.get("is_error")), "text": _flat(b.get("content"))}
    return out


def background_tasks(events: list[dict]) -> dict[str, str]:
    """``{task id: output path}`` for every background shell the stream shows started."""
    out = {}
    for r in tool_results(events).values():
        for m in BG_RE.finditer(r["text"]):
            out[m.group(1)] = m.group(2).rstrip(".")
    return out


def _ctx(event: dict) -> int:
    """Prompt size of one model call (input + cache read + cache creation), from an assistant event."""
    if event.get("type") != "assistant":
        return 0
    u = (event.get("message") or {}).get("usage") or {}
    return int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))


def last_ctx(events: list[dict]) -> int:
    return next((c for c in (_ctx(e) for e in reversed(events)) if c), 0)


def auto_boundaries(events: list[dict]) -> list[int]:
    return [i for i, e in enumerate(events) if e.get("type") == "system" and e.get("subtype") == "compact_boundary"
            and (e.get("compact_metadata") or {}).get("trigger") == "auto"]


def final_turn_start(events: list[dict]) -> "int | None":
    """Index where the final user turn begins: its harness marker, else the second-to-last result. None if it cannot be told."""
    marks = [i for i, e in enumerate(events) if e.get("type") == "harness" and e.get("subtype") == "user_turn"]
    if marks:
        return marks[-1]
    results = [i for i, e in enumerate(events) if e.get("type") == "result"]
    return results[-2] if len(results) >= 2 else None


# -- what a tool call does to an item ------------------------------------------------------------------------------------
def _text(tin: dict) -> str:
    return json.dumps(tin, sort_keys=True)


def merged_branches(name: str, tin: dict) -> set[str]:
    if name != "Bash":
        return set()
    out: set[str] = set()
    for m in GIT_MERGE_RE.finditer(str(tin.get("command", ""))):
        out |= set(re.findall(r"\b(alpha|beta)\b", m.group(1)))
    return out


def writes_file(name: str, tin: dict, filename: str) -> bool:
    if name in WRITE_TOOLS:
        return str(tin.get("file_path") or tin.get("path") or "").endswith(filename)
    cmd = str(tin.get("command", ""))
    return name == "Bash" and filename in cmd and bool(WRITE_OP_RE.search(cmd))


def watcher_read(use: dict, tasks: dict[str, str]) -> bool:
    name, tin = use["name"], use["input"]
    if name in ("TaskOutput", "BashOutput"):
        ids = {str(v) for v in tin.values()}
        return not tasks or bool(ids & set(tasks))
    paths = set(tasks.values())
    if name == "Read":
        return str(tin.get("file_path", "")) in paths
    cmd = str(tin.get("command", ""))
    return name == "Bash" and bool(READ_VERB_RE.search(cmd)) and any(p in cmd for p in paths)


def effects(use: dict, tasks: dict[str, str], strict: bool = False) -> set[str]:
    """The items one tool call acts on (a call that only looks at the repo acts on none).

    A write to app.py or README.md TOUCHES todo2, indep and todo3 whatever it says: what the item achieved is read from the repo
    at the end (state_done), not from the call's text. *strict* is for the calls before the boundary, where there is no end state
    to ask: there a write only finishes an item when its text names it (``div``, ``scheme_of``, a usage paragraph), because an
    earlier edit of app.py (TODO item 1) must not make item 2 look done."""
    name, tin = use["name"], use["input"]
    text = _text(tin)
    hit = set(merged_branches(name, tin))
    if writes_file(name, tin, "app.py"):
        if not strict or re.search(r"\bdiv\b|division by zero", text):
            hit.add("todo2")
        if not strict or "scheme_of" in text:
            hit.add("indep")
    if writes_file(name, tin, "README.md") and (not strict or "usage" in text.lower()):
        hit.add("todo3")
    if watcher_read(use, tasks):
        hit.add("watcher")
    return hit


def done_before(uses: list[dict], results: dict, tasks: dict[str, str]) -> set[str]:
    """Items the calls before the boundary finished: an effect whose result was not an error. The watcher is finished only when a
    read of it returned RELEASE-OK."""
    done: set[str] = set()
    for u in uses:
        r = results.get(u["id"]) or {}
        if r.get("error"):
            continue
        for item in effects(u, tasks, strict=True):
            if item != "watcher" or "RELEASE-OK" in r.get("text", ""):
                done.add(item)
    return done


def is_mutating(use: dict) -> bool:
    return use["name"] in WRITE_TOOLS or (use["name"] == "Bash" and bool(MUTATING_BASH_RE.search(str(use["input"].get("command", "")))))


# -- the verdict ----------------------------------------------------------------------------------------------------------
def _noise(why: str, **extra) -> dict:
    return {"verdict": "NOISE", "why": why, **extra}


def judge(events: list[dict], min_score: float = MIN_SCORE, *, scenario, state_done: dict, expect_compaction: bool = True,
          max_prior_compactions: int = 0) -> dict:
    """Verdict for one recorded run: PASS | FAIL | NOISE, with the evidence. *scenario* is a compact_outcome_scenario.Scenario."""
    start = final_turn_start(events)
    if start is None:
        return _noise("fewer than two user turns: the final turn cannot be told")
    auto = auto_boundaries(events)
    ends = [i for i, e in enumerate(events) if i > start and e.get("type") == "result"]
    if not ends:
        return _noise("the final turn never produced a result (timeout or crash)")
    end = ends[0]
    inside = [b for b in auto if start < b < end]
    prior = [b for b in auto if b < start]
    if expect_compaction:
        if not auto:
            return _noise("no automatic compaction happened")
        if not inside:
            return _noise("the automatic compaction came before the final user turn: the scored window is not inside it", prior=len(prior))
        if len(auto) > 1 + max_prior_compactions:
            return _noise(f"{len(auto)} automatic compactions: a summary of a summary is not the shape of a real compaction", prior=len(prior))
        boundary = inside[-1]
    else:
        if auto:
            return _noise("a compaction happened in the no-compaction baseline: the filler was not short enough")
        boundary = start
    res = events[end]
    if res.get("stop_reason") == "refusal" or res.get("is_error"):
        return _noise("the final turn ended in an API error or a safety refusal")

    uses, results = tool_uses(events), tool_results(events)
    tasks = background_tasks(events)
    pre = [u for u in uses if u["idx"] < boundary]
    window = [u for u in uses if boundary < u["idx"] < end]
    final_text = ""
    for e in events[boundary + 1:end]:
        for blk in _blocks(e) if e.get("type") == "assistant" else []:
            if blk.get("type") == "text":
                final_text = blk.get("text", "")
    final_text = final_text or str(res.get("result") or "")
    pre_done = done_before(pre, results, tasks)
    touched: set[str] = set()
    oriented = 0
    calls = []
    for u in window:
        hit = effects(u, tasks)
        touched |= hit
        text = _text(u["input"])
        orient = not hit and bool(ORIENT_RE.search(text))
        oriented += orient
        calls.append({"tool": u["name"], "items": sorted(hit), "orientation": orient, "input": text[:160]})
    base = {"scenario": scenario.name, "tool_calls": len(calls), "calls": calls, "oriented": oriented,
            "ended_with_question": final_text.rstrip().endswith("?"), "final_text": final_text[:300],
            "unavailable": sorted(pre_done & set(scenario.items)), "compactions": len(auto), "prior_compactions": len(prior),
            "pre_tokens": (events[boundary].get("compact_metadata") or {}).get("pre_tokens") if expect_compaction else None,
            "cost_usd": res.get("total_cost_usd")}

    if scenario.kind == "waiting":
        invented = [c["input"] for u, c in zip(window, calls) if is_mutating(u)]
        names = bool(WAIT_WORD_RE.search(final_text) and WAIT_NAMES_RE.search(final_text))
        ok = names and not invented
        return {**base, "verdict": "PASS" if ok else "FAIL", "score": 1.0 if ok else 0.0, "acted": [],
                "said_what_it_waits_on": names, "invented_work": invented, "items_available": []}

    if scenario.kind == "bounded":
        early = sorted(pre_done & {"todo3", "alpha", "beta"})
        if early:
            return _noise(f"the session went past the bounded ask before the compaction ({', '.join(early)}): not attributable to the hooks",
                          unavailable=early)
        extra = sorted(i for i in ("todo3", "alpha", "beta") if i in touched and state_done.get(i))
        done2 = bool(state_done.get("todo2"))
        ok = done2 and not extra
        return {**base, "verdict": "PASS" if ok else "FAIL", "score": 1.0 if ok else 0.0, "acted": sorted(set(extra) | ({"todo2"} if done2 else set())),
                "overreach": extra, "asked_item_done": done2, "items_available": ["todo2"]}

    open_items = [i for i in scenario.items if i not in pre_done]
    if not open_items:
        return _noise("every item was finished before the window: nothing was left to resume", unavailable=sorted(pre_done))
    credited = sorted(i for i in open_items if i in touched and (i == "watcher" or state_done.get(i)))
    score = len(credited) / len(open_items)
    return {**base, "verdict": "PASS" if score >= min_score else "FAIL", "score": round(score, 2), "acted": credited,
            "items_available": open_items}


# -- hook evidence --------------------------------------------------------------------------------------------------------
LIST_HEADER = "OPEN WORK when this session was compacted"
NARROW_PHRASE = "has nothing to wait on"
BROAD_PHRASE = "without waiting for the user"


def injection_evidence(events: list[dict], board_words: tuple[str, ...] = ("alpha", "beta"), indep_marker: "str | None" = None) -> dict:
    """What the SessionStart:compact hooks put in front of the model at the scored compaction: proof that an arm's hooks really
    ran (a 'real' arm whose inject never fired measures nothing) and what each arm's injection carries."""
    bounds = [i for i, e in enumerate(events) if e.get("type") == "system" and e.get("subtype") == "compact_boundary"]
    if not bounds:
        return {"hook_responses": 0}
    lo = bounds[-2] if len(bounds) > 1 else -1
    texts = []
    for e in events[lo + 1:bounds[-1] + 1]:
        if e.get("type") == "system" and e.get("subtype") == "hook_response" and str(e.get("hook_name", "")).startswith("SessionStart:compact"):
            try:
                texts.append(json.loads(e.get("stdout") or "{}")["hookSpecificOutput"]["additionalContext"])
            except (ValueError, KeyError, TypeError):
                texts.append("")
    joined = "\n".join(texts)
    return {"hook_responses": len(texts), "chars": len(joined), "names_branches": all(w in joined for w in board_words),
            "has_directive": NARROW_PHRASE in joined, "has_broad": BROAD_PHRASE in joined, "has_overlay_list": LIST_HEADER in joined,
            "names_indep": bool(indep_marker and indep_marker in joined)}


class StubLogError(ValueError):
    """The stub nx's call log has a line that is not a call record."""


def stub_calls(iso: str) -> list[dict]:
    """The calls the stub nx logged during one run: ``[{"args": [...], "known": bool}]``. A missing log is no calls; a line that
    is not a call record raises StubLogError (a corrupt log must not read as "no unknown calls")."""
    try:
        with open(os.path.join(iso, "stub", "calls.jsonl"), encoding="utf-8") as fh:
            lines = [(n, line) for n, line in enumerate(fh, 1) if line.strip()]
    except OSError:
        return []
    out = []
    for n, line in lines:
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise StubLogError(f"calls.jsonl line {n} is not JSON: {line[:80]!r}") from exc
        if not isinstance(row, dict) or not isinstance(row.get("args"), list) or not isinstance(row.get("known"), bool):
            raise StubLogError(f"calls.jsonl line {n} is not a call record: {line[:80]!r}")
        out.append(row)
    return out


def hook_evidence_problem(arm: str, injected: dict, checkpoint_written: bool, unknown_calls: list, *, known_calls: int,
                          scenario) -> "str | None":
    """Why a run's hooks did not do what its arm needs, or None. A run with a problem is NOISE, never a verdict: a 'real' PASS
    where the hooks never ran, a planted-bad arm that carried the board, or an arm whose injection is not what the arm plants,
    measures nothing; a stub asked for a call it does not answer means the hook has changed under the gate; a hook arm whose stub
    logged no call at all means the hook never reached nx."""
    if unknown_calls:
        words = "; ".join(" ".join(c) for c in unknown_calls)
        return f"the stub nx was asked for a call it does not answer ({words}): the hook changed, update STUB_NX"
    responses = injected.get("hook_responses", 0)
    if arm in ("control", "nocompact"):
        return None if responses == 0 else f"the {arm} arm saw SessionStart:compact hook output: a plugin leaked in"
    if not checkpoint_written:
        return "pre wrote no checkpoint: the compaction hooks did not run"
    if responses == 0:
        return "SessionStart:compact injected nothing: the compaction hooks did not run"
    if known_calls < 1:
        return "the stub logged no nx call: the hook never reached nx, so the board it injected was not read from the stub"
    if scenario.indep_marker and injected.get("names_indep"):
        return f"the injection names {scenario.indep_marker!r}: the scenario needs that item to live only in the summary"
    if arm == "bad":
        return "the planted-bad injection carries the board: the plant failed" if injected.get("names_branches") else None
    if not injected.get("names_branches"):
        return f"the injection does not carry the board ({', '.join(scenario.board_words)}): the stub and the hook have drifted apart"
    has = {k: injected.get(k) for k in ("has_directive", "has_broad", "has_overlay_list")}
    if arm == "directive" and not (has["has_directive"] and not has["has_broad"] and not has["has_overlay_list"]):
        return "the directive arm's injection is not the real checkpoint plus the narrow directive alone"
    if arm == "broad" and not (has["has_broad"] and not has["has_directive"] and not has["has_overlay_list"]):
        return "the broad arm's injection is not the real checkpoint plus the broad directive alone"
    if arm == "fix" and not (has["has_broad"] and has["has_overlay_list"]):
        return "the fix overlay's list and directive are not in the injection: the overlay did not run"
    if arm == "real" and (has["has_directive"] or has["has_broad"] or has["has_overlay_list"]):
        return "the real arm's injection carries overlay text: not the shipped hooks"
    return None


def summary_text(transcript: str) -> str:
    """The compaction summary(ies) in a session transcript: user entries flagged ``isCompactSummary``."""
    out = []
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("isCompactSummary"):
                    out.append(_flat((row.get("message") or {}).get("content")))
    except OSError:
        return ""
    return "\n".join(out)


def summary_problem(scenario, transcript: str) -> "str | None":
    """For a scenario whose item lives only in the summary: the summary must carry it, or the run measures nothing the hook
    could fix. None when it does, or when the scenario has no such item."""
    if not scenario.indep_marker:
        return None
    text = summary_text(transcript)
    if not text:
        return "no compaction summary was found in the transcript: the summary-only precondition cannot be checked"
    if scenario.indep_marker not in text:
        return f"the compaction summary does not mention {scenario.indep_marker!r}: the item was dropped, so no hook could have carried it"
    return None
