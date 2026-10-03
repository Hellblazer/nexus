#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2 records for the prose editor (RDR-221, "Memory in T2").

The records go through the `nx memory` CLI.

Every other part of the editor reads and writes these records through this
script, never through `nx memory put` directly. It is stdlib only and never
imports nexus; it shells out to the `nx` CLI (override the command with
PROSE_EDIT_NX). Output is JSON on stdout. Errors go to stderr.

Usage (all commands run inside the repo, from any subdirectory or worktree):

  memory.py [--prefix P] repo [PATH]
  memory.py read TARGET|- [--genre G] [--site-layer FILE]
  memory.py genre-for TARGET [--site-layer FILE]
  memory.py add-entry --level user|repo|doc [--path P]
                      (--key K --value V [--list] | --from-stdin)
  memory.py reject TARGET (--old S --new S [--reason R] | --from-stdin)
  memory.py entries --level user|repo|doc [--path P]
                    [--remove KEY | --remove-item KEY=VALUE]
  memory.py rejections TARGET [--remove N]
  memory.py filter TARGET|-                    # proposal JSON on stdin
  memory.py voice-card TARGET [--from-stdin | --remove]  # {"voice_card": str} on stdin
  memory.py promote TARGET N --level user|repo [--dry-run]
  memory.py not-a-defect --level user|repo [--remove N]
  memory.py log TARGET|- [--genre G] [--stamp S] # session JSON on stdin; S fixes the record's name and time
  memory.py genre-put NAME [--replace]         # {"exemplars": [...], "notes": [...]} on stdin
  memory.py exemplar-add GENRE PATH:START-END [--rev SHA]
  memory.py viewer [--set V]

Site-page section 3 treatments (what a repo or one document does with a rule of the site-page style sheet):
add-entry --level repo|doc (--path P for doc) takes a list under one of three keys, each entry the rule's opening
words in double quotes, three dots inside the closing quote, then a reason in parentheses:
  site_page_section3_ignored     the rule is dropped for that level
  site_page_section3_query_only  the rule produces queries, never an edit
  site_page_section3_note_only   the rule appears in the editor's note only
For example {"scalars": {}, "lists": {"site_page_section3_ignored": ["\"Plain technical English...\" (this essay)"]}}
on stdin with --from-stdin. The narrower layer wins; `brief.py build` stops, naming the `entries --remove-item`
command, when an entry matches no rule. `entries --level L [--path P] --remove-item KEY=VALUE` removes one.

TARGET is a path, optionally with a line range: PATH, PATH:START-END, or PATH:N
(one line, the same as N-N). The path is repo-relative (an absolute path or one relative to the current
directory is converted; a symlink is NOT followed, so CLAUDE.md stays
CLAUDE.md). Every record keys on the bare repo-relative path; the range only
narrows a run, and `read` and `log` report it as {"start", "end"} (null when
absent). A rejection stored during a range run therefore filters the whole
file too. "-" means a stdin run: no document record, no stored rejections; its
session is logged under log/stdin/<utc timestamp>.

