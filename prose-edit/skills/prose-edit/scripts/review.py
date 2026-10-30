#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review loop of the prose-edit skill (RDR-221 Steps 1.1 and 1.5): session, MarkupEditor translation, accept, apply.

The skill hands every mechanical job to this script so the model's instructions carry none of
it. Stdlib only; T2 is reached only by running memory.py beside it (through brief.py's child
runner), and the project prefix override (PROSE_EDIT_PROJECT_PREFIX) passes through. Errors go
to stderr with exit 1; memory.py's exit code 3 (T2 unavailable) passes through.

  review.py render TARGET --work WORK [--file F] [--genre G]
  review.py apply --work WORK [--accept SPEC] [--hold SPEC] [--reject SPEC] [--reasons-file F] [--reason N=TEXT]... [--dry-run]
  review.py log-retry --work WORK
  review.py translate --work WORK --blocks-file F
  review.py proposed --work WORK --from-file F
  review.py decisions --work WORK [--from-file F] [--accept SPEC] [--reject SPEC] [--hold SPEC]

The MarkupEditor review (nexus-scmcs.2): the author decides in the app's suggestions and never saves there, so
`apply` stays the only writer of the document, the only store of rejections and the only session logger. `translate`
reads get_document's blocks (F, written inside WORK) and writes WORK/propose.json, the payload of one propose_edits
call plus the refused edits (cause: not-found, ambiguous, formatting, code-block, metadata, spans-lines, no-change);
it prints {edits, sent, refused}. `proposed` reads propose_edits' reply (F), checks it holds one result per edit
sent, and writes WORK/suggestions.json (edit number, suggestion id, find, replace) and adds the app's refusals
(cause app-refused) to the refused list. `decisions` reads list_suggestions' reply (F), maps THIS run's ids
(accepted -> accept, rejected -> reject, pending, stale or missing -> hold; ids of other documents are ignored),
merges the author's chat decisions for the leftovers (edits the script or the app refused, the author's own
answers) and prints the flags for `apply`: {accept, reject, hold, apply_args, app, chat, leftover, undecided,
warnings}. None of the three writes the document or T2. Every file named is a file directly inside WORK
(the BLOCKS=, PROPOSED= and STATUSES= header paths).

