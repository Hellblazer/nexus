#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in MarkupEditor MCP server for the prose-edit acceptance runners (nexus-scmcs.3).

The path-run review of the prose-edit skill needs MarkupEditor's open_document, get_document, propose_edits,
list_suggestions and undo tools, which a headless `claude -p` session cannot reach. run-scenario.sh points a session at
this server (`--mcp-config`, under the server name `markupeditor`) when PROSE_EDIT_APP_RUNNER=1. It is a stand-in and
nothing more: what it says about the app is what tests/prose_edit/fixtures/markupeditor/ recorded.

  usage: markup_standin.py [serve]                     serve the tools over stdio (the `mcp` SDK is needed)
         markup_standin.py mcp-config --out FILE ...   write the --mcp-config file that launches it (stdlib only)

What it reproduces (NOTES.md there):
  - reply shapes, key for key (get_document, propose_edits, list_suggestions); the block hash is a stand-in of the
    app's shape (14 hex digits, below 2**53), not its value
  - the layout the app reads a file in: wrapped paragraphs unwrapped, `-` lists as `*`, table rules as `| --- |`
  - edits are located by visible text, one paragraph, list item or table cell at a time, and refused when the find is
    absent, crosses a formatting boundary, hits a code block, writes the same text, or overlaps a pending suggestion
  - quirk e: suggestion ids keep counting when another document is opened, and list_suggestions keeps returning the
    earlier documents' entries with nothing that says which document each belongs to
  - reopening the SAME document drops that document's suggestions and restarts the ids (NOTES quirk e only covers
    opening another document; this half is the app's own server instructions: "suggestions are lost when the document
    is reopened ... ids restart after the document is reopened". Not yet confirmed on the app. The ids restart after
    the highest id another document's entries still hold, so two entries never share an id)
  - NOTES h, live run 3: an accepted suggestion sets a dirty mark that undo does not clear. Undo steps back through
    the accepted changes and then says "There is nothing to undo" with hasChanges still true. Only open_document or
    save_document clears it
  - quirk f is NOT reproduced: open_document finishes loading before it replies
  - quirk g is NOT reproduced: the app's serializer rewrites what it saves (`_x_` to `*x*`, `&amp;` to `&`, `[sic]`
    escaping, reference links to inline links), and this server writes the Markdown it was given

The app decides, so the author's decisions are statuses in list_suggestions. A script says what the author decided: a
JSON table (MARKUP_STANDIN_DECISIONS) maps a scenario name (MARKUP_STANDIN_SCENARIO, an fnmatch pattern, the first
match wins) to a rule {"accepted": [edit numbers], "rejected": [edit numbers], "default": "pending|accepted|rejected"}.
The rule applies when that scenario's session calls list_suggestions, to the suggestions still pending, by the number
in the note ("Edit <n>"), and a decision made is kept. A scenario with no rule leaves everything pending: the author
has not looked yet, or held every edit.

What it invents (no fixture shows it): the success replies of undo and save_document, the error texts (no document is
open, an edit that is not an object, cannot open a file), and the text of every refusal except the three the fixtures
hold verbatim (formatting boundary, no-op, code block). The replies for the unsaved-document branch (undo with nothing
to undo, the Reload from Disk warning) are imitations, not recorded from the real app; the real-app facts are an open
gap that needs the Mac. Each tool call is appended to MARKUP_STANDIN_CALLS (a JSON line: the tool, its
arguments, the outcome, the decisions the rule applied, any `invented` reply, any `ambiguous` find) so a stand-in run
can be read for what it did and where it made something up. An edit whose find occurs more than once in its block is
located at the FIRST occurrence (as written, not confirmed against the app) and flagged `ambiguous` in that log. An
accepted edit does not touch get_document's text: the text changes in the app only when the file is reloaded, so the
stand-in keeps the blocks as opened until the next open_document.