Exit codes: 0 ok, 1 bad input, malformed stored data, or an nx failure that is
not a connection failure ("T2 request failed (exit N): <nx's stderr without the lines
that name nx>"; a status error from the storage service is reported as "T2 refused the
request: the storage service answered HTTP <code>"),
2 usage (argparse), 3 T2 unavailable (nx cannot reach the service, or an edge in
front of it answers 502, 503 or 504). On 3 the script says "T2 unavailable:
<cause>" and prints nothing on stdout. No message relays a line of nx's that
names nx: nx's remedies are addressed to an operator, and the model reading this
stderr is not one. A record that is absent
(confirmed by `nx memory list`) is a normal answer and reads as null; it is
never confused with T2 being down, and an unreachable T2 is never answered
with an empty style sheet.

Records (T2 project, title), with PREFIX empty unless overridden by --prefix or
PROSE_EDIT_PROJECT_PREFIX (tests use it so they never touch the live projects):

  user   project  PREFIX + "prose"                stylesheet, viewer, not-a-defect
  repo   project  PREFIX + "<repo>_prose"         stylesheet, not-a-defect,
                                                  genre/<name>, doc/<path>,
                                                  log/<path>/<utc ts> (90-day TTL)

<repo> is the basename of the parent of `git rev-parse --path-format=absolute
--git-common-dir`, so the primary checkout and every worktree share one repo
style sheet. All record bodies are JSON:

  stylesheet, doc/<path>:  {"scalars": {k: v}, "lists": {k: [v, ...]},
                            "rejections": [{"old", "new", "at", "reason"?}],   (doc only)
                            "voice_card": {"text", "at"}}                      (doc only, optional)
  not-a-defect:            {"entries": [{"old", "new", "from", "at"}]}
  genre/<name>:            {"exemplars": [{"text", "path", "start", "end", "rev"?}],
                            "notes": [str]}
  viewer:                  {"viewer": str}
  log/...:                 {"path", "range", "genre", "at", "session": <caller's JSON>}

A stored record that does not have this shape stops the command with
"record <project>/<title> is malformed: ..." (exit 1); nothing is guessed.

Merge. PRECEDENCE (exported as data, and the order the merge really uses) is
user, repo, site-page, document. Later layers win for scalar settings; list
entries add across layers, first occurrence kept. The merged read also carries
each layer's not-a-defect entries under the list key "not-a-defect": that is how
they reach the editor (they are read, never applied as a filter here).
`read --site-layer FILE` slots a {"scalars", "lists"} JSON file between the repo
layer and the document layer; reading site-page section 3 is the skill's job,
this script only merges what it is handed.

Genre from path. Any layer's "genre_map" list may hold "prefix=genre" entries.
The longest matching prefix wins across all layers; on equal length the more
specific layer wins (document > site-page > repo > user). The built-in defaults
compete as the lowest layer, each with a prefix length of its own:

  docs/rdr/*.md -> rdr           docs/exploration/*.md -> exploration-essay
  web/*.html -> how-to           CHANGELOG.md -> changelog
  docs/<name>.md (top level only) -> reference-doc      anything else -> none

"None" means the caller asks the author. Genre names are the six in GENRES (rdr,
reference-doc, how-to, exploration-essay, changelog, commit-message); any other
name is refused wherever a genre is given. `exemplar-add --rev REV` resolves REV
(a branch, tag or sha) to a full commit sha, reads the passage from that commit,
and records the sha; a file that is not UTF-8 text is refused. Line numbers count
newline-separated lines.

Proposal format (the editor agent's output; Step 1.4 writes it, `filter` reads
it). One JSON object, optionally wrapped in a single ```json fenced block.
`filter` validates it and rejects a malformed one (exit 1, nothing on stdout):

  {"voice_card": str, "note": str,
   "paragraphs": [{"n": int, "action": str, "paragraphs": str, "advice": str}],
   "edits":   [{"n": int, "old": str, "new": str, "reason": str}],
   "queries": [{"n": int, "anchor": str, "text": str}]}

  edits[].n     positive integer, unique within edits
  edits[].old   non-empty string, an exact substring of the document
  edits[].new   string, different from old; the empty string is a pure cut
  edits[].reason string
  paragraphs[].n, queries[].n   integer, unique within its list (they number
                                the [P1] / [Q1] marks)
  queries[].anchor              non-empty string: the exact text the query
                                attaches to
  queries[].text                string

This script checks the types, uniqueness and presence above. That an old string
or an anchor is an exact substring of the document (occurring once within a line
range) is checked by the copy writer of the review loop (nexus-ger02.4), which
holds the document text; this script never reads it.

The minimal change. A rejection matches a later proposal by what the edit CHANGES,
not by the span the editor happened to pick. `minimal_change(old, new)` splits both
strings into words and whitespace, drops the words they share at the start, then the
words they share at the end (so "The worker really quite simply retries" ->
"The worker simply retries" is the change "really quite " -> ""), and returns what is
left of each. Words are the unit: "retries" and "retried" share no word. A shared word is
compared loosely: any whitespace is one space, and case and apostrophe style (curly or straight)
are ignored, because the editor retypes those inside a span it otherwise leaves alone; a change
that is ONLY one of those keeps its own key, and the replacement text is never compared loosely.
A trailing mark is NOT ignored: "paged;" is not "paged", because a dash replaced by a semicolon
is a mark moved onto the neighbouring word and must stay a change of its own. The match key,
`change_key`, is that pair with its whitespace collapsed and its ends stripped (so
"really quite" and " really quite " are one change); when collapsing would leave the two
sides empty (a change of whitespace only) the raw pair is the key. Overlap alone is NOT
a match: the same words with another replacement, or a cut of part of them, have another
key. Three limits, all deliberate: the key has no position, so a stored cut of "really
quite " also drops that cut in another sentence; a pure insertion (no old text) matches
the same insertion anywhere in the document; and a case or apostrophe difference inside a
shared word is not part of the change, so a fix that also re-capitalises a word next to the
stored words is taken for the stored fix. Dropped edits stay visible: the review copy
lists them with their cause. The key is computed from "old" and "new" every time it is
needed, never stored, so records written before the key existed are read the same way.

`filter TARGET` drops every edit whose change key equals the key of a rejection stored
for that document, and every edit whose change key equals that of a promoted not-a-defect entry at user or
repo level (a general entry, so it applies to a stdin run and to every document; overlap alone is not a match
there either). Every other key passes through unchanged. Edit numbers "n" are kept, so dropped edits leave GAPS
in the numbering: the marks in the marked-up copy keep their meaning. The output gains "dropped":
[{"n", "old", "cause"}] (cause is "rejected" for the document's own rejection, "not-a-defect" for a promoted
entry; an edit that matches both is "rejected"; any "dropped" in the input is overwritten). The skill runs `brief.py filter`, which calls this and returns each dropped
edit as {"n", "old", "new", "cause"}: it adds "new" (the proposed text, so the review copy
can show what was dropped) and drops further edits with its own causes.

Rejections are one per change key: rejecting a change that is already stored REPLACES that
rejection in place (its number and position are kept; its old, new and time are
overwritten, and so is its reason when the new rejection gives one: a re-rejection with no
reason keeps the earlier reason). `reject --reason R` (or "reason" in the --from-stdin object) keeps the
author's one line on why, to tell a wrong edit from a right one the author does not want;
a reason that is not a string is refused. `entries` lists a level's style-sheet scalars and lists (doc level: that document's
record; it needs --path). `--remove KEY` deletes the scalar KEY, or the whole list KEY;
`--remove-item KEY=VALUE` deletes one value from the list KEY (VALUE is everything after
the first "=", so a genre_map entry "notes/=how-to" is KEY=genre_map, VALUE=notes/=how-to).
A key or value that is not there stops with exit 1. A record left with no scalars, lists
or (doc level) rejections is deleted. Removal runs under the same lock as add-entry.

Numbers in `rejections` are 1-based and renumbered after
a removal. A document record left holding no rejections, scalars or lists is
deleted. `promote` copies a stored rejection to the not-a-defect list at user or
repo level and leaves the rejection where it is; `promote --dry-run` prints the
entry and writes nothing to T2. `not-a-defect --level L` lists that level's entries
numbered; `--remove N` deletes one.

A real promote needs a matching dry run first, as apply does (RDR-221, Sam 2026-10-03). The dry run leaves a
record in `<git common dir>/prose-edit-promote/<h>.json`, `{"entry": "<h>"}`, where h is the sha256 of exactly
what would be promoted: the level, its T2 project, the document path and the rejection's old and new strings
(not the time, not the number: renumbering after a removal changes which rejection N is, and the hash then no
longer matches). A real promote without that record, or with one older than two hours, stops with exit 1 and
stores nothing; one that goes through consumes it, so a dry run vouches for one promote. The record is a
hash, never the text.

The voice card. `doc/<path>` may hold one author-approved voice card, the key "voice_card": {"text", "at"}
(text a non-empty string of at most 4000 characters, at the UTC time the author approved it). It is absent
until the author says yes to saving one after a run; the editor's own card is never stored by itself.
`voice-card TARGET` prints {"path", "range", "voice_card": {text, at} | null}. `voice-card TARGET --from-stdin`
takes {"voice_card": "<text>"} (nothing else) and replaces any card stored; `--remove` deletes it and stops with
exit 1 when none is stored. A stdin run (TARGET "-") has no document record and stores no card. A range target
keys on the bare path, like every record. The card sits beside "scalars", "lists" and "rejections" and none of the
commands that write those touches it; a record that holds only a card is not empty and is not deleted with its
last scalar, list or rejection. `read` carries it with the document layer (layers.document.voice_card); the brief
builder puts it in the brief as the anchor for the next run, in place of a card rebuilt from the edited text. It is
not a scalar or a list, so it takes no part in the merge.

Concurrency. Every read-modify-write (add-entry, reject, rejections --remove, voice-card,
promote, not-a-defect --remove, genre-put, exemplar-add, viewer --set) holds an
exclusive flock on a lock file, keyed by project and title, from the read through
the write. Repo records (repo project) lock under the git common directory, which
every worktree of that clone shares. User records (the "prose" project) are shared
by every repo, so they lock under <temp dir>/prose-edit-locks-<uid> instead, the
same file for every repo of that user on the machine. Limits: two separate clones
of one repo have separate git directories, so their repo-record locks do not meet;
sessions with different TMPDIR settings do not share the user lock; and no lock
spans machines. In each case two writers can still lose an update.

Set PROSE_EDIT_NOW (a UTC timestamp) together with PROSE_EDIT_TEST=1 to fix the
clock in tests; PROSE_EDIT_NOW alone is ignored.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, cast

Obj = dict[str, Any]

PRECEDENCE: list[str] = ["user", "repo", "site-page", "document"]
LOG_TTL: str = "90d"
NX_TIMEOUT: int = 120
NAD: str = "not-a-defect"
GENRES: tuple[str, ...] = (
    "rdr", "reference-doc", "how-to", "exploration-essay", "changelog", "commit-message",
)


class UserError(Exception):
    """Bad input, malformed stored data, or an nx failure that is not a connection failure. Exit 1."""


class T2Unavailable(Exception):
    """`nx` cannot reach the T2 service. Exit 3."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def projects(prefix: str, repo: str) -> tuple[str, str]:
    """(user project, repo project) under an optional test prefix."""
    return f"{prefix}prose", f"{prefix}{repo}_prose"


def _default_genre(path: str) -> tuple[int, str] | None:
    """(specificity, genre) from the built-in map; specificity is the prefix length."""
    if path.startswith("docs/rdr/") and path.endswith(".md"):
        return len("docs/rdr/"), "rdr"
    if path.startswith("docs/exploration/") and path.endswith(".md"):
        return len("docs/exploration/"), "exploration-essay"
    if path.startswith("web/") and path.endswith(".html"):
        return len("web/"), "how-to"
    if path == "CHANGELOG.md":
        return len(path), "changelog"
    if path.startswith("docs/") and path.endswith(".md") and "/" not in path[len("docs/"):]:
        return len("docs/"), "reference-doc"
    return None


def genre_for(path: str, override_layers: list[list[Any]]) -> str | None:
    """Genre for a repo-relative path.

    `override_layers` holds each layer's "prefix=genre" strings in PRECEDENCE
    order. Longest matching prefix wins; a tie goes to the later (more specific)
    layer; the built-in defaults are the lowest layer.
    """
    best_key: tuple[int, int] | None = None
    best: str | None = None
    default = _default_genre(path)
    if default is not None:
        best_key, best = (default[0], -1), default[1]
    for rank, entries in enumerate(override_layers):
        for entry in entries:
            prefix, sep, genre = str(entry).partition("=")
            if not (sep and prefix and genre and path.startswith(prefix)):
                continue
            key = (len(prefix), rank)
            if best_key is None or key >= best_key:
                best_key, best = key, genre
    return best


def _same(a: object, b: object) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _extend(target: list[Any], items: list[Any]) -> None:
    for item in items:
        if not any(_same(item, seen) for seen in target):
            target.append(item)


def merge_layers(layers: list[Obj | None]) -> Obj:
    """Merge layers given in precedence order (None = absent layer).

    Scalars: the most specific layer wins. Lists: entries add, first kept.
    """
    scalars: Obj = {}
    lists: dict[str, list[Any]] = {}
    for layer in layers:
        if not layer:
            continue
        scalars.update(layer.get("scalars") or {})
        layer_lists: Obj = layer.get("lists") or {}
        for key, items in layer_lists.items():
            _extend(lists.setdefault(key, []), list(items))
    return {"scalars": scalars, "lists": lists}


def _merge_input(layer: Obj | None) -> Obj | None:
    """A layer as merge_layers sees it: its not-a-defect entries become a list."""
    if layer is None:
        return None
    lists: Obj = dict(layer.get("lists") or {})
    if NAD in layer:
        lists[NAD] = layer[NAD]
    return {"scalars": layer.get("scalars") or {}, "lists": lists}


def merge_named(layers: dict[str, Obj | None]) -> Obj:
    """Merge named layers in the order PRECEDENCE says (read at call time)."""
    return merge_layers([_merge_input(layers.get(name)) for name in PRECEDENCE])


_WORDS = re.compile(r"\s+|\S+")
_APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201b": "'", "\u02bc": "'"})


def _loose(token: str) -> str:
    """A token as the trim loops compare it: any whitespace is one space, and case and apostrophe style are
    ignored. A trailing mark is NOT ignored ("paged;" and "paged" are different words here): the editor's
    replacement for a dash is exactly a mark moved onto the neighbouring word, so ignoring it would turn every
    "word — x" -> "word; x" into a cut of the dash (measured on the 360 replayed storage pairs: 138 matches
    became 334, all of them false)."""
    return " " if token.isspace() else token.translate(_APOSTROPHES).casefold()


def _strict(token: str) -> str:
    return token


def _trim(a: list[str], b: list[str], same: Callable[[str], str]) -> tuple[str, str]:
    head = 0
    while head < len(a) and head < len(b) and same(a[head]) == same(b[head]):
        head += 1
    tail = 0
    while tail < len(a) - head and tail < len(b) - head and same(a[-1 - tail]) == same(b[-1 - tail]):
        tail += 1
    return "".join(a[head:len(a) - tail]), "".join(b[head:len(b) - tail])


def minimal_change(old: str, new: str) -> tuple[str, str]:
    """`old` and `new` with the words they share at the start, then at the end, trimmed (see the docstring).

    The leading words are trimmed first, so when the shared words repeat ("a b a" -> "a") the change is
    the later text ("b a"); both the stored rejection and the proposal go through the same rule. A shared
    word is compared loosely (`_loose`): the editor retypes a curly apostrophe as a straight one, re-capitalises
    a word, or flows a newline into a space inside a span it otherwise left alone, and none of that is a
    different change. The replacement text left after the trim is never compared loosely. When the loose trim
    would leave nothing of two strings that differ (a change of case, of an apostrophe or of whitespace only),
    the strict trim is used, so that change keeps its own key and cannot be taken for a no-op or for another
    change.
    """
    a, b = _WORDS.findall(old), _WORDS.findall(new)
    gone, put = _trim(a, b, _loose)
    if not gone and not put and old != new:
        gone, put = _trim(a, b, _strict)
    return gone, put


def change_key(old: str, new: str) -> tuple[str, str]:
    """The key a rejection and a proposal are matched on: the minimal change, whitespace collapsed."""
    gone, put = minimal_change(old, new)
    key = (" ".join(gone.split()), " ".join(put.split()))
    return key if key[0] != key[1] else (gone, put)


def _now() -> datetime:
    fixed = os.environ.get("PROSE_EDIT_NOW")
    if fixed and os.environ.get("PROSE_EDIT_TEST") == "1":
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(fixed, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        raise UserError(f"PROSE_EDIT_NOW is not a UTC timestamp: {fixed!r}")
    return datetime.now(timezone.utc)


def _iso(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


_TARGET = re.compile(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def split_range(arg: str) -> tuple[str, Obj | None]:
    """Split PATH[:START-END] or PATH[:N] into (bare path, {"start", "end"} or None)."""
    m = _TARGET.match(arg)
    if not m:
        return arg, None
    start = int(m.group("start"))
    end = int(m.group("end")) if m.group("end") else start
    if not 1 <= start <= end:
        raise UserError(f"{arg}: line range {start}-{end} is not a valid range")
    return m.group("path"), {"start": start, "end": end}


# ---------------------------------------------------------------------------
# Shape checks for stored records, site layers and proposals
# ---------------------------------------------------------------------------


def _bad(what: str, why: str) -> UserError:
    return UserError(f"{what} is malformed: {why}")


def _obj(value: Any) -> Obj | None:
    return cast(Obj, value) if isinstance(value, dict) else None


def _list(value: Any) -> list[Any] | None:
    return cast(list[Any], value) if isinstance(value, list) else None


def _is_str_dicts(value: Any, keys: tuple[str, ...]) -> bool:
    items = _list(value)
    if items is None:
        return False
    for item in items:
        obj = _obj(item)
        if obj is None or not all(isinstance(obj.get(k), str) for k in keys):
            return False
    return True


def check_record(kind: str, rec: Any, what: str) -> Obj:
    """Validate a record body of the given kind ("sheet", "doc", "nad", "genre", "viewer")."""
    body = _obj(rec)
    if body is None:
        raise _bad(what, "not a JSON object")
    if kind in ("sheet", "doc"):
        if _obj(body.get("scalars", {})) is None:
            raise _bad(what, '"scalars" is not an object')
        lists = _obj(body.get("lists", {}))
        if lists is None or not all(_list(v) is not None for v in lists.values()):
            raise _bad(what, '"lists" is not an object of lists')
    if kind == "doc" and not _is_str_dicts(body.get("rejections", []), ("old", "new")):
        raise _bad(what, '"rejections" is not a list of {old, new} strings')
    if kind == "doc" and not all(
        isinstance((_obj(r) or {}).get("reason", ""), str) for r in cast(list[Any], body.get("rejections", []))
    ):
        raise _bad(what, 'a rejection\'s "reason" is not a string')
    if kind == "doc" and "voice_card" in body:
        card = _obj(body["voice_card"])
        if card is None or not (isinstance(card.get("text"), str) and card["text"].strip()
                                and isinstance(card.get("at"), str)):
            raise _bad(what, '"voice_card" is not {text, at} strings with a non-empty text')
    if kind == "nad" and not _is_str_dicts(body.get("entries", []), ("old", "new")):
        raise _bad(what, '"entries" is not a list of {old, new} strings')
    if kind == "genre":
        exemplars = body.get("exemplars", [])
        notes = _list(body.get("notes", []))
        if not _is_str_dicts(exemplars, ("text", "path")) or not all(
            isinstance((_obj(x) or {}).get("start"), int) and isinstance((_obj(x) or {}).get("end"), int)
            for x in cast(list[Any], exemplars)
        ):
            raise _bad(what, '"exemplars" is not a list of {text, path, start, end}')
        if notes is None or not all(isinstance(n, str) for n in notes):
            raise _bad(what, '"notes" is not a list of strings')
    if kind == "viewer" and not isinstance(body.get("viewer", ""), str):
        raise _bad(what, '"viewer" is not a string')
    return body


_FENCE = re.compile(r"\A\s*```(?:json)?[ \t]*\n(?P<body>.*)\n```\s*\Z", re.DOTALL)


def _unfence(text: str) -> str:
    m = _FENCE.match(text)
    return m.group("body") if m else text


def validate_proposal(body: Any) -> Obj:
    """Check the proposal format in the module docstring; return it or raise UserError."""
    prop = _obj(body)
    edits = _list(prop.get("edits")) if prop is not None else None
    if prop is None or edits is None:
        raise UserError('proposal: expected a JSON object with an "edits" list')
    seen: set[int] = set()
    for i, edit in enumerate(edits):
        where = f"proposal: edits[{i}]"
        e = _obj(edit)
        if e is None:
            raise UserError(f"{where} is not an object")
        n = e.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise UserError(f'{where}: "n" must be a positive integer')
        if n in seen:
            raise UserError(f'{where}: "n" {n} is not unique')
        seen.add(n)
        old = e.get("old")
        if not (isinstance(old, str) and old):
            raise UserError(f'{where}: "old" must be a non-empty string')
        if not isinstance(e.get("new"), str):
            raise UserError(f'{where}: "new" must be a string')
        if e["new"] == old:
            raise UserError(f'{where}: "new" equals "old", which is not an edit')
        if not isinstance(e.get("reason"), str):
            raise UserError(f'{where}: "reason" must be a string')
    for key in ("paragraphs", "queries"):
        items = _list(prop.get(key, []))
        if items is None:
            raise UserError(f'proposal: "{key}" must be a list')
        seen_n: set[int] = set()
        for i, item in enumerate(items):
            obj = _obj(item) or {}
            num = obj.get("n")
            if not isinstance(num, int) or isinstance(num, bool):
                raise UserError(f'proposal: {key}[{i}] needs an integer "n"')
            if num in seen_n:
                raise UserError(f'proposal: {key}[{i}]: "n" {num} is not unique')
            seen_n.add(num)
            if key == "queries":
                anchor = obj.get("anchor")
                if not (isinstance(anchor, str) and anchor):
                    raise UserError(f'proposal: queries[{i}]: "anchor" must be a non-empty string')
                if not isinstance(obj.get("text"), str):
                    raise UserError(f'proposal: queries[{i}]: "text" must be a string')
    return prop


# ---------------------------------------------------------------------------
# Git: repo name and repo-relative paths
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, encoding="utf-8",
            timeout=30, cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError(f"cannot run git: {exc}") from exc


def _git_out(args: list[str]) -> str:
    proc = _git(args)
    if proc.returncode != 0:
        raise UserError(
            "not inside a git repository (git rev-parse failed): " + proc.stderr.strip()
        )
    return proc.stdout.strip()


class Repo:
    def __init__(self) -> None:
        self.common = Path(_git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"]))
        self.name = self.common.parent.name
        self.root = Path(_git_out(["rev-parse", "--show-toplevel"]))

    def rel(self, path: str) -> str:
        """Repo-relative posix path. Relative input is taken from the repo root first,
        then from the current directory; symlinks are not followed."""
        raw = Path(path)
        if raw.is_absolute():
            cand = raw
        elif (self.root / raw).exists() or not (Path.cwd() / raw).exists():
            cand = self.root / raw
        else:
            cand = Path.cwd() / raw
        cand = Path(os.path.normpath(cand))
        for base in (cand, cand.parent.resolve() / cand.name):
            try:
                return base.relative_to(self.root).as_posix()
            except ValueError:
                continue
        raise UserError(f"{path}: outside the repository at {self.root}")

    def resolve_rev(self, rev: str) -> str:
        """Full commit sha for REV (a branch, tag or sha); anything else is refused."""
        if rev.startswith("-"):
            raise UserError(f"--rev {rev!r}: not a revision")
        proc = _git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=self.root)
        sha = proc.stdout.strip()
        if proc.returncode != 0 or not sha:
            raise UserError(f"--rev {rev!r}: not a commit in this repository")
        return sha

    def show(self, sha: str, rel: str) -> str:
        try:
            proc = _git(["show", f"{sha}:{rel}"], cwd=self.root)
        except UnicodeDecodeError:
            raise UserError(f"cannot read {rel} as UTF-8 text (at {sha[:12]})") from None
        if proc.returncode != 0:
            raise UserError(f"git show {sha[:12]}:{rel} failed: {proc.stderr.strip()}")
        return proc.stdout


# ---------------------------------------------------------------------------
# T2 through the nx CLI
# ---------------------------------------------------------------------------

_LIST_LINE = re.compile(r"^\[(\d+)\] (.*)  \([^()]*\)$")
# nx wraps a transport failure as "T2 storage service error: [Errno 61] ..." and a missing
# endpoint as "T2 storage service unavailable: ..."; an HTTP status error carries httpx's status
# line ("Client error '401 ...'", "Redirect response '307 ...'") and is a rejection by a reachable
# service, not a connection failure. A 502, 503 or 504 comes from an edge or gateway with no engine
# behind it, so it counts as unavailable too. nx's top-level handler (cli.py) reports the same two
# conditions without the "T2 storage service" prefix when they surface mid-command, as "nexus-service
# endpoint is not resolvable" and "a service this command needs did not answer".
_HTTPX_STATUS = r"(?:(?:Client|Server) error|Redirect response|Informational response) '"
_CONNECTION = re.compile(
    r"T2 storage service (?:unavailable|error: "
    rf"(?:(?!{_HTTPX_STATUS})|Server error '50[234]\b))"
    r"|nexus-service endpoint is not resolvable"
    r"|a service this command needs did not answer"
)
_STATUS = re.compile(_HTTPX_STATUS + r"(\d{3})\b")
_SERVICE_ERROR = "T2 storage service error:"


def neutral_cause(detail: str) -> str:
    """The cause of a T2 failure in this script's own words, never nx's.

    nx ends these failures with a remedy addressed to an operator ("nx daemon service
    start", "nx doctor"); a model that reads it runs it (nexus-ger02.15). Only the status
    code is carried over, never a reason phrase or any other text from the response.
    """
    status = _STATUS.search(detail)
    if status:
        return f"the storage service answered HTTP {status.group(1)}"
    return "the storage service could not be reached"


# The words a remedy line carries. "daemon" and "doctor" also catch the continuation of a
# remedy that nx wrapped onto a second line ("... or restart the\nsupervisor: nx daemon ...").
_NAMES_NX = re.compile(r"\bnx\b|daemon|doctor|repair|start a service", re.IGNORECASE)


def without_nx(detail: str) -> str:
    """The last 20 lines of nx's error text, minus every line that names nx or a repair, as ": ..." or "".

    The lines that name nx are its remedies ("run 'nx upgrade'"), addressed to an operator;
    a model that reads one runs it (nexus-ger02.15). The rest (a refusal, a cause) is kept.
    """
    kept = [line for line in detail.splitlines()[-20:] if not _NAMES_NX.search(line)]
    return ": " + "\n".join(kept) if any(line.strip() for line in kept) else ""


def nx_unavailable(detail: str) -> bool:
    """True when nx's error text says it could not reach the T2 service."""
    return _CONNECTION.search(detail) is not None


class T2:
    """`nx memory` driver. Content goes in on stdin, reads go by numeric id.

    `nx memory get -t` falls back to a unique title PREFIX, so a title read
    would let doc/a.md answer with doc/a.md.bak. Titles are therefore confirmed
    exactly against `nx memory list`, and the record is fetched by its id.
    """

    def __init__(self) -> None:
        self.nx = shlex.split(os.environ.get("PROSE_EDIT_NX", "nx"))
        self._index: dict[str, dict[str, int]] = {}

    def forget(self, project: str) -> None:
        self._index.pop(project, None)

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            proc = subprocess.run(
                [*self.nx, *args], input=stdin, capture_output=True, text=True,
                encoding="utf-8", timeout=NX_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise T2Unavailable(f"the storage client did not finish within {NX_TIMEOUT} seconds") from exc
        except OSError as exc:
            raise T2Unavailable(f"the storage client could not be started ({exc.strerror or type(exc).__name__})") from exc
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip())
            if nx_unavailable(detail):
                raise T2Unavailable(neutral_cause(detail))
            if _SERVICE_ERROR in detail:
                raise UserError(f"T2 refused the request: {neutral_cause(detail)}")
            raise UserError(f"T2 request failed (exit {proc.returncode}){without_nx(detail)}")
        return proc.stdout

    def index(self, project: str) -> dict[str, int]:
        """Exact title -> row id for one project."""
        if project not in self._index:
            out = self._run(["memory", "list", "-p", project])
            found: dict[str, int] = {}
            for line in out.splitlines():
                if not line.strip() or line.strip() == "No entries found.":
                    continue
                m = _LIST_LINE.match(line)
                if not m or not m.group(2).startswith(project + "/"):
                    raise UserError(f"unparseable T2 list line: {line!r}")
                found[m.group(2)[len(project) + 1:]] = int(m.group(1))
            self._index[project] = found
        return self._index[project]

    def get(self, project: str, title: str) -> str | None:
        """Record body, or None when `nx memory list` shows no such exact title."""
        row = self.index(project).get(title)
        if row is None:
            return None
        out = self._run(["memory", "get", str(row)])
        return out[:-1] if out.endswith("\n") else out

    def get_json(self, project: str, title: str, kind: str) -> Obj | None:
        body = self.get(project, title)
        if body is None:
            return None
        what = f"record {project}/{title}"
        try:
            value: Any = json.loads(body)
        except ValueError as exc:
            raise _bad(what, f"not valid JSON: {exc}") from exc
        return check_record(kind, value, what)

    def put(self, project: str, title: str, value: Obj, ttl: str | None = None) -> None:
        args = ["memory", "put", "-", "-p", project, "-t", title, "--tags", "prose-edit"]
        if ttl:
            args += ["--ttl", ttl]
        self._run(args, stdin=json.dumps(value, ensure_ascii=False, indent=1))
        self.forget(project)

    def delete(self, project: str, title: str) -> None:
        row = self.index(project).get(title)
        if row is None:
            return
        self._run(["memory", "delete", "--id", str(row), "-y"])
        self.forget(project)


# ---------------------------------------------------------------------------
# Command context
# ---------------------------------------------------------------------------


class Target(NamedTuple):
    rel: str | None  # None for a stdin run
    range: Obj | None


class Ctx:
    def __init__(self, prefix: str) -> None:
        self.repo = Repo()
        self.t2 = T2()
        self.user_project, self.repo_project = projects(prefix, self.repo.name)
        self.now = _now()

    def target(self, arg: str) -> Target:
        if arg == "-":
            return Target(None, None)
        path, rng = split_range(arg)
        return Target(self.repo.rel(path), rng)

    def doc_title(self, arg: str) -> tuple[str, Target]:
        tgt = self.target(arg)
        if tgt.rel is None:
            raise UserError("a stdin run keeps no document record and stores no rejections")
        return f"doc/{tgt.rel}", tgt

    def level_project(self, level: str) -> str:
        return self.user_project if level == "user" else self.repo_project

    @contextmanager
    def locked(self, project: str, title: str) -> Generator[None]:
        """Exclusive lock on one record across processes.

        Repo records lock under this repo's git common directory (shared by its worktrees).
        User-level records are shared by every repo, so they lock in a per-user directory
        under the temp dir instead, where every repo on the machine finds the same file.
        """
        if project == self.user_project:
            lock_dir = Path(tempfile.gettempdir()) / f"prose-edit-locks-{os.getuid()}"
        else:
            lock_dir = self.repo.common / "prose-edit-locks"
        key = hashlib.sha256(f"{project}\0{title}".encode()).hexdigest()[:32]
        try:
            lock_dir.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(lock_dir / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise UserError(f"cannot take the record lock under {lock_dir}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.t2.forget(project)  # another writer may have changed the index
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load_layers(self, tgt: Target, site_layer: str | None) -> dict[str, Obj | None]:
        """All four layers by PRECEDENCE name (None = absent)."""
        site: Obj | None = None
        if site_layer:
            what = f"--site-layer {site_layer}"
            try:
                raw: Any = json.loads(Path(site_layer).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise _bad(what, str(exc)) from exc
            site = check_record("sheet", raw, what)
        jobs: list[tuple[str, str, str]] = [
            (self.user_project, "stylesheet", "sheet"), (self.user_project, NAD, "nad"),
            (self.repo_project, "stylesheet", "sheet"), (self.repo_project, NAD, "nad"),
        ]
        if tgt.rel is not None:
            jobs.append((self.repo_project, f"doc/{tgt.rel}", "doc"))
        for project in sorted({p for p, _, _ in jobs}):
            self.t2.index(project)
        def fetch(job: tuple[str, str, str]) -> Obj | None:
            return self.t2.get_json(job[0], job[1], job[2])

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            got = list(pool.map(fetch, jobs))
        doc: Obj | None = None
        if tgt.rel is not None and got[4] is not None:
            doc = _record(got[4])
            doc.setdefault("rejections", [])
        return {
            "user": _layer(got[0], got[1]), "repo": _layer(got[2], got[3]),
            "site-page": site, "document": doc,
        }


def _read_stdin_json(what: str, default: Any = None) -> Any:
    if sys.stdin.isatty():
        raise UserError(f"{what}: expected JSON on stdin but stdin is a terminal; pipe it in")
    text = sys.stdin.read()
    if not text.strip():
        if default is not None:
            return default
        raise UserError(f"{what}: expected JSON on stdin, got nothing")
    try:
        return json.loads(_unfence(text))
    except ValueError as exc:
        raise UserError(f"{what}: stdin is not valid JSON: {exc}") from exc


def _emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=1) + "\n")


def _record(rec: Obj | None) -> Obj:
    out: Obj = dict(rec or {})
    out.setdefault("scalars", {})
    out.setdefault("lists", {})
    return out


def _layer(stylesheet: Obj | None, nad: Obj | None) -> Obj | None:
    if stylesheet is None and nad is None:
        return None
    layer = _record(stylesheet)
    if nad is not None:
        layer[NAD] = list(nad.get("entries") or [])
    return layer


def _is_empty_doc(rec: Obj) -> bool:
    return not (rec.get("scalars") or rec.get("lists") or rec.get("rejections") or rec.get("voice_card"))


def _items(rec: Obj | None, key: str) -> list[Obj]:
    found: list[Obj] = (rec or {}).get(key) or []
    return list(found)


def _numbered(items: list[Obj]) -> list[Obj]:
    return [{"n": i, **r} for i, r in enumerate(items, start=1)]


def _save_doc(ctx: Ctx, title: str, rec: Obj) -> None:
    if _is_empty_doc(rec):
        ctx.t2.delete(ctx.repo_project, title)
    else:
        rec.setdefault("rejections", [])
        ctx.t2.put(ctx.repo_project, title, rec)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_repo(ctx: Ctx, a: argparse.Namespace) -> None:
    out: Obj = {
        "repo": ctx.repo.name, "root": str(ctx.repo.root),
        "projects": {"user": ctx.user_project, "repo": ctx.repo_project},
    }
    if a.path:
        tgt = ctx.target(a.path)
        out["path"], out["range"] = tgt.rel, tgt.range
    _emit(out)


def _genre_entries(layer: Obj | None) -> list[Any]:
    lists: Obj = (layer or {}).get("lists") or {}
    entries: list[Any] = lists.get("genre_map") or []
    return list(entries)


def _genre_from_layers(rel: str, layers: dict[str, Obj | None]) -> str | None:
    override_layers: list[list[Any]] = [_genre_entries(layers.get(name)) for name in PRECEDENCE]
    return genre_for(rel, override_layers)


def cmd_read(ctx: Ctx, a: argparse.Namespace) -> None:
    if a.genre:
        _genre_name(a.genre)
    tgt = ctx.target(a.path)
    layers = ctx.load_layers(tgt, a.site_layer)
    merged = merge_named(layers)
    genre: str | None = a.genre
    source: str | None = "flag" if genre else None
    if genre is None and tgt.rel is not None:
        genre = _genre_from_layers(tgt.rel, layers)
        source = "map" if genre else None
    genre_record = ctx.t2.get_json(ctx.repo_project, f"genre/{genre}", "genre") if genre else None
    shown: Obj = {n: layers[n] for n in ("user", "repo", "document")}
    if layers["site-page"] is not None:
        shown["site-page"] = layers["site-page"]
    _emit({
        "repo": ctx.repo.name, "path": tgt.rel, "range": tgt.range,
        "genre": genre, "genre_source": source, "precedence": PRECEDENCE,
        "layers": shown, "merged": merged, "genre_record": genre_record,
    })


def cmd_genre_for(ctx: Ctx, a: argparse.Namespace) -> None:
    tgt = ctx.target(a.path)
    if tgt.rel is None:
        raise UserError("genre-for needs a path")
    layers = ctx.load_layers(tgt, a.site_layer)
    _emit({"path": tgt.rel, "range": tgt.range, "genre": _genre_from_layers(tgt.rel, layers)})


def _check_entry_body(raw: Any) -> Obj:
    shape = 'add-entry --from-stdin: expected {"scalars": {...}, "lists": {"key": [...]}}'
    body = _obj(raw)
    if body is None or set(body) - {"scalars", "lists"}:
        raise UserError(shape)
    scalars, lists = _obj(body.get("scalars", {})), _obj(body.get("lists", {}))
    if scalars is None or lists is None or not all(_list(v) is not None for v in lists.values()):
        raise UserError(shape)
    return body


def cmd_add_entry(ctx: Ctx, a: argparse.Namespace) -> None:
    if a.level == "doc":
        if not a.path or a.path == "-":
            raise UserError("--level doc needs --path <document>")
        title, _ = ctx.doc_title(a.path)
        kind = "doc"
    else:
        title, kind = "stylesheet", "sheet"
    project = ctx.level_project(a.level)
    body: Obj = _check_entry_body(_read_stdin_json("add-entry")) if a.from_stdin else {}
    if not a.from_stdin and (not a.key or a.value is None):
        raise UserError("add-entry needs --key and --value, or --from-stdin")
    with ctx.locked(project, title):
        rec = _record(ctx.t2.get_json(project, title, kind))
        if a.from_stdin:
            in_scalars: Obj = body.get("scalars") or {}
            in_lists: Obj = body.get("lists") or {}
            rec["scalars"].update(in_scalars)
            for key, items in in_lists.items():
                if not isinstance(items, list):
                    raise UserError(f"add-entry: lists.{key} must be a list")
                _extend(rec["lists"].setdefault(key, []), cast(list[Any], items))
        elif a.list:
            _extend(rec["lists"].setdefault(a.key, []), [a.value])
        else:
            rec["scalars"][a.key] = a.value
        if kind == "doc":
            rec.setdefault("rejections", [])
        ctx.t2.put(project, title, rec)
    _emit({"project": project, "title": title, "record": rec})


def _rejection_items(a: argparse.Namespace) -> list[Obj]:
    if a.from_stdin:
        body: Any = _read_stdin_json("reject")
        raw: list[Any] = _list(body) or [body]
    else:
        raw = [{"old": a.old, "new": a.new, **({"reason": a.reason} if a.reason is not None else {})}]
    out: list[Obj] = []
    for item in raw:
        obj = _obj(item)
        if obj is None or not (isinstance(obj.get("old"), str) and isinstance(obj.get("new"), str)
                               and obj["old"]):
            raise UserError('reject: expected {"old": str, "new": str} with a non-empty old')
        if "reason" in obj and not isinstance(obj["reason"], str):
            raise UserError('reject: "reason" must be a string')
        out.append(obj)
    return out


def cmd_reject(ctx: Ctx, a: argparse.Namespace) -> None:
    title, tgt = ctx.doc_title(a.path)
    items = _rejection_items(a)
    with ctx.locked(ctx.repo_project, title):
        rec = _record(ctx.t2.get_json(ctx.repo_project, title, "doc"))
        rejections: list[Obj] = rec.setdefault("rejections", [])
        for item in items:
            entry: Obj = {"old": item["old"], "new": item["new"], "at": _iso(ctx.now)}
            if isinstance(item.get("reason"), str):
                entry["reason"] = item["reason"]
            key = change_key(entry["old"], entry["new"])
            for i, seen in enumerate(rejections):
                if change_key(seen["old"], seen["new"]) == key:  # one rejection per change: replace in place
                    if "reason" not in entry and isinstance(seen.get("reason"), str):
                        entry["reason"] = seen["reason"]  # a re-rejection that gives none keeps the earlier one
                    rejections[i] = entry
                    break
            else:
                rejections.append(entry)
        ctx.t2.put(ctx.repo_project, title, rec)
    _emit({"path": tgt.rel, "range": tgt.range, "rejections": _numbered(rejections)})


def _remove_nth(items: list[Obj], n: int, what: str) -> None:
    if not 1 <= n <= len(items):
        raise UserError(f"{what}: no entry number {n} ({len(items)} stored)")
    del items[n - 1]


def cmd_entries(ctx: Ctx, a: argparse.Namespace) -> None:
    if a.level == "doc":
        if not a.path or a.path == "-":
            raise UserError("--level doc needs --path <document>")
        title, _ = ctx.doc_title(a.path)
        kind = "doc"
    else:
        title, kind = "stylesheet", "sheet"
    project = ctx.level_project(a.level)

    def shown(rec: Obj | None) -> Obj:
        rec = _record(rec)
        return {"level": a.level, "project": project, "title": title,
                "scalars": rec["scalars"], "lists": rec["lists"]}

    if a.remove is None and a.remove_item is None:
        _emit(shown(ctx.t2.get_json(project, title, kind)))
        return
    what = f"{a.level} record {title}"
    with ctx.locked(project, title):
        found = ctx.t2.get_json(project, title, kind)
        if found is None:
            raise UserError(f"{what}: no entries stored")
        rec = _record(found)
        scalars: Obj = rec["scalars"]
        lists: dict[str, list[Any]] = rec["lists"]
        if a.remove is not None:
            key = str(a.remove)
            if key not in scalars and key not in lists:
                raise UserError(f"{what}: no entry named {key!r}")
            scalars.pop(key, None)
            lists.pop(key, None)
        else:
            key, sep, value = str(a.remove_item).partition("=")
            if not sep or not key:
                raise UserError("--remove-item expects KEY=VALUE")
            items = lists.get(key)
            if items is None or value not in items:
                raise UserError(f"{what}: list {key!r} has no item {value!r}")
            items.remove(value)
            if not items:
                del lists[key]
        if _is_empty_doc(rec):
            ctx.t2.delete(project, title)
        else:
            if kind == "doc":
                rec.setdefault("rejections", [])
            ctx.t2.put(project, title, rec)
    _emit(shown(rec))


def cmd_rejections(ctx: Ctx, a: argparse.Namespace) -> None:
    title, tgt = ctx.doc_title(a.path)
    if a.remove is None:
        rec = ctx.t2.get_json(ctx.repo_project, title, "doc")
        rejections: list[Obj] = _items(rec, "rejections")
    else:
        with ctx.locked(ctx.repo_project, title):
            rec = ctx.t2.get_json(ctx.repo_project, title, "doc")
            rejections = _items(rec, "rejections")
            if rec is None:
                raise UserError(f"{a.path}: no stored rejections")
            _remove_nth(rejections, a.remove, a.path)
            rec["rejections"] = rejections
            _save_doc(ctx, title, rec)
    _emit({"path": tgt.rel, "range": tgt.range, "rejections": _numbered(rejections)})


def cmd_filter(ctx: Ctx, a: argparse.Namespace) -> None:
    proposal = validate_proposal(_read_stdin_json("proposal"))
    tgt = ctx.target(a.path)
    stored: set[tuple[str, str]] = set()
    if tgt.rel is not None:
        rec = ctx.t2.get_json(ctx.repo_project, f"doc/{tgt.rel}", "doc")
        stored = {change_key(rej["old"], rej["new"]) for rej in _items(rec, "rejections")}
    promoted: set[tuple[str, str]] = set()
    for project in (ctx.user_project, ctx.repo_project):
        nad = ctx.t2.get_json(project, NAD, "nad")
        promoted |= {change_key(e["old"], e["new"]) for e in _items(nad, "entries")}
    kept: list[Obj] = []
    dropped: list[Obj] = []
    for edit in proposal["edits"]:
        key = change_key(edit["old"], edit["new"])
        if key in stored:
            dropped.append({"n": edit["n"], "old": edit["old"], "cause": "rejected"})
        elif key in promoted:
            dropped.append({"n": edit["n"], "old": edit["old"], "cause": "not-a-defect"})
        else:
            kept.append(edit)
    _emit({**proposal, "edits": kept, "dropped": dropped})


PROMOTE_DRYRUN_DIR = "prose-edit-promote"
PROMOTE_DRYRUN_MAX_AGE = 2 * 3600
VOICE_CARD_MAX = 4000


def _promote_hash(level: str, project: str, rel: str | None, entry: Obj) -> str:
    """What a promote would store, as a hash: level, project, document and the rejection's old and new.
    Not the time and not the rejection's number."""
    what = {"level": level, "project": project, "path": rel, "old": entry["old"], "new": entry["new"]}
    return hashlib.sha256(json.dumps(what, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _promote_record(ctx: Ctx, digest: str) -> Path:
    return ctx.repo.common / PROMOTE_DRYRUN_DIR / f"{digest}.json"


def _write_promote_dry_run(ctx: Ctx, digest: str) -> None:
    """Leave the record a real promote of exactly this entry will look for. Best effort sweep of old ones."""
    directory = ctx.repo.common / PROMOTE_DRYRUN_DIR
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        cutoff = datetime.now(timezone.utc).timestamp() - PROMOTE_DRYRUN_MAX_AGE
        for old in (*directory.glob("*.json"), *directory.glob("*.tmp")):  # a .tmp is a write that died before its rename
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                continue
        tmp = directory / f"{digest}.tmp"
        tmp.write_text(json.dumps({"entry": digest}), encoding="utf-8")
        os.replace(tmp, directory / f"{digest}.json")
    except OSError as exc:
        raise UserError(f"promote: cannot record the dry run under {directory}: {exc}") from exc


def _require_promote_dry_run(ctx: Ctx, digest: str, a: argparse.Namespace) -> Path:
    """The record of the matching dry run, or UserError: a real promote needs one the author was shown."""
    run_it = (f"Run `memory.py promote {a.path} {a.n} --level {a.level} --dry-run`, show the entry to the "
              "author, and run the real promote only after the author confirms it")
    path = _promote_record(ctx, digest)
    try:
        age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        saved: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UserError(f"promote: no matching dry run on record for this entry at this level. {run_it}.") from None
    if _obj(saved) is None or saved.get("entry") != digest:
        raise UserError(f"promote: no matching dry run on record for this entry at this level. {run_it}.")
    if age > PROMOTE_DRYRUN_MAX_AGE:
        raise UserError(f"promote: the dry run for this entry is older than two hours. {run_it}.")
    return path


def cmd_promote(ctx: Ctx, a: argparse.Namespace) -> None:
    title, tgt = ctx.doc_title(a.path)
    rec = ctx.t2.get_json(ctx.repo_project, title, "doc")
    rejections: list[Obj] = _items(rec, "rejections")
    if not 1 <= a.n <= len(rejections):
        raise UserError(f"{a.path}: no stored rejection number {a.n} ({len(rejections)} stored)")
    src: Obj = rejections[int(a.n) - 1]
    entry: Obj = {"old": src["old"], "new": src["new"], "from": tgt.rel, "at": _iso(ctx.now)}
    project = ctx.level_project(a.level)
    out: Obj = {"level": a.level, "project": project, "title": NAD, "entry": entry}
    digest = _promote_hash(a.level, project, tgt.rel, entry)
    if a.dry_run:
        _write_promote_dry_run(ctx, digest)
        _emit({**out, "dry_run": True})
        return
    record = _require_promote_dry_run(ctx, digest, a)
    with ctx.locked(project, NAD):
        nad = ctx.t2.get_json(project, NAD, "nad") or {}
        entries: list[Obj] = nad.setdefault("entries", [])
        key = change_key(str(entry["old"]), str(entry["new"]))
        entries[:] = [e for e in entries if change_key(str(e.get("old", "")), str(e.get("new", ""))) != key] + [entry]
        ctx.t2.put(project, NAD, nad)
    record.unlink(missing_ok=True)  # one dry run vouches for one promote
    _emit(out)


def _read_card_stdin() -> str:
    """The card text from stdin: {"voice_card": "<text>"}, a JSON string, or the plain text itself.

    A card is one string, so plain text is unambiguous; the live check at 858d959df saw a model write it that way
    first (nexus-ger02.7). Anything that parses as JSON but is not a string or that one-key object is refused.
    """
    if sys.stdin.isatty():
        raise UserError("voice-card: expected the card on stdin but stdin is a terminal; pipe it in")
    raw = sys.stdin.read()
    try:
        body: Any = json.loads(_unfence(raw))
    except ValueError:
        body = raw
    if isinstance(body, dict) and set(body) == {"voice_card"}:
        body = body["voice_card"]
    if not isinstance(body, str) or not body.strip():
        raise UserError('voice-card: expected the card text, or {"voice_card": "<text>"}: one non-empty string')
    return body.strip()


def cmd_voice_card(ctx: Ctx, a: argparse.Namespace) -> None:
    if a.path == "-":
        raise UserError("voice-card: a stdin run has no document record and stores no voice card")
    title, tgt = ctx.doc_title(a.path)
    shown: Obj = {"path": tgt.rel, "range": tgt.range}
    if not (a.from_stdin or a.remove):
        rec = ctx.t2.get_json(ctx.repo_project, title, "doc")
        _emit({**shown, "voice_card": (rec or {}).get("voice_card")})
        return
    text = ""
    if a.from_stdin:
        text = _read_card_stdin()
        if len(text) > VOICE_CARD_MAX:
            raise UserError(f"voice-card: the card is {len(text)} characters; the limit is {VOICE_CARD_MAX}")
    with ctx.locked(ctx.repo_project, title):
        rec = _record(ctx.t2.get_json(ctx.repo_project, title, "doc"))
        if a.from_stdin:
            rec["voice_card"] = {"text": text, "at": _iso(ctx.now)}
            rec.setdefault("rejections", [])
            ctx.t2.put(ctx.repo_project, title, rec)
        else:
            if "voice_card" not in rec:
                raise UserError(f"voice-card: no voice card stored for {tgt.rel}")
            del rec["voice_card"]
            _save_doc(ctx, title, rec)
    _emit({**shown, "voice_card": rec.get("voice_card")})


def cmd_nad(ctx: Ctx, a: argparse.Namespace) -> None:
    project = ctx.level_project(a.level)
    if a.remove is None:
        nad = ctx.t2.get_json(project, NAD, "nad")
        entries: list[Obj] = _items(nad, "entries")
    else:
        with ctx.locked(project, NAD):
            nad = ctx.t2.get_json(project, NAD, "nad")
            entries = _items(nad, "entries")
            _remove_nth(entries, a.remove, f"{a.level} not-a-defect")
            if entries:
                ctx.t2.put(project, NAD, {**(nad or {}), "entries": entries})
            else:
                ctx.t2.delete(project, NAD)
    _emit({"level": a.level, "project": project, "entries": _numbered(entries)})


_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z")


def _stamp_time(stamp: str) -> datetime:
    if not _STAMP.fullmatch(stamp):
        raise UserError(f"--stamp {stamp!r}: expected a UTC stamp like 20261003T101500.123456Z")
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise UserError(f"--stamp {stamp!r}: not a valid time ({exc})") from exc


def cmd_log(ctx: Ctx, a: argparse.Namespace) -> None:
    if a.genre:
        _genre_name(a.genre)
    tgt = ctx.target(a.path)
    # --stamp fixes the record's name and time: a caller that retries a log whose first put may have landed
    # passes the same stamp, so the retry writes over the record instead of adding a second
    when = _stamp_time(a.stamp) if a.stamp else ctx.now
    stamp = when.strftime("%Y%m%dT%H%M%S.%fZ")
    title = f"log/stdin/{stamp}" if tgt.rel is None else f"log/{tgt.rel}/{stamp}"
    session = _read_stdin_json("log", default={})
    record: Obj = {
        "path": tgt.rel, "range": tgt.range, "genre": a.genre,
        "at": _iso(when), "session": session,
    }
    ctx.t2.put(ctx.repo_project, title, record, ttl=LOG_TTL)
    _emit({"project": ctx.repo_project, "title": title, "ttl": LOG_TTL, "range": tgt.range})


def _genre_name(name: str) -> str:
    if name not in GENRES:
        raise UserError(f"genre {name!r}: not one of {', '.join(GENRES)}")
    return name


def _genre_title(name: str) -> str:
    return f"genre/{_genre_name(name)}"


def _exemplar_key(e: Obj) -> tuple[Any, ...]:
    return (e["path"], e["start"], e["end"], e["text"])


def _merge_exemplars(existing: list[Obj], new: list[Obj]) -> list[Obj]:
    out = list(existing)
    seen = {_exemplar_key(e) for e in out}
    for e in new:
        if _exemplar_key(e) not in seen:
            out.append(e)
            seen.add(_exemplar_key(e))
    return out


def cmd_genre_put(ctx: Ctx, a: argparse.Namespace) -> None:
    title = _genre_title(a.name)
    body: Any = _read_stdin_json("genre-put")
    shape = 'genre-put: expected {"exemplars": [{"text","path","start","end"}], "notes": [str]}'
    given = _obj(body)
    if given is None or set(given) - {"exemplars", "notes"}:
        raise UserError(shape)
    incoming = check_record("genre", {"exemplars": [], "notes": [], **given}, "genre-put input")
    with ctx.locked(ctx.repo_project, title):
        old = ctx.t2.get_json(ctx.repo_project, title, "genre")
        if a.replace or old is None:
            rec: Obj = {"exemplars": incoming["exemplars"], "notes": incoming["notes"]}
        else:
            rec = {
                "exemplars": _merge_exemplars(old.get("exemplars", []), incoming["exemplars"]),
                "notes": old.get("notes", []) + [
                    n for n in incoming["notes"] if n not in old.get("notes", [])
                ],
            }
        ctx.t2.put(ctx.repo_project, title, rec)
    _emit({"project": ctx.repo_project, "title": title, "record": rec})


def cmd_exemplar_add(ctx: Ctx, a: argparse.Namespace) -> None:
    title = _genre_title(a.genre)
    path, rng = split_range(a.where)
    if rng is None:
        raise UserError(f"{a.where!r}: expected <path>:<start>-<end>")
    rel = ctx.repo.rel(path)
    start, end = rng["start"], rng["end"]
    sha = ctx.repo.resolve_rev(a.rev) if a.rev else None
    if sha:
        source = ctx.repo.show(sha, rel)
    else:
        try:
            source = (ctx.repo.root / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise UserError(f"cannot read {rel} as UTF-8 text") from None
        except OSError as exc:
            raise UserError(f"{rel}: {exc}") from exc
    if "\x00" in source:
        raise UserError(f"cannot read {rel} as UTF-8 text (binary content)")
    # Split on newlines only: str.splitlines also breaks on U+2028, U+0085 and others,
    # which would shift every later line number away from the editor's.
    lines = [ln.removesuffix("\r") for ln in source.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    if end > len(lines):
        raise UserError(f"{rel}: line range {start}-{end} is outside the file ({len(lines)} lines)")
    exemplar: Obj = {"text": "\n".join(lines[start - 1:end]), "path": rel, "start": start, "end": end}
    if sha:
        exemplar["rev"] = sha
    with ctx.locked(ctx.repo_project, title):
        rec = ctx.t2.get_json(ctx.repo_project, title, "genre") or {}
        exemplars: list[Obj] = rec.setdefault("exemplars", [])
        rec.setdefault("notes", [])
        duplicate = _exemplar_key(exemplar) in {_exemplar_key(e) for e in exemplars}
        if not duplicate:
            exemplars.append(exemplar)
            ctx.t2.put(ctx.repo_project, title, rec)
    _emit({"project": ctx.repo_project, "title": title, "exemplar": exemplar,
           "duplicate": duplicate})


def cmd_viewer(ctx: Ctx, a: argparse.Namespace) -> None:
    if a.set is not None:
        with ctx.locked(ctx.user_project, "viewer"):
            ctx.t2.put(ctx.user_project, "viewer", {"viewer": a.set})
        _emit({"viewer": a.set})
        return
    rec = ctx.t2.get_json(ctx.user_project, "viewer", "viewer")
    _emit({"viewer": (rec or {}).get("viewer")})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

Handler = Callable[[Ctx, argparse.Namespace], None]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memory.py", description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--prefix", default=os.environ.get("PROSE_EDIT_PROJECT_PREFIX", ""),
                   help="prepended to every T2 project name (tests)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Handler) -> argparse.ArgumentParser:
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        return sp

    add("repo", cmd_repo).add_argument("path", nargs="?")
    sp = add("read", cmd_read)
    sp.add_argument("path")
    sp.add_argument("--genre")
    sp.add_argument("--site-layer", help="JSON file: {scalars, lists} between repo and document")
    sp = add("genre-for", cmd_genre_for)
    sp.add_argument("path")
    sp.add_argument("--site-layer")
    sp = add("add-entry", cmd_add_entry)
    sp.epilog = ("A rule of the site-page style sheet (section 3) is treated per level with a list under "
                 "site_page_section3_ignored, site_page_section3_query_only or site_page_section3_note_only; each "
                 "entry is the rule's opening words in double quotes, three dots inside the closing quote, then the "
                 "reason in parentheses, for example \"Plain technical English...\" (this essay).")
    sp.add_argument("--level", choices=["user", "repo", "doc"], required=True)
    sp.add_argument("--path")
    sp.add_argument("--key")
    sp.add_argument("--value")
    sp.add_argument("--list", action="store_true", help="append to the list KEY")
    sp.add_argument("--from-stdin", action="store_true")
    sp = add("reject", cmd_reject)
    sp.add_argument("path")
    sp.add_argument("--old")
    sp.add_argument("--new")
    sp.add_argument("--reason", help="the author's one line on why (optional)")
    sp.add_argument("--from-stdin", action="store_true")
    sp = add("entries", cmd_entries)
    sp.add_argument("--level", choices=["user", "repo", "doc"], required=True)
    sp.add_argument("--path")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--remove", metavar="KEY")
    grp.add_argument("--remove-item", metavar="KEY=VALUE")
    sp = add("rejections", cmd_rejections)
    sp.add_argument("path")
    sp.add_argument("--remove", type=int)
    add("filter", cmd_filter).add_argument("path")
    sp = add("promote", cmd_promote)
    sp.add_argument("path")
    sp.add_argument("n", type=int)
    sp.add_argument("--level", choices=["user", "repo"], required=True)
    sp.add_argument("--dry-run", action="store_true")
    sp = add("voice-card", cmd_voice_card)
    sp.add_argument("path")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--from-stdin", action="store_true", help='set it from {"voice_card": "<text>"} on stdin')
    grp.add_argument("--remove", action="store_true", help="delete the stored card")
    sp = add(NAD, cmd_nad)
    sp.add_argument("--level", choices=["user", "repo"], required=True)
    sp.add_argument("--remove", type=int)
    sp = add("log", cmd_log)
    sp.add_argument("path")
    sp.add_argument("--genre")
    sp.add_argument("--stamp", help="UTC stamp of the record (20261003T101500.123456Z); default now")
    sp = add("genre-put", cmd_genre_put)
    sp.add_argument("name")
    sp.add_argument("--replace", action="store_true", help="replace the record instead of merging")
    sp = add("exemplar-add", cmd_exemplar_add)
    sp.add_argument("genre")
    sp.add_argument("where", metavar="PATH:START-END")
    sp.add_argument("--rev", help="read the passage from this revision (git show REV:PATH)")
    add("viewer", cmd_viewer).add_argument("--set")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "reject" and not args.from_stdin and (args.old is None or args.new is None):
            raise UserError("reject needs --old and --new, or --from-stdin")
        args.fn(Ctx(args.prefix), args)
    except UserError as exc:
        sys.stderr.write(f"memory.py: {exc}\n")
        return 1
    except T2Unavailable as exc:
        sys.stderr.write(f"memory.py: T2 unavailable: {exc}\n")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
