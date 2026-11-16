#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MarkupEditor suggestions for the prose-edit review (nexus-scmcs.2): translation and status mapping.

Pure functions, stdlib only; review.py loads this file by path, the way it loads brief.py. Nothing here
reads or writes a file or reaches T2.

  translate_edits(blocks, edits) -> (proposals, refused)
      Markdown edits {n, old, new, reason} become the entries of one propose_edits call. `blocks` is the
      `blocks` list of get_document. `old` is written in the file's Markdown but the blocks are the app's own
      serialization of it (emphasis, escapes, entities and links come back in other forms), so `old` is found
      verbatim when it can be and otherwise by the text a reader sees. An edit that is not found in exactly one
      block, or that the app could not show (it crosses formatting, adds formatting, lies in a code block), is
      refused with a cause and goes to the chat route; the script never guesses.
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
_BR = "\x01"  # a hard line break: the app holds it as a node, not as text, so a find never holds it


class Parsed:
    """The visible text of one block, with what maps it back to the Markdown.

    vis: the rendered text. vmd[i]: the Markdown index of visible char i. mdv[j]: the visible index of Markdown
    char j, or None when that char is syntax (a marker, an escape, link syntax, a pipe). ctx[i]: the formatting
    runs visible char i lies in; two chars with the same ctx are in one run. atoms: the Markdown ranges
    (start, end, label) that stand for one visible char or none and so must be edited whole: an escape, an
    entity, a hard break. `refs` reads `[text][label]` as a link, which the file writes and the app never does.
    """

    def __init__(self, md: str, kind: str = "", *, refs: bool = False) -> None:
        self.md = md
        self.chars: list[str] = []
        self.vmd: list[int] = []
        self.ctx: list[Mark] = []
        self.atoms: list[tuple[int, int, str]] = []
        self.mdv: list[int | None] = [None] * len(md)
        self._table = kind == "table"
        self._refs = refs
        lines = md.split("\n")
        pos = 0
        broke = False
        for k, line in enumerate(lines):
            start, end = pos, pos + len(line)
            if start:  # the newline before this line
                self._emit(start - 1, _BR if broke else "\n", ())
            stop, broke = end, False
            if not self._table and k < len(lines) - 1:
                stop, broke = self._hard_break(line, start, end)
            self._line(start, stop)
            pos = end + 1
        self.vis = "".join(self.chars)

    def _hard_break(self, line: str, start: int, end: int) -> tuple[int, bool]:
        """(where the text of the line stops, whether the line ends in a hard break): two spaces or an odd backslash run."""
        body = line.rstrip(" ")
        if len(line) - len(body) >= 2 and body.strip():
            self.atoms.append((start + len(body), end + 1, "break"))
            return start + len(body), True
        run = len(line) - len(line.rstrip("\\"))
        if run % 2 == 1 and line[:-run].strip():
            self.atoms.append((end - 1, end + 1, "break"))
            return end - 1, True
        return end, False

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

    def _close(self, i: int, end: int) -> int | None:
        """For `[` at i: the index of the matching `]`."""
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
                    return k
            k += 1
        return None

    def _reference(self, i: int, end: int) -> tuple[int, int, str] | None:
        """For `[` at i: (index of the `]`, index after the label, the label) when `][label]` or `][]` follows."""
        k = self._close(i, end)
        if k is None or k + 1 >= end or self.md[k + 1] != "[":
            return None
        q = k + 2
        while q < end and self.md[q] not in "[]":
            q += 1
        if q >= end or self.md[q] != "]":
            return None
        label = self.md[k + 2:q] or self.md[i + 1:k]
        return k, q + 1, " ".join(label.lower().split())

    def _bracket(self, i: int, end: int) -> tuple[int, int, str] | None:
        """For `[` at i: (index of the matching `]`, index after `)`, the url) when `](...)` follows."""
        md = self.md
        k = self._close(i, end)
        if k is None:
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
                self.atoms.append((i, i + 2, "escape"))
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
                r = self._reference(i, end) if self._refs and not image else None
                if r is not None:
                    close, after, label = r
                    self._range(i + 1, close, (*ctx, ("link", "ref:" + label)))
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
                        self.atoms.append((i, m4.end(), "entity"))
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

