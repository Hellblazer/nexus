#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MarkupEditor suggestions for the prose-edit review (nexus-scmcs.2): translation and status mapping.

Pure functions, stdlib only; review.py loads this file by path, the way it loads brief.py. Nothing here
reads or writes a file or reaches T2.

  translate_edits(blocks, edits) -> (proposals, refused)
      Markdown edits {n, old, new, reason} become the entries of one propose_edits call. `blocks` is the
      `blocks` list of get_document. An edit that is not found verbatim in exactly one block, or that the
      app could not show (it crosses formatting, adds formatting, lies in a code block), is refused with a
      cause and goes to the chat route; the script never guesses.
  map_statuses(sent, suggestions) -> {"accept", "reject", "hold", "warnings"}
      list_suggestions statuses mapped back to edit numbers, for the ids this run's propose_edits made.
  change_core(old, new)
      The part of an edit that changes, cut at word edges.

The author never saves in the app (A1): the app only collects decisions, and `review.py apply` writes
the file. So there is nothing here that reads a saved document.
"""
from __future__ import annotations

import re
from html import unescape as _unescape
from typing import Any

Obj = dict[str, Any]
Mark = tuple[Any, ...]

# ---------------------------------------------------------------------------
# The part of an edit that changes
# ---------------------------------------------------------------------------


def change_core(old: str, new: str) -> tuple[str, str, str, str]:
    """(shared head, old part, new part, shared tail) of an edit, cut at word edges (nexus-ger02.19).

    The review strikes only the part that changes: a whole sentence struck and re-inserted hides a one-character
    change in two near-identical lines. The changed part grows to whole words, so a word is never split.
    """
    p = 0
    while p < min(len(old), len(new)) and old[p] == new[p]:
        p += 1
    s = 0
    while s < min(len(old), len(new)) - p and old[-1 - s] == new[-1 - s]:
        s += 1
    lo, hi_old, hi_new = p, len(old) - s, len(new) - s

    def word(c: str) -> bool:
        return c.isalnum() or c in "_'"

    def starts_word(part: str) -> bool:
        return bool(part) and word(part[0])

    while lo > 0 and word(old[lo - 1]) and (starts_word(old[lo:hi_old]) or starts_word(new[lo:hi_new])):
        lo -= 1
    tail = len(old) - hi_old
    while tail > 0 and word(old[hi_old]) and (old[lo:hi_old][-1:].isalnum() or new[lo:hi_new][-1:].isalnum()):
        hi_old += 1
        hi_new += 1
        tail -= 1
    return old[:lo], old[lo:hi_old], new[lo:hi_new], old[hi_old:]


# ---------------------------------------------------------------------------
# Visible text of a block's Markdown
# ---------------------------------------------------------------------------

_ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_TAG = re.compile(r"</?[A-Za-z][^<>]*>|<!--.*?-->", re.DOTALL)
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.\-]*:[^\s<>]*)>")
_ENTITY = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")
_LINE_PREFIX = re.compile(r"[ \t]*(?:(?:#{1,6}|[*+\-]|[0-9]{1,9}[.)]|>)[ \t]+)+")
_RULE_ROW = re.compile(r"[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*")
_SEP = "\x00"  # a cell edge in a table block: a find never holds it


class Parsed:
    """The visible text of one block, with what maps it back to the Markdown.

    vis: the rendered text. vmd[i]: the Markdown index of visible char i. mdv[j]: the visible index of Markdown
    char j, or None when that char is syntax (a marker, an escape, link syntax, a pipe). ctx[i]: the formatting
    runs visible char i lies in; two chars with the same ctx are in one run.
    """

    def __init__(self, md: str, kind: str = "") -> None:
        self.md = md
        self.chars: list[str] = []
        self.vmd: list[int] = []
        self.ctx: list[Mark] = []
        self.mdv: list[int | None] = [None] * len(md)
        self._table = "table" in kind.lower()
        pos = 0
        for line in md.split("\n"):
            start, end = pos, pos + len(line)
            if start:  # the newline before this line
                self._emit(start - 1, "\n", ())
            self._line(start, end)
            pos = end + 1
        self.vis = "".join(self.chars)

    def _emit(self, j: int, ch: str, ctx: Mark, *, mapped: bool = True) -> None:
        if mapped:
            self.mdv[j] = len(self.chars)
        self.chars.append(ch)
        self.vmd.append(j)
        self.ctx.append(ctx)

    def _line(self, start: int, end: int) -> None:
        line = self.md[start:end]
        if self._table:
            if _RULE_ROW.fullmatch(line) and "-" in line:
                return
            i = start
            for k in range(start, end):
                if self.md[k] == "|" and (k == start or self.md[k - 1] != "\\"):
                    self._range(i, k, ())
                    self._emit(k, _SEP, (), mapped=False)
                    i = k + 1
            self._range(i, end, ())
            return
        m = _LINE_PREFIX.match(line)
        self._range(start + (m.end() if m else 0), end, ())

    def _closer(self, mark: str, i: int, end: int) -> int | None:
        md, n = self.md, len(mark)
        c = mark[0]
        if i + n >= end or md[i + n].isspace():
            return None
        if c == "_" and i > 0 and md[i - 1].isalnum():
            return None
        x = i + n + 1
        while x <= end - n:
            if md.startswith(mark, x) and md[x - 1] != "\\" and not md[x - 1].isspace():
                after = md[x + n] if x + n < end else ""
                if n == 1 and (md[x - 1] == c or after == c):
                    x += 1
                    continue
                if c == "_" and after.isalnum():
                    x += 1
                    continue
                return x
            x += 1
        return None

    def _bracket(self, i: int, end: int) -> tuple[int, int, str] | None:
        """For `[` at i: (index of the matching `]`, index after `)`, the url) when `](...)` follows."""
        md = self.md
        depth, k = 0, i
        while k < end:
            if md[k] == "\\":
                k += 2
                continue
            if md[k] == "[":
                depth += 1
            elif md[k] == "]":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        else:
            return None
        if k + 1 >= end or md[k + 1] != "(":
            return None
        depth, q = 0, k + 1
        while q < end:
            if md[q] == "\\":
                q += 2
                continue
            if md[q] == "(":
                depth += 1
            elif md[q] == ")":
                depth -= 1
                if depth == 0:
                    return k, q + 1, md[k + 2:q]
            q += 1
        return None

    def _range(self, i: int, end: int, ctx: Mark) -> None:
        md = self.md
        while i < end:
            c = md[i]
            if c == "\\" and i + 1 < end and md[i + 1] in _ESCAPABLE:
                self._emit(i + 1, md[i + 1], ctx)
                i += 2
                continue
            if c == "`":
                n = 1
                while i + n < end and md[i + n] == "`":
                    n += 1
                m = re.compile(r"(?<!`)`{%d}(?!`)" % n).search(md, i + n, end)
                if m:
                    inner = range(i + n, m.start())
                    if len(inner) > 2 and md[inner[0]] == " " and md[inner[-1]] == " " and md[i + n:m.start()].strip():
                        inner = inner[1:-1]
                    for k in inner:
                        self._emit(k, md[k], (*ctx, "code"))
                    i = m.end()
                    continue
                for k in range(i, i + n):
                    self._emit(k, "`", ctx)
                i += n
                continue
            if c == "[" or (c == "!" and md.startswith("![", i)):
                image = c == "!"
                b = self._bracket(i + 1 if image else i, end)
                if b is not None:
                    close, after, url = b
                    self._range(i + (2 if image else 1), close, (*ctx, ("image" if image else "link", url)))
                    i = after
                    continue
            if c == "<":
                m2 = _AUTOLINK.match(md, i, end)
                if m2:
                    for k in range(m2.start(1), m2.end(1)):
                        self._emit(k, md[k], (*ctx, ("link", m2.group(1))))
                    i = m2.end()
                    continue
                m3 = _TAG.match(md, i, end)
                if m3:
                    i = m3.end()
                    continue
            if c == "&":
                m4 = _ENTITY.match(md, i, end)
                if m4:
                    text = _unescape(m4.group(0))
                    if text != m4.group(0) and len(text) == 1:
                        self._emit(i, text, ctx)
                        i = m4.end()
                        continue
            if c in "*_~":
                for mark, name in (("**", "strong"), ("__", "strong"), ("~~", "strike"), ("*", "em"), ("_", "em")):
                    if md.startswith(mark, i) and mark[0] == c:
                        k = self._closer(mark, i, end)
                        if k is not None:
                            self._range(i + len(mark), k, (*ctx, name))
                            i = k + len(mark)
                            break
                else:
                    self._emit(i, c, ctx)
                    i += 1
                continue
            self._emit(i, c, ctx)
            i += 1


def visible(md: str) -> str:
    """The rendered text of Markdown: the text a reader sees and MarkupEditor's `find` is matched against."""
    return Parsed(md).vis


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def _collapse(text: str) -> tuple[str, list[int], list[int]]:
    """`text` with each run of whitespace as one space, and for each char of the result its start and end in `text`."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i = 0
    while i < len(text):
        if text[i].isspace():
            j = i
            while j < len(text) and text[j].isspace():
                j += 1
            out.append(" ")
            starts.append(i)
            ends.append(j)
            i = j
        else:
            out.append(text[i])
            starts.append(i)
            ends.append(i + 1)
            i += 1
    return "".join(out), starts, ends


def _occurrences(text: str, needle: str) -> list[int]:
    out: list[int] = []
    i = text.find(needle)
    while i != -1:
        out.append(i)
        i = text.find(needle, i + 1)
    return out


class _Refusal(Exception):
    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(detail)
        self.cause = cause
        self.detail = detail


_BLANK_LINE = re.compile(r"\n[ \t]*\n")


class _Block:
    def __init__(self, raw: Obj) -> None:
        self.raw = raw
        self.kind = str(raw.get("kind", ""))
        self.md = str(raw.get("markdown", ""))
        self.cmd, self.starts, self.ends = _collapse(self.md)
        self._parsed: Parsed | None = None

    @property
    def parsed(self) -> Parsed:
        if self._parsed is None:
            self._parsed = Parsed(self.md, self.kind)
        return self._parsed


def _step(vis: str, a: int, b: int, side: str, lo: int, hi: int) -> tuple[int, int] | None:
    """[a, b) grown by one whole word on `side` ("L" or "R"), inside [lo, hi); None when it cannot grow."""
    if side == "L":
        i = a
        if i <= lo:
            return None
        if vis[i - 1].isspace():
            while i > lo and vis[i - 1].isspace():
                i -= 1
        while i > lo and not vis[i - 1].isspace():
            i -= 1
        return (i, b) if i < a else None
    j = b
    if j >= hi:
        return None
    if vis[j].isspace():
        while j < hi and vis[j].isspace():
            j += 1
    while j < hi and not vis[j].isspace():
        j += 1
    return (a, j) if j > b else None


def _usable(p: Parsed, a: int, b: int) -> bool:
    """True when vis[a:b] holds no line break or cell edge and lies in one run of formatting."""
    chunk = p.vis[a:b]
    return "\n" not in chunk and _SEP not in chunk and len(set(p.ctx[a:b])) <= 1


def _plan(block: _Block, c0: int, cold: str, cnew: str) -> tuple[str, str]:
    """(find, replace) for the edit cold -> cnew whose old text starts at collapsed index c0 of `block`."""
    p = block.parsed
    head, core_old, core_new, _tail = change_core(cold, cnew)
    if core_new and Parsed(core_new).vis != core_new:
        raise _Refusal("formatting", "the new text carries Markdown formatting (emphasis, code, a link or a tag), "
                                     "which the app cannot add; decide this edit in the chat")
    a_c = c0 + len(head)
    old_md = [block.starts[c0], block.ends[c0 + len(cold) - 1]]
    seen = [p.mdv[j] for j in range(old_md[0], old_md[1]) if p.mdv[j] is not None]
    if not seen:
        raise _Refusal("formatting", "the old text holds no visible text")
    lo, hi = min(_visible_only(seen)), max(_visible_only(seen)) + 1
    if core_old:
        first, last = block.starts[a_c], block.ends[a_c + len(core_old) - 1]
        vs = [p.mdv[j] for j in range(first, last)]
        if any(v is None for v in vs):
            raise _Refusal("formatting", "the changed text includes Markdown syntax (a marker, a link or a list "
                                         "or table mark), which the app cannot show; decide this edit in the chat")
        a = _visible_only(vs)[0]
        b = _visible_only(vs)[-1] + 1
        if b - a != len(vs) or p.vis[a:b] != block.md[first:last]:
            raise _Refusal("formatting", "the changed text does not match the text the app shows")
        repl = core_new
        if "\n" in p.vis[a:b] or _SEP in p.vis[a:b]:
            raise _Refusal("spans-lines", "the changed text spans lines or table cells")
        if not _usable(p, a, b):
            raise _Refusal("formatting", "the changed text crosses a formatting run (bold, italic, code or a link)")
    else:
        prev = block.ends[a_c - 1] - 1 if head else None
        pv = p.mdv[prev] if prev is not None else None
        nxt = block.starts[a_c] if a_c < c0 + len(cold) else None
        nv = p.mdv[nxt] if nxt is not None else None
        if pv is not None and nv is not None and nv != pv + 1:
            raise _Refusal("formatting", "the insertion point lies beside Markdown syntax")
        if pv is not None:
            at = pv + 1
        elif nv is not None:
            at = nv
        else:
            raise _Refusal("formatting", "the insertion point lies beside Markdown syntax")
        a = b = at
        grown = None
        for side in ("L", "R"):
            grown = _step(p.vis, a, b, side, lo, hi)
            if grown and _usable(p, *grown):
                break
            grown = None
        if grown is None:
            raise _Refusal("ambiguous", "no word of the old text can anchor the insertion")
        repl = p.vis[grown[0]:at] + core_new + p.vis[at:grown[1]]
        a, b = grown
    # widen by whole neighbouring words, left then right, until the find is unique in the block's visible text
    dead = {"L": False, "R": False}
    blocked = False
    side = "L"
    while len(_occurrences(p.vis, p.vis[a:b])) != 1:
        if dead["L"] and dead["R"]:
            raise _Refusal("formatting" if blocked else "ambiguous",
                           "the text to find is not unique in its block inside one run of formatting; "
                           "decide this edit in the chat" if blocked else
                           "the text to find occurs more than once in its block, even with the words around it")
        grown = _step(p.vis, a, b, side, lo, hi)
        if grown is not None and _usable(p, *grown):
            repl = p.vis[grown[0]:a] + repl + p.vis[b:grown[1]]
            a, b = grown
        else:
            if grown is not None:
                blocked = True
            dead[side] = True
        side = "R" if side == "L" else "L"
    find = p.vis[a:b]
    if find == repl:
        raise _Refusal("no-change", "the edit changes nothing in the visible text")
    return find, repl


def _visible_only(values: list[int | None]) -> list[int]:
    return [v for v in values if v is not None]


def _translate_one(blocks: list[_Block], edit: Obj) -> Obj:
    old, new = str(edit["old"]), str(edit["new"])
    if _BLANK_LINE.search(old) or _BLANK_LINE.search(new):
        raise _Refusal("spans-lines", "the edit spans a paragraph break")
    cold, _, _ = _collapse(old)
    cnew, _, _ = _collapse(new)
    if cold == cnew:
        raise _Refusal("no-change", "old and new differ only in whitespace")
    hits = [(blk, c) for blk in blocks for c in _occurrences(blk.cmd, cold)]
    if not hits:
        raise _Refusal("not-found", "the old text is not in any block of the document the app shows")
    if len(hits) > 1:
        raise _Refusal("ambiguous", f"the old text occurs {len(hits)} times in the document the app shows")
    blk, c0 = hits[0]
    kind = blk.kind.lower()
    if "code" in kind:
        raise _Refusal("code-block", "the old text is in a code block; the app suggests prose only")
    if "meta" in kind or "front" in kind:
        raise _Refusal("metadata", "the old text is in the metadata block")
    find, repl = _plan(blk, c0, cold, cnew)
    reason = str(edit.get("reason") or "").strip()
    return {"n": edit["n"], "block": blk.raw["index"], "expected": blk.raw["hash"], "find": find,
            "replace": repl, "note": f"Edit {edit['n']}: {reason}" if reason else f"Edit {edit['n']}"}


def translate_edits(blocks: list[Obj], edits: list[Obj]) -> tuple[list[Obj], list[Obj]]:
    """(proposals, refused): the propose_edits entries for `edits`, in call order, and the edits that go to the chat.

    proposals: {"n", "block", "expected", "find", "replace", "note"}; refused: {"n", "cause", "detail"} with
    cause one of not-found, ambiguous, formatting, code-block, metadata, spans-lines, no-change.
    """
    infos = [_Block(b) for b in blocks]
    proposals: list[Obj] = []
    refused: list[Obj] = []
    for edit in edits:
        try:
            proposals.append(_translate_one(infos, edit))
        except _Refusal as r:
            refused.append({"n": edit["n"], "cause": r.cause, "detail": r.detail})
    return proposals, refused


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

_STATUS = {"accepted": "accept", "rejected": "reject", "pending": "hold", "stale": "hold"}


def map_statuses(sent: list[Obj], suggestions: list[Obj]) -> Obj:
    """The author's decisions in the app as edit numbers, for the suggestions THIS run made.

    `sent` is [{"n", "id", "find"}] from the `proposed` verb. accepted -> accept, rejected -> reject,
    pending or stale -> hold. An id the list does not return, or returns with a different `find` (ids can
    name another suggestion after the document is reopened), is held with a warning. Ids in the list that
    were never sent are ignored: list_suggestions also returns the suggestions of earlier documents.
    """
    by_id = {str(s.get("id")): s for s in suggestions if isinstance(s, dict)}
    out: dict[str, list[int]] = {"accept": [], "reject": [], "hold": []}
    warnings: list[str] = []
    for item in sent:
        n, sid = int(item["n"]), str(item["id"])
        got = by_id.get(sid)
        if got is None:
            out["hold"].append(n)
            warnings.append(f"edit {n}: suggestion {sid} is no longer listed by the app; held")
        elif got.get("find") != item.get("find"):
            out["hold"].append(n)
            warnings.append(f"edit {n}: suggestion {sid} now names different text than was sent; held")
        elif got.get("status") in _STATUS:
            out[_STATUS[str(got["status"])]].append(n)
        else:
            out["hold"].append(n)
            warnings.append(f"edit {n}: suggestion {sid} has status {got.get('status')!r}; held")
    return {**{k: sorted(v) for k, v in out.items()}, "warnings": warnings}
