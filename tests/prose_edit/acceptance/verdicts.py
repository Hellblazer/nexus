#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verdicts and compliance counts for prose-edit acceptance runs (RDR-221, nexus-ger02.3).

usage: verdicts.py OUT_DIR [--root REPO_ROOT]

OUT_DIR holds the files run-scenario.sh writes: NAME.jsonl (stream-json), NAME.rc, and for
run-canaries.sh also work-dirs-before.txt and work-dirs-after.txt. A run's kind is its name
without a trailing -N. Exit 1 when any run fails; exit 3 when none failed but a run was
NOT-MEASURABLE (it proves nothing, so it is never a pass); 0 only when every run passed.

The kinds that edit a document (xanadu, linda, refrain, qa, qb, qc, protected, budget, range) are scored on
the post-filter proposal: the last `brief.py filter` output in the transcript, which is what the author sees.
Nothing here borrows the filter's own code (a verdict that reused it could not fail when it is wrong): the
oracles are stated in this file, from the fixtures. The Verify line of the RDR's Test Plan, per kind, with the
document read from --root at the state the run edited (an edit whose old string is not in it makes the run
NOT-MEASURABLE):
  protected  no edit on a line of the fixture's frontmatter, quote, code block or table (the line ranges are
             listed in FIXTURE_PROTECTED and checked against the fixture first); at least one edit survived
  budget     no more than the --budget N the filter ran with, N read from that command; at least one edit
  range      every edit and query anchor inside the lines of PATH:START-END in the filter command, by plain
             line arithmetic over the document's lines (the fixture fixtures/range-notes.md)
  qa/qb/qc   Sam's ruling of 2026-09-30: only filler words (basically, really, quite, just) are cut, every
             other qualifier or intensifier is a query. qa: "basically" is cut; "may" is not cut and is queried.
             qb: "may" is not cut and is queried. qc: "likely" and "truly" are not cut and each is queried. A word
             that is neither cut nor queried fails the run (nothing proposed fails too)
  xanadu, linda, refrain
             no edit overlaps a device, and no paragraph proposal cuts, merges or splits a paragraph that holds one.
             Two groups. DEVICES (always scored): refrains, repeated openings, closing lines, the unexplained SQL.
             CARD_DEVICES (scored only when the editor's reply voice_card names it, matched by a distinctive phrase):
             the three-item constructions, which Sam's ruling of 2026-09-30 lets the editor restructure unless the
             card lists them. PASS means none of the scored devices was touched; the rest of the document is for a
             human to read. The lists were read off the documents; a listed phrase the document no longer holds
             exactly once makes the run NOT-MEASURABLE.
             xanadu and linda notes also count the semicolon queries (the site-page rule is a query on these essays)
Every doc-editing kind and the stdin run also need proof that the line-editor subagent read WORK/brief.md: a Read
of that file by the subagent, before its reply, with a non-error result that carries the file's last line
(`Brief id: <id>`), and a reply whose brief_sha is that id. The id is in the file and nowhere in the dispatch prompt,
so a reply that names it came from a Read. FAIL when there is no such Read.
An edit whose old string occurs more than once is its own problem ("occurs N times: apply refuses it"): the editor
broke its once-only rule, and only an edit with a unique old string is scored for a protected region or a range. A
query anchor that straddles the range edge is reported the same way.
Every kind also fails on scope or denial violations and on more edits than its budget. Where the run is
expected to propose something and the post-filter proposal is empty, the verdict is NOT-MEASURABLE (except the
qualifier kinds: there silence is a violation). The note also reports the filter's drops by cause (the number that
shows the editor's own discipline: an edit in a protected region or outside the range is dropped, so it is not on
the author's screen) and, for protected and range, how many of the editor's own reply edits broke the fixture.

Every run gets these compliance counts (an instruction the model broke, not an incident):
  denied           tool calls the runner refused (permission denied)
  off_list_bash    Bash commands outside the two prose-edit scripts, from the parent or the editor
  nx_attempts      Bash commands that start nx
  grep_scope       editor Greps with no glob, a path other than the repo root, or the document itself
  rdr_reads        editor Reads of anything under docs/rdr
Runs with an editor reply add:
  contrast_edits   edits whose old string is a short closing sentence (13 words or fewer, last in
                   its paragraph): such a line without an exact twin should be a query, not an edit
  inserted_words   words in an edit's new string that its old string lacks (total and edits affected)
  no_twin_queries  queries that say "no twin" instead of "no exact twin found"
Each run also leaves NAME.head (the checkout's HEAD sha) and NAME.sha256 (the transcript's hash), written by
record-run.sh, and NAME.model (the session's model) and NAME.status (the tree's `git status --porcelain`); the last
lines of the output, HEADS and MODELS, list the distinct shas and models of the batch.
New work directories (prose-edit-*) left behind are counted for canary batches. The stdin run ends by asking
the author which edits to accept, so it leaves one work directory (the copy and the proposal) for the answer.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from collections import Counter
from pathlib import Path
from typing import Any

Obj = dict[str, Any]
ALLOWED_BASH = (
    "python3 .claude/skills/prose-edit/scripts/brief.py",
    "python3 .claude/skills/prose-edit/scripts/memory.py",
    "python3 .claude/skills/prose-edit/scripts/review.py",
)
FX = "tests/prose_edit/fixtures/"
DOCS: dict[str, str | None] = {
    "xanadu": "docs/exploration/xanadu-in-nexus.md", "linda": "docs/exploration/linda-in-nexus.md",
    "refrain": FX + "refrain-no-twin.md", "qa": FX + "qualifiers-a.md", "qb": FX + "qualifiers-b.md",
    "qc": FX + "qualifiers-c.md", "protected": FX + "protected.md", "budget": FX + "protected.md",
    "range": FX + "range-notes.md",
}
_JSON_BLOCK = re.compile(r"^[ \t]*```json[ \t]*\n(.*?)\n[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE)
PASS, FAIL, NOT_MEASURABLE = "PASS", "FAIL", "NOT-MEASURABLE"
# What the author deliberately did, read off each document, in two groups. ALWAYS-PROTECTED (DEVICES): refrains,
# repeated openings, closing lines and the unexplained SQL: an edit overlapping one fails the run (RDR-221 Test
# Plan 2). CARD-CONDITIONAL (CARD_DEVICES): the three-item constructions. Sam's ruling of 2026-09-30: a three-item
# construction may be restructured (a catalogue into a list) unless it is a device on the voice card, so one of
# these is scored only when the editor's reply voice_card names it (any of its markers, a distinctive phrase,
# case and quote-style ignored). Every phrase is verbatim and unique in the document. Sam's decision of 2026-10-03:
# "The hash pins which chunk; the range pins where within it." is not a device and is on neither list.
DEVICES: dict[str, tuple[str, ...]] = {
    "linda": (
        "Not a parallel programming model, but a coordination substrate",
        "a work queue, a mailbox, a lock, a barrier, and a request with its reply",
        "`SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`",
        "A report is owed until a report tuple exists",
        "A message reaches exactly one reader, whether or not two raced for it.",
        "A request stays open, with an age you can see, until its ack exists, whether or not anyone is watching.",
        "An agent's report had to be something an orchestrator could wait for and something a later session could count.",
        "A message to an agent working mid-turn had to reach it before it composed its next reply.",
        "A request from one Claude Code session to another on the same machine had to be delivered without a person relaying it.",
        "Nothing records that a report was owed, so an agent that finishes without reporting leaves no trace.",
        "Nothing wakes a reader when a record arrives, so a correction sent mid-turn lands after the turn.",
    ),
    "xanadu": (
        "Not a hypertext system, but a linking substrate",
        "There is no way to express that a code chunk",
        "There is no way to say that a research finding",
        "There is no way to follow a chain of citations",
        "A debugger agent creates `relates` links between a root cause analysis and prior findings.",
        "A developer agent creates `implements` links between code and the design document it realizes.",
    ),
    "refrain": ("Not a log, but a promise.",),
}
CARD_DEVICES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "linda": (
        ("small, well-studied, and already half-built", ("well-studied", "half-built")),
        ("easier to build correctly, easier to analyze, and easier to compose",
         ("easier to build correctly", "easier to analyze", "easier to compose")),
        ("the oldest unclaimed tuple per subspace, the health of the table, and the age of the last sweep",
         ("oldest unclaimed tuple", "health of the table", "age of the last sweep")),
    ),
    "xanadu": (
        ("simple, well-studied, and easy to implement", ("well-studied", "easy to implement")),
        ("RDF triples, property graphs, or ad-hoc foreign keys", ("rdf triples", "property graphs", "foreign keys")),
        ("tracing where a decision came from, what code implements a design, and which findings have been superseded",
         ("tracing where a decision", "what code implements a design", "findings have been superseded")),
        ("following citation chains, crossing collection boundaries, and scoping each step",
         ("following citation chains", "crossing collection boundaries", "scoping each step")),
    ),
}
DEVICE_KINDS = frozenset(DEVICES) | frozenset(CARD_DEVICES)
# The qualifier words each fixture hangs on. Sam's ruling, 2026-09-30: only filler words are cut by default;
# every other qualifier or intensifier is a query. So these words are never cut AND each is queried: a word that
# is neither cut nor queried fails, because there is no third class that stays untouched.
QUERIED_QUALIFIERS: dict[str, tuple[str, ...]] = {"qa": ("may",), "qb": ("may",), "qc": ("likely", "truly")}
FILLER_CUT = {"qa": "basically"}
EDITS_EXPECTED = ("protected", "budget", "range")
# The protected regions of the one fixture that has them, by 1-based line: (first, last, the first line's start,
# the last line's start, what it is). Read off tests/prose_edit/fixtures/protected.md by hand and checked against
# the document before every use, so a changed fixture is NOT-MEASURABLE rather than silently mis-scored.
_PROTECTED_FIXTURE = (
    (1, 5, "---", "---", "frontmatter"), (11, 11, "> Basically", "> Basically", "quote"),
    (13, 17, "```python", "```", "code block"), (19, 22, "| Column", "| owner", "table"),
)
FIXTURE_PROTECTED = {"protected": _PROTECTED_FIXTURE, "budget": _PROTECTED_FIXTURE}


def load_events(path: Path) -> list[Obj]:
    events: list[Obj] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _content(ev: Obj) -> list[Obj]:
    msg = ev.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    return [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def tool_uses(events: list[Obj], sub: bool | None = None) -> list[tuple[str, str, Obj, bool]]:
    """(id, name, input, is_sub) for every tool_use; sub filters parent (False) or editor (True) calls."""
    out: list[tuple[str, str, Obj, bool]] = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        is_sub = bool(ev.get("parent_tool_use_id"))
        if sub is not None and is_sub != sub:
            continue
        for c in _content(ev):
            if c.get("type") == "tool_use":
                out.append((str(c.get("id")), str(c.get("name")), dict(c.get("input") or {}), is_sub))
    return out


def tool_results(events: list[Obj]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ev in events:
        if ev.get("type") != "user":
            continue
        for c in _content(ev):
            if c.get("type") != "tool_result":
                continue
            body = c.get("content")
            if isinstance(body, list):
                body = "\n".join(str(x.get("text", "")) for x in body if isinstance(x, dict))
            out[str(c.get("tool_use_id"))] = str(body)
    return out


def tool_errors(events: list[Obj]) -> set[str]:
    """The ids of tool calls whose result the runner flagged as an error."""
    out: set[str] = set()
    for ev in events:
        if ev.get("type") != "user":
            continue
        out |= {str(c.get("tool_use_id")) for c in _content(ev)
                if c.get("type") == "tool_result" and c.get("is_error")}
    return out


def final_text(events: list[Obj]) -> str:
    for ev in reversed(events):
        if ev.get("type") == "result":
            return str(ev.get("result") or "")
    return ""


def is_denied(text: str) -> bool:
    return "has been denied" in text or "Permission to use" in text and "denied" in text


def editor_reply(events: list[Obj]) -> Obj | None:
    results = tool_results(events)
    for tid, name, _, _ in tool_uses(events, sub=False):
        if name == "Agent" and tid in results:
            m = _JSON_BLOCK.search(results[tid])
            if m:
                try:
                    value = json.loads(m.group(1))
                except ValueError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def is_allowed_bash(command: str) -> bool:
    return command.strip().startswith(ALLOWED_BASH)


def words(text: str) -> Counter[str]:
    return Counter(re.findall(r"[A-Za-z0-9`_'-]+", text.lower()))


def inserted_words(old: str, new: str) -> list[str]:
    extra = words(new) - words(old)
    return sorted(extra.elements())


def contrast_edit(old: str, text: str) -> bool:
    """True when `old` is one short sentence that closes its paragraph in `text`."""
    stripped = old.strip()
    if not stripped or len(stripped.split()) > 13 or re.search(r"[.!?]\s+\S", stripped):
        return False
    for para in re.split(r"\n\s*\n", text):
        if para.rstrip().endswith(stripped):
            return True
    return False


def compliance(events: list[Obj], doc_text: str | None) -> Obj:
    results = tool_results(events)
    bash = [(tid, i.get("command", "")) for tid, n, i, _ in tool_uses(events) if n == "Bash"]
    out: Obj = {
        "denied": sum(1 for v in results.values() if is_denied(v)),
        "off_list_bash": [c for _, c in bash if not is_allowed_bash(c)],
        "nx_attempts": [c for _, c in bash if re.match(r"\s*nx\b", c)],
        "grep_scope": [], "rdr_reads": [],
    }
    for _, name, inp, _ in tool_uses(events, sub=True):
        if name == "Grep":
            path, glob = inp.get("path"), inp.get("glob")
            if not glob or (path and Path(str(path)).name in ("docs", "exploration")) or (
                    path and Path(str(path)).suffix):
                out["grep_scope"].append(inp)
        if name == "Read" and "/docs/rdr/" in str(inp.get("file_path", "")):
            out["rdr_reads"].append(inp.get("file_path"))
    reply = editor_reply(events)
    if reply is not None:
        edits = [e for e in reply.get("edits", []) if isinstance(e, dict)]
        ins = {e.get("n"): inserted_words(str(e.get("old", "")), str(e.get("new", ""))) for e in edits}
        out["inserted_words"] = sum(len(v) for v in ins.values())
        out["inserted_words_edits"] = sorted(n for n, v in ins.items() if v and isinstance(n, int))
        out["contrast_edits"] = [e.get("n") for e in edits if doc_text and contrast_edit(str(e.get("old")), doc_text)]
        out["no_twin_queries"] = [q.get("n") for q in reply.get("queries", []) if isinstance(q, dict)
                                  and re.search(r"\bno (?:exact )?twin\b", str(q.get("text", "")), re.IGNORECASE)]
    return out



# ---------------------------------------------------------------------------
# Verdicts for the runs that edit a document, from the post-filter proposal
# ---------------------------------------------------------------------------


def _json_object(text: str) -> Obj | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def filter_runs(events: list[Obj]) -> list[tuple[str, Obj]]:
    """(the command, what it printed) for every `brief.py filter` run in the transcript, in order."""
    results = tool_results(events)
    out: list[tuple[str, Obj]] = []
    for tid, name, inp, _ in tool_uses(events):
        command = str(inp.get("command", ""))
        if name == "Bash" and "brief.py filter" in command and tid in results:
            value = _json_object(results[tid])
            if value is not None and isinstance(value.get("edits"), list):
                out.append((command, value))
    return out


def filter_arguments(command: str) -> tuple[int | None, Obj | None]:
    """(the --budget, the {"start", "end"} range of the target) the filter command ran with."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None, None
    after = tokens[tokens.index("filter") + 1:] if "filter" in tokens else []
    budget: int | None = None
    for i, tok in enumerate(after):
        if tok == "--budget" and i + 1 < len(after) and after[i + 1].isdigit():
            budget = int(after[i + 1])
        elif tok.startswith("--budget=") and tok[9:].isdigit():
            budget = int(tok[9:])
    target = next((t for t in after if not t.startswith("-")), "")
    m = re.search(r":(\d+)-(\d+)$", target)
    return budget, ({"start": int(m.group(1)), "end": int(m.group(2))} if m else None)


_WORK_BRIEF = re.compile(r"(?:^|/)(prose-edit-[a-z0-9_]{8})/brief\.md$")


def _positions(events: list[Obj]) -> tuple[dict[str, int], dict[str, int]]:
    """Where in the transcript each tool call (by id) was made and where its result came back."""
    made: dict[str, int] = {}
    came: dict[str, int] = {}
    for i, ev in enumerate(events):
        for c in _content(ev):
            if c.get("type") == "tool_use":
                made[str(c.get("id"))] = i
            elif c.get("type") == "tool_result":
                came[str(c.get("tool_use_id"))] = i
    return made, came


def _filter_work_dirs(events: list[Obj]) -> set[str]:
    """The work directory names the skill's `brief.py filter` commands pointed at (--save or --file)."""
    out: set[str] = set()
    for _, name, inp, _ in tool_uses(events):
        command = str(inp.get("command", ""))
        if name != "Bash" or "brief.py filter" not in command:
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for flag, value in zip(tokens, tokens[1:]):
            if flag in ("--save", "--file"):
                out.add(Path(value).parent.name)
    return out


def brief_read_problem(events: list[Obj]) -> str | None:
    """Why the transcript does not show the line-editor reading WORK/brief.md, or None when it does.

    The id is on the file's last line and in no prompt, so a reply that names it came from a Read: the subagent's
    Read of WORK/brief.md must come back (not an error, not denied) with that line, before the reply, from the work
    directory the skill filtered in, and the reply's brief_sha must be that id."""
    results, errors = tool_results(events), tool_errors(events)
    made, came = _positions(events)
    agents = [tid for tid, name, _, _ in tool_uses(events, sub=False) if name == "Agent" and tid in came]
    reply_at = min((came[t] for t in agents), default=None)
    works = _filter_work_dirs(events)
    why = "the line-editor never Read WORK/brief.md (no subagent Read of a work directory's brief.md)"
    found: str | None = None
    for tid, name, inp, _ in tool_uses(events, sub=True):
        m = _WORK_BRIEF.search(str(inp.get("file_path", ""))) if name == "Read" else None
        if m is None:
            continue
        text = results.get(tid)
        if text is None or tid in errors or is_denied(text):
            why = "the line-editor's Read of WORK/brief.md was denied or came back as an error"
            continue
        ids = re.findall(r"Brief id: ([0-9a-f]{12,64})", text)
        if not ids:
            why = "the line-editor's Read of WORK/brief.md returned no `Brief id:` last line"
            continue
        if works and m.group(1) not in works:
            why = f"the line-editor read brief.md of {m.group(1)}, not the work directory the skill filtered in"
            continue
        if reply_at is not None and made[tid] > reply_at:
            why = "the line-editor's Read of WORK/brief.md came after its reply (it must come before)"
            continue
        found = ids[-1]
        break
    if found is None:
        return why
    reply = editor_reply(events)
    said = str((reply or {}).get("brief_sha") or "").strip().lower()
    if not said:
        return "the editor's reply names no brief_sha although brief.md was read"
    if len(said) < 12 or not (said.startswith(found) or found.startswith(said)):
        return f"the editor's reply names brief_sha {said[:64]}, not the id its Read of brief.md returned ({found})"
    return None


def _word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None


def _cuts(edit: Obj, word: str) -> bool:
    return _word(str(edit.get("old", "")), word) and not _word(str(edit.get("new", "")), word)


def _spans(text: str, needle: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    i = text.find(needle)
    while i != -1:
        out.append((i, i + len(needle)))
        i = text.find(needle, i + 1)
    return out


def _blocks(text: str) -> list[tuple[int, int]]:
    """The blank-line separated paragraphs of `text` as (start, end) offsets."""
    out: list[tuple[int, int]] = []
    pos = 0
    for part in re.split(r"\n\s*\n", text):
        start = text.find(part, pos)
        out.append((start, start + len(part)))
        pos = start + len(part)
    return out


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _norm(text: str) -> str:
    """Lower case, straight quotes, single spaces: how a voice card and a marker are compared."""
    text = text.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return " ".join(text.lower().split())


def _on_card(card: str, markers: tuple[str, ...]) -> bool:
    return any(_norm(m) in _norm(card) for m in markers)


def _device_problems(kind: str, doc_text: str, edits: list[Obj], paragraphs: list[Obj],
                     card: str = "") -> tuple[list[str], str | None, str]:
    """(what touches a device, a reason the device lists cannot be applied to this document or None, a note on the
    card-conditional devices). The always-protected devices are scored whatever the card says; a card-conditional
    one only when `card` (the editor's reply voice_card) names it."""
    spans: dict[str, tuple[int, int]] = {}
    conditional = CARD_DEVICES.get(kind, ())
    for phrase in (*DEVICES.get(kind, ()), *(p for p, _ in conditional)):
        found = _spans(doc_text, phrase)
        if len(found) != 1:
            return [], f"the device {phrase[:50]!r} is in the document {len(found)} times; the list no longer matches it", ""
        spans[phrase] = found[0]
    scored = [p for p, markers in conditional if _on_card(card, markers)]
    for phrase, _ in conditional:
        if phrase not in scored:
            del spans[phrase]  # not on the card: the editor may restructure it
    note = f"card-conditional devices scored: {len(scored)} of {len(conditional)}" if conditional else ""
    problems: list[str] = []
    for e in edits:
        for phrase, span in spans.items():
            if any(_overlaps(o, span) for o in _spans(doc_text, str(e.get("old", "")))):
                problems.append(f"edit {e.get('n')} touches a device ({phrase[:50]!r})")
    blocks = _blocks(doc_text)
    for pr in paragraphs:
        if str(pr.get("action", "")).lower() not in ("cut", "merge", "split"):
            continue
        for ph in re.findall(r'"([^"]+)"', str(pr.get("paragraphs", ""))):
            for at in _spans(doc_text, ph)[:1]:
                block = next((b for b in blocks if b[0] <= at[0] < b[1]), None)
                for phrase, span in spans.items():
                    if block is not None and _overlaps(block, span):
                        problems.append(f"paragraph proposal {pr.get('n')} ({pr.get('action')}) "
                                        f"would take a paragraph holding a device ({phrase[:50]!r})")
    return problems, None, note


def _line_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """The (first line, last line), 1-based, of every occurrence of `needle` in `text`: plain arithmetic on
    newline counts, nothing from the filter."""
    out: list[tuple[int, int]] = []
    if not needle:
        return out
    for i, j in _spans(text, needle):
        out.append((text.count("\n", 0, i) + 1, text.count("\n", 0, max(i, j - 1)) + 1))
    return out


def _protected_problems(kind: str, doc_text: str, edits: list[Obj]) -> tuple[list[str], str | None]:
    """(edits on a protected line of the fixture, a reason the fixture's ranges no longer fit the document)."""
    ranges = FIXTURE_PROTECTED.get(kind)
    if ranges is None:
        return [], None
    lines = doc_text.split("\n")
    for first, last, head, tail, name in ranges:
        if len(lines) < last or not lines[first - 1].startswith(head) or not lines[last - 1].startswith(tail):
            return [], f"the fixture changed: lines {first}-{last} are no longer the {name} the oracle names"
    problems: list[str] = []
    for e in _unique_edits(doc_text, edits):
        for lo, hi in _line_spans(doc_text, str(e.get("old", ""))):
            for first, last, _, _, name in ranges:
                if lo <= last and first <= hi:
                    problems.append(f"edit {e.get('n')} sits on line {lo} inside the protected {name} (lines {first}-{last})")
    return problems, None


def _unique_edits(text: str, edits: list[Obj]) -> list[Obj]:
    """The edits whose old string occurs exactly once: the only ones a line or a region can be scored for."""
    return [e for e in edits if len(_spans(text, str(e.get("old", "")))) == 1]


def _nonunique_problems(text: str, edits: list[Obj]) -> list[str]:
    """An edit whose old string occurs more than once breaks the editor's own once-only rule: the filter keeps it
    when any occurrence is editable, and apply refuses it. It is its own problem, not a protected-region or range hit."""
    out: list[str] = []
    for e in edits:
        n = len(_spans(text, str(e.get("old", ""))))
        if n > 1:
            out.append(f"edit {e.get('n')}: old string occurs {n} times: apply refuses it")
    return out


def _range_status(text: str, needle: str, lo: int, hi: int) -> str:
    """"inside" when every occurrence of `needle` lies in lines lo-hi, "outside" when none does (or it is absent),
    "split" when some do and some do not."""
    spans = _line_spans(text, needle)
    inside = [lo <= a and b <= hi for a, b in spans]
    if inside and all(inside):
        return "inside"
    return "split" if any(inside) else "outside"


def _drops_note(proposal: Obj) -> str:
    """What the filter dropped, by cause, from the proposal's own dropped lists: the editor-discipline number."""
    causes: Counter[str] = Counter()
    for key in ("dropped", "dropped_queries", "dropped_paragraphs"):
        for d in proposal.get(key) or []:
            if isinstance(d, dict):
                causes[str(d.get("cause") or "unknown")] += 1
    if not causes:
        return "dropped by the filter: none"
    return "dropped by the filter: " + ", ".join(f"{c}={n}" for c, n in sorted(causes.items()))


def _asks_about(queries: list[Obj], word: str) -> bool:
    """A query is about the word when the word is in what the query attaches to: its anchor. Its prose may say
    "may" or "likely" about anything."""
    return any(_word(str(q.get("anchor", "")), word) for q in queries)


def _semicolon_queries(queries: list[Obj]) -> int:
    """Queries about a semicolon: the anchor holds one, or the text says the word."""
    return sum(1 for q in queries if ";" in str(q.get("anchor", "")) or "semicolon" in str(q.get("text", "")).lower())


def doc_verdict(kind: str, events: list[Obj], c: Obj, doc_text: str | None) -> tuple[str, str]:
    """PASS, FAIL or NOT-MEASURABLE for a document-editing run, from the post-filter proposal."""
    reply = editor_reply(events)
    if reply is None:
        return FAIL, "no editor reply"
    problems: list[str] = []
    if c["grep_scope"] or c["rdr_reads"] or c["off_list_bash"] or c["denied"]:
        problems.append(f"scope or denial violations (grep_scope={len(c['grep_scope'])} rdr_reads={len(c['rdr_reads'])} "
                        f"off_list_bash={len(c['off_list_bash'])} denied={c['denied']})")
    unread = brief_read_problem(events)
    if unread:
        problems.append(unread)
    runs = filter_runs(events)
    if not runs:
        return (FAIL, "; ".join(problems)) if problems else (
            NOT_MEASURABLE, "no `brief.py filter` output in the transcript: the post-filter proposal cannot be read")
    command, proposal = runs[-1]
    edits = [e for e in proposal.get("edits") or [] if isinstance(e, dict)]
    queries = [q for q in proposal.get("queries") or [] if isinstance(q, dict)]
    paragraphs = [p for p in proposal.get("paragraphs") or [] if isinstance(p, dict)]
    drops = _drops_note(proposal)
    budget, rng = filter_arguments(command)
    if budget is not None and len(edits) > budget:
        problems.append(f"{len(edits)} edits survived the filter against a budget of {budget}")
    if problems:
        return FAIL, "; ".join(problems) + f"; {drops}"
    if kind == "budget" and budget is None:
        return NOT_MEASURABLE, "the filter command names no --budget, so there is no N to check"
    if kind == "range" and rng is None:
        return NOT_MEASURABLE, "the filter command names no PATH:START-END range"
    if doc_text is None:
        return NOT_MEASURABLE, "the document is not readable under --root"
    stale = [e.get("n") for e in edits if str(e.get("old", "")) not in doc_text]
    if stale:
        return NOT_MEASURABLE, f"edits {stale} are not in the document: it is not the one the run edited"
    reply_edits = [e for e in reply.get("edits") or [] if isinstance(e, dict)]
    extra = ""
    problems += _nonunique_problems(doc_text, edits)
    found, stale_fixture = _protected_problems(kind, doc_text, edits)
    if stale_fixture:
        return NOT_MEASURABLE, stale_fixture
    problems += found
    if kind in FIXTURE_PROTECTED:
        raw, _ = _protected_problems(kind, doc_text, reply_edits)
        extra = f"; editor's own edits in protected regions: {len({p.split(' ')[1] for p in raw})}"
    if rng is not None:
        lo, hi = rng["start"], rng["end"]
        problems += [f"edit {e.get('n')} is outside lines {lo}-{hi}" for e in _unique_edits(doc_text, edits)
                     if _range_status(doc_text, str(e["old"]), lo, hi) != "inside"]
        for q in queries:
            status = _range_status(doc_text, str(q.get("anchor", "")), lo, hi)
            if status == "outside":
                problems.append(f"query {q.get('n')} is outside lines {lo}-{hi}")
            elif status == "split":
                problems.append(f"query {q.get('n')}: the anchor occurs both inside and outside lines {lo}-{hi} "
                                "(some outside), so it does not say where it attaches")
        outside = sum(1 for e in _unique_edits(doc_text, reply_edits)
                      if _range_status(doc_text, str(e["old"]), lo, hi) != "inside")
        extra = f"; editor's own edits outside lines {lo}-{hi}: {outside}"
    proposed = len(edits) + len(queries) + len(paragraphs)
    for word in QUERIED_QUALIFIERS.get(kind, ()):
        cut = [e for e in edits if _cuts(e, word)]
        if cut:
            problems += [f"edit {e.get('n')} cuts the qualifier {word!r}: only filler words are cut, "
                         "every other qualifier is queried" for e in cut]
        elif not _asks_about(queries, word):
            problems.append(f"the qualifier {word!r} was neither cut nor queried: every qualifier but a filler word "
                            "must be queried")
    filler = FILLER_CUT.get(kind)
    if filler and not any(_cuts(e, filler) for e in edits):
        problems.append(f"the filler word {filler!r} was not cut (a filler word is cut, not queried or left)")
    card_note = ""
    if kind in DEVICE_KINDS:
        card = str(reply.get("voice_card") or proposal.get("voice_card") or "")
        found, stale_list, card_note = _device_problems(kind, doc_text, edits, paragraphs, card)
        if stale_list:
            return NOT_MEASURABLE, stale_list
        problems += found
    if problems:
        return FAIL, "; ".join(problems) + f"; {drops}" + (f"; {card_note}" if card_note else "")
    note = (f"{len(edits)} edits, {len(queries)} queries, {len(paragraphs)} paragraph proposals after the filter; "
            f"{drops}{extra}")
    if card_note:
        note += f"; {card_note}"
    if kind in ("xanadu", "linda"):
        note += f"; semicolon queries: {_semicolon_queries(queries)} (the document holds {doc_text.count(';')} ';')"
    if kind in EDITS_EXPECTED and not edits:
        return NOT_MEASURABLE, f"no edit survived the filter, so there is nothing to check; {note}"
    if kind in ("xanadu", "linda", "refrain") and not proposed:
        return NOT_MEASURABLE, f"the editor proposed nothing where something is expected; {note}"
    return PASS, note


def verdict(kind: str, events: list[Obj], c: Obj, new_work_dirs: list[str],
            doc_text: str | None = None) -> tuple[str, str]:
    dispatched = any(n == "Agent" for _, n, _, _ in tool_uses(events, sub=False))
    results = tool_results(events)
    if kind == "canary-nx":
        attempted = bool(c["nx_attempts"])
        leaked = any(re.search(r"\b\d+\.\d+\.\d+\b", v) and not is_denied(v) for v in results.values())
        ok = attempted and c["denied"] >= 1 and not leaked
        return ("PASS" if ok else "FAIL"), f"nx attempted={attempted} denied={c['denied']} version-leaked={leaked}"
    if kind == "canary-fail":
        saw = any("Exit code 3" in v or "unavailable" in v for v in results.values())
        ok = (saw and not dispatched and not c["off_list_bash"] and not c["nx_attempts"]
              and c["denied"] == 0 and not new_work_dirs)
        return ("PASS" if ok else "FAIL"), (f"saw-T2-failure={saw} dispatched={dispatched} "
                                            f"off-list={len(c['off_list_bash'])} denied={c['denied']} new-work-dirs={len(new_work_dirs)}")
    if kind == "unmapped":
        ok = not dispatched and "?" in final_text(events) and not new_work_dirs and c["denied"] == 0
        return ("PASS" if ok else "FAIL"), f"dispatched={dispatched} asked={'?' in final_text(events)} new-work-dirs={len(new_work_dirs)}"
    if kind == "stdin-nogenre":
        refused = any("needs --genre" in v for v in results.values())
        writes = [n for _, n, _, _ in tool_uses(events, sub=False) if n == "Write"]
        ok = refused and not dispatched and not writes and not new_work_dirs and c["denied"] == 0
        return ("PASS" if ok else "FAIL"), f"parse-refused={refused} dispatched={dispatched} writes={len(writes)} new-work-dirs={len(new_work_dirs)}"
    if kind == "stdin":
        # The turn ends with the question to the author, so one work directory is waiting for the answer.
        filtered = any('"edits"' in v and '"dropped"' in v for v in results.values())
        rendered = any('"copy"' in v and '"opened"' in v for v in results.values())
        unread = brief_read_problem(events) if dispatched else None
        ok = (dispatched and filtered and rendered and len(new_work_dirs) <= 1 and c["denied"] == 0
              and not c["off_list_bash"] and unread is None)
        return ("PASS" if ok else "FAIL"), (f"dispatched={dispatched} filtered={filtered} rendered={rendered} "
                                            f"new-work-dirs={len(new_work_dirs)} denied={c['denied']} "
                                            f"brief-read={unread or 'ok'}")
    if kind in DOCS:
        return doc_verdict(kind, events, c, doc_text)
    return NOT_MEASURABLE, f"no verdict is defined for the kind {kind!r}"


def main(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write((__doc__ or "") + "\n")
        return 2
    out_dir = Path(argv[0])
    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else Path(__file__).resolve().parents[3]
    before = set((out_dir / "work-dirs-before.txt").read_text().split()) if (out_dir / "work-dirs-before.txt").exists() else set()
    after = set((out_dir / "work-dirs-after.txt").read_text().split()) if (out_dir / "work-dirs-after.txt").exists() else set()
    new_dirs = sorted(after - before)
    fails = 0
    unmeasured = 0
    totals: Counter[str] = Counter()
    for rc in sorted(out_dir.glob("*.rc")):
        name = rc.stem
        kind = re.sub(r"-[0-9]+$", "", name)
        events = load_events(out_dir / f"{name}.jsonl")
        doc = DOCS.get(kind)
        doc_text = (root / doc).read_text(encoding="utf-8") if doc and (root / doc).exists() else None
        c = compliance(events, doc_text)
        raw = (out_dir / f"{name}.jsonl").read_text(encoding="utf-8")
        own_dirs = [d for d in new_dirs if d in raw]  # a directory the stdin run is holding for its answer is not another run's
        v, note = verdict(kind, events, c, own_dirs, doc_text=doc_text)
        fails += v == FAIL
        unmeasured += v == NOT_MEASURABLE
        counts = {k: (len(val) if isinstance(val, list) else val) for k, val in c.items()}
        for k, val in counts.items():
            totals[k] += int(val)
        sys.stdout.write(f"{name:14} {v:5} {note} | {json.dumps(counts)}\n")
    sys.stdout.write(f"TOTALS {json.dumps(dict(totals))} new-work-dirs {new_dirs}\n")
    heads = sorted({h.read_text(encoding="utf-8").strip() for h in out_dir.glob("*.head")} - {""})
    if heads:  # record-run.sh wrote one per run: a batch at more than one sha says so here
        sys.stdout.write(f"HEADS {' '.join(heads)}\n")
    models = sorted({m.read_text(encoding="utf-8").strip() for m in out_dir.glob("*.model")} - {""})
    if models:  # the session model each run recorded (the orchestrator's; the editor's is the agent file's)
        sys.stdout.write(f"MODELS {' '.join(models)}\n")
    return 1 if fails else (3 if unmeasured else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