State (the suggestions, the id counter, the open document) lives in MARKUP_STANDIN_STATE when set, so a resumed session,
which starts a new server process, finds what the last one proposed. Unset, it lives in the process.
"""
from __future__ import annotations

import argparse
import functools
import fnmatch
import hashlib
import inspect
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Any

HERE = Path(__file__).resolve()
SERVER_NAME = "markupeditor"
STATUSES = ("pending", "accepted", "rejected")
NOT_FOUND = ("find not found in block {block}; matching is exact and case-sensitive against the visible text of one "
             "paragraph, list item or table cell, without Markdown syntax (leave out ** _ ` ~~ [ ]( ) ![]( ) # list "
             "markers and backslash escapes; inline code matches without its backticks)")
CROSSES = ("find crosses a formatting boundary (bold, code, link...); split the edit so each part lies inside one run "
           "of formatting")
IS_CODE = "block {block} is a code block; suggestions are for prose"
NO_OP = "replace is the same as find; that edit is a no-op"
NOTHING_TO_UNDO = "There is nothing to undo"   # recorded: NOTES h, live run 3


class StandinError(Exception):
    """A tool call the app would answer with an error."""


class _Refused(Exception):
    """One edit of a propose_edits call the app refuses. `recorded` is True when the text is one the fixtures hold
    (fixtures/markupeditor/propose_edits.json): the formatting-boundary, no-op and code-block refusals."""

    def __init__(self, text: str, *, recorded: bool = False) -> None:
        super().__init__(text)
        self.recorded = recorded


def render(reply: dict[str, Any]) -> str:
    """The reply as the app sends it: JSON text, keys sorted."""
    return json.dumps(reply, indent=2, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Decision rules
# ---------------------------------------------------------------------------


def check_rule(rule: Any) -> dict[str, Any]:
    """The rule, or ValueError saying what is wrong with it."""
    if not isinstance(rule, dict) or not set(rule) <= {"accepted", "rejected", "default"}:
        raise ValueError(f"a decision rule has only accepted, rejected and default: {rule!r}")
    lists: dict[str, list[int]] = {}
    for key in ("accepted", "rejected"):
        value = rule.get(key, [])
        if not isinstance(value, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in value):
            raise ValueError(f"{key} is a list of edit numbers: {value!r}")
        lists[key] = value
    if set(lists["accepted"]) & set(lists["rejected"]):
        raise ValueError(f"an edit is both accepted and rejected: {rule!r}")
    if rule.get("default", "pending") not in STATUSES:
        raise ValueError(f"default is one of {', '.join(STATUSES)}: {rule.get('default')!r}")
    return rule


def rule_for(scenario: str, table: dict[str, Any]) -> dict[str, Any] | None:
    """The rule of the first pattern in `table` that matches the scenario's name; None when no pattern does."""
    for pattern, rule in table.items():
        if fnmatch.fnmatchcase(scenario, pattern):
            return check_rule(rule)
    return None


# ---------------------------------------------------------------------------
# Reading a Markdown file as the app does
# ---------------------------------------------------------------------------