A cut-only session (RDR-221 Step 3.1, nexus-ger02.10; the prose-cut skill) is a model's run, with no author to read
the copy: its filtered.json carries "mode": "cut-only" or its work directory holds a cut-only run's brief or card
(brief.py cut_only_work), `render` opens no viewer and records `cut_only` in session.json, and from then on an html page also protects its nav, heading, button and samp
elements (a normal html run does not) and `apply` refuses (exit 1, nothing written) an accepted edit that is not a cut
(it adds, replaces or reorders a word, or adds markup), which a hand-edited filtered.json could hold. The session log
then carries `cut_only`, `word_budget` and `words` {document, budget, removed (the applied cuts' words), after,
proposed (the filter's count)}.

`render` and `apply` take the filtered proposal that `brief.py filter TARGET --save WORK/filtered.json` wrote:
the machine format brief.py already emits. No second format exists. TARGET is PATH,
PATH:START-END or "-" (a stdin run; --file names the saved text). WORK is a directory made by
`brief.py tmpdir` (or `build --work`); every path this script reads or writes is inside it
except the document itself and the temp file described under `apply`.

`render` sets the session up and writes nothing the author reads: it places each edit against the file and writes
WORK/session.json (the target, the genre, whether it is a stdin run and the edits that cannot be placed), which
`apply`, `answer`, `translate` and `decisions` need. It prints {genre, counts, unplaced, dropped, dropped_queries,
dropped_paragraphs, warnings}. The review itself is in the MarkupEditor app for a path run (`translate`, `proposed`
and `decisions`, below) and in the chat for a stdin run.

An edit that cannot be placed (its old string is absent, occurs more than once, or overlaps another edit) is
listed with its cause. One whose old string is absent or occurs more than once is skipped if accepted; two that
overlap are both skipped when both are accepted, and either is applied when accepted alone. An edit the review
could not show cannot be decided there, so it is never stored as a rejection. For an .html or .htm target an edit
is matched against the raw source, entities and all.

`apply` takes the author's answer. --accept (default "none") is the numbers to apply, --reject the numbers to store
as rejections for the document, and --hold the numbers left undecided: a comma or space separated
list of numbers and ranges ("1, 3-5"), or "all" or "none"; --reject also takes "rest", which is
every edit not accepted, held or named otherwise (the author's "reject the rest"). An edit the
author does not name is HELD: neither applied nor stored. Only --reject stores a rejection, so
silence, or "accept 1", never does. An edit the review could not show is left alone by "rest"
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
     stored, "apply did not change the file, and the rejections already stored are kept; run apply
     again" (not "the file was not changed": the author's own save may have changed it). The skill
     keys on the lower-case phrase `run apply again`.
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
  6. WORK is deleted, even when nothing was accepted. A run that stops
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
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

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


def _load_suggest() -> Any:
    spec = importlib.util.spec_from_file_location("prose_edit_suggest_lib", HERE / "suggest.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {HERE / 'suggest.py'}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


_BRIEF = _load_brief()
_SUGGEST = _load_suggest()
UserError: type[Exception] = _BRIEF.UserError
Passthrough: type[Exception] = _BRIEF.Passthrough


def _user(message: str) -> Exception:
    return UserError(message)


RETRY = "run apply again"


def _aborted(exc: BaseException, *, stored: bool) -> BaseException:
    """`exc` with an ending that says what is true of the file and to run apply again, same type and exit code.

    Called for a failure after the plan was made and before the file was written. Nothing is claimed that
    a stored rejection would make false: with rejections already stored apply did not change the file but the
    store is kept. A message that already says the retry words and that nothing was changed is left as it is.
    """
    if isinstance(exc, OSError):
        exc = _user(f"{exc}")
    if not isinstance(exc, (Passthrough, UserError)):
        return exc
    msg = str(exc).rstrip()
    low = msg.lower()
    state: str | None = None
    if stored:
        # "the file was not changed" would be false when the author's own save changed it: say what apply did
        state = "apply did not change the file, and the rejections already stored are kept"
    elif "nothing was written" not in low and "nothing was changed" not in low:
        state = "nothing was written"
    if RETRY in msg:  # the message already ends with the retry words: the state goes before it, not after
        parts = ([state] if state else []) + [msg.rstrip(".")]
    else:
        parts = [msg.rstrip("."), *([state] if state else []), RETRY]
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


def plan_edits(text: str, edits: list[Obj], rng: Obj | None, html: bool, cut_only: bool = False) -> list[Obj]:
    """Place each edit against `text`; one plan per edit, in the order given.

    A plan is {"n", "old", "new", "span", "cause", "detail"}. `span` is (start, end) when the
    edit can be applied, else None with a `cause`. Overlap is judged among the edits given,
    so the caller decides which edits count (all of them to place, the accepted ones to apply). `cut_only` (a
    cut-only session) protects the nav, heading, button and samp elements of an html page too.
    """
    window = cast("Span | None", _BRIEF._range_span(text, rng))
    spans = cast("list[Span]", _BRIEF.protected_spans(text, html=html, cut_only=cut_only))
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


change_core = _SUGGEST.change_core  # one copy, in suggest.py


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
                          capture_output=True, text=True, encoding="utf-8")
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
    # a work directory that holds a cut-only run is one whatever its proposal says: the proposal can be rewritten
    cut_only = prop.get("mode") == "cut-only" or bool(_BRIEF.cut_only_work(work))
    plans = plan_edits(text, cast("list[Obj]", prop.get("edits") or []), rng, html, cut_only)
    unplaced = [{"n": p["n"], "cause": p["cause"], "detail": p["detail"]} for p in plans if p["span"] is None]
    session: Obj = {"target": a.target, "genre": genre, "stdin": stdin, "file": a.file, "unplaced": unplaced}
    if cut_only:
        session["cut_only"] = True
    (work / "session.json").write_text(json.dumps(session), encoding="utf-8")
    return {
        "genre": genre,
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
    # No flag name in this text: the skill keys on `--accept` and the others to mean "ask the author for numbers
    # again", and on `dry run` to mean "run the dry run", so a refusal about a missing dry run must hold only the latter.
    run_it = ("Run `review.py apply --work WORK <the answer's flags> --dry-run` for the answer, show its output to the "
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
        n = int(key)  # "2" and "02" are the same edit: two keys for it are two reasons, not one that wins
        if n in out:
            raise _user(f"--reasons-file {raw!r}: two reasons for edit {n} (more than one key names that number)")
        out[n] = text.strip()
    return out


_ANSWERS = re.compile(r"[QP][0-9]+")


def _load_answers(raw: str, work: Path) -> list[Obj]:
    """The author's answers as edits: a JSON list of {"answers": "Q4" or "P1", "old": ..., "new": ...} inside WORK."""
    try:
        path = cast(Path, _BRIEF.work_file(raw))
    except UserError as exc:
        raise _user(f"--from-file {raw!r}: {exc}") from exc
    if path.parent != work:
        raise _user(f"--from-file {raw!r}: must sit directly inside this work directory")
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _user(f"--from-file {raw!r}: cannot read it as JSON ({exc})") from exc
    if not isinstance(value, list) or not value:
        raise _user(f'--from-file {raw!r}: expected a JSON list like [{{"answers": "Q4", "old": "...", "new": "..."}}]')
    out: list[Obj] = []
    for i, item in enumerate(cast("list[Any]", value), 1):
        if not isinstance(item, dict) or set(cast(Obj, item)) != {"answers", "old", "new"}:
            raise _user(f"--from-file {raw!r}: entry {i} needs exactly the keys answers, old and new")
        e = cast(Obj, item)
        if not isinstance(e["answers"], str) or not _ANSWERS.fullmatch(e["answers"]):
            raise _user(f"--from-file {raw!r}: entry {i}: answers must name a query or paragraph proposal, like Q4 or P1")
        if not isinstance(e["old"], str) or not e["old"] or not isinstance(e["new"], str) or e["old"] == e["new"]:
            raise _user(f"--from-file {raw!r}: entry {i}: old must be non-empty text from the document and new must differ")
        out.append(e)
    return out


def cmd_answer(a: argparse.Namespace) -> Obj:
    """Add the author's answers to queries and paragraph proposals as numbered edits (nexus-ger02.21).

    They are the author's own changes, so they go through the same dry run and apply as the editor's
    edits: the document changes only through apply. They are marked `by: author`, and apply never stores one
    as a rejection. Nothing is written when any answer cannot be placed.
    """
    work = cast(Path, _BRIEF.work_dir(a.work))
    _BRIEF.touch_work(work)
    sess_file = work / "session.json"
    if not sess_file.is_file():
        raise _user("no session in the work directory: run `review.py render` first")
    session = _load_json(sess_file, "WORK/session.json")
    prop = _load_json(work / "filtered.json", "WORK/filtered.json")
    answers = _load_answers(a.from_file, work)
    named = {f"Q{q['n']}" for q in cast("list[Obj]", prop.get("queries") or [])} | {
        f"P{pr['n']}" for pr in cast("list[Obj]", prop.get("paragraphs") or [])}
    _, rng, source, html, _ = _source_of(str(session["target"]), cast("str | None", session.get("file")), work)
    text, _eol = read_source(source)
    spans = cast("list[Span]", _BRIEF.protected_spans(text, html=html, cut_only=bool(session.get("cut_only"))))
    window = cast("Span | None", _BRIEF._range_span(text, rng))
    edits = cast("list[Obj]", prop.get("edits") or [])
    problems: list[str] = []
    # an earlier answer's span counts too: two edits over the same text are both skipped at apply
    placed: list[tuple[str, Span]] = []
    for e in edits:
        if e.get("by") == "author":
            hits = [i for i in _occurrences(text, str(e["old"])) if _within(window, i, i + len(str(e["old"])))]
            if len(hits) == 1:
                placed.append((f"edit {e['n']} ({e.get('answers')})", (hits[0], hits[0] + len(str(e["old"])))))
    for i, e in enumerate(answers, 1):
        if e["answers"] not in named:
            problems.append(f"entry {i}: {e['answers']} is not a query or paragraph proposal of this review")
            continue
        cause = cast("str | None", _BRIEF.edit_problem(text, e["old"], spans, window))
        hits = [j for j in _occurrences(text, e["old"]) if _within(window, j, j + len(e["old"]))]
        if cause is None and len(hits) > 1:
            cause = f"ambiguous ({len(hits)} occurrences): quote more of the text"
        if cause:
            problems.append(f"entry {i} ({e['answers']}): old text {cause}")
            continue
        span = (hits[0], hits[0] + len(e["old"]))
        clash = next((who for who, (lo, hi) in placed if lo < span[1] and span[0] < hi), None)
        if clash:
            problems.append(f"entry {i} ({e['answers']}): old text overlaps {clash}; hold or replace that one")
            continue
        placed.append((f"entry {i} ({e['answers']})", span))
    if problems:
        raise _user("nothing was added: " + "; ".join(problems))
    taken = [int(x["n"]) for key in ("edits", "dropped") for x in cast("list[Obj]", prop.get(key) or [])
             if isinstance(x.get("n"), int)]
    n = max(taken, default=0)
    added: list[Obj] = []
    for e in answers:
        n += 1
        added.append({"n": n, "old": e["old"], "new": e["new"], "reason": f"the author's answer to {e['answers']}",
                      "by": "author", "answers": e["answers"]})
    prop["edits"] = edits + added
    before = _sha(read_bytes(work / "filtered.json"))
    (work / "filtered.json").write_text(json.dumps(prop, indent=1, ensure_ascii=False), encoding="utf-8")
    sent = work / "propose.json"
    if sent.is_file():  # an answer only appends, so the numbers translate read still mean the same edits
        payload = _load_json(sent, "WORK/propose.json")
        if payload.get("filtered_sha") == before:
            payload["filtered_sha"] = _sha(read_bytes(work / "filtered.json"))
            _write_json(sent, payload)
    return {"added": [{"n": x["n"], "answers": x["answers"], "old": x["old"], "new": x["new"]} for x in added]}


def _work_input(raw: str, work: Path, flag: str) -> Path:
    """A file the skill wrote directly inside WORK (one of the header paths), or a refusal."""
    try:
        path = cast(Path, _BRIEF.work_file(raw))
    except UserError as exc:
        raise _user(f"{flag} {raw!r}: {exc}") from exc
    if path.parent != work:
        raise _user(f"{flag} {raw!r}: must sit directly inside this work directory")
    return path


def _read_json_value(path: Path, flag: str, raw: str, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _user(f"{flag} {raw!r}: {what}: cannot read it as JSON ({exc})") from exc


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def _same_file(url: str, expected: Path) -> bool:
    """True when get_document's `url` names `expected`: compared by realpath (macOS reports /private/tmp for /tmp)."""
    if url.startswith("file://"):
        url = unquote(urlparse(url).path)
    return os.path.realpath(url) == os.path.realpath(expected)


def _load_blocks(raw: str, work: Path, target: Path) -> list[Obj]:
    """The blocks of get_document's reply: one reply, or the pages of a long document one after another.

    The blocks must be the whole of THIS run's document, unchanged: `translate` claims an old text occurs exactly
    once in them. So the read is refused when a page says the buffer has unsaved changes, when `url` is not `target`
    (open_document can report "Opened" while the previous document is still shown), when the last page still has
    `next` (the reply was cut), or when the block indexes are not contiguous from 0 or `firstEditable`.
    """
    path = _work_input(raw, work, "--blocks-file")
    value = _read_json_value(path, "--blocks-file", raw, "the blocks")
    pages = value if isinstance(value, list) and value and all(isinstance(v, dict) and "blocks" in v for v in value) \
        else [value]
    blocks: list[Obj] = []
    for page in pages:
        if not isinstance(page, dict):
            raise _user("--blocks-file: expected get_document's reply, with a `blocks` list, `url` and `hasChanges`")
        if page.get("view") == "source":
            raise _user("--blocks-file: the app is in its source view, which has no blocks; "
                        "ask the author to switch back to the document view, then read the document again")
        items = page.get("blocks")
        if not isinstance(items, list):
            raise _user("--blocks-file: expected get_document's reply, with a `blocks` list")
        if page.get("hasChanges") is True:
            raise _user("--blocks-file: the app's buffer has unsaved changes (hasChanges is true), so these blocks are "
                        "not the file's text; undo the changes or reopen the file, then read the document again")
        url = page.get("url")
        if not isinstance(url, str) or not url:
            raise _user("--blocks-file: the reply names no `url`, so it cannot be checked against this run's file; "
                        "write get_document's reply as it came back")
        if not _same_file(url, target):
            raise _user(f"--blocks-file: the app shows {url}, not this run's file {target} (url); open the file, wait "
                        "until app_state's url is the file's and modalWindow is empty, then read the document again")
        blocks.extend(cast("list[Obj]", items))
    if not blocks:
        raise _user("--blocks-file: the reply holds no blocks")
    if pages[-1].get("next") is not None:
        raise _user(f"--blocks-file: the last page still has `next` ({pages[-1]['next']}): the reply was cut short, so "
                    "blocks are missing; read on from `next` and put every page in the file")
    seen: set[int] = set()
    for b in blocks:
        if (not isinstance(b, dict) or not isinstance(b.get("index"), int) or not isinstance(b.get("hash"), str)
                or not isinstance(b.get("markdown"), str)):
            raise _user("--blocks-file: every block needs an integer index, a hash and its markdown")
        if b["index"] in seen:
            raise _user(f"--blocks-file: block {b['index']} appears twice (pages that overlap)")
        seen.add(b["index"])
    first = pages[0].get("firstEditable")
    starts = {0, first} if isinstance(first, int) else {0}
    indexes = [int(b["index"]) for b in blocks]
    if indexes[0] not in starts or any(y != x + 1 for x, y in zip(indexes, indexes[1:])):
        raise _user(f"--blocks-file: the block indexes are not contiguous from {' or '.join(str(x) for x in sorted(starts))} "
                    f"(they run {indexes[:6]}{'...' if len(indexes) > 6 else ''}): blocks are missing or out of order; "
                    "read the whole document and put every page in the file in order")
    return blocks


_PROPOSE_KEYS = ("block", "expected", "find", "replace", "note")


def cmd_translate(a: argparse.Namespace) -> Obj:
    """The placed edits as one propose_edits payload (WORK/propose.json); the edits the app could not show wait in the chat.

    Nothing is written but WORK/propose.json, and a suggestions.json from an earlier translate is dropped (its ids
    named the earlier payload). The edits `render` could not place, and the author's own answers, are not sent.
    propose.json records the sha256 of filtered.json as read here (`filtered_sha`): `decisions` refuses when the
    proposal has changed since, because the edit numbers in suggestions.json would name other edits.
    """
    work = cast(Path, _BRIEF.work_dir(a.work))
    _BRIEF.touch_work(work)
    sess_file = work / "session.json"
    if not sess_file.is_file():
        raise _user("no session in the work directory: run `review.py render` first")
    session = _load_json(sess_file, "WORK/session.json")
    prop = _load_json(work / "filtered.json", "WORK/filtered.json")
    _, _, target, _, _ = _source_of(str(session["target"]), cast("str | None", session.get("file")), work)
    blocks = _load_blocks(a.blocks_file, work, target)
    unplaced = {int(u["n"]) for u in cast("list[Obj]", session.get("unplaced") or [])}
    edits = [e for e in cast("list[Obj]", prop.get("edits") or [])
             if e.get("by") != "author" and int(e["n"]) not in unplaced]
    proposals, refused = _SUGGEST.translate_edits(blocks, edits)
    payload: Obj = {"edits": [{k: p[k] for k in _PROPOSE_KEYS} for p in proposals],
                    "order": [int(p["n"]) for p in proposals], "refused": refused,
                    "filtered_sha": _sha(read_bytes(work / "filtered.json"))}
    _write_json(work / "propose.json", payload)
    (work / "suggestions.json").unlink(missing_ok=True)
    return {"edits": payload["edits"], "sent": len(proposals), "refused": refused}


def cmd_proposed(a: argparse.Namespace) -> Obj:
    """Record the app's reply to propose_edits: the suggestion id of each edit sent (WORK/suggestions.json) and the app's refusals.

    The reply must hold exactly one result for each edit sent, indexes 0..k-1 once each, each proposed result naming
    the block it was sent for and its own id; anything else stops the run with nothing written. An edit the app refused joins the refused list of WORK/propose.json (cause
    app-refused, the app's reason as detail) and goes to the chat like a local refusal.
    """
    work = cast(Path, _BRIEF.work_dir(a.work))
    _BRIEF.touch_work(work)
    propose = _load_json(work / "propose.json", "WORK/propose.json (run `review.py translate` first)")
    path = _work_input(a.from_file, work, "--from-file")
    reply = _read_json_value(path, "--from-file", a.from_file, "the propose_edits reply")
    sent = cast("list[Obj]", propose["edits"])
    order = [int(n) for n in cast("list[Any]", propose["order"])]
    results = reply.get("results") if isinstance(reply, dict) else None

    def bad(why: str) -> Exception:
        return _user(f"propose_edits reply: {why}; nothing was written. Write the app's reply as it came back, "
                     "or run `review.py translate` again")

    if not isinstance(results, list):
        raise bad("no `results` list" + (f" (the app said: {reply.get('error') or reply.get('reason')})"
                                         if isinstance(reply, dict) and (reply.get("error") or reply.get("reason")) else ""))
    if len(results) != len(sent):
        raise bad(f"it holds {len(results)} results for {len(sent)} edits sent")
    by_index: dict[int, Obj] = {}
    for r in cast("list[Any]", results):
        if not isinstance(r, dict) or not isinstance(r.get("index"), int) or r["index"] in by_index:
            raise bad("every result needs its own integer `index`")
        by_index[r["index"]] = cast(Obj, r)
    if set(by_index) != set(range(len(sent))):
        raise bad(f"the result indexes are not 0..{len(sent) - 1}")
    suggestions: list[Obj] = []
    app_refused: list[Obj] = []
    ids: set[str] = set()
    for i in range(len(sent)):
        r = by_index[i]
        if r.get("status") == "proposed":
            if not isinstance(r.get("id"), str) or not r["id"]:
                raise bad(f"result {i} is proposed but has no id")
            if r.get("block") != sent[i]["block"]:
                raise bad(f"result {i} is for block {r.get('block')!r} but edit {order[i]} was sent for block "
                          f"{sent[i]['block']}; this reply is not for these edits")
            if r["id"] in ids:
                raise bad(f"two results share the id {r['id']}")
            ids.add(r["id"])
            suggestions.append({"n": order[i], "id": r["id"], "find": sent[i]["find"], "replace": sent[i]["replace"]})
        elif r.get("status") == "refused":
            app_refused.append({"n": order[i], "cause": "app-refused", "detail": str(r.get("reason") or "no reason given")})
        else:
            raise bad(f"result {i} has status {r.get('status')!r}")
    kept = [x for x in cast("list[Obj]", propose.get("refused") or []) if x.get("cause") != "app-refused"]
    propose["refused"] = kept + app_refused
    _write_json(work / "suggestions.json", suggestions)
    _write_json(work / "propose.json", propose)
    return {"suggested": len(suggestions), "refused": app_refused}


def _both(flags: list[tuple[str, set[int]]]) -> None:
    """An edit can be named by one of --accept, --hold and --reject only."""
    for i, (first, a) in enumerate(flags):
        for second, b in flags[i + 1:]:
            if a & b:
                raise _user(f"{first} and {second} both name edit {min(a & b)}")


def _spec(numbers: set[int]) -> str:
    return ",".join(str(n) for n in sorted(numbers)) or "none"


def cmd_decisions(a: argparse.Namespace) -> Obj:
    """The author's decisions in the app, plus any made in the chat for the leftovers, as the flags `apply` takes.

    list_suggestions' reply (--from-file) is mapped for THIS run's suggestion ids only: accepted -> accept,
    rejected -> reject, pending, stale or missing -> hold. --accept/--reject/--hold name the edits the app never
    held (refused by the script or by the app, the author's own answers): `reject` also takes `rest`. An edit the
    app held cannot be decided in the chat. Nothing is written: `apply` stays the only writer of the document, the
    only store of rejections and the only session logger.
    """
    work = cast(Path, _BRIEF.work_dir(a.work))
    _BRIEF.touch_work(work)
    sess_file = work / "session.json"
    if not sess_file.is_file():
        raise _user("no session in the work directory: run `review.py render` first")
    session = _load_json(sess_file, "WORK/session.json")
    prop = _load_json(work / "filtered.json", "WORK/filtered.json")
    edits = cast("list[Obj]", prop.get("edits") or [])
    if (work / "propose.json").is_file():
        propose = _load_json(work / "propose.json", "WORK/propose.json (run `review.py translate` first)")
        if propose.get("filtered_sha") != _sha(read_bytes(work / "filtered.json")):
            raise _user("WORK/filtered.json changed since translate, so the edit numbers in the app's suggestions may "
                        "no longer be these edits; run `review.py translate` again and ask the author to decide again")
    else:
        # Step 10 skips translate when the proposal has no `edits`, and the answer route may then add the run's only
        # edits (the author's own): nothing was sent to the app, so there is nothing to map. An editor edit that is
        # placed and was never translated is a skipped step, not that case.
        shown = {int(u["n"]) for u in cast("list[Obj]", session.get("unplaced") or [])}
        if any(e.get("by") != "author" and int(e["n"]) not in shown for e in edits):
            raise _user("WORK/propose.json is missing (run `review.py translate` first): the editor's placed edits "
                        "have not been sent to the app")
        propose = {"order": [], "refused": []}
    valid = {int(e["n"]) for e in edits}
    mapped: Obj = {"accept": [], "reject": [], "hold": [], "warnings": []}
    held_by_app: set[int] = set()
    if propose.get("order"):
        sugg_file = work / "suggestions.json"
        if not sugg_file.is_file():
            raise _user("the app's reply to propose_edits is not recorded: run `review.py proposed` first")
        if not a.from_file:
            raise _user("--from-file is required: write list_suggestions' reply to the STATUSES= path")
        path = _work_input(a.from_file, work, "--from-file")
        reply = _read_json_value(path, "--from-file", a.from_file, "the list_suggestions reply")
        listed = reply.get("suggestions") if isinstance(reply, dict) else reply
        if not isinstance(listed, list):
            raise _user("--from-file: expected list_suggestions' reply, with a `suggestions` list")
        sent = cast("list[Obj]", json.loads(sugg_file.read_text(encoding="utf-8")))
        mapped = _SUGGEST.map_statuses(sent, cast("list[Obj]", listed))
        held_by_app = {int(s["n"]) for s in sent}
    leftover_n = valid - held_by_app
    unshown = {int(u["n"]) for u in cast("list[Obj]", session.get("unplaced") or [])}
    mine = {int(e["n"]) for e in edits if e.get("by") == "author"}
    named = {"--accept": a.accept, "--hold": a.hold, "--reject": a.reject}
    for flag, spec in named.items():
        for token in re.findall(r"[0-9]+", spec or ""):
            if int(token) in held_by_app:
                raise _user(f"{flag}: edit {token} is with the app; its decision is the one made there")
    c_accept = parse_numbers(a.accept, leftover_n, "--accept") if a.accept else set()
    c_hold = parse_numbers(a.hold, leftover_n, "--hold") if a.hold else set()
    rest = leftover_n - unshown - c_accept - c_hold - mine
    c_reject = parse_numbers(a.reject, leftover_n, "--reject", rest=rest) if a.reject else set()
    _both([("--accept", c_accept), ("--hold", c_hold), ("--reject", c_reject)])
    accept = set(mapped["accept"]) | c_accept
    reject = set(mapped["reject"]) | c_reject
    hold = set(mapped["hold"]) | c_hold
    args: list[str] = []
    for flag, group in (("--accept", accept), ("--reject", reject), ("--hold", hold)):
        if group:
            args += [flag, _spec(group)]
    refused = cast("list[Obj]", propose.get("refused") or [])
    return {
        "accept": sorted(accept), "reject": sorted(reject), "hold": sorted(hold), "apply_args": args,
        "app": {k: mapped[k] for k in ("accept", "reject", "hold")},
        "chat": {"accept": sorted(c_accept), "reject": sorted(c_reject), "hold": sorted(c_hold)},
        "leftover": [r for r in refused if int(r["n"]) in leftover_n],
        "undecided": sorted(leftover_n - unshown - accept - reject - hold),
        "warnings": list(mapped["warnings"]),
    }


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
    rel, rng, source, html, root = _source_of(str(session["target"]), cast("str | None", session.get("file")), work)
    cut_only = bool(session.get("cut_only"))
    if cut_only:
        adds = sorted(n for n in accept if not _BRIEF.is_cut(str(by_n[n]["old"]), str(by_n[n]["new"]), html))
        if adds:
            raise _user(f"--accept: edit {adds[0]} is not a cut (it adds, replaces or reorders a word, or adds "
                        "markup); a cut-only session applies edits that remove words only")
    hold: set[int] = parse_numbers(a.hold, valid, "--hold") if a.hold else set()
    # an edit the review could not show: `reject the rest` leaves it alone, naming it rejects it
    unshown = {int(u["n"]): u for u in cast("list[Obj]", session.get("unplaced") or [])}
    mine = {n for n, e in by_n.items() if e.get("by") == "author"}  # the author's own answers: never a rejection
    rest = valid - set(unshown) - accept - hold - mine
    reject: set[int] = parse_numbers(a.reject, valid, "--reject", rest=rest) if a.reject else set()
    if reject & mine:
        raise _user(f"--reject: edit {min(reject & mine)} is the author's own answer, not the editor's; "
                    "hold it instead (nothing is stored for it)")
    _both([("--accept", accept), ("--hold", hold), ("--reject", reject)])
    reasons = parse_reasons(list(a.reason or []), reject, load_reasons_file(a.reasons_file, work))
    stdin = bool(session["stdin"])
    target = str(session["target"])
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
    plans = plan_edits(text, [e for e in edits if int(e["n"]) in accept], rng, html, cut_only)
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
            _BRIEF.memory_json(["status"])  # nothing to store: ask T2 anyway, so a service that is down stops us before the write
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
    # the author's own answers are not the editor's work: kept apart so the editor's accept rate counts only its edits
    log_payload: Obj = {
        "stdin": stdin, "edits": [e for e in edits if int(e["n"]) not in mine],
        "author_edits": [e for e in edits if int(e["n"]) in mine],
        "author_accepted": sorted(accept & mine), "paragraphs": prop.get("paragraphs") or [],
        "queries": prop.get("queries") or [], "dropped": prop.get("dropped") or [],
        "note": prop.get("note"), "voice_card": prop.get("voice_card"),
        "accepted": sorted(accept - mine), "applied": [] if stdin else applied_n, "skipped": [
            {"n": s["n"], "cause": s["cause"], "detail": s["detail"]} for s in skipped],
        "rejected": rejected, "held": sorted(set(held) - mine), "reasons": {str(n): reasons[n] for n in sorted(reasons)},
        "unplaced": [{"n": u["n"], "cause": u["cause"]} for u in left_alone],
    }
    if cut_only:
        # the filter's `removed` is what the editor proposed; the log keeps what the applied cuts took out
        words = dict(cast("Obj", prop.get("words") or {}))
        removed = sum(max(0, len(_BRIEF._words(str(p["old"]))) - len(_BRIEF._words(str(p["new"])))) for p in placed)
        if "document" in words:
            # no `after`: the gate edits the page before it decides, so `document` minus the applied cuts is not
            # the page's final count; `brief.py count` on the page is
            words.pop("after", None)
            words.update(proposed=words.get("removed"), removed=removed, counts="applied sentence edits only")
        log_payload.update(cut_only=True, word_budget=prop.get("word_budget"), words=words or None)
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
    r = sub.add_parser("render", help="set the session up")
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
    an = sub.add_parser("answer", help="add the author's answers to queries and paragraph proposals as edits")
    an.add_argument("--work", required=True)
    an.add_argument("--from-file", required=True, metavar="FILE",
                    help='a JSON list [{"answers": "Q4", "old": "...", "new": "..."}] inside WORK (written with Write)')
    tr = sub.add_parser("translate", help="turn the placed edits into one propose_edits payload")
    tr.add_argument("--work", required=True)
    tr.add_argument("--blocks-file", required=True, metavar="FILE",
                    help="get_document's reply, written with Write to the BLOCKS= path")
    pr = sub.add_parser("proposed", help="record the app's reply to propose_edits")
    pr.add_argument("--work", required=True)
    pr.add_argument("--from-file", required=True, metavar="FILE",
                    help="propose_edits' reply, written with Write to the PROPOSED= path")
    de = sub.add_parser("decisions", help="the app's decisions (and the chat's, for the leftovers) as apply's flags")
    de.add_argument("--work", required=True)
    de.add_argument("--from-file", metavar="FILE",
                    help="list_suggestions' reply, written with Write to the STATUSES= path (needed when edits went to the app)")
    de.add_argument("--accept", help="leftover edits the author accepted in the chat")
    de.add_argument("--hold", help="leftover edits the author left undecided in the chat")
    de.add_argument("--reject", help="leftover edits the author rejected in the chat, or `rest`")
    lr = sub.add_parser("log-retry", help="send the session log of an apply whose log failed, then delete WORK")
    lr.add_argument("--work", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        commands = {"render": cmd_render, "apply": cmd_apply, "answer": cmd_answer, "log-retry": cmd_log_retry,
                    "translate": cmd_translate, "proposed": cmd_proposed,
                    "decisions": cmd_decisions}
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
