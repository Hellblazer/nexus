#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic half of the prose-edit skill (RDR-221 Step 1.4): grammar, brief, agent output.

The skill hands every mechanical job to this script so the model's instructions carry
none of it. Stdlib only; T2 is reached only by running memory.py beside it, and the
project prefix override (PROSE_EDIT_PROJECT_PREFIX) passes through to that child.
Errors go to stderr with exit 1; memory.py's own exit codes (3: T2 unavailable) pass through.

  brief.py parse TOKEN...        the invocation, one token per argv, printed as JSON
  brief.py build TARGET [--genre G] [--budget N] [--file F] [--work] [--site-page FILE]
                                 the brief text for the editor agent, on stdout
  brief.py filter TARGET [--budget N] [--file F] [--save F | --work DIR]
                                 the agent's reply on stdin -> filtered proposal JSON
  brief.py tmpdir                a fresh temporary directory outside the repository
  brief.py site-layer [--site-page FILE]
                                 the site-page section 3 layer memory.py `read` takes

TARGET is PATH, PATH:START-END or "-" (a stdin run; --file names the saved text).

The brief travels to the editor as a file, not as text the orchestrating model retypes. `build --work`
(a path run) makes the work directory; a stdin run's --file already sits in one. In both cases `build`
writes the brief to WORK/brief.md and prints the brief on stdout, preceded for a path run by `WORK=<dir>` and a blank
line. The file ends with a blank line and `Brief id: <the first 12 hex digits of the sha256 of the brief above it>`.
The id is in the file and nowhere else (not on stdout, not in the dispatch prompt): the editor is told to read the file
and to echo that last line's id as `brief_sha` in its reply, so a reply that names it came from reading to the end of
the file. `filter` compares the reply with the file's id and, when the reply names none or another, or the file's id
line is missing or no longer matches its text, warns on stderr and in `warnings` (never fatal). A copied id proves
nothing and a missing one proves the file was not read to its last line. When the brief carries a stored voice card
(section 2, between <voice_card> tags) and the reply's voice_card is not that card, `filter` warns the same way.
A build whose --file is not in a work directory writes no file and prints the brief alone.

Grammar (`parse`; every token is one argv element, never a shell string):

  <target> [--genre G] [--budget N]       edit run; budget defaults to 10
  rejections <path> [--remove N]          -> memory.py rejections
  exemplar <genre> <path>:<start>-<end>   -> memory.py exemplar-add

A file named `rejections` or `exemplar` is reached as `-- rejections` or `./rejections`.
`--mode`, `--voice-card` and `--word-budget` are Phase 3 (Step 3.1) and are refused.

`parse` prints {"mode": "edit", path, range, stdin, genre, budget, target} or
{"mode": "rejections"|"exemplar", ..., "memory_argv": [...]}.

Section 3 of the brief (the style sheet) ends every bullet with its layer, "(layer: document|genre|repo|user)",
and states the rule: an entry from a narrower layer that contradicts a broader one wins (document > genre > repo >
user). The site-page section 3 rules are the genre layer; an entry two layers hold is labelled with the narrower.
Section 2 (the voice card) holds the document's saved author-approved card, when memory.py has one stored for it
(`voice-card`), as the anchor the editor uses and returns unchanged; without one it asks the editor to write the card.

`build` reads site-page section 3 from the sibling skill at run time for how-to and
exploration-essay. Which section 3 rules are ignored, query-only or note-only comes from
the repo style sheet lists site_page_section3_ignored, _query_only and _note_only, each
entry starting with the rule's opening words in quotes; a document's own record (`memory.py add-entry --level
doc`) can carry the same three lists, and for a rule both treat the document's wins, so one essay can ignore or
query a rule the genre applies. A listed rule that matches no bullet stops the build, so a skill edit cannot
silently change what the editor applies.

`filter` takes the agent's reply (one ```json block, a verbatim repeat of it, or bare JSON), keeps the first
--budget edits, runs memory.py filter (validation and stored rejections), then drops any
edit whose old string has no occurrence inside the range and outside quote, code, table,
frontmatter and HTML pre/table/blockquote/style/script/head blocks. Every drop is listed
under "dropped" with a cause: rejected, over-budget, not-found, outside-range,
protected-region. That the old string occurs exactly once is the apply step's check.
`--save FILE` also writes the printed JSON to FILE, which must sit directly inside a work
directory: review.py (the review loop, Steps 1.1 and 1.5) reads that file as its input, so the
model never retypes it. `--save` and `--work` are exclusive; the review loop deletes WORK itself.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any, cast

Obj = dict[str, Any]

HERE = Path(__file__).resolve().parent
MEMORY = HERE / "memory.py"
DEFAULT_SITE_PAGE = HERE.parents[1] / "site-page" / "SKILL.md"
COMMANDS = ("parse", "build", "filter", "tmpdir", "rmtmp", "site-layer")
SITE_GENRES = ("how-to", "exploration-essay")
DEFAULT_BUDGET = 10
PHASE3_FLAGS = ("--mode", "--voice-card", "--word-budget")
TREATMENT_KEYS = {
    "ignored": "site_page_section3_ignored",
    "query_only": "site_page_section3_query_only",
    "note_only": "site_page_section3_note_only",
}


def _load_memory() -> Any:
    spec = importlib.util.spec_from_file_location("prose_edit_memory_lib", MEMORY)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {MEMORY}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


_MEM = _load_memory()
UserError: type[Exception] = _MEM.UserError
GENRES: tuple[str, ...] = tuple(_MEM.GENRES)


