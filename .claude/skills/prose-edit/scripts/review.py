#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review loop of the prose-edit skill (RDR-221 Steps 1.1 and 1.5): marked-up copy, accept, apply.

The skill hands every mechanical job to this script so the model's instructions carry none of
it. Stdlib only; T2 is reached only by running memory.py beside it (through brief.py's child
runner), and the project prefix override (PROSE_EDIT_PROJECT_PREFIX) passes through. Errors go
to stderr with exit 1; memory.py's exit code 3 (T2 unavailable) passes through.

  review.py render TARGET --work WORK [--file F] [--genre G]
  review.py apply --work WORK [--accept SPEC] [--hold SPEC] [--reject SPEC] [--reasons-file F] [--reason N=TEXT]... [--dry-run]
  review.py log-retry --work WORK

`render` and `apply` take the filtered proposal that `brief.py filter TARGET --save WORK/filtered.json` wrote:
the machine format brief.py already emits. No second format exists. TARGET is PATH,
PATH:START-END or "-" (a stdin run; --file names the saved text). WORK is a directory made by
`brief.py tmpdir` (or `build --work`); every path this script reads or writes is inside it
except the document itself and the temp file described under `apply`.

`render` writes WORK/<name>.review.md, the marked-up copy, which lives outside the repository
because WORK does. Its format (Step 1.1):

  - a header line naming the file and the genre, then one line on how to answer
  - the editor's note as a blockquote, then the voice card as a blockquote
  - each sentence edit inline as <del>old</del><ins>new</ins><sup>N</sup> (a cut has no ins)
  - paragraph proposals as `> **[P1] action.** advice` before their paragraph, queries as
    `> **[Q1]** "anchor": text` after theirs
  - the reasons as numbered footnotes, `<sup>N</sup> reason`, one per paragraph at the end;
    edits the filter dropped keep their number there ("dropped before this copy: cause")
  - what the filter dropped, with old and new text and the cause, in a blockquote after the note

An edit that cannot be placed (its old string is absent, occurs more than once, or overlaps
another edit) is not marked inline; it is listed after the note with its cause. One whose old
string is absent or occurs more than once is skipped if accepted; two that overlap are both
skipped when both are accepted, and either is applied when accepted alone. An edit the copy could
not show inline cannot be decided in it, so it is never stored as a rejection. For an .html or
.htm target the copy is flat excerpts: only the sections a mark or note belongs to, as text with
no head, style, script or tags, each note beside its section. Entities show as the characters they
stand for (`&lt;` as `<`); a mark keeps the source text its edit matched, since matching is against
the source.

The copy is opened in the viewer stored at T2 prose/viewer (an application name, run as
`open -a NAME FILE` on macOS). With no record, or off macOS, or when the open fails, the copy is
not opened and its path is the answer. PROSE_EDIT_OPEN replaces `open -a` with a command (the
viewer name and the path are appended); like PROSE_EDIT_NOW it needs PROSE_EDIT_TEST=1 and is
ignored without it. `render` also writes WORK/session.json; `apply` needs it.