MAX_EDITS = 200  # one propose_edits call takes 1 to 200 edits
MAX_TEXT = 2000  # the longest replace or note the app takes

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")  # what the app rejects in replace and note
_SPACES = re.compile(r"\s+")


def _clean(text: str) -> str:
    """One line of plain text: every run of whitespace, line breaks included, is one space; control characters go."""
    return _CONTROL.sub("", _SPACES.sub(" ", text))


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
        self._cvis: tuple[str, list[int], list[int]] | None = None

    @property
    def parsed(self) -> Parsed:
        if self._parsed is None:
            self._parsed = Parsed(self.md, self.kind)
        return self._parsed

    @property
    def cvis(self) -> tuple[str, list[int], list[int]]:
        """The visible text with whitespace runs collapsed, and each char's span in the visible text."""
        if self._cvis is None:
            self._cvis = _collapse(self.parsed.vis)
        return self._cvis


class _Frame:
    """The text an edit's `old` is read through.

    src is the Markdown the edit's raw indexes refer to and p its parse; starts and ends map the collapsed old text
    (from collapsed index c0 on) to src. When src is the block's own Markdown, visible indexes of p are the block's.
    When src is `old` itself (found by its visible text), `shift` maps a visible index of p to its span in the block.
    """

    def __init__(self, src: str, p: Parsed, starts: list[int], ends: list[int], c0: int,
                 shift: list[tuple[int, int]] | None = None) -> None:
        self.src, self.p, self.starts, self.ends, self.c0, self._shift = src, p, starts, ends, c0, shift

    def s(self, i: int) -> int:
        return i if self._shift is None else self._shift[i][0]

    def e(self, i: int) -> int:
        return i + 1 if self._shift is None else self._shift[i][1]


def _visible_frame(blk: _Block, po: Parsed, src: str, bc0: int) -> _Frame:
    """The frame for an `old` whose visible text sits at collapsed index bc0 of the block's visible text."""
    _, vst, vend = _collapse(po.vis)
    _, bst, bend = blk.cvis
    shift: list[tuple[int, int]] = [(0, 0)] * len(po.vis)
    for ci, (a, b) in enumerate(zip(vst, vend)):
        for i in range(a, b):
            shift[i] = (bst[bc0 + ci], bend[bc0 + ci])
    _, starts, ends = _collapse(src)
    return _Frame(src, po, starts, ends, 0, shift)


def _star_emphasis(text: str, p: Parsed) -> str:
    """`text` with each `_` that is an emphasis marker written as `*`: the same text for a reader.

    change_core treats `_` as a letter, so it would take `_really_` -> `_truly_` as a change of the whole run with its
    markers. A `*` is not a letter. The length is unchanged, so every index into `text` still holds.
    """
    return "".join("*" if ch == "_" and p.mdv[j] is None else ch for j, ch in enumerate(text))


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
    """True when vis[a:b] holds no line break, hard break or cell edge and lies in one run of formatting."""
    chunk = p.vis[a:b]
    return "\n" not in chunk and _SEP not in chunk and _BR not in chunk and len(set(p.ctx[a:b])) <= 1


_BREAK_REFUSAL = ("the text crosses a hard line break, which the app holds as a break and not as text; "
                  "decide this edit in the chat")