class Passthrough(Exception):
    """A memory.py failure whose message and exit code go to our caller unchanged."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _user(message: str) -> Exception:
    return UserError(message)


# ---------------------------------------------------------------------------
# Invocation grammar
# ---------------------------------------------------------------------------

_USAGE = (
    "usage: <path>[:<start>-<end>] [--genre G] [--budget N] | - [--genre G] [--budget N] | "
    "rejections <path> [--remove N] | exemplar <genre> <path>:<start>-<end>"
)


def _positive(flag: str, raw: str) -> int:
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) < 1:
        raise _user(f"{flag} needs a positive integer, got {raw!r}")
    return int(raw)


def _split_flag(token: str) -> tuple[str, str | None]:
    if token.startswith("--") and "=" in token:
        flag, _, value = token.partition("=")
        return flag, value
    return token, None


def _range_of(target: str) -> tuple[str, Obj | None]:
    try:
        path, rng = cast("tuple[str, Obj | None]", _MEM.split_range(target))
    except _MEM.UserError as exc:
        raise _user(f"line range: {exc}") from exc
    if rng is not None and not re.search(r":[0-9]+(?:-[0-9]+)?$", target):
        raise _user(f"line range in {target!r}: digits must be ASCII")
    return path, rng


def _scan(tokens: list[str], value_flags: tuple[str, ...]) -> tuple[list[tuple[str, bool]], dict[str, str]]:
    """Positionals as (token, literal) and flag values; after a bare `--` every token is literal."""
    positionals: list[tuple[str, bool]] = []
    flags: dict[str, str] = {}
    literal = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if literal:
            positionals.append((token, True))
        elif token == "-" or not token.startswith("-"):
            positionals.append((token, False))
        elif token == "--":
            literal = True
        else:
            flag, inline = _split_flag(token)
            if flag in PHASE3_FLAGS:
                raise _user(f"{flag} is not built yet (RDR-221 Step 3.1)")
            if flag not in value_flags:
                raise _user(f"unknown flag {flag}")
            if flag in flags:
                raise _user(f"{flag} given more than once")
            if inline is None:
                if i >= len(tokens):
                    raise _user(f"{flag} needs a value")
                inline = tokens[i]
                i += 1
            flags[flag] = inline
    return positionals, flags


def _genre_arg(raw: str) -> str:
    if raw not in GENRES:
        raise _user(f"genre {raw!r}: not one of {', '.join(GENRES)}")
    return raw


def _parse_edit(tokens: list[str]) -> Obj:
    positionals, flags = _scan(tokens, ("--genre", "--budget", "--remove"))
    if "--remove" in flags:
        raise _user("--remove belongs to `rejections <path> --remove N`")
    if not positionals:
        raise _user("an edit run needs a path or - for stdin")
    if len(positionals) > 1:
        raise _user(f"an edit run takes one path, got {len(positionals)}: "
                    f"{' '.join(t for t, _ in positionals)}")
    given, literal_path = positionals[0]
    genre = _genre_arg(flags["--genre"]) if "--genre" in flags else None
    budget = _positive("--budget", flags["--budget"]) if "--budget" in flags else DEFAULT_BUDGET
    if given == "-" and not literal_path:
        if genre is None:
            raise _user("a stdin run needs --genre: stdin has no path to infer it from")
        return {"mode": "edit", "path": None, "range": None, "stdin": True, "genre": genre,
                "budget": budget, "target": "-"}
    path, rng = (given, None) if literal_path else _range_of(given)
    target = given if rng is not None else path
    return {"mode": "edit", "path": path, "range": rng, "stdin": False, "genre": genre,
            "budget": budget, "target": target}


def _parse_rejections(tokens: list[str]) -> Obj:
    positionals, flags = _scan(tokens, ("--remove", "--genre", "--budget"))
    if "--genre" in flags or "--budget" in flags:
        raise _user("rejections takes only <path> and --remove N")
    if not positionals:
        raise _user("rejections needs a path")
    if len(positionals) > 1:
        raise _user(f"rejections takes one path, got {len(positionals)}")
    path, literal_path = positionals[0]
    if path == "-" and not literal_path:
        raise _user("a stdin run keeps no rejections")
    if not literal_path and _range_of(path)[1] is not None:
        raise _user("rejections takes a bare path, not a line range")
    remove: int | None = None
    argv = ["rejections", path]
    if "--remove" in flags:
        remove = _positive("--remove", flags["--remove"])
        argv += ["--remove", str(remove)]
    return {"mode": "rejections", "path": path, "remove": remove, "memory_argv": argv}


def _parse_exemplar(tokens: list[str]) -> Obj:
    positionals, _ = _scan(tokens, ())
    if len(positionals) != 2:
        raise _user("exemplar needs <genre> <path>:<start>-<end>; " + _USAGE)
    genre = _genre_arg(positionals[0][0])
    where = positionals[1][0]
    rng = _range_of(where)[1]
    if rng is None or not re.search(r":[0-9]+-[0-9]+$", where):
        raise _user(f"exemplar {where!r}: expected <path>:<start>-<end> (a line range)")
    return {"mode": "exemplar", "genre": genre, "where": where,
            "memory_argv": ["exemplar-add", genre, where]}


def parse_invocation(tokens: list[str]) -> Obj:
    """The skill's argument tokens as a structured invocation, or UserError."""
    if not tokens:
        raise _user(_USAGE)
    if tokens[0] == "rejections":
        return _parse_rejections(tokens[1:])
    if tokens[0] == "exemplar":
        return _parse_exemplar(tokens[1:])
    return _parse_edit(tokens)


# ---------------------------------------------------------------------------
# site-page section 3 -> the site layer
# ---------------------------------------------------------------------------