`apply` takes the author's answer. --accept (default "none") is the numbers to apply, --reject the numbers to store
as rejections for the document, and --hold the numbers left undecided: a comma or space separated
list of numbers and ranges ("1, 3-5"), or "all" or "none"; --reject also takes "rest", which is
every edit not accepted, held or named otherwise (the author's "reject the rest"). An edit the
author does not name is HELD: neither applied nor stored. Only --reject stores a rejection, so
silence, or "accept 1", never does. An edit the copy could not show inline is left alone by "rest"
and by being unnamed; the author may still name it in --reject. --reason N=TEXT (repeatable) keeps
the author's one line on why edit N is rejected; N must be a rejected edit and TEXT not empty.
--reasons-file F is the same thing as a file, for words that must not ride a shell string: a JSON object
{"2": "why", ...} that the model writes with its Write tool, directly inside WORK (WORK/reasons.json).
Both may be given; two reasons for one edit stop the run. The dry run's hashes cover the reasons however
they arrive, so editing the file after the dry run needs the dry run again.
An unknown number, an edit named by two of the three flags, or a reason that names no rejected edit
stops the run before anything is written.
--dry-run prints the interpreted sets (accept, hold, reject with its reasons, left alone, would
apply, would skip) and does nothing else: no file, no T2, WORK kept, apart from WORK/dryrun.json,
which holds hashes of the answer (the accepted, held and rejected sets and the reasons; "1,2" and
"2 1" are one answer), of filtered.json and of the document's bytes. A real apply refuses (exit 1, nothing written, nothing stored, WORK kept) unless that file
exists and all three still match: the dry run is how the author sees what will happen, so the
script will not run without it. A different answer, a changed proposal or a document saved since
the dry run each need the dry run again. Otherwise, in order:

  1. Each accepted edit is placed against the file as it is now: its old string must occur
     exactly once in the file (once inside the range, for a range run) and overlap no other
     accepted edit. Otherwise it is skipped and reported with the cause: not-found,
     outside-range, ambiguous, overlap (both edits are skipped), protected-region or markup.
  2. A path run with edits to write checks that the document resolves inside the repository and
     that it and its directory are writable, then writes the new text to a temp file and fsyncs
     it. A failure here stops the run with a message ending "run apply again": nothing was
     changed and nothing was stored. So does every failure before the file is written (T2 down
     or refusing, the file saved meanwhile, mixed line endings): the message says "nothing was
     written; run apply again" (exit code kept, 3 for T2 down), or, when rejections were already
     stored, "the file was not changed, and the rejections already stored are kept; run apply
     again". The skill keys on the lower-case phrase `run apply again`.
  3. A path run stores every rejected edit verbatim in the document's record (memory.py reject),
     with its reason when the author gave one. With none to store it still asks T2 one question,
     so a service that is down stops the run here, before the file changes. The rejections are the
     author's decisions and stay stored if step 4 then stops the run.
  4. The document is read again and compared byte for byte with what the plan was made against;
     a difference stops the run (the author saved in the meantime) with nothing written. If it
     is the same, the temp file is renamed over it. The placed edits are written in one pass, in
     file order, computed against the original text. A stdin run applies nothing: it prints the
     text with the accepted edits applied, under "text".
  5. The session is logged (memory.py log --stamp S; under log/stdin/ for a stdin run; S is fixed
     before the first try and kept in the pending file, so a retry whose first put landed with the
     reply lost writes the same record again, not a second one). The log comes last,
     so it never says "applied" for a file that was not written. If T2 fails after the file was
     written, whatever the failure (T2 down, a timeout, bad output), the output carries "log_error"
     and "log_retry" and the exit status is 0, and the payload waits in WORK/log-pending.json:
     `log-retry --work WORK` sends it, so the session still reaches the accept-rate evidence. An
     apply run again over such a WORK is refused and names log-retry.
  6. WORK is deleted, so the copy is gone even when nothing was accepted. A run that stops
     before step 5 keeps WORK so the author can run apply again; a run whose log failed keeps it
     for log-retry. A work directory idle for two hours is swept by the next `brief.py tmpdir`; `render`,
     `apply` (a dry run, a refused answer, a failed run) and `log-retry` each renew it.

The file is read and written byte for byte apart from the edits: UTF-8, line endings kept
(a file with mixed line endings is refused), a symbolic link followed and kept, the mode kept.
Not kept: hard links (the rename gives the document a new inode), extended attributes and
ownership. The temp file sits in <git dir>/prose-edit-tmp, where `git status` cannot show a stray
left by a killed process (stale ones are swept after two hours); when the git directory is on
another device than the document, and only then, it sits beside the document instead, and the
output says so ("staged_beside_document", and a line on stderr). A git directory the run cannot
make the stage directory in stops it, nothing changed. Each run also removes this document's own
`.NAME.*.prose-edit` strays beside it that are more than two hours old.
PROSE_EDIT_BEFORE_REPLACE (a command, run with the document path appended, only with
PROSE_EDIT_TEST=1) runs just before the comparison in step 4, to test that window.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from html import unescape as _unescape
from pathlib import Path
from typing import Any, cast

Obj = dict[str, Any]
Span = tuple[int, int]

HERE = Path(__file__).resolve().parent
STAGE_DIRNAME = "prose-edit-tmp"
STAGE_MAX_AGE = 2 * 3600


def _load_brief() -> Any:
    spec = importlib.util.spec_from_file_location("prose_edit_brief_lib", HERE / "brief.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {HERE / 'brief.py'}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


_BRIEF = _load_brief()
UserError: type[Exception] = _BRIEF.UserError
Passthrough: type[Exception] = _BRIEF.Passthrough


def _user(message: str) -> Exception:
    return UserError(message)


RETRY = "run apply again"


def _aborted(exc: BaseException, *, stored: bool) -> BaseException:
    """`exc` with an ending that says what is true of the file and to run apply again, same type and exit code.

    Called for a failure after the plan was made and before the file was written. Nothing is claimed that
    a stored rejection would make false: with rejections already stored the file was not changed but the
    store is kept. A message that already says the retry words and that nothing was changed is left as it is.
    """
    if isinstance(exc, OSError):
        exc = _user(f"{exc}")
    if not isinstance(exc, (Passthrough, UserError)):
        return exc
    msg = str(exc).rstrip()
    low = msg.lower()
    parts = [msg.rstrip(".")]
    if stored:
        parts.append("the file was not changed, and the rejections already stored are kept")
    elif "nothing was written" not in low and "nothing was changed" not in low:
        parts.append("nothing was written")
    if RETRY not in msg:
        parts.append(RETRY)
    new = "; ".join(parts)
    if isinstance(exc, Passthrough):
        return Passthrough(int(getattr(exc, "code", 1)), new)
    return _user(new)


def _test_mode() -> bool:
    return os.environ.get("PROSE_EDIT_TEST") == "1"


# ---------------------------------------------------------------------------
# The author's answer
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"([0-9]+)(?:-([0-9]+))?")


def parse_numbers(spec: str, valid: set[int], flag: str, rest: set[int] | None = None) -> set[int]:
    """The edit numbers a --accept, --hold or --reject value names: numbers and ranges, "all" or "none".

    A range's two ends must be edits; a number between them that is not one (the filter
    dropped it) is passed over. "rest" is accepted only where the caller passes `rest` (the set it
    stands for): --reject, whose "reject the rest" is every edit nobody else named.
    """
    text = spec.strip().lower()
    have = ", ".join(str(n) for n in sorted(valid)) or "none"
    if text == "all":
        return set(valid)
    if text == "none":
        return set()
    if text == "rest" and rest is not None:
        return set(rest)
    found: set[int] = set()
    for token in re.split(r"\s*,\s*|\s+", text):
        m = _NUMBER.fullmatch(token)
        if m is None:
            raise _user(f"{flag} {spec!r}: expected edit numbers like '1, 3-5', 'all' or 'none'"
                        + (", or 'rest'" if rest is not None else ""))
        low = int(m.group(1))
        high = int(m.group(2)) if m.group(2) else low
        if low > high:
            raise _user(f"{flag} {spec!r}: range {token} runs backwards")
        for end in (low, high):
            if end not in valid:
                raise _user(f"{flag} {spec!r}: no edit number {end} (the edits are {have})")
        found |= {n for n in range(low, high + 1) if n in valid}
    return found


# ---------------------------------------------------------------------------
# Placing edits
# ---------------------------------------------------------------------------


def _occurrences(text: str, needle: str) -> list[int]:
    """Every start of `needle` in `text`, overlapping ones included ("aa" is twice in "aaa")."""
    out: list[int] = []
    i = text.find(needle)
    while i != -1:
        out.append(i)
        i = text.find(needle, i + 1)
    return out


def _within(window: Span | None, start: int, end: int) -> bool:
    return window is None or (window[0] <= start and end <= window[1])


def plan_edits(text: str, edits: list[Obj], rng: Obj | None, html: bool) -> list[Obj]:
    """Place each edit against `text`; one plan per edit, in the order given.

    A plan is {"n", "old", "new", "span", "cause", "detail"}. `span` is (start, end) when the
    edit can be applied, else None with a `cause`. Overlap is judged among the edits given,
    so the caller decides which edits count (all of them for the copy, the accepted ones to apply).
    """
    window = cast("Span | None", _BRIEF._range_span(text, rng))
    spans = cast("list[Span]", _BRIEF.protected_spans(text, html=html))
    tags = cast("list[Span]", _BRIEF.html_tag_spans(text)) if html else []
    plans: list[Obj] = []
    for e in edits:
        old = str(e["old"])
        spots = _occurrences(text, old)
        inside = [i for i in spots if _within(window, i, i + len(old))]
        plan: Obj = {"n": e["n"], "old": old, "new": str(e["new"]), "span": None, "cause": None, "detail": ""}
        if len(inside) > 1:
            plan.update(cause="ambiguous", detail=f"the old string occurs {len(inside)} times")
        elif inside:
            span = (inside[0], inside[0] + len(old))
            if any(span[0] < b and a < span[1] for a, b in spans):
                plan.update(cause="protected-region", detail="the old string is inside a quote, code, table, "
                                                             "link destination or frontmatter")
            elif any(span[0] < b and a < span[1] for a, b in tags):
                plan.update(cause="markup", detail="the old string overlaps HTML markup")
            else:
                plan["span"] = span
        elif spots:
            assert window is not None
            plan.update(cause="outside-range", detail="the old string occurs only outside the line range")
        else:
            plan.update(cause="not-found", detail="the old string is not in the file")
        plans.append(plan)
    placed = [p for p in plans if p["span"] is not None]
    for p in placed:
        others = [q["n"] for q in placed if q is not p and p["span"][0] < q["span"][1] and q["span"][0] < p["span"][1]]
        if others:
            p["clash"] = others
    for p in placed:
        if "clash" in p:
            p.update(cause="overlap", detail="it overlaps " + ", ".join(f"edit {n}" for n in p.pop("clash")))
    for p in placed:
        if p["cause"] is not None:
            p["span"] = None
    return plans


def apply_plan(text: str, plans: list[Obj]) -> str:
    """`text` with every placed edit replaced, in file order, all positions from the original."""
    out: list[str] = []
    pos = 0
    for p in sorted((p for p in plans if p["span"] is not None), key=lambda p: p["span"][0]):
        start, end = cast(Span, p["span"])
        out += [text[pos:start], str(p["new"])]
        pos = end
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------
# The marked-up copy
# ---------------------------------------------------------------------------


def _block(text: str, offset: int, protected: list[Span]) -> Span:
    """The paragraph holding `offset` (lines up to a blank line), grown to cover any protected region it touches."""
    starts: list[int] = []
    pos = 0
    lines = text.split("\n")
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    idx = max(i for i, s in enumerate(starts) if s <= offset)
    if not lines[idx].strip():
        return offset, offset
    lo = hi = idx
    while lo > 0 and lines[lo - 1].strip():
        lo -= 1
    while hi + 1 < len(lines) and lines[hi + 1].strip():
        hi += 1
    start, end = starts[lo], starts[hi] + len(lines[hi])
    grown = True
    while grown:
        grown = False
        for a, b in protected:
            if a < end and start < b and (a < start or b > end):
                start, end, grown = min(start, a), max(end, b), True
    return start, end


def _quote(body: str) -> str:
    return "> " + body.replace("\n", "\n> ")


def _first(text: str, needle: str, window: Span | None) -> int | None:
    for i in _occurrences(text, needle):
        if _within(window, i, i + len(needle)):
            return i
    return None


# An .html page is shown as flat text: what a reader sees, in sections, with no tags.
_SKIP_OR_TAG = re.compile(
    r"(?P<skip><!--.*?-->|<(?P<name>script|style|head|svg|noscript|template)\b.*?</(?P=name)\s*>)|(?P<tag><[^<>]*>)",
    re.IGNORECASE | re.DOTALL)
_BLOCK_TAG = re.compile(
    r"</?(?:p|div|h[1-6]|li|ul|ol|dl|dt|dd|section|article|header|footer|nav|main|aside|table|thead|tbody|tr|td|th|"
    r"blockquote|pre|figure|figcaption|br|hr|body|html|details|summary|form)\b", re.IGNORECASE)
_HEADING_TAG = re.compile(r"<h[1-6]\b", re.IGNORECASE)


def html_blocks(text: str) -> list[tuple[list[Span], bool]]:
    """The text nodes of an HTML page grouped into sections: ([(start, end) of each node], is_heading).

    Comments, head, style, script, svg, noscript and template contribute nothing; a block-level tag
    ends a section; inline tags (a, em, span) do not.
    """
    blocks: list[tuple[list[Span], bool]] = []
    state: dict[str, Any] = {"segs": [], "heading": False}

    def flush(next_heading: bool) -> None:
        segs = cast("list[Span]", state["segs"])
        if any(text[a:b].strip() for a, b in segs):
            blocks.append((segs, bool(state["heading"])))
        state["segs"], state["heading"] = [], next_heading

    last = 0
    for m in _SKIP_OR_TAG.finditer(text):
        if m.start() > last:
            state["segs"].append((last, m.start()))
        last = m.end()
        tag = m.group("tag")
        if tag and _BLOCK_TAG.match(tag):
            flush(_HEADING_TAG.match(tag) is not None)
    if last < len(text):
        state["segs"].append((last, len(text)))
    flush(False)
    return blocks


def _flat(text: str, segs: list[Span], marks: list[tuple[int, int, str]]) -> str:
    """The text of the segments, entities shown as the characters they stand for (`&lt;` as `<`). A mark
    is put in as it is: it holds the source text its edit matched, which is what apply matches against."""
    out: list[str] = []
    for a, b in segs:
        pos = a
        for ms, me, piece in marks:
            if a <= ms and me <= b and ms >= pos:
                out += [_unescape(text[pos:ms]), piece]
                pos = me
        out.append(_unescape(text[pos:b]))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _html_excerpts(text: str, marks: list[tuple[int, int, str]], p_notes: list[tuple[int, str]],
                   q_notes: list[tuple[int, str]]) -> tuple[str, list[str]]:
    """(the flat excerpts, the marks and notes that fell in no section). Marks must be sorted."""
    blocks = html_blocks(text)

    def block_of(offset: int, end: int | None = None) -> int | None:
        for i, (segs, _) in enumerate(blocks):
            if any(a <= offset and (offset if end is None else end) <= b for a, b in segs):
                return i
        return None

    want: dict[int, dict[str, list[str]]] = {}
    lost: list[str] = []
    for ms, me, piece in marks:
        i = block_of(ms, me)
        if i is None:
            lost.append(piece)
        else:
            want.setdefault(i, {"p": [], "q": []})
    for offset, note in p_notes:
        i = block_of(offset)
        if i is None:
            lost.append(note)
        else:
            want.setdefault(i, {"p": [], "q": []})["p"].append(note)
    for offset, note in q_notes:
        i = block_of(offset)
        if i is None:
            lost.append(note)
        else:
            want.setdefault(i, {"p": [], "q": []})["q"].append(note)
    parts: list[str] = []
    shown_heading: int | None = None
    for i in sorted(want):
        segs, is_heading = blocks[i]
        if is_heading:
            parts += [*want[i]["p"], "### " + _flat(text, segs, marks), *want[i]["q"]]
            shown_heading = i
            continue
        head = max((j for j in range(i) if blocks[j][1]), default=None)
        if head is not None and head != shown_heading:
            parts.append("### " + _flat(text, blocks[head][0], []))
            shown_heading = head
        parts += [*want[i]["p"], _flat(text, segs, marks), *want[i]["q"]]
    return "\n\n".join(parts), lost


def _dropped_lines(prop: Obj) -> list[str]:
    """One line per thing the filter dropped: the old and new text and the cause."""
    lines: list[str] = []
    for d in cast("list[Obj]", prop.get("dropped") or []):
        new = f' -> "{d["new"]}"' if d.get("new") is not None else ""
        lines.append(f'- E{d.get("n")} "{d.get("old")}"{new} ({d.get("cause", "")})')
    for d in cast("list[Obj]", prop.get("dropped_queries") or []):
        lines.append(f'- [Q{d.get("n")}] "{d.get("anchor")}" ({d.get("cause", "")})')
    for d in cast("list[Obj]", prop.get("dropped_paragraphs") or []):
        lines.append(f'- [P{d.get("n")}] {d.get("paragraphs")} ({d.get("cause", "")})')
    return lines


def _unplaced_note(p: Obj) -> str:
    tail = (" Accepted together they are both skipped; accepted alone it is applied."
            if p["cause"] == "overlap" else " It is skipped if accepted.")
    return _quote(f"**[E{p['n']}]** Cannot be placed: {p['detail']}.{tail}\n\"{p['old']}\" -> \"{p['new']}\"")


def build_copy(text: str, prop: Obj, *, label: str, genre: str, rng: Obj | None, html: bool, stdin: bool) -> str:
    """The marked-up copy of `text` for the filtered proposal `prop` (Step 1.1)."""
    edits = cast("list[Obj]", prop.get("edits") or [])
    paragraphs = cast("list[Obj]", prop.get("paragraphs") or [])
    queries = cast("list[Obj]", prop.get("queries") or [])
    dropped = cast("list[Obj]", prop.get("dropped") or [])
    plans = plan_edits(text, edits, rng, html)
    window = cast("Span | None", _BRIEF._range_span(text, rng))
    protected = cast("list[Span]", _BRIEF.protected_spans(text, html=html))
    replaced = [cast(Span, p["span"]) for p in plans if p["span"] is not None]

    def settle(offset: int) -> int:
        for a, b in replaced:
            if a < offset < b:
                return b
        return offset

    edit_marks: list[tuple[int, int, str]] = []
    for p in plans:
        if p["span"] is not None:
            start, end = cast(Span, p["span"])
            ins = f"<ins>{p['new']}</ins>" if p["new"] else ""
            edit_marks.append((start, end, f"<del>{p['old']}</del>{ins}<sup>{p['n']}</sup>"))
    unplaced = [_unplaced_note(p) for p in plans if p["span"] is None]
    p_notes: list[tuple[int, str]] = []
    for pr in sorted(paragraphs, key=lambda x: int(x["n"])):
        spots = [s for ph in cast("list[str]", _BRIEF._quoted_phrases(str(pr.get("paragraphs", ""))))
                 if (s := _first(text, ph, window)) is not None]
        head = f"**[P{pr['n']}] {pr.get('action', '')}.**"
        if spots:
            p_notes.append((min(spots), _quote(f"{head} {pr.get('advice', '')}")))
        else:
            unplaced.append(_quote(f"{head} {pr.get('paragraphs', '')}: {pr.get('advice', '')}"))
    q_notes: list[tuple[int, str]] = []
    for q in sorted(queries, key=lambda x: int(x["n"])):
        at = _first(text, str(q["anchor"]), window)
        body = _quote(f"**[Q{q['n']}]** \"{q['anchor']}\": {q.get('text', '')}")
        if at is None:
            unplaced.append(body)
        else:
            q_notes.append((at, body))

    if html:
        body_text, lost = _html_excerpts(text, sorted(edit_marks), p_notes, q_notes)
        unplaced += [_quote(f"Not in any section of the page text: {piece}") for piece in lost]
        if not body_text:
            body_text = "No edit, paragraph proposal or query falls in the page text."
    else:
        marks = list(edit_marks)
        for offset, note in p_notes:
            start = settle(_block(text, offset, protected)[0])
            marks.append((start, start, note + "\n\n"))
        for offset, note in q_notes:
            end = settle(_block(text, offset, protected)[1])
            marks.append((end, end, "\n\n" + note))
        marks.sort(key=lambda m: (m[0], m[1]))
        body_out: list[str] = []
        pos = 0
        for start, end, piece in marks:
            body_out += [text[pos:start], piece]
            pos = max(pos, end)
        body_out.append(text[pos:])
        body_text = "".join(body_out)

    counts = f"{len(edits)} sentence edits, {len(paragraphs)} paragraph proposals, {len(queries)} queries"
    where = f"Lines {rng['start']}-{rng['end']} only. " if rng else ""
    head_lines = [f"# Line edit: {label}", "", f"Genre: {genre}. {where}{counts}.", ""]
    answer = "Answer with the numbers to accept, for example `1, 3-5`, `all` or `none`."
    if stdin:
        head_lines.append(f"Nothing is applied to a file; the accepted text is printed. {answer} "
                          "Edits you do not accept are not stored: a stdin run keeps no rejections.")
    else:
        head_lines.append(f"{answer} An edit you do not name stays undecided and nothing is stored for it. "
                          "To store a rejection for this document say `reject 2` or `reject the rest`, and give a "
                          "reason if you like (`reject 2 because it changes my meaning`). An edit listed below as "
                          "cannot be placed is left alone by `reject the rest`; name it to reject it.")
    for w in cast("list[str]", prop.get("warnings") or []):
        head_lines += ["", f"Warning: {w}"]
    for key, title in (("note", "Editor's note"), ("voice_card", "Voice card")):
        if str(prop.get(key) or "").strip():
            head_lines += ["", _quote(f"**{title}.** {str(prop[key]).strip()}")]
    for block in unplaced:
        head_lines += ["", block]
    gone = _dropped_lines(prop)
    if gone:
        head_lines += ["", _quote("**Dropped before this copy.**\n" + "\n".join(gone))]
    notes: list[tuple[int, str]] = [(int(e["n"]), str(e.get("reason", ""))) for e in edits]
    notes += [(int(d["n"]), f"dropped before this copy: {d.get('cause', '')}") for d in dropped if d.get("n") is not None]
    foot = "\n\n".join(f"<sup>{n}</sup> {why}" for n, why in sorted(notes))
    return "\n".join(head_lines) + "\n\n---\n\n" + body_text.rstrip("\n") + "\n\n---\n\n" + foot + "\n"


# ---------------------------------------------------------------------------
# The viewer
# ---------------------------------------------------------------------------


def _run_open(argv: list[str]) -> int:
    return subprocess.run(argv, capture_output=True, timeout=30).returncode


def open_copy(viewer: str | None, copy: Path, *, platform: str | None = None, override: str | None = None,
              run: Callable[[list[str]], int] = _run_open) -> Obj:
    """Open the copy in the stored viewer; say why when it is not opened (the caller prints the path)."""
    if not viewer:
        return {"opened": False, "reason": "no viewer is stored at prose/viewer"}
    if override:
        argv = [*shlex.split(override), viewer, str(copy)]
    elif (platform or sys.platform) != "darwin":
        return {"opened": False, "reason": f"not macOS ({platform or sys.platform}); the viewer is opened with `open -a`"}
    else:
        argv = ["open", "-a", viewer, str(copy)]
    try:
        code = run(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"opened": False, "reason": f"{argv[0]}: {exc}"}
    return {"opened": code == 0, "reason": "" if code == 0 else f"{argv[0]} exited {code}"}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _user(f"cannot read {path}: {exc}") from exc


def decode_source(raw: bytes, path: Path) -> tuple[str, str]:
    """(text with LF endings, "lf" | "crlf" | "mixed"); UTF-8 only."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _user(f"cannot read {path} as UTF-8 text: {exc}") from exc
    if "\r" not in text:
        return text, "lf"
    lf = text.replace("\r\n", "\n")
    if "\r" in lf:
        return lf, "mixed"  # a lone carriage return
    if text.count("\r\n") == lf.count("\n"):
        return lf, "crlf"
    return lf, "mixed"


def read_source(path: Path) -> tuple[str, str]:
    return decode_source(read_bytes(path), path)


def _same_device(a: Path, b: Path) -> bool:
    return a.stat().st_dev == b.stat().st_dev


def _sweep_old(directory: Path, belongs: Callable[[str], bool]) -> None:
    """Delete the temp files in `directory` a killed run left more than two hours ago. Best effort:
    a file or a directory this cannot read or remove is skipped, never a reason to stop."""
    cutoff = time.time() - STAGE_MAX_AGE
    try:
        children = list(directory.iterdir())
    except OSError:
        return
    for p in children:
        try:
            if belongs(p.name) and p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            continue


def _sweep_stage(directory: Path) -> None:
    """Delete temp files a killed run left in the stage directory more than two hours ago."""
    _sweep_old(directory, lambda name: name.endswith(".prose-edit"))


def _sweep_beside(target: Path) -> None:
    """Delete this document's own `.NAME.*.prose-edit` strays beside it more than two hours old: what a
    run killed after it staged beside the document (the other-device case) leaves, where `git status`
    shows it. Only this document's, only old ones."""
    prefix = f".{target.name}."
    _sweep_old(target.parent, lambda name: name.startswith(prefix) and name.endswith(".prose-edit"))


def _stage_dir(root: Path, target: Path) -> tuple[Path, bool]:
    """(where the temp file goes, whether that is beside the document). The git directory, where `git status`
    cannot see a stray; beside the document only when that directory is on another device (a rename cannot
    cross one) or the repository has no git directory. A directory that cannot be made is an error, not a
    reason to fall back: a stage beside the document is never reached by accident."""
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return target.parent, True
    directory = Path(proc.stdout.strip()) / STAGE_DIRNAME
    try:
        directory.mkdir(exist_ok=True)
        same = _same_device(directory, target.parent)
    except OSError as exc:
        raise _user(f"cannot stage the new text for {target}: {directory}: {exc}. Nothing was changed; "
                    "fix it and run apply again") from exc
    if not same:
        return target.parent, True
    try:
        _sweep_stage(directory)
    except OSError:
        pass  # a sweep that fails is not a reason to leave the git directory
    return directory, False


def check_target(path: Path, root: Path) -> Path:
    """The real file `path` writes to, after checks that need no side effect. Raises UserError
    (nothing changed) when it resolves outside the repository or it or its directory is not writable."""
    target = path.resolve()
    base = root.resolve()
    if target != base and base not in target.parents:
        raise _user(f"{path} resolves to {target}, outside the repository {base}; nothing was changed")
    if not os.access(target, os.W_OK):
        raise _user(f"{target} is not writable; nothing was changed. Fix the permissions and run apply again")
    if not os.access(target.parent, os.W_OK | os.X_OK):
        raise _user(f"the directory {target.parent} is not writable; nothing was changed. "
                    "Fix the permissions and run apply again")
    return target


class Staged:
    """The new text, written and fsynced to a temp file, waiting to replace the document."""

    def __init__(self, target: Path, tmp: Path, beside_document: bool = False) -> None:
        self.target, self.tmp, self.beside_document = target, tmp, beside_document

    def discard(self) -> None:
        self.tmp.unlink(missing_ok=True)

    def commit(self, expected: bytes, *, before_replace: Callable[[Path], None] | None = None,
               replace: Callable[[str, str], None] = os.replace) -> None:
        """Rename over the document if it still holds `expected` byte for byte; else write nothing."""
        try:
            if before_replace is not None:
                before_replace(self.target)
            require_unchanged(self.target, expected)
            try:
                replace(str(self.tmp), str(self.target))
            except OSError as exc:
                raise _user(f"cannot replace {self.target}: {exc}. Fix it and run apply again") from exc
        except BaseException:
            self.discard()
            raise


def require_unchanged(target: Path, expected: bytes) -> None:
    if read_bytes(target) != expected:
        raise _user(f"{target} changed while the edits were being applied. "
                    "Save and close it in the editor, then run apply again")


def stage_write(path: Path, root: Path, data: bytes, *, fsync: Callable[[int], None] = os.fsync) -> Staged:
    """Write `data` to a temp file, fsynced, mode copied from the document; nothing else changes."""
    target = check_target(path, root)
    directory, beside = _stage_dir(root, target)
    try:
        _sweep_beside(target)
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".prose-edit", dir=directory)
    except OSError as exc:
        raise _user(f"cannot stage the new text for {target}: {exc}. Nothing was changed; "
                    "fix it and run apply again") from exc
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            fsync(fh.fileno())
        shutil.copymode(target, tmp)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise _user(f"cannot stage the new text for {target}: {exc}. Nothing was changed; "
                    "fix it and run apply again") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return Staged(target, tmp, beside)