def _atom_scan(p: Parsed, lo: int, hi: int, *, core: bool) -> set[int]:
    """Check Markdown [lo, hi) against the parse's escapes, entities and hard breaks; the indexes of those it holds whole.

    A range that starts or ends inside one is refused, since where it lies is a guess (Gate 1 F4). A hard break is
    also refused when the changed text (`core`) holds any of it: the app cannot find across one.
    """
    inside: set[int] = set()
    for s, e, label in p.atoms:
        if s >= hi or e <= lo:
            continue
        whole = lo <= s and e <= hi
        if label == "break" and (core or not whole):
            raise _Refusal("spans-lines", _BREAK_REFUSAL)
        if not whole:
            raise _Refusal("formatting", "the old text starts or ends inside " + (
                "an HTML entity" if label == "entity" else "a backslash escape") + ", so where it lies is a guess; "
                "decide this edit in the chat")
        inside.update(range(s, e))
    return inside


def _plan(block: _Block, fr: _Frame, cold: str, cnew: str) -> tuple[str, str, int, int]:
    """(find, replace, start, end) for the edit cold -> cnew read through `fr`; start and end are the find's place in the block's visible text."""
    bp = block.parsed
    p = fr.p
    head, core_old, core_new, _tail = change_core(cold, cnew)
    if core_new and Parsed(core_new).vis != core_new:
        raise _Refusal("formatting", "the new text carries Markdown formatting (emphasis, code, a link or a tag), "
                                     "which the app cannot add; decide this edit in the chat")
    a_c = fr.c0 + len(head)
    o0, o1 = fr.starts[fr.c0], fr.ends[fr.c0 + len(cold) - 1]
    _atom_scan(p, o0, o1, core=False)
    seen = _visible_only([p.mdv[j] for j in range(o0, o1)])
    if not seen:
        raise _Refusal("formatting", "the old text holds no visible text")
    lo, hi = fr.s(min(seen)), fr.e(max(seen))
    if core_old:
        first, last = fr.starts[a_c], fr.ends[a_c + len(core_old) - 1]
        inside = _atom_scan(p, first, last, core=True)
        vs: list[int] = []
        for j in range(first, last):
            v = p.mdv[j]
            if v is None:
                if j in inside:
                    continue
                raise _Refusal("formatting", "the changed text includes Markdown syntax (a marker, a link or a list "
                                             "or table mark), which the app cannot show; decide this edit in the chat")
            vs.append(v)
        if not vs or vs[-1] - vs[0] + 1 != len(vs):
            raise _Refusal("formatting", "the changed text does not match the text the app shows")
        a, b = fr.s(vs[0]), fr.e(vs[-1])
        repl = core_new
        if "\n" in bp.vis[a:b] or _SEP in bp.vis[a:b] or _BR in bp.vis[a:b]:
            raise _Refusal("spans-lines", "the changed text spans lines or table cells")
        if not _usable(bp, a, b):
            raise _Refusal("formatting", "the changed text crosses a formatting run (bold, italic, code or a link)")
    else:
        cut = fr.ends[a_c - 1] if head else fr.starts[a_c]
        if any(s < cut < e for s, e, _label in p.atoms):
            raise _Refusal("formatting", "the insertion point lies inside an escape, an entity or a hard break")
        prev = fr.ends[a_c - 1] - 1 if head else None
        pv = p.mdv[prev] if prev is not None else None
        nxt = fr.starts[a_c] if a_c < fr.c0 + len(cold) else None
        nv = p.mdv[nxt] if nxt is not None else None
        if pv is not None and nv is not None and nv != pv + 1:
            raise _Refusal("formatting", "the insertion point lies beside Markdown syntax")
        if pv is not None:
            at = fr.e(pv)
        elif nv is not None:
            at = fr.s(nv)
        else:
            raise _Refusal("formatting", "the insertion point lies beside Markdown syntax")
        a = b = at
        grown = None
        for side in ("L", "R"):
            grown = _step(bp.vis, a, b, side, lo, hi)
            if grown and _usable(bp, *grown):
                break
            grown = None
        if grown is None:
            raise _Refusal("ambiguous", "no word of the old text can anchor the insertion")
        repl = bp.vis[grown[0]:at] + core_new + bp.vis[at:grown[1]]
        a, b = grown
    # widen by whole neighbouring words, left then right, until the find is unique in the block's visible text
    dead = {"L": False, "R": False}
    blocked = False
    side = "L"
    while len(_occurrences(bp.vis, bp.vis[a:b])) != 1:
        if dead["L"] and dead["R"]:
            raise _Refusal("formatting" if blocked else "ambiguous",
                           "the text to find is not unique in its block inside one run of formatting; "
                           "decide this edit in the chat" if blocked else
                           "the text to find occurs more than once in its block, even with the words around it")
        grown = _step(bp.vis, a, b, side, lo, hi)
        if grown is not None and _usable(bp, *grown):
            repl = bp.vis[grown[0]:a] + repl + bp.vis[b:grown[1]]
            a, b = grown
        else:
            if grown is not None:
                blocked = True
            dead[side] = True
        side = "R" if side == "L" else "L"
    find = bp.vis[a:b]
    if find == repl:
        raise _Refusal("no-change", "the edit changes nothing in the visible text")
    return find, repl, a, b