def section3_bullets(text: str) -> list[str]:
    """Bullets under the `## 3.` heading, continuation lines joined."""
    bullets: list[str] = []
    inside = False
    found = False
    for line in text.split("\n"):
        if line.startswith("## "):
            inside = line.startswith("## 3.")
            found = found or inside
            continue
        if not inside:
            continue
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif line[:1] in (" ", "\t") and line.strip() and bullets:
            bullets[-1] += " " + line.strip()
    if not found:
        raise _user("site-page: no section 3 heading ('## 3.') found")
    if not bullets:
        raise _user("site-page: section 3 has no bullets")
    return bullets


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[`\\]", "", text).split())


_OPENING = re.compile(r'^\s*"(?P<open>.+?)\.\.\."')


def treatments_from_lists(lists: Obj) -> dict[str, list[str]]:
    """The three treatment lists of the repo style sheet, by kind."""
    out: dict[str, list[str]] = {}
    for kind, key in TREATMENT_KEYS.items():
        entries = cast("list[Any]", lists.get(key) or [])
        out[kind] = [str(e) for e in entries]
    return out


def _treatment_kinds(normalized: list[str], treatments: dict[str, list[str]], layer: str,
                     where: str = "--level repo") -> dict[int, str]:
    """The treatment each bullet gets from one layer's lists, by bullet index. Each listed rule is matched to
    exactly one bullet by its opening words; a stale or doubled entry stops the build, naming the layer and the
    command that removes the entry (`where` is that command's --level and --path)."""
    kind_of: dict[int, str] = {}
    for kind in ("ignored", "query_only", "note_only"):
        for entry in treatments.get(kind, []):
            m = _OPENING.match(entry)
            if not m:
                raise _user(f'malformed treatment entry {entry!r}: expected "opening words..." first')
            opening = _norm(m.group("open"))
            hits = [i for i, b in enumerate(normalized) if b.startswith(opening)]
            if not hits:
                remove = shlex.quote(f"{TREATMENT_KEYS[kind]}={entry}")
                raise _user(
                    f'site-page section 3 has no bullet opening "{opening}..." ({kind} in the {layer} '
                    "style sheet): the skill changed or the entry is stale. Remove the entry with: "
                    f"memory.py entries {where} --remove-item {remove}"
                )
            if len(hits) > 1:
                raise _user(f'"{opening}..." matches more than one section 3 bullet')
            if hits[0] in kind_of:
                raise _user(f'section 3 bullet "{opening}..." matches more than one treatment')
            kind_of[hits[0]] = kind
    return kind_of


def build_site_layer(bullets: list[str], treatments: dict[str, list[str]],
                     *narrower: tuple[str, dict[str, list[str]], str]) -> Obj:
    """memory.py's {"scalars", "lists"} layer for site-page section 3.

    Ignored rules are dropped; query-only and note-only rules are kept under their own
    list. `treatments` is the repo style sheet's; each (layer name, treatments) after it is a narrower layer
    (the document's), and for a bullet that two layers treat, the narrower layer's treatment wins.
    """
    normalized = [_norm(b) for b in bullets]
    kind_of = _treatment_kinds(normalized, treatments, "repo")
    for name, extra, where in narrower:
        kind_of.update(_treatment_kinds(normalized, extra, name, where))
    rules: list[str] = []
    queries: list[str] = []
    notes: list[str] = []
    for i, bullet in enumerate(bullets):
        kind = kind_of.get(i)
        if kind == "ignored":
            continue
        {"query_only": queries, "note_only": notes}.get(kind or "", rules).append(bullet)
    return {"scalars": {}, "lists": {
        "site_page_rules": rules, "site_page_queries": queries, "site_page_editors_note": notes,
    }}


# ---------------------------------------------------------------------------
# memory.py as a child process
# ---------------------------------------------------------------------------


def memory(args: list[str], stdin: str | None = None) -> str:
    proc = subprocess.run(
        [sys.executable, str(MEMORY), *args], input=stdin, capture_output=True, text=True,
        encoding="utf-8", timeout=600,
    )
    if proc.returncode != 0:
        raise Passthrough(proc.returncode, proc.stderr.strip() or f"memory.py {args[0]} failed")
    return proc.stdout


def memory_json(args: list[str], stdin: str | None = None) -> Obj:
    return cast(Obj, json.loads(memory(args, stdin)))


def _site_page_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _user(f"site-page skill unreadable at {path}: {exc}") from exc


def site_layer(site_page: Path, target: str | None = None) -> Obj:
    """Section 3 of the site-page skill as the genre layer. The repo style sheet lists which rules are ignored,
    query-only or note-only; a document's own record can list them too (`target` names the document), and for a
    rule both treat the document's entry wins, so one essay can ignore or query a rule the genre applies."""
    repo_sheet = memory_json(["entries", "--level", "repo"])
    treatments = treatments_from_lists(cast(Obj, repo_sheet.get("lists") or {}))
    narrower: list[tuple[str, dict[str, list[str]], str]] = []
    if target and target != "-":
        doc_sheet = memory_json(["entries", "--level", "doc", "--path", target])
        narrower.append(("document", treatments_from_lists(cast(Obj, doc_sheet.get("lists") or {})),
                         f"--level doc --path {shlex.quote(target)}"))
    return build_site_layer(section3_bullets(_site_page_text(site_page)), treatments, *narrower)


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------

_HIDDEN_LISTS = ("genre_map", "not-a-defect")


def _shown(item: object) -> str:
    return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)


def _bullets(items: list[Any], label: Callable[[Any], str | None] | None = None) -> str:
    """Bullets; with `label`, each ends in " (layer: <name>)" when the label function names one."""
    out: list[str] = []
    for i in items:
        name = label(i) if label else None
        out.append(f"- {_shown(i)}" + (f" (layer: {name})" if name else ""))
    return "\n".join(out)


# The brief's layer names. The site-page section 3 rules are the genre layer's (how-to, exploration-essay).
LAYER_LABEL = {"user": "user", "repo": "repo", "site-page": "genre", "document": "document"}


def _list_owner(layers: Obj, key: str) -> Callable[[Any], str | None]:
    """The layer label of a merged list entry: the narrowest layer that holds the same entry under `key`."""
    def owner(item: Any) -> str | None:
        for name in reversed(list(_MEM.PRECEDENCE)):  # narrowest first
            held = cast("list[Any]", cast(Obj, (layers.get(name) or {}).get("lists") or {}).get(key) or [])
            if any(_MEM._same(item, h) for h in held):
                return LAYER_LABEL.get(str(name))
        return None
    return owner


def _scalar_owner(layers: Obj, key: str) -> str | None:
    for name in reversed(list(_MEM.PRECEDENCE)):
        if key in cast(Obj, (layers.get(name) or {}).get("scalars") or {}):
            return LAYER_LABEL.get(str(name))
    return None


REJECTION_LIMIT = 20
REJECTION_CLIP = 100


def _clip(text: str, limit: int = REJECTION_CLIP) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _change_line(old: str, new: str, reason: object) -> str:
    """One stored rejection as the editor reads it: the minimal change, the author's reason, the span it was in."""
    gone, put = cast("tuple[str, str]", _MEM.change_key(old, new))
    if not gone.strip() and not put.strip():
        what = "change spacing only"
    elif not put:
        what = f'cut {json.dumps(_clip(gone), ensure_ascii=False)}'
    elif not gone:
        what = f'insert {json.dumps(_clip(put), ensure_ascii=False)}'
    else:
        what = (f'replace {json.dumps(_clip(gone), ensure_ascii=False)} with '
                f'{json.dumps(_clip(put), ensure_ascii=False)}')
    why = (f" (reason: {json.dumps(_clip(reason, 120), ensure_ascii=False)})"
           if isinstance(reason, str) and reason.strip() else "")  # quoted like old and new: no line breaks get in
    context = "" if old.strip() == gone.strip() else f" (in: {json.dumps(_clip(old), ensure_ascii=False)})"
    return f"- {what}{why}{context}"


def _document_rejections(read: Obj) -> list[str]:
    """The brief lines for this document's stored rejections: the newest REJECTION_LIMIT, each clipped,
    with a count of the older ones left out. Empty when there are none (or on a stdin run, which has no document)."""
    doc = cast("Obj | None", cast(Obj, read.get("layers") or {}).get("document"))
    stored = cast("list[Obj]", (doc or {}).get("rejections") or [])
    if not stored:
        return []
    newest_first = sorted(enumerate(stored), key=lambda p: (str(p[1].get("at", "")), p[0]), reverse=True)
    seen: set[tuple[str, str]] = set()
    unique: list[Obj] = []
    for _, r in newest_first:  # one line per change, the newest entry for it, BEFORE the bound is applied
        key = cast("tuple[str, str]", _MEM.change_key(str(r["old"]), str(r["new"])))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    shown = unique[:REJECTION_LIMIT]
    out = ["The author rejected these edits for THIS document. Do not propose them. Do not propose any edit "
           "that makes the same change, wherever it sits and however much text around the words it spans: "
           "a longer or a shorter old string around the same words is the same change."]
    out += [_change_line(str(r["old"]), str(r["new"]), r.get("reason")) for r in shown]
    left = len(unique) - len(shown)
    if left > 0:
        out.append(f"{left} older rejections are not listed here; the same rule holds for them.")
    return out


def render_brief(read: Obj, budget: int, input_file: str | None, input_text: str | None = None,
                 header: list[str] | None = None) -> str:
    """The editor's brief, in the RDR order, from a memory.py `read` result."""
    genre = str(read["genre"])
    merged = cast(Obj, read["merged"])
    scalars = cast(Obj, merged.get("scalars") or {})
    lists = cast("dict[str, list[Any]]", merged.get("lists") or {})
    record = cast("Obj | None", read.get("genre_record"))
    rng = cast("Obj | None", read.get("range"))
    layers = cast(Obj, read.get("layers") or {})
    doc_layer = cast("Obj | None", layers.get("document"))
    card = cast("Obj | None", (doc_layer or {}).get("voice_card"))
    out: list[str] = ["# Editing brief", "", f"Genre: {genre}"]
    if input_file:
        out.append(f"Input: the text under \"Text to edit\" below (a stdin run, saved at {input_file}; "
                   "no repository path, no document record). Edit only this text.")
    else:
        out.append(f"Document: {read['path']}")
    if rng:
        out.append(f"Propose edits only inside lines {rng['start']}-{rng['end']}. "
                   + ("The voice card is the whole file's." if card else "Build the voice card from the whole file."))
    out += header or []
    out += ["", "## 1. Exemplars", ""]
    exemplars = cast("list[Obj]", (record or {}).get("exemplars") or [])
    if exemplars:
        out.append(f"Passages in the voice this genre wants ({genre}):")
        for ex in exemplars:
            out += ["", f'<exemplar path="{ex["path"]}" lines="{ex["start"]}-{ex["end"]}">',
                    str(ex["text"]), "</exemplar>"]
        notes = cast("list[Any]", (record or {}).get("notes") or [])
        if notes:
            out += ["", "Genre notes:", _bullets(notes, lambda _n: "genre")]
    else:
        out.append(
            f"No exemplars are stored for genre {genre}. Run without exemplars, build the voice "
            "card from the document alone, and say in the editor's note that no exemplars were used."
        )
    out += ["", "## 2. Voice card", ""]
    if card:
        out += ["The author approved this voice card for this document on "
                f"{str(card.get('at', ''))[:10]}. It is author-approved: it is your anchor. Do not rebuild it "
                "from the document, which has been edited since it was written. Use it as written to tell a "
                "device from a defect. Return it unchanged in the \"voice_card\" field. If the document shows "
                "a device the card does not list, say so in the note, never in the card.",
                "", "<voice_card>", str(card["text"]), "</voice_card>"]
    else:
        out.append("Before proposing any edit, write the voice card for this document from the whole "
                   "document and the exemplars above. Return it in the \"voice_card\" field.")
    out += ["", "## 3. Style sheet", "",
            "Every entry below ends with the layer it comes from: document, genre (site-page section 3, "
            "for how-to and exploration-essay), repo or user.",
            "When two entries contradict each other, the entry from the narrower layer wins and you ignore "
            "the other (document > genre > repo > user).",
            "Entries that do not contradict each other all apply. A setting shows the value of the "
            "narrowest layer that gives it.",
            "A voice-card device is not an entry and not a layer: it wins over every layer. A device is "
            "never edited, whatever an entry below says. When an entry conflicts with a device, raise a "
            "query instead, never an edit."]
    if scalars:
        out += ["", "### Settings", "", "\n".join(
            f"- {k}: {_shown(v)}" + (f" (layer: {own})" if (own := _scalar_owner(layers, k)) else "")
            for k, v in scalars.items())]
    keys = [k for k in lists if k not in _HIDDEN_LISTS and not k.startswith("site_page_") and lists[k]]
    for key in sorted(keys, key=lambda k: (k != "diagnostics", k)):
        out += ["", f"### {key}", "", _bullets(lists[key], _list_owner(layers, key))]
    if lists.get("site_page_rules"):
        out += ["", "### site-page section 3 rules (genre layer)", "",
                _bullets(lists["site_page_rules"], _list_owner(layers, "site_page_rules"))]
    if lists.get("site_page_queries"):
        out += ["", "### site-page section 3 rules that produce QUERY ONLY (never an edit)", "",
                _bullets(lists["site_page_queries"], _list_owner(layers, "site_page_queries"))]
    if lists.get("site_page_editors_note"):
        out += ["", "### site-page section 3 rule for the editor's note only (never an edit)", "",
                _bullets(lists["site_page_editors_note"], _list_owner(layers, "site_page_editors_note"))]
    out += ["", "## 4. Not a defect", ""]
    nad = cast("list[Obj]", lists.get("not-a-defect") or [])
    rejected = _document_rejections(read)
    if nad:
        out.append("The author rejected these edits before. Never propose one, or any edit that makes "
                   "the same change:")
        for e in nad:
            src = f" (from {e['from']})" if e.get("from") else ""
            out.append(f"- {json.dumps(e['old'], ensure_ascii=False)} -> "
                       f"{json.dumps(e['new'], ensure_ascii=False)}{src}")
    if rejected:
        out += ([""] if nad else []) + rejected
    if not nad and not rejected:
        out.append("None stored.")
    out += ["", "## 5. Budget", "",
            f"Propose at most {budget} sentence edits. Fewer is right when fewer are earned. "
            "Paragraph proposals and queries are not counted."]
    out += ["", "## 6. Prefer cutting", "",
            "Prefer cutting to rewriting: when a word, clause or sentence can go without loss, "
            "propose the cut (an empty new string). Cut only filler words; every other qualifier "
            "or intensifier is a query. Never cut a voice-card device, however this section or a "
            "diagnostic reads. Never put in content or claims."]
    if input_text is not None:
        out += ["", "## Text to edit", "", "<document>", input_text.rstrip("\n"), "</document>"]
    return "\n".join(out) + "\n"


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                          cwd=root, timeout=120)


def _glob_of(path: str) -> str:
    pure = Path(path)
    return f"{pure.parent.as_posix()}/*{pure.suffix}" if pure.parent.as_posix() != "." else f"/*{pure.suffix}"


def genre_paths(files: list[str], genre: str, layers: Obj, target: str | None) -> list[str]:
    """Globs (or file names) of the repository's documents of `genre`, the target left out.

    A glob is used only when every listed file it would reach has the genre.
    """
    override: list[list[Any]] = []
    for name in _MEM.PRECEDENCE:
        layer = cast("Obj | None", layers.get(str(name)))
        lists = cast(Obj, (layer or {}).get("lists") or {})
        override.append(list(cast("list[Any]", lists.get("genre_map") or [])))
    has = {f for f in files if _MEM.genre_for(f, override) == genre}
    others = sorted(has - {target} if target else has)
    out: list[str] = []
    seen: set[str] = set()
    for f in others:
        glob = _glob_of(f)
        if glob in seen:
            continue
        reach = [g for g in files if _glob_of(g) == glob]
        if all(g in has for g in reach):
            seen.add(glob)
            out.append(glob)
        else:
            out.append(f)
    return out


def _ranges(lines: list[int]) -> str:
    runs: list[tuple[int, int]] = []
    for n in sorted(set(lines)):
        if runs and n == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return ", ".join(f"{a}-{b}" for a, b in runs)


_HUNK = re.compile(r"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,([0-9]+))? @@", re.MULTILINE)


def new_prose_line(root: Path, rel: str) -> str:
    """The brief's "New prose:" line: changed lines against HEAD (index and working tree)."""
    tracked = _run_git(root, ["ls-files", "--error-unmatch", "--", rel]).returncode == 0
    if not tracked:
        return "New prose: all lines (the file is not tracked). An em dash on any line is an edit."
    paths = [rel]
    status = _run_git(root, ["diff", "HEAD", "-M", "--name-status", "-z"]).stdout.split("\0")
    for i, field in enumerate(status):
        if field.startswith("R") and i + 2 < len(status) and status[i + 2] == rel:
            paths = [status[i + 1], rel]  # a rename: only the changed lines are new
    diff = _run_git(root, ["diff", "HEAD", "-M", "-U0", "--no-color", "--no-ext-diff", "--", *paths])
    if diff.returncode != 0:
        return "New prose: unknown (the diff failed). An em dash is a query, never an edit."
    changed: list[int] = []
    for m in _HUNK.finditer(diff.stdout):
        start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        changed += range(start, start + count)
    if rel == "CHANGELOG.md":
        lines = (root / rel).read_text(encoding="utf-8").split("\n")
        begin = next((i for i, ln in enumerate(lines) if ln.startswith("## [Unreleased]")), None)
        if begin is not None:
            end = next((i for i in range(begin + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
            while end > begin + 1 and not lines[end - 1].strip():
                end -= 1
            changed += range(begin + 1, end + 1)
    if not changed:
        return "New prose: none (no changed lines)"
    return f"New prose: lines {_ranges(changed)}. An em dash on any other line is a query, never an edit."


def cmd_build(a: argparse.Namespace) -> str:
    stdin = a.target == "-"
    if stdin and not a.file:
        raise _user("a stdin run needs --file <path of the saved text>")
    if not stdin and a.file:
        raise _user("--file is only for a stdin run (target -)")
    budget = _positive("--budget", a.budget)
    genre: str | None = _genre_arg(a.genre) if a.genre else None
    if genre is None:
        if not stdin:
            genre = cast("str | None", memory_json(["genre-for", a.target]).get("genre"))
        if genre is None:
            raise _user(
                f"no genre for {'stdin' if stdin else a.target}: ask the author which of "
                f"{', '.join(GENRES)} applies, then pass --genre"
            )
    read_args = ["read", a.target, "--genre", genre]
    layer_file: Path | None = None
    try:
        if genre in SITE_GENRES:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
                layer_file = Path(fh.name)
                json.dump(site_layer(Path(a.site_page), a.target), fh)
            read_args += ["--site-layer", str(layer_file)]
        read = memory_json(read_args)
    finally:
        if layer_file is not None:
            layer_file.unlink(missing_ok=True)
    text: str | None = None
    if stdin:
        try:
            text = Path(a.file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _user(f"cannot read --file {a.file} as UTF-8 text: {exc}") from exc
    root = Path(str(memory_json(["repo"])["root"]))
    listed = _run_git(root, ["ls-files", "-co", "--exclude-standard", "-z"]).stdout
    files = [f for f in listed.split("\0") if f]
    rel = None if stdin else cast("str | None", read.get("path"))
    found = genre_paths(files, genre, cast(Obj, read.get("layers") or {}), rel)
    header = [new_prose_line(root, rel) if rel else "New prose: all text is new (a stdin run).",
              "Genre paths: " + (", ".join(found) if found else "none")]
    if rel:
        header.append(f"Exclude from the search: {rel}")
    brief = render_brief(read, budget, a.file if stdin else None, text, header)
    if a.work and stdin:
        raise _user("--work is for a path run; a stdin run already has its work directory")
    work: Path | None = None
    head = ""
    if a.work:
        work = Path(cmd_tmpdir())
        head = f"WORK={work}\n\n"
    elif stdin and _is_work_dir(Path(a.file).parent.resolve(), Path(tempfile.gettempdir()).resolve()):
        work = work_file(a.file).parent
    if work is None:
        return brief
    # the id goes into the file only: stdout and the dispatch prompt never carry it, so a reply that names it read it
    (work / BRIEF_FILE).write_text(f"{brief}\n{ID_LABEL}{brief_sha(brief.encode('utf-8'))}\n", encoding="utf-8")
    return head + brief


# ---------------------------------------------------------------------------
# The agent's reply
# ---------------------------------------------------------------------------

# Indented fences are accepted: the harness indents every line of a subagent's report.
_JSON_BLOCK = re.compile(r"^[ \t]*```json[ \t]*\n(?P<body>.*?)\n[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE)


def _canonical(block: str) -> str:
    try:
        return json.dumps(json.loads(block), sort_keys=True)
    except ValueError:
        return block


def extract_proposal(text: str) -> Obj:
    """The agent's proposal object: one ```json block (a repeat of the same block is one), or the
    whole reply as JSON. Two different blocks are refused."""
    blocks = [m.group("body") for m in _JSON_BLOCK.finditer(text)]
    if len(blocks) > 1 and len({_canonical(b) for b in blocks}) > 1:
        raise _user(f"the reply holds more than one different ```json block ({len(blocks)}); expected one")
    if blocks:
        raw = blocks[0]
    elif text.strip().startswith("{"):
        raw = text.strip()
    else:
        raise _user("the reply holds no JSON proposal (no ```json block)")
    try:
        value: Any = json.loads(raw)
    except ValueError as exc:
        raise _user(f"the proposal is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise _user("the proposal is not a JSON object")
    return cast(Obj, value)


Span = tuple[int, int]
_HTML_TAGS = ("pre", "table", "blockquote", "style", "script", "head", "svg", "code", "kbd")
_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|[0-9]{1,9}[.)])\s")
_TABLE_DELIM = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_BLOCK_START = re.compile(r"^\s*(?:#{1,6}\s|`{3,}|~{3,}|[-*_]{3,}\s*$)")