def _before_replace_hook() -> Callable[[Path], None] | None:
    command = os.environ.get("PROSE_EDIT_BEFORE_REPLACE")
    if not command or not _test_mode():
        return None

    def run(path: Path) -> None:
        subprocess.run([*shlex.split(command), str(path)], check=False, timeout=60)

    return run


def _load_json(path: Path, what: str) -> Obj:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _user(f"{what}: {exc}") from exc
    if not isinstance(value, dict):
        raise _user(f"{what}: not a JSON object")
    return cast(Obj, value)


def _emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=1) + "\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _source_of(target: str, file: str | None, work: Path) -> tuple[str | None, Obj | None, Path, bool, Path | None]:
    """(repo-relative path or None, range, the file to read, is_html, repo root or None) for a target."""
    if target == "-":
        if not file:
            raise _user("a stdin run needs --file <path of the saved text>")
        return None, None, cast(Path, _BRIEF.work_file(file)), False, None
    if file:
        raise _user("--file is only for a stdin run (target -)")
    where = cast(Obj, _BRIEF.memory_json(["repo", target]))
    root = Path(str(where["root"]))
    source = root / str(where["path"])
    return (str(where["path"]), cast("Obj | None", where.get("range")), source,
            source.suffix.lower() in (".html", ".htm"), root)