_LIST_ITEM = re.compile(r"^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _unwrap(lines: list[str]) -> str:
    """Lines of one paragraph as one line; a hard break (a trailing backslash) stays a break."""
    out = ""
    for k, raw in enumerate(lines):
        line = raw.strip()
        out += line
        if k < len(lines) - 1:
            out += "\n" if line.endswith("\\") else " "
    return out


def _cells(row: str) -> list[str]:
    body = row.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [c.strip() for c in re.split(r"(?<!\\)\|", body)]


def _starts_block(line: str) -> bool:
    s = line.lstrip()
    return bool(_HEADING.match(line) or s.startswith(("```", ">", "|")) or _LIST_ITEM.match(line))


def parse_blocks(text: str) -> list[dict[str, Any]]:
    """[{kind, markdown, units}]: the blocks of the file, in the app's Markdown, with the units edits are located in."""
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.lstrip().startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("```"):
                j += 1
            blocks.append({"kind": "code", "markdown": "\n".join(lines[i:j + 1]), "units": []})
            i = j + 1
        elif (m := _HEADING.match(line)):
            blocks.append({"kind": "heading", "markdown": f"{m.group(1)} {m.group(2)}", "units": [m.group(2)]})
            i += 1
        elif line.lstrip().startswith(">"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith(">"):
                j += 1
            quoted = _unwrap([re.sub(r"^\s*>\s?", "", row) for row in lines[i:j]])
            blocks.append({"kind": "quote", "markdown": f"> {quoted}", "units": [quoted]})
            i = j
        elif line.lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            rows = [_cells(r) for r in lines[i:j]]
            shown = ["| " + " | ".join("---" if all(re.fullmatch(r":?-+:?", c) for c in r if c) and any(r) else c
                                       for c in r) + " |" for r in rows]
            units = [c for r in rows if not all(re.fullmatch(r":?-+:?", c) for c in r if c) or not any(r) for c in r]
            blocks.append({"kind": "table", "markdown": "\n".join(shown), "units": units})
            i = j
        elif (m := _LIST_ITEM.match(line)):
            bullet = m.group(1) is not None
            number = int(m.group(2)) if m.group(2) else 1
            items: list[list[str]] = []
            j = i
            while j < len(lines):
                nxt = lines[j]
                if (mm := _LIST_ITEM.match(nxt)) and (mm.group(1) is not None) == bullet:
                    items.append([mm.group(3)])
                elif nxt.strip() and items and not _starts_block(nxt):
                    items[-1].append(nxt)
                elif not nxt.strip():
                    peek = j + 1
                    while peek < len(lines) and not lines[peek].strip():
                        peek += 1
                    if not (peek < len(lines) and (pm := _LIST_ITEM.match(lines[peek])) and (pm.group(1) is not None) == bullet):
                        break
                else:
                    break
                j += 1
            texts = [_unwrap(item) for item in items]
            shown = [f"* {t}" if bullet else f"{number + k}. {t}" for k, t in enumerate(texts)]
            blocks.append({"kind": "bullet-list" if bullet else "numbered-list", "markdown": "\n\n".join(shown), "units": texts})
            i = j
        else:
            j = i
            while j < len(lines) and lines[j].strip() and (j == i or not _starts_block(lines[j])):
                j += 1
            para = _unwrap(lines[i:j])
            blocks.append({"kind": "paragraph", "markdown": para, "units": [para]})
            i = j
    return blocks


def block_hash(markdown: str) -> str:
    """14 hex digits below 2**53, the shape of the app's hashes; the value is this stand-in's own."""
    return f"{int(hashlib.sha256(markdown.encode('utf-8')).hexdigest()[:16], 16) >> 11:014x}"


def visible(md: str) -> tuple[str, list[int]]:
    """The visible text of one unit of Markdown and, for each character, the run of formatting it lies in."""
    chars: list[str] = []
    runs: list[int] = []
    counter = 0
    run = 0
    skip_to: dict[int, int] = {}
    i, n = 0, len(md)
    while i < n:
        if i in skip_to:
            i, run, counter = skip_to.pop(i), counter + 1, counter + 1
            continue
        c = md[i]
        if c == "\\" and i + 1 < n:
            chars.append(md[i + 1])
            runs.append(run)
            i += 2
        elif c == "`" and (j := md.find("`", i + 1)) != -1:
            counter += 1
            for ch in md[i + 1:j]:
                chars.append(ch)
                runs.append(counter)
            counter += 1
            run = counter
            i = j + 1
        elif md.startswith(("**", "~~"), i):
            counter += 1
            run = counter
            i += 2
        elif c == "*":
            counter += 1
            run = counter
            i += 1
        elif c == "!" and md.startswith("![", i) and (m := re.compile(r"!\[[^\]]*\]\([^)]*\)").match(md, i)):
            i = m.end()
        elif c == "[" and (m := re.compile(r"\]\([^)]*\)").search(md, i)) and "]" not in md[i + 1:m.start()]:
            counter += 1
            run = counter
            skip_to[m.start()] = m.end()
            i += 1
        else:
            chars.append(c)
            runs.append(run)
            i += 1
    return "".join(chars), runs


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


def _edit_number(note: str) -> int | None:
    m = re.match(r"Edit (\d+)(?!\d)", note or "")
    return int(m.group(1)) if m else None


def _logged(fn: Any) -> Any:
    """Record the call in the stand-in's log: its arguments, its outcome and whatever the method noted."""
    @functools.wraps(fn)
    def inner(self: Standin, *a: Any, **kw: Any) -> Any:
        self._note = {}
        bound = inspect.signature(fn).bind(self, *a, **kw).arguments
        args = {k: v for k, v in bound.items() if k != "self"}
        try:
            reply = fn(self, *a, **kw)
        except StandinError as exc:
            self._log(fn.__name__, args, {"ok": False, "error": str(exc)})
            raise
        self._log(fn.__name__, args, {"ok": True})
        return reply
    return inner


class Standin:
    """The app's state and tools. Each tool returns the reply as a dict, or raises StandinError."""

    def __init__(self, *, state_path: Path | str | None = None, decisions: dict[str, Any] | None = None,
                 first_id: int = 1, calls_path: Path | str | None = None, scenario: str = "") -> None:
        self.state_path = Path(state_path) if state_path else None
        self.calls_path = Path(calls_path) if calls_path else None
        self.scenario = scenario
        self.decisions = check_rule(decisions) if decisions is not None else None
        self._state: dict[str, Any] = {"counter": first_id - 1, "url": None, "blocks": [], "undo": 0, "dirty": False,
                                       "suggestions": []}
        self._note: dict[str, Any] = {}

    # -- state ------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.state_path is not None and self.state_path.is_file():
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._state.setdefault("dirty", False)
        return self._state

    def _save(self) -> None:
        if self.state_path is not None:
            fd, name = tempfile.mkstemp(dir=self.state_path.parent, prefix=self.state_path.name + ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._state))
            os.replace(name, self.state_path)

    def _open(self) -> dict[str, Any]:
        state = self._load()
        if state["url"] is None:
            self._invent("error text: no document is open")
            raise StandinError("no document is open")
        return state

    # -- the call log -------------------------------------------------------

    def _invent(self, why: str) -> None:
        self._note.setdefault("invented", []).append(why)

    def _log(self, tool: str, args: dict[str, Any], outcome: dict[str, Any]) -> None:
        if self.calls_path is None:
            return
        row = {"scenario": self.scenario, "tool": tool, "args": args, **outcome, **self._note}
        self.calls_path.parent.mkdir(parents=True, exist_ok=True)
        with self.calls_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


    # -- tools ------------------------------------------------------------

    @_logged
    def open_document(self, path: str) -> str:
        state = self._load()
        real = os.path.realpath(path)
        try:
            text = Path(real).read_text(encoding="utf-8")
        except OSError as exc:
            self._invent("error text: cannot open")
            raise StandinError(f"cannot open {path}: {exc.strerror or exc}") from exc
        # A reopen discards the unsaved changes and the dirty mark. It also drops THIS document's suggestions and restarts
        # the ids (the app's server instructions; not confirmed on the app); another document's entries stay listed and
        # the ids keep counting past them (NOTES quirk e).
        mine = [s for s in state["suggestions"] if s["url"] == real]
        if mine:
            state["suggestions"] = [s for s in state["suggestions"] if s["url"] != real]
            state["counter"] = max((int(s["id"][1:]) for s in state["suggestions"]), default=0)
            self._note["dropped_suggestions"] = [s["id"] for s in mine]
        state.update(url=real, blocks=parse_blocks(text), undo=0, dirty=False)
        self._save()
        return "Opened"

    @_logged
    def get_document(self, from_: int = 0, limit: int = 100000) -> dict[str, Any]:
        state = self._open()
        blocks, shown, chars, at = state["blocks"], [], 0, max(from_, 0)
        while at < len(blocks):
            size = len(blocks[at]["markdown"])
            if shown and chars + size > limit:
                break
            chars += size
            shown.append({"hash": block_hash(blocks[at]["markdown"]), "index": at, "kind": blocks[at]["kind"],
                          "markdown": blocks[at]["markdown"], "warnings": []})
            at += 1
        reply: dict[str, Any] = {"blocks": shown, "documentType": "md", "firstEditable": 0,
                                 "hasChanges": bool(state["dirty"]), "selectionBlock": 0, "url": state["url"],
                                 "view": "editor"}
        if at < len(blocks):
            reply["next"] = at
        return reply

    @_logged
    def propose_edits(self, edits: list[dict[str, Any]]) -> dict[str, Any]:
        state = self._open()
        results: list[dict[str, Any]] = []
        for index, edit in enumerate(edits):   # all of them first: a bad entry must leave the state as it was
            if not isinstance(edit, dict):
                self._invent("error text: an edit that is not an object")
                raise StandinError(f"edit {index} is not an object with block, find, replace and note: {edit!r}")
        for index, edit in enumerate(edits):
            try:
                found = self._locate(state, edit)
            except _Refused as why:
                if not why.recorded:
                    self._invent("refusal text (only the formatting-boundary, no-op and code-block refusals are in the fixtures)")
                results.append({"index": index, "reason": str(why), "status": "refused"})
                continue
            state["counter"] += 1
            sid = f"s{state['counter']}"
            block, unit, start, end = found
            state["suggestions"].append({
                "id": sid, "block": block, "find": edit["find"], "replace": edit["replace"],
                "note": str(edit.get("note") or ""), "status": "pending",
                "url": state["url"], "unit": unit, "start": start, "end": end})
            results.append({"block": block, "id": sid, "index": index, "status": "proposed"})
        self._save()
        return {"ok": True, "results": results}

    def _locate(self, state: dict[str, Any], edit: dict[str, Any]) -> tuple[int, int, int, int]:
        block, find, replace = edit.get("block"), edit.get("find"), edit.get("replace")
        blocks = state["blocks"]
        if not isinstance(block, int) or isinstance(block, bool) or not 0 <= block < len(blocks):
            raise _Refused(f"block {block} does not exist; the document has {len(blocks)} blocks")
        if not isinstance(find, str) or not find or not isinstance(replace, str):
            raise _Refused("an edit needs a non-empty find and a replace, both text")
        if blocks[block]["kind"] == "code":
            raise _Refused(IS_CODE.format(block=block), recorded=True)
        if find == replace:
            raise _Refused(NO_OP, recorded=True)
        expected = edit.get("expected")
        if expected is not None and expected != block_hash(blocks[block]["markdown"]):
            raise _Refused(f"block {block} changed since it was read (expected hash {expected}, it is now "
                           f"{block_hash(blocks[block]['markdown'])}); read the document again with get_document")
        views = [visible(md) for md in blocks[block]["units"]]
        occurrences = sum(text.count(find) for text, _ in views)
        for unit, (text, runs) in enumerate(views):
            at = text.find(find)
            if at == -1:
                continue
            end = at + len(find)
            if len(set(runs[at:end])) > 1:
                raise _Refused(CROSSES, recorded=True)
            for other in state["suggestions"]:
                if (other["status"] == "pending" and other["url"] == state["url"] and other["block"] == block
                        and other["unit"] == unit and at < other["end"] and other["start"] < end):
                    raise _Refused(f"find overlaps pending suggestion {other['id']}")
            if occurrences > 1:
                # Located at the first occurrence. What the app does with an ambiguous find is not recorded.
                self._note.setdefault("ambiguous", []).append(
                    {"block": block, "find": find, "occurrences": occurrences, "located": {"unit": unit, "start": at}})
            return block, unit, at, end
        raise _Refused(NOT_FOUND.format(block=block))

    @_logged
    def list_suggestions(self) -> dict[str, Any]:
        state = self._load()
        rule = self.decisions
        decided: list[dict[str, Any]] = []
        if rule is not None:
            for s in state["suggestions"]:
                if s["status"] != "pending":
                    continue
                n = _edit_number(s["note"])
                if n in rule.get("accepted", []):
                    s["status"] = "accepted"
                elif n in rule.get("rejected", []):
                    s["status"] = "rejected"
                else:
                    s["status"] = rule.get("default", "pending")
                if s["status"] != "pending":
                    decided.append({"id": s["id"], "edit": n, "status": s["status"]})
                if s["status"] == "accepted" and s["url"] == state["url"]:
                    state["undo"] += 1
                    state["dirty"] = True   # NOTES h: an accept dirties the buffer, and undo does not clear it
            self._save()
        self._note["decisions_applied"] = decided
        keys = ("block", "find", "id", "note", "replace", "status")
        return {"ok": True, "suggestions": [{k: s[k] for k in keys} for s in state["suggestions"]]}

    @_logged
    def undo(self) -> dict[str, Any]:
        state = self._open()
        if state["undo"] <= 0:
            # recorded (NOTES h): "There is nothing to undo", and hasChanges stays as it was
            raise StandinError(NOTHING_TO_UNDO)
        state["undo"] -= 1
        self._save()
        self._invent("undo success reply (only the error is in the fixtures)")
        return {"ok": True}

    @_logged
    def save_document(self) -> dict[str, Any]:
        """Writes the whole file in the app's layout, accepted suggestions applied where their text is in the Markdown."""
        state = self._open()
        out = []
        for index, block in enumerate(state["blocks"]):
            md = block["markdown"]
            for s in state["suggestions"]:
                if s["status"] == "accepted" and s["url"] == state["url"] and s["block"] == index and s["find"] in md:
                    md = md.replace(s["find"], s["replace"], 1)
            out.append(md)
        Path(state["url"]).write_text("\n\n".join(out), encoding="utf-8")
        state["undo"] = 0
        state["dirty"] = False
        self._save()
        self._invent("save_document reply (not in the fixtures)")
        return {"ok": True}


# ---------------------------------------------------------------------------
# The MCP server
# ---------------------------------------------------------------------------


def build_server(app: Standin) -> Any:
    """The FastMCP-style server for `app`. The `mcp` SDK is imported here, so `mcp-config` runs without it."""
    from pydantic import Field  # noqa: PLC0415 — deferred: `mcp-config` runs on a python3 without these packages

    from mcp.server.mcpserver import MCPServer  # noqa: PLC0415 — deferred, as above
    from mcp.server.mcpserver.exceptions import ToolError  # noqa: PLC0415 — deferred, as above

    server = MCPServer("markupeditor-standin")

    def tool(fn: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> str:
            try:
                result = fn(*args, **kwargs)
            except StandinError as exc:
                raise ToolError(str(exc)) from exc
            return result if isinstance(result, str) else render(result)
        call.__name__, call.__doc__ = fn.__name__, fn.__doc__
        call.__signature__ = inspect.signature(fn, eval_str=True).replace(return_annotation=str)  # type: ignore[attr-defined]
        server.tool(structured_output=False)(call)
        return fn

    @tool
    def open_document(path: str) -> str:
        """Open a Markdown file in the editor. `path` is the file's absolute path."""
        return app.open_document(path)

    # The app names this parameter `from`, a Python keyword: the schema says `from`, the call comes in as **kwargs.
    def get_document(**kw: Any) -> dict[str, Any]:
        """Read the open document as blocks. `from` is the first block, `limit` the most characters of Markdown."""
        return app.get_document(from_=kw.get("from", 0), limit=kw.get("limit", 100000))
    get_document.__signature__ = inspect.Signature([  # type: ignore[attr-defined]
        inspect.Parameter("from_", inspect.Parameter.KEYWORD_ONLY, default=0,
                          annotation=Annotated[int, Field(alias="from", description="Index of the first block.")]),
        inspect.Parameter("limit", inspect.Parameter.KEYWORD_ONLY, default=100000,
                          annotation=Annotated[int, Field(description="Most characters of block Markdown in the reply.")])])
    tool(get_document)

    @tool
    def propose_edits(edits: list[dict[str, Any]]) -> dict[str, Any]:
        """Suggest edits for the author to accept or reject. Each edit: block, expected (the block's hash), find, replace, note."""
        return app.propose_edits(edits)

    @tool
    def list_suggestions() -> dict[str, Any]:
        """List the suggestions and what the author decided about each."""
        return app.list_suggestions()

    @tool
    def undo() -> dict[str, Any]:
        """Undo the last change in the open document."""
        return app.undo()

    @tool
    def save_document() -> dict[str, Any]:
        """Save the open document to its file."""
        return app.save_document()

    return server


def app_from_env() -> Standin:
    table_path = os.environ.get("MARKUP_STANDIN_DECISIONS")
    rule = None
    if table_path:
        rule = rule_for(os.environ.get("MARKUP_STANDIN_SCENARIO", ""), json.loads(Path(table_path).read_text(encoding="utf-8")))
    return Standin(state_path=os.environ.get("MARKUP_STANDIN_STATE") or None, decisions=rule,
                   calls_path=os.environ.get("MARKUP_STANDIN_CALLS") or None,
                   scenario=os.environ.get("MARKUP_STANDIN_SCENARIO", ""))


def write_mcp_config(out: Path, *, python: str, state: str, decisions: str | None, scenario: str,
                     calls: str | None = None) -> None:
    """The `--mcp-config` file that makes a claude session start this server under the name `markupeditor`."""
    env = {"MARKUP_STANDIN_STATE": state, "MARKUP_STANDIN_SCENARIO": scenario}
    if decisions:
        env["MARKUP_STANDIN_DECISIONS"] = decisions
    if calls:
        env["MARKUP_STANDIN_CALLS"] = calls
    config = {"mcpServers": {SERVER_NAME: {"command": python, "args": [str(HERE)], "env": env}}}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="serve the tools over stdio (the default)")
    cfg = sub.add_parser("mcp-config", help="write the --mcp-config file that launches this server")
    cfg.add_argument("--out", required=True)
    cfg.add_argument("--python", required=True, help="the interpreter that has the mcp package")
    cfg.add_argument("--state", required=True, help="the file the suggestions are kept in between sessions")
    cfg.add_argument("--decisions", default="", help="the decision table (JSON); empty leaves everything pending")
    cfg.add_argument("--calls", default="", help="the file the server logs each tool call to (a JSON line each)")
    cfg.add_argument("--scenario", required=True, help="the scenario's name, which picks the rule")
    args = parser.parse_args(argv)
    if args.cmd == "mcp-config":
        write_mcp_config(Path(args.out), python=args.python, state=args.state, decisions=args.decisions or None,
                         scenario=args.scenario, calls=args.calls or None)
        return 0
    build_server(app_from_env()).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