def _html_spans(text: str) -> list[Span]:
    """Comments and whole (nested-aware) pre, table, blockquote, style, script, head, svg, code, kbd."""
    spans = [m.span() for m in re.finditer(r"<!--.*?-->", text, re.DOTALL)]
    for tag in _HTML_TAGS:
        opener = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
        token = re.compile(rf"<(/?){tag}\b[^>]*>", re.IGNORECASE)
        pos = 0
        while (m := opener.search(text, pos)) is not None:
            depth, end = 1, None
            for t in token.finditer(text, m.end()):
                depth += -1 if t.group(1) else 1
                if depth == 0:
                    end = t.end()
                    break
            # An opener with no closer (a tag named in prose) protects only itself.
            spans.append((m.start(), end if end is not None else m.end()))
            pos = end if end is not None else m.end()
    return spans


def _markdown_spans(text: str) -> list[Span]:
    lines = text.split("\n")
    starts: list[int] = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    ends = [st + len(ln) for st, ln in zip(starts, lines)]
    spans: list[Span] = []
    first = 0
    marker = lines[0].lstrip(chr(0xFEFF)).strip() if lines else ""
    if marker in ("---", "+++"):
        closers = ("---", "...") if marker == "---" else ("+++",)
        for j in range(1, len(lines)):
            if lines[j].strip() in closers:
                spans.append((0, ends[j]))
                first = j + 1
                break
    n = len(lines)
    i = first
    in_list = False
    prev_blank = True
    while i < n:
        line = lines[i]
        if not line.strip():
            prev_blank = True
            i += 1
            continue
        fence = _FENCE_OPEN.match(line)
        if fence:
            mark = fence.group(1)
            closer = re.compile(rf"^\s*{re.escape(mark[0])}{{{len(mark)},}}\s*$")
            j = i + 1
            while j < n and not closer.match(lines[j]):
                j += 1
            last = min(j, n - 1)
            spans.append((starts[i], ends[last]))
            i, prev_blank = j + 1, False
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        lead = line.lstrip()
        table = lead.startswith("|") or (
            "|" in line and i + 1 < n and "|" in lines[i + 1] and _TABLE_DELIM.match(lines[i + 1]) is not None
            and "-" in lines[i + 1]
        )
        if table:
            j = i
            while j + 1 < n and lines[j + 1].strip() and "|" in lines[j + 1]:
                j += 1
            spans.append((starts[i], ends[j]))
            i, prev_blank, in_list = j + 1, False, False
            continue
        if lead.startswith(">"):
            j = i
            while j + 1 < n and lines[j + 1].strip() and not _BLOCK_START.match(lines[j + 1]):
                j += 1
            spans.append((starts[i], ends[j]))
            i, prev_blank, in_list = j + 1, False, False
            continue
        if _LIST_ITEM.match(line):
            in_list, prev_blank = True, False
            i += 1
            continue
        if indent > 0 and in_list:
            prev_blank = False
            i += 1
            continue
        if prev_blank and (line.startswith("    ") or line.startswith("\t")):
            j = i
            k = i + 1
            while k < n:
                if lines[k].startswith("    ") or lines[k].startswith("\t"):
                    j = k
                elif lines[k].strip():
                    break
                k += 1
            spans.append((starts[i], ends[j]))
            i, prev_blank = j + 1, False
            continue
        if indent == 0:
            in_list = False
        prev_blank = False
        i += 1
    return spans