def cmd_render(a: argparse.Namespace) -> Obj:
    work = cast(Path, _BRIEF.work_dir(a.work))
    _BRIEF.touch_work(work)
    stdin = a.target == "-"
    prop = _load_json(work / "filtered.json", "WORK/filtered.json (run `brief.py filter ... --save` first)")
    rel, rng, source, html, _ = _source_of(a.target, a.file, work)
    genre = a.genre
    if genre is None and not stdin:
        genre = cast("str | None", _BRIEF.memory_json(["genre-for", a.target]).get("genre"))
    if genre is None:
        raise _user("no genre: pass --genre")
    text, _eol = read_source(source)
    viewer = cast("str | None", _BRIEF.memory_json(["viewer"]).get("viewer"))
    copy = work / f"{Path(rel).stem if rel else 'stdin'}.review.md"
    copy.write_text(build_copy(text, prop, label=rel or "stdin", genre=genre, rng=rng, html=html, stdin=stdin),
                    encoding="utf-8")
    opened = open_copy(viewer, copy, override=os.environ.get("PROSE_EDIT_OPEN") if _test_mode() else None)
    plans = plan_edits(text, cast("list[Obj]", prop.get("edits") or []), rng, html)
    unplaced = [{"n": p["n"], "cause": p["cause"], "detail": p["detail"]} for p in plans if p["span"] is None]
    (work / "session.json").write_text(json.dumps(
        {"target": a.target, "genre": genre, "stdin": stdin, "file": a.file, "copy": str(copy),
         "unplaced": unplaced}), encoding="utf-8")
    return {
        "copy": str(copy), "genre": genre, "viewer": viewer, **opened,
        "counts": {"edits": len(plans), "paragraphs": len(prop.get("paragraphs") or []),
                   "queries": len(prop.get("queries") or []), "dropped": len(prop.get("dropped") or [])},
        "unplaced": unplaced,
        "dropped": list(prop.get("dropped") or []),
        "dropped_queries": list(prop.get("dropped_queries") or []),
        "dropped_paragraphs": list(prop.get("dropped_paragraphs") or []),
        "warnings": list(prop.get("warnings") or []),
    }