def _visible_only(values: list[int | None]) -> list[int]:
    return [v for v in values if v is not None]


def _translate_one(blocks: list[_Block], edit: Obj, taken: dict[int, list[tuple[int, int, int]]]) -> Obj:
    old, new = str(edit["old"]), str(edit["new"])
    if _BLANK_LINE.search(old) or _BLANK_LINE.search(new):
        raise _Refusal("spans-lines", "the edit spans a paragraph break")
    cold, _, _ = _collapse(old)
    cnew, _, _ = _collapse(new)
    if cold == cnew:
        raise _Refusal("no-change", "old and new differ only in whitespace")
    # `old` is the file's Markdown and the blocks are the app's: find it verbatim, else by the text a reader sees
    by_md = [(blk, c) for blk in blocks for c in _occurrences(blk.cmd, cold)]
    po = Parsed(old, refs=True)
    vold = _collapse(po.vis)[0]
    by_text = [(blk, c) for blk in blocks for c in _occurrences(blk.cvis[0], vold)] if vold.strip() else []
    if len(by_md) > 1 or len(by_text) > 1 or (by_md and by_text and by_md[0][0] is not by_text[0][0]):
        raise _Refusal("ambiguous", f"the old text occurs {max(len(by_md), len(by_text), 2)} times in the document the app shows")
    if not by_md and not by_text:
        raise _Refusal("not-found", "the old text is not in any block of the document the app shows")
    blk = (by_md or by_text)[0][0]
    if blk.kind == "code":
        raise _Refusal("code-block", "the old text is in a code block; the app suggests prose only")
    if blk.kind in ("metadata", "html-preamble"):
        raise _Refusal("metadata", f"the old text is in the {blk.kind} block, which the app does not edit")
    if blk.kind == "image":
        raise _Refusal("formatting", "the old text is in an image block, whose only text is its alt text")
    if by_md:
        find, repl, a, b = _plan(blk, _Frame(blk.md, blk.parsed, blk.starts, blk.ends, by_md[0][1]), cold, cnew)
    else:
        old_n = _star_emphasis(old, po)
        new_n = _star_emphasis(new, Parsed(new, refs=True))
        cold_n, cnew_n = _collapse(old_n)[0], _collapse(new_n)[0]
        if cold_n == cnew_n:
            raise _Refusal("no-change", "old and new differ only in whitespace")
        find, repl, a, b = _plan(blk, _visible_frame(blk, po, old_n, by_text[0][1]), cold_n, cnew_n)
    repl = _clean(repl)
    if len(repl) > MAX_TEXT:
        raise _Refusal("too-long", f"the new text is {len(repl)} characters; the app takes at most {MAX_TEXT} in a "
                                   "replacement and it is never cut. Decide this edit in the chat")
    if find == repl:
        raise _Refusal("no-change", "the edit changes nothing in the visible text")
    index = int(blk.raw["index"])
    for x, y, other in taken.get(index, []):
        if a < y and x < b:
            raise _Refusal("overlap", f"the text to find overlaps the one for edit {other} in the same block, "
                                      "and the app refuses a suggestion over a pending one; decide this edit in the chat")
    taken.setdefault(index, []).append((a, b, int(edit["n"])))
    reason = _clean(str(edit.get("reason") or "")).strip()
    return {"n": edit["n"], "block": blk.raw["index"], "expected": blk.raw["hash"], "find": find,
            "replace": repl, "note": (f"Edit {edit['n']}: {reason}" if reason else f"Edit {edit['n']}")[:MAX_TEXT]}