def html_tag_spans(text: str) -> list[Span]:
    """Every <...> tag, comment marker and declaration: markup, not prose."""
    return [m.span() for m in re.finditer(r"<[^<>]*>", text)]


_LINK_DEST = re.compile(r"\]\((?:[^()\s]|\([^()\s]*\))*(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_REF_DEF = re.compile(
    r"^ {0,3}\[(?!\^)[^\]\n]+\]:[ \t]*"                                    # the label: prose, never protected
    r"(?P<p>(?:<[^>\n]*>|\S+)(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?)[ \t]*$",  # destination + title
    re.MULTILINE)
_AUTOLINK = re.compile(r"<(?:https?|ftp|mailto):[^<>\s]+>")
_OPEN_TAG = re.compile(r"<[A-Za-z][^<>]*>")
_ATTR_VALUE = re.compile(r"=\s*(\"[^\"]*\"|'[^']*')")


def _destination_spans(text: str) -> list[Span]:
    """Where a link goes is not prose: the (...) after a markdown link's text, the destination and
    title of a reference definition (its label is prose, and a footnote definition `[^1]: ...` is
    all prose), an autolink, and every attribute value of inline HTML. A rewrite there breaks the link."""
    spans: list[Span] = []
    for rx in (_LINK_DEST, _AUTOLINK):
        spans += [m.span() for m in rx.finditer(text)]
    spans += [m.span("p") for m in _REF_DEF.finditer(text)]
    for tag in _OPEN_TAG.finditer(text):
        spans += [(tag.start() + m.start(1), tag.start() + m.end(1)) for m in _ATTR_VALUE.finditer(tag.group())]
    return spans