def _shown(plan: Obj, reasons: dict[int, str] | None = None) -> Obj:
    out: Obj = {"n": plan["n"], "old": plan["old"], "new": plan["new"]}
    if reasons and int(plan["n"]) in reasons:
        out["reason"] = reasons[int(plan["n"])]
    return out


def _skipped(plans: list[Obj]) -> list[Obj]:
    return [{**_shown(p), "cause": p["cause"], "detail": p["detail"]} for p in plans if p["span"] is None]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dry_run_record(accept: set[int], hold: set[int], reject: set[int], reasons: dict[int, str],
                    proposal: bytes, document: bytes) -> Obj:
    """What a dry run vouches for: the answer (as sets and the reasons, so "1,2" and "2 1" are one answer),
    the filtered proposal and the document, as hashes."""
    answer = json.dumps({"accept": sorted(accept), "hold": sorted(hold), "reject": sorted(reject),
                         "reasons": {str(n): reasons[n] for n in sorted(reasons)}}).encode("utf-8")
    return {"answer": _sha(answer), "proposal": _sha(proposal), "document": _sha(document)}


def _require_dry_run(work: Path, record: Obj, source: Path) -> None:
    """Refuse a real apply that no matching dry run, shown to the author, came before."""
    run_it = ("Run `review.py apply --work WORK --accept ... --dry-run` for the answer, show its output to the "
              "author, and run the real apply only after the author confirms it")
    path = work / "dryrun.json"
    if not path.is_file():
        raise _user(f"no dry run on record for this work directory. {run_it}.")
    saved = _load_json(path, "WORK/dryrun.json")
    if saved.get("answer") != record["answer"]:
        raise _user(f"this is a different answer than the dry run showed the author. {run_it}.")
    if saved.get("proposal") != record["proposal"]:
        raise _user(f"the filtered proposal changed since the dry run. {run_it}.")
    if saved.get("document") != record["document"]:
        raise _user(f"{source} changed since the dry run (the author saved it, or the file moved). "
                    f"Nothing was written. {run_it}.")