def translate_edits(blocks: list[Obj], edits: list[Obj]) -> tuple[list[Obj], list[Obj]]:
    """(proposals, refused): the propose_edits entries for `edits`, in call order, and the edits that go to the chat.

    proposals: {"n", "block", "expected", "find", "replace", "note"}, at most MAX_EDITS; refused: {"n", "cause",
    "detail"} with cause one of not-found, ambiguous, formatting, code-block, metadata, spans-lines, no-change,
    too-long (a replacement over 2000 characters), overlap (its find overlaps an earlier edit's in one block) and
    over-limit (past the 200 edits one call takes).
    """
    infos = [_Block(b) for b in blocks]
    proposals: list[Obj] = []
    refused: list[Obj] = []
    taken: dict[int, list[tuple[int, int, int]]] = {}
    for edit in edits:
        try:
            proposals.append(_translate_one(infos, edit, taken))
        except _Refusal as r:
            refused.append({"n": edit["n"], "cause": r.cause, "detail": r.detail})
    for extra in proposals[MAX_EDITS:]:
        refused.append({"n": extra["n"], "cause": "over-limit",
                        "detail": f"one propose_edits call takes at most {MAX_EDITS} edits; decide this one in the chat"})
    return proposals[:MAX_EDITS], refused

# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

_STATUS = {"accepted": "accept", "rejected": "reject", "pending": "hold", "stale": "hold"}
_STRAIGHT = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})  # the app straightens curly quotes in replace


def _is_mine(item: Obj, got: Obj) -> str | None:
    """None when the listed suggestion `got` is the one `item` was sent as; else what differs.

    An id alone proves nothing: ids restart when the app reopens a document, and another run can send the same find.
    The id, the find, the replacement and the note's "Edit <n>" must all agree.
    """
    if got.get("find") != item.get("find"):
        return "the text to find"
    if str(got.get("replace")).translate(_STRAIGHT) != str(item.get("replace")).translate(_STRAIGHT):
        return "the replacement"
    if not re.match(rf"Edit {int(item['n'])}(?![0-9])", str(got.get("note") or "")):
        return "the edit number in its note"
    return None


def map_statuses(sent: list[Obj], suggestions: list[Obj]) -> Obj:
    """The author's decisions in the app as edit numbers, for the suggestions THIS run made.

    `sent` is [{"n", "id", "find", "replace"}] from the `proposed` verb. accepted -> accept, rejected -> reject,
    pending or stale -> hold. An id the list does not return, or returns for another suggestion (its find, its
    replacement or the "Edit <n>" its note starts with differs), is held with a warning. Ids in the list that
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
            continue
        differs = _is_mine(item, got)
        if differs is not None:
            out["hold"].append(n)
            warnings.append(f"edit {n}: suggestion {sid} is not the one sent ({differs} differs); held")
        elif got.get("status") in _STATUS:
            out[_STATUS[str(got["status"])]].append(n)
        else:
            out["hold"].append(n)
            warnings.append(f"edit {n}: suggestion {sid} has status {got.get('status')!r}; held")
    return {**{k: sorted(v) for k, v in out.items()}, "warnings": warnings}