def protected_spans(text: str, html: bool = False) -> list[Span]:
    """Character spans no edit may touch: frontmatter, fenced and indented code, quotes, tables,
    comments, HTML code-like blocks, and in markdown the link destinations and the attribute values
    of inline HTML. An HTML file gets only the HTML rules (its tags are checked apart)."""
    if html:
        return _html_spans(text)
    return _markdown_spans(text) + _html_spans(text) + _destination_spans(text)


_ABBREVIATIONS = frozenset({"e.g", "i.e", "etc", "vs", "cf", "approx", "fig", "no", "dr", "mr", "mrs", "ms",
                            "st", "inc", "ltd", "al"})
_BREAK = re.compile(r"(?P<term>[.!?])(?P<close>[\"')\]]*)(?P<gap>\s+)(?=[\"'(\[]*[A-Z])")


def sentence_span(old: str) -> str | None:
    """"multi" when `old` clearly holds more than one sentence, "maybe" when a period might end one."""
    if "\n\n" in old:
        return "multi"
    verdict: str | None = None
    for m in _BREAK.finditer(old):
        before = old[:m.start()]
        if before.count("`") % 2 == 1:
            continue
        word = re.search(r"([A-Za-z0-9.]+)$", before)
        token = word.group(1) if word else ""
        if token.lower() in _ABBREVIATIONS:
            continue
        if re.fullmatch(r"[A-Za-z]{3,}", token):
            return "multi"
        verdict = "maybe"
    return verdict


def _quoted_phrases(text: str) -> list[str]:
    lq, rq = chr(0x201C), chr(0x201D)
    found = re.findall(rf'["{lq}]([^"{lq}{rq}]{{3,}}?)["{rq}]', text)
    return [f.strip().rstrip(".,;:") for f in found if f.strip()]


def _occurrences(text: str, needle: str) -> list[int]:
    out: list[int] = []
    i = text.find(needle)
    while i != -1:
        out.append(i)
        i = text.find(needle, i + 1)
    return out


def _range_span(text: str, rng: Obj | None) -> Span | None:
    if not rng:
        return None
    lines = text.split("\n")
    starts: list[int] = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    lo = int(rng["start"]) - 1
    hi = min(int(rng["end"]), len(lines)) - 1
    if lo >= len(lines):
        return (len(text) + 1, len(text) + 1)
    return (starts[lo], starts[hi] + len(lines[hi]))


def edit_problem(text: str, old: str, spans: list[Span], rng: Span | None) -> str | None:
    """None when `old` occurs somewhere editable; else the cause it cannot be applied."""
    found = in_range = False
    i = text.find(old)
    while i != -1:
        found = True
        j = i + len(old)
        if rng is None or (rng[0] <= i and j <= rng[1]):
            in_range = True
            if not any(i < b and a < j for a, b in spans):
                return None
        i = text.find(old, i + 1)
    if not found:
        return "not-found"
    return "protected-region" if in_range else "outside-range"


def _n_of(item: object) -> int:
    n = cast(Obj, item).get("n") if isinstance(item, dict) else None
    return n if isinstance(n, int) else 10**9


def _with_new(dropped: Obj, by_n: dict[Any, Obj]) -> Obj:
    """memory.py's dropped entry (n, old, cause) plus the edit's new text, so the author can audit it."""
    src = by_n.get(dropped.get("n"))
    if src is None or "new" in dropped:
        return dropped
    return {"n": dropped["n"], "old": dropped.get("old"), "new": src.get("new"), "cause": dropped.get("cause")}