_REASON = re.compile(r"\s*([0-9]+)\s*=(.*)", re.DOTALL)


def parse_reasons(raw: list[str], rejected: set[int], from_file: dict[int, str] | None = None) -> dict[int, str]:
    """The author's reasons, `N=TEXT` each (and `from_file`, read from --reasons-file): N must be a rejected
    edit, TEXT not empty, one reason per edit across both."""
    out: dict[int, str] = {}
    have = ", ".join(str(r) for r in sorted(rejected)) or "none"
    for item in raw:
        m = _REASON.fullmatch(item)
        if m is None or not m.group(2).strip():
            raise _user(f"--reason {item!r}: expected N=TEXT with a reason after the equals sign")
        n = int(m.group(1))
        if n not in rejected:
            raise _user(f"--reason {item!r}: edit {n} is not rejected (the rejected edits are {have})")
        if n in out:
            raise _user(f"--reason: two reasons for edit {n}")
        out[n] = m.group(2).strip()
    for n, text in sorted((from_file or {}).items()):
        if n not in rejected:
            raise _user(f"--reasons-file: edit {n} is not rejected (the rejected edits are {have})")
        if n in out:
            raise _user(f"--reasons-file: two reasons for edit {n}")
        out[n] = text
    return out


def load_reasons_file(raw: str | None, work: Path) -> dict[int, str]:
    """The reasons in --reasons-file: a JSON object {"2": "why"} directly inside WORK. Empty when not given."""
    if not raw:
        return {}
    try:
        path = cast(Path, _BRIEF.work_file(raw))
    except UserError as exc:
        raise _user(f"--reasons-file {raw!r}: {exc}") from exc
    if path.parent != work:
        raise _user(f"--reasons-file {raw!r}: must sit directly inside this work directory")
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _user(f"--reasons-file {raw!r}: cannot read it as JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise _user(f'--reasons-file {raw!r}: expected a JSON object like {{"2": "why"}}')
    out: dict[int, str] = {}
    for key, text in cast("dict[Any, Any]", value).items():
        if not re.fullmatch(r"[0-9]+", str(key)):
            raise _user(f"--reasons-file {raw!r}: key {key!r} is not an edit number")
        if not isinstance(text, str) or not text.strip():
            raise _user(f"--reasons-file {raw!r}: the reason for edit {key} must be a non-empty string")
        out[int(key)] = text.strip()
    return out


def _both(flags: list[tuple[str, set[int]]]) -> None:
    """An edit can be named by one of --accept, --hold and --reject only."""
    for i, (first, a) in enumerate(flags):
        for second, b in flags[i + 1:]:
            if a & b:
                raise _user(f"{first} and {second} both name edit {min(a & b)}")


def cmd_apply(a: argparse.Namespace) -> Obj:
    work = cast(Path, _BRIEF.work_dir(a.work))
    _BRIEF.touch_work(work)  # the author is here: a dry run, a refused answer or a failed run all count
    if (work / "log-pending.json").is_file():
        raise _user("the file was already written; only its session log is outstanding. "
                    f"Run `review.py log-retry --work {work}` instead of applying again")
    sess_file = work / "session.json"
    if not sess_file.is_file():
        raise _user("no session in the work directory: run `review.py render` first")
    session = _load_json(sess_file, "WORK/session.json")
    prop = _load_json(work / "filtered.json", "WORK/filtered.json")
    proposal_bytes = read_bytes(work / "filtered.json")
    edits = cast("list[Obj]", prop.get("edits") or [])
    by_n = {int(e["n"]): e for e in edits}
    valid = set(by_n)
    accept = parse_numbers(a.accept, valid, "--accept")
    hold: set[int] = parse_numbers(a.hold, valid, "--hold") if a.hold else set()
    # an edit the copy could not show inline: `reject the rest` leaves it alone, naming it rejects it
    unshown = {int(u["n"]): u for u in cast("list[Obj]", session.get("unplaced") or [])}
    rest = valid - set(unshown) - accept - hold
    reject: set[int] = parse_numbers(a.reject, valid, "--reject", rest=rest) if a.reject else set()
    _both([("--accept", accept), ("--hold", hold), ("--reject", reject)])
    reasons = parse_reasons(list(a.reason or []), reject, load_reasons_file(a.reasons_file, work))
    stdin = bool(session["stdin"])
    target = str(session["target"])
    rel, rng, source, html, root = _source_of(target, cast("str | None", session.get("file")), work)
    try:
        raw = read_bytes(source)
    except UserError as exc:
        raise _aborted(exc, stored=False) from exc
    text, eol = decode_source(raw, source)
    if eol == "mixed" and not stdin:
        raise _user(f"{source}: mixed line endings (CRLF and LF); the file is not touched; nothing was written. "
                    f"Fix the line endings, then {RETRY}")
    record = _dry_run_record(accept, hold, reject, reasons, proposal_bytes, raw)
    if not a.dry_run:
        _require_dry_run(work, record, source)
    plans = plan_edits(text, [e for e in edits if int(e["n"]) in accept], rng, html)
    placed = [p for p in plans if p["span"] is not None]
    skipped = _skipped(plans)
    left_alone = [{**_shown(by_n[n]), "cause": unshown[n].get("cause"), "detail": unshown[n].get("detail")}
                  for n in sorted(unshown) if n in by_n and n not in accept | hold | reject]
    rejected = sorted(reject)
    # an edit nobody named is held: neither applied nor stored (unshown ones are `unplaced`, not held)
    held = sorted(((valid - set(unshown)) | hold) - accept - reject)
    store = not stdin and bool(rejected)
    writes = bool(placed) and not stdin
    if writes:
        check_target(source, cast(Path, root))
    if a.dry_run:
        (work / "dryrun.json").write_text(json.dumps(record), encoding="utf-8")
        return {
            "dry_run": True, "mode": "stdin" if stdin else "edit", "path": rel,
            "accept": [_shown(by_n[n]) for n in sorted(accept)], "hold": [_shown(by_n[n]) for n in held],
            "reject": [_shown(by_n[n], reasons) for n in rejected], "unplaced": left_alone,
            "would_apply": [] if stdin else [_shown(p) for p in sorted(placed, key=lambda p: p["span"][0])],
            "would_skip": skipped, "stores_rejections": store,
        }
    new_text = apply_plan(text, plans)
    staged: Staged | None = None
    stored = False  # the rejections are the author's decisions: once stored they stay, whatever stops the run
    try:
        if writes:
            data = (new_text.replace("\n", "\r\n") if eol == "crlf" else new_text).encode("utf-8")
            staged = stage_write(source, cast(Path, root), data)
        if store:
            _BRIEF.memory(["reject", target, "--from-stdin"], json.dumps([
                {"old": by_n[n]["old"], "new": by_n[n]["new"], **({"reason": reasons[n]} if n in reasons else {})}
                for n in rejected]))
            stored = True
        elif staged is not None:
            _BRIEF.memory_json(["viewer"])  # nothing to store: ask T2 anyway, so a service that is down stops us before the write
        if staged is not None:
            staged.commit(raw, before_replace=_before_replace_hook())
    except BaseException as exc:
        if staged is not None:
            staged.discard()
        failure = _aborted(exc, stored=stored)
        if failure is exc:
            raise
        raise failure from exc
    applied_n = sorted(int(p["n"]) for p in placed)
    entry: Obj | None = None
    log_error: str | None = None
    # the stamp is fixed here, before the first try, and travels in the args the retry replays: a first put that
    # landed with its reply lost is then written over, not logged a second time
    stamp = cast(Any, _BRIEF._MEM)._now().strftime("%Y%m%dT%H%M%S.%fZ")
    log_args = ["log", target, "--genre", str(session["genre"]), "--stamp", stamp]
    log_payload: Obj = {
        "stdin": stdin, "edits": edits, "paragraphs": prop.get("paragraphs") or [],
        "queries": prop.get("queries") or [], "dropped": prop.get("dropped") or [],
        "note": prop.get("note"), "voice_card": prop.get("voice_card"),
        "accepted": sorted(accept), "applied": [] if stdin else applied_n, "skipped": [
            {"n": s["n"], "cause": s["cause"], "detail": s["detail"]} for s in skipped],
        "rejected": rejected, "held": held, "reasons": {str(n): reasons[n] for n in sorted(reasons)},
        "unplaced": [{"n": u["n"], "cause": u["cause"]} for u in left_alone],
    }
    try:
        if staged is not None:
            # the file is written: from here the session log is the only thing left to lose, so its
            # payload is kept in WORK until T2 has it, and `log-retry` can send it again
            (work / "log-pending.json").write_text(
                json.dumps({"args": log_args, "payload": log_payload}), encoding="utf-8")
        entry = _BRIEF.memory_json(log_args, json.dumps(log_payload))
    except Exception as exc:  # after the write, no failure of the log step may be a traceback
        if staged is None:
            # no file was written: stop here, WORK kept, the author can run apply again
            cause = exc if isinstance(exc, (Passthrough, UserError)) else _user(
                f"the session log failed ({type(exc).__name__}: {exc})")
            raise _aborted(cause, stored=stored) from exc
        log_error = str(exc) or type(exc).__name__
        sys.stderr.write(f"review.py: the file was written but the session log was not: {log_error}\n")
    if log_error is None:
        _BRIEF.remove_work(str(work))
    ordered = sorted(placed, key=lambda p: p["span"][0])
    out: Obj = {
        "mode": "stdin" if stdin else "edit", "path": rel,
        "accepted": [_shown(p) for p in ordered], "applied": [] if stdin else [_shown(p) for p in ordered],
        "skipped": skipped, "rejected": rejected, "held": held, "unplaced": left_alone,
        "rejections_stored": store, "log": entry,
    }
    if staged is not None and staged.beside_document:
        out["staged_beside_document"] = True
        sys.stderr.write("review.py: the new text was staged beside the document, not in the git directory "
                         "(they are on different devices)\n")
    if log_error is not None:
        out["log_error"] = log_error
        out["log_retry"] = f"log-retry --work {work}"
    if stdin:
        out["text"] = new_text
    return out


def cmd_log_retry(a: argparse.Namespace) -> Obj:
    """Send the session log an `apply` wrote to WORK but could not deliver, then delete WORK."""
    work = cast(Path, _BRIEF.work_dir(a.work))
    _BRIEF.touch_work(work)
    pending = work / "log-pending.json"
    if not pending.is_file():
        raise _user("no pending session log in this work directory: `apply` leaves one only when the file "
                    "was written and the log was not")
    saved = _load_json(pending, "WORK/log-pending.json")
    try:
        entry = _BRIEF.memory_json(cast("list[str]", saved["args"]), json.dumps(saved["payload"]))
    except (Passthrough, UserError):
        raise
    except Exception as exc:  # never a traceback; WORK is kept for another try
        raise _user(f"the session log failed again ({type(exc).__name__}: {exc}); WORK is kept, "
                    "run log-retry again") from exc
    _BRIEF.remove_work(str(work))
    return {"retried": True, "log": entry}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="review.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="write the marked-up copy and open it")
    r.add_argument("target")
    r.add_argument("--work", required=True)
    r.add_argument("--file")
    r.add_argument("--genre")
    ap = sub.add_parser("apply", help="take the author's answer: apply, store rejections, log, clean up")
    ap.add_argument("--work", required=True)
    ap.add_argument("--accept", default="none",
                    help="numbers to apply (default none: an edit nobody names is held)")
    ap.add_argument("--hold", help="numbers left undecided (an edit nobody names is held anyway)")
    ap.add_argument("--reject", help="numbers to store as rejections, or `rest`: every edit nobody else names")
    ap.add_argument("--reason", action="append", metavar="N=TEXT",
                    help="the author's reason for rejecting edit N (repeatable)")
    ap.add_argument("--reasons-file", metavar="FILE",
                    help="the reasons as a JSON object {\"2\": \"why\"} in a file inside WORK (written with the Write tool)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the interpreted sets and change nothing; a real apply needs one first")
    lr = sub.add_parser("log-retry", help="send the session log of an apply whose log failed, then delete WORK")
    lr.add_argument("--work", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        commands = {"render": cmd_render, "apply": cmd_apply, "log-retry": cmd_log_retry}
        _emit(commands[args.cmd](args))
    except Passthrough as exc:
        sys.stderr.write(f"{exc}\n")
        code = int(getattr(exc, "code", 1))
        if code == 3:
            sys.stderr.write("review.py: T2 is unavailable. Stop here and tell the author.\n")
        return code
    except UserError as exc:
        sys.stderr.write(f"review.py: {exc}\n")
        return 1
    except OSError as exc:  # the backstop: no traceback for a file system error nothing above named
        sys.stderr.write(f"review.py: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