def cmd_filter(a: argparse.Namespace, reply: str) -> Obj:
    stdin = a.target == "-"
    if stdin and not a.file:
        raise _user("a stdin run needs --file <path of the saved text>")
    if not stdin and a.file:
        raise _user("--file is only for a stdin run (target -)")
    budget = _positive("--budget", a.budget)
    proposal = extract_proposal(reply)
    over: list[Any] = []
    raw_edits = proposal.get("edits")
    if isinstance(raw_edits, list) and len(cast("list[Any]", raw_edits)) > budget:
        over = cast("list[Any]", raw_edits)[budget:]
        proposal["edits"] = cast("list[Any]", raw_edits)[:budget]
    result = memory_json(["filter", a.target], json.dumps(proposal))
    by_n: dict[Any, Obj] = {}
    for entry in cast("list[Any]", raw_edits or []):
        if isinstance(entry, dict) and "n" in entry:
            by_n[cast(Obj, entry)["n"]] = cast(Obj, entry)
    dropped = [_with_new(d, by_n) for d in cast("list[Obj]", result.get("dropped") or [])]
    warnings: list[str] = []
    if over:
        warnings.append(f"the editor returned {len(over) + budget} edits against a budget of "
                        f"{budget}; kept the first {budget}")
        for item in over:
            o = cast(Obj, item) if isinstance(item, dict) else {}
            dropped.append({"n": o.get("n"), "old": o.get("old"), "new": o.get("new"), "cause": "over-budget"})
    if stdin:
        source, rng = Path(a.file), None
    else:
        where = memory_json(["repo", a.target])
        source, rng = Path(str(where["root"])) / str(where["path"]), cast("Obj | None", where.get("range"))
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _user(f"cannot read {source} as UTF-8 text: {exc}") from exc
    is_html = source.suffix.lower() in (".html", ".htm")
    spans = protected_spans(text, html=is_html)
    tags = html_tag_spans(text) if is_html else []
    window = _range_span(text, rng)
    kept: list[Obj] = []
    for e in cast("list[Obj]", result.get("edits") or []):
        cause = edit_problem(text, str(e["old"]), spans, window)
        if cause is None and tags and edit_problem(text, str(e["old"]), tags, window) is not None:
            cause = "markup"
        sentences = sentence_span(str(e["old"])) if cause is None else None
        if cause is None and sentences == "multi":
            cause = "multi-sentence"
        if cause:
            dropped.append({"n": e["n"], "old": e["old"], "new": e.get("new"), "cause": cause})
            continue
        if sentences == "maybe":
            warnings.append(f"edit {e['n']} may cover more than one sentence")
        kept.append(e)
    dropped.sort(key=_n_of)

    def where_is(needle: str) -> str:
        spots = _occurrences(text, needle)
        if not spots:
            return "absent"
        if window is None or any(window[0] <= i and i + len(needle) <= window[1] for i in spots):
            return "inside"
        return "outside"

    queries: list[Obj] = []
    dropped_queries: list[Obj] = []
    for q in cast("list[Obj]", result.get("queries") or []):
        place = where_is(str(q["anchor"]))
        if place == "inside":
            queries.append(q)
        else:
            dropped_queries.append({"n": q["n"], "anchor": q["anchor"],
                                    "cause": "anchor-not-found" if place == "absent" else "outside-range"})
    paragraphs: list[Obj] = []
    dropped_paragraphs: list[Obj] = []
    for pr in cast("list[Obj]", result.get("paragraphs") or []):
        phrases = _quoted_phrases(str(pr.get("paragraphs", "")))
        places = [where_is(ph) for ph in phrases]
        if window is not None and places and "inside" not in places and "outside" in places:
            dropped_paragraphs.append({"n": pr["n"], "paragraphs": pr.get("paragraphs"),
                                       "cause": "outside-range"})
            continue
        if window is not None and "inside" not in places:
            warnings.append(f"paragraph proposal {pr['n']} could not be placed inside the range")
        paragraphs.append(pr)
    for brief_warning in _brief_warnings(a, proposal):
        warnings.append(brief_warning)
        sys.stderr.write(f"brief.py: {brief_warning}\n")
    return {**result, "edits": kept, "dropped": dropped, "queries": queries,
            "dropped_queries": dropped_queries, "paragraphs": paragraphs,
            "dropped_paragraphs": dropped_paragraphs, "warnings": warnings}


_BRIEF_FILE_ID = re.compile(r"(?s)(.*\n)\nBrief id: ([0-9a-f]{12})\n")
_CARD_IN_BRIEF = re.compile(r"\n<voice_card>\n(.*?)\n</voice_card>\n", re.DOTALL)


def _brief_warnings(a: argparse.Namespace, proposal: Obj) -> list[str]:
    """What the reply shows about the brief file, as warnings. Only a work directory that holds a brief.md is
    checked. The id is the file's last line and is in no prompt: a reply that names it read to the end of the
    file, and one that does not did not. A stored voice card in the brief (the author-approved anchor) must come
    back unchanged."""
    work: Path | None = None
    if a.save:
        work = work_file(a.save).parent
    elif a.work:
        work = work_dir(a.work)
    if work is None or not (work / BRIEF_FILE).is_file():
        return []
    text = (work / BRIEF_FILE).read_text(encoding="utf-8")
    found = _BRIEF_FILE_ID.fullmatch(text)
    if found is None:
        return [f"{BRIEF_FILE} has no id line (`Brief id: <id>` as its last line), so the reply cannot be checked "
                "against it"]
    body, expected = found.group(1), found.group(2)
    out: list[str] = []
    if brief_sha(body.encode("utf-8")) != expected:
        out.append(f"{BRIEF_FILE} changed after it was built: its text no longer matches the id on its last line")
    said = proposal.get("brief_sha")
    if not isinstance(said, str) or not said.strip():
        out.append(f"the editor's reply names no brief_sha: the id is the last line of {BRIEF_FILE} "
                   f"({expected}), so the editor did not read the file to its last line")
    elif not said.strip().lower().startswith(expected):
        out.append(f"the editor's reply names brief_sha {said.strip()[:64]} but the last line of {BRIEF_FILE} "
                   f"is {expected}: it did not read this file to its last line")
    card = _CARD_IN_BRIEF.search(body)
    if card is not None and _norm_card(str(proposal.get("voice_card") or "")) != _norm_card(card.group(1)):
        out.append("the editor's voice_card is not the author-approved card the brief gave it (section 2); the "
                   "stored card is unchanged and this run's card is the editor's")
    return out


def _norm_card(text: str) -> str:
    return " ".join(text.split())


BRIEF_FILE = "brief.md"
ID_LABEL = "Brief id: "
SHA_LENGTH = 12


def brief_sha(data: bytes) -> str:
    """The first SHA_LENGTH hex digits of the sha256 of the brief file's bytes."""
    return hashlib.sha256(data).hexdigest()[:SHA_LENGTH]


WORK_SENTINEL = ".prose-edit-work"
WORK_BUILDING_PREFIX = ".prose-edit-building-"
WORK_MAX_AGE = 2 * 3600
_WORK_NAME = re.compile(r"prose-edit-[a-z0-9_]{8}")


def _is_work_dir(real: Path, base: Path) -> bool:
    return (real.parent == base and _WORK_NAME.fullmatch(real.name) is not None and real.is_dir()
            and (real / WORK_SENTINEL).is_file())


def work_dir(raw: str) -> Path:
    """The real path of a work directory made by `tmpdir`; refuse anything else.

    It must sit directly under the temp dir, carry the mkdtemp name shape and hold the sentinel
    file `tmpdir` wrote. memory.py's lock directory (prose-edit-locks-<uid>) has neither shape
    nor sentinel. A name of the right shape with no directory is one the sweep took: the message says so.
    """
    path = Path(raw)
    if ".." in path.parts:
        raise _user(f"{raw}: not a prose-edit work directory (it contains '..')")
    base = Path(tempfile.gettempdir()).resolve()
    if path.is_symlink():
        raise _user(f"{raw}: not a prose-edit work directory (it is a symbolic link)")
    real = path.resolve()
    if not _is_work_dir(real, base):
        if real.parent == base and _WORK_NAME.fullmatch(real.name) and not real.exists():
            raise _user(f"{raw}: the work directory has expired: one idle for more than two hours is swept, and "
                        "this one was swept. The review in it is gone; run the edit again from the start")
        raise _user(f"{raw}: not a prose-edit work directory made by `tmpdir` directly under {base}")
    return real


def touch_work(work: Path) -> None:
    """Mark the work directory as in use now: the sweep takes a directory idle for two hours, so a review
    the author is still reading must show activity. Best effort; a failure never stops the command."""
    try:
        os.utime(work / WORK_SENTINEL)
    except OSError:
        pass


def work_file(raw: str) -> Path:
    """A file path directly inside a work directory; anything else is refused."""
    path = Path(raw)
    if ".." in path.parts:
        raise _user(f"{raw}: not a prose-edit work directory file (it contains '..')")
    base = Path(tempfile.gettempdir()).resolve()
    parent = path.parent.resolve()
    if not _is_work_dir(parent, base):
        if parent.parent == base and _WORK_NAME.fullmatch(parent.name) and not parent.exists():
            raise _user(f"{raw}: the work directory has expired: one idle for more than two hours is swept, and "
                        "this one was swept. The review in it is gone; run the edit again from the start")
        if any(_is_work_dir(up, base) for up in parent.parents):
            raise _user(f"{raw}: must sit directly inside the work directory")
        raise _user(f"{raw}: not inside a prose-edit work directory made by `tmpdir` directly under {base}")
    if path.is_symlink() or path.is_dir():
        raise _user(f"{raw}: not a plain file")
    return parent / path.name


def remove_work(raw: str) -> None:
    shutil.rmtree(work_dir(raw))


def cmd_rmtmp(raw: str) -> None:
    remove_work(raw)


def sweep_stale_work(now: float | None = None) -> list[str]:
    """Delete work directories idle for two hours (the sentinel's mtime, which `touch_work` renews): the
    backstop for a run that stopped early, never for a review the author is still reading."""
    base = Path(tempfile.gettempdir()).resolve()
    cutoff = (time.time() if now is None else now) - WORK_MAX_AGE
    gone: list[str] = []
    for child in base.iterdir():
        try:
            if child.is_symlink():
                continue
            if _is_work_dir(child, base) and (child / WORK_SENTINEL).stat().st_mtime < cutoff:
                shutil.rmtree(child)
                gone.append(child.name)
            elif (child.name.startswith(WORK_BUILDING_PREFIX) and child.is_dir()
                  and child.stat().st_mtime < cutoff):
                shutil.rmtree(child)  # a `tmpdir` killed before it renamed its directory into place
                gone.append(child.name)
        except OSError:
            continue
    return gone


def cmd_tmpdir() -> str:
    """A fresh work directory. It is built under a name that is not a work directory's, given its
    sentinel, then renamed into place, so no `prose-edit-<8>` directory ever exists without one."""
    sweep_stale_work()
    path = Path(tempfile.mkdtemp(prefix=WORK_BUILDING_PREFIX)).resolve()
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        root = Path(proc.stdout.strip()).resolve()
        if path == root or root in path.parents:
            path.rmdir()
            raise _user(f"the temporary directory would land inside the repository at {root}; "
                        "set TMPDIR outside it")
    try:
        (path / WORK_SENTINEL).write_text("made by brief.py tmpdir\n", encoding="utf-8")
        for _ in range(8):
            final = path.with_name("prose-edit-" + secrets.token_hex(4))
            try:
                os.rename(path, final)  # an existing non-empty directory refuses; try another name
            except OSError:
                continue
            return str(final)
        raise _user(f"could not name a work directory under {path.parent}")
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brief.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("parse", help="the invocation tokens as JSON (handled before argparse)")
    q.add_argument("tokens", nargs=argparse.REMAINDER)
    b = sub.add_parser("build", help="the brief for the editor agent")
    b.add_argument("target")
    b.add_argument("--genre")
    b.add_argument("--budget", default=str(DEFAULT_BUDGET))
    b.add_argument("--file")
    b.add_argument("--work", action="store_true",
                   help="path run: make the work directory now and name it on the first line (WORK=<dir>)")
    b.add_argument("--site-page", default=str(DEFAULT_SITE_PAGE))
    f = sub.add_parser("filter", help="the agent's reply on stdin to filtered proposal JSON")
    f.add_argument("target")
    f.add_argument("--budget", default=str(DEFAULT_BUDGET))
    f.add_argument("--file")
    f.add_argument("--work", help="delete this work directory after a successful filter")
    f.add_argument("--save", help="also write the filtered JSON to this file inside a work directory")
    sub.add_parser("tmpdir", help="a fresh work directory outside the repository")
    r = sub.add_parser("rmtmp", help="delete a work directory made by tmpdir")
    r.add_argument("path")
    s = sub.add_parser("site-layer", help="the site-page section 3 layer")
    s.add_argument("--site-page", default=str(DEFAULT_SITE_PAGE))
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    try:
        if argv[:1] == ["parse"] and argv[1:2] not in (["--help"], ["-h"]):
            sys.stdout.write(json.dumps(parse_invocation(argv[1:]), ensure_ascii=False, indent=1) + "\n")
            return 0
        args = _parser().parse_args(argv)
        if args.cmd == "build":
            sys.stdout.write(cmd_build(args))
        elif args.cmd == "filter":
            if args.save and args.work:
                raise _user("--save and --work do not go together: --work deletes the directory --save writes into")
            save = work_file(args.save) if args.save else None  # a bad path stops before any work
            if save is not None:
                touch_work(save.parent)  # a filter after a long outage is the author still here
            out = cmd_filter(args, sys.stdin.read())
            if args.work:
                remove_work(args.work)  # a bad path stops here, before any output
            text_out = json.dumps(out, ensure_ascii=False, indent=1) + "\n"
            if save is not None:
                save.write_text(text_out, encoding="utf-8")
            sys.stdout.write(text_out)
        elif args.cmd == "tmpdir":
            sys.stdout.write(cmd_tmpdir() + "\n")
        elif args.cmd == "rmtmp":
            cmd_rmtmp(args.path)
        else:
            layer = site_layer(Path(args.site_page))
            sys.stdout.write(json.dumps(layer, ensure_ascii=False, indent=1) + "\n")
    except Passthrough as exc:
        sys.stderr.write(f"{exc}\n")
        if exc.code == 3:
            sys.stderr.write("brief.py: T2 is unavailable. Stop here and tell the author.\n")
        return exc.code
    except _MEM.UserError as exc:
        sys.stderr.write(f"brief.py: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
