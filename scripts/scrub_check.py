#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scrub gate: no personal home paths, tailnet hostnames or chat links in committed files (nexus-3ceda.7).

    scrub_check.py check                              # exit 0 when clean, 10 otherwise
    scrub_check.py check --rev <tip> --base <ref>     # the push gate: the tip's files, the range's
                                                      # commit messages, and no allowlist widening
    scrub_check.py update-exceptions [--allow-raise]  # rewrite the per-file ceilings to today's counts

Sam ruled that the nexus repo stays scrubbed (ask nexus-scrubbed, 2026-11-09). Every tracked text file
is scanned for:

- ``home``: a ``/Users/<name>`` or ``/home/<name>`` path (with or without a slash after the name,
  ``/mnt/c/Users/<name>`` included, ``src/home/...`` not) whose ``<name>`` is not listed as
  ``user <name>`` in the allowlist (placeholders such as ``x``, ``someone``, ``you``, and service
  accounts such as ``runner``). Write examples with a placeholder (``/Users/you/...``) or ``~``.
- ``winhome``: the Windows form, ``C:\\Users\\<name>`` (one or more backslashes), same names.
- ``tailnet``: a ``<host>.ts.net`` or ``<host>.<tailnet>.ts.net`` hostname.
- ``chat``: a claude.ai chat, share or code-session link (the ``RULES`` pattern below).
- ``session``: a ``Claude-Session:`` trailer carrying an id, any case (code and docs that name the
  trailer pass).

A bare UUID is not flagged: a real session id cannot be told from the fixture ids the tests use. A
claude.ai artifact link is not flagged: RDRs cite published artifacts on purpose.

The allowlist, ``tests/fixtures/scrub-allowlist.txt`` (under ``tests/`` so a plugin-only commit that
touches it stays eligible for the plugin-only fast lane, which refuses ``scripts/``), also holds
``<path> <count>`` ceilings for the files that carried hits when the gate was introduced (spike
results, RDR text, fixtures). The count only falls: a file over its ceiling, or a new file with a
hit, fails; a file under its ceiling fails until the commit that removed the hit lowers it with
``update-exceptions``. Raising a ceiling or adding a file needs ``--allow-raise`` and a reason in the
commit message (a test that must spell a real-looking path can build it by concatenation instead).

``.test_durations.json`` is exempt: it is regenerated from test ids and follows them.

CI runs ``check`` in the lint bucket (``tests/scripts/test_scrub_check.py``) and in the docs lane.
``scripts/git-push-develop.sh`` runs ``check --rev <tip> --base <remote branch>`` before a develop push
(``PUSH_REFUSED_SCRUB``, exit 12). With ``--base`` it also scans the commit messages of
``<base>..<tip>`` (where a ``Claude-Session:`` trailer lands) and refuses an allowlist that adds a
``user`` line, adds a file or raises a count over the base's, so a widening is a recorded
``--skip-scrub`` rather than a silent edit.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ALLOWLIST: str = "tests/fixtures/scrub-allowlist.txt"

EXIT_OK: int = 0
EXIT_FOUND: int = 10

#: rule name -> Python pattern. The user segment of ``home`` is captured so a placeholder name can be
#: told from a real one.
_NAME = r"([A-Za-z0-9._-]+)(?![A-Za-z0-9._-])"
RULES: dict[str, str] = {
    # Not after two path characters, so src/home/pages is no hit but /mnt/c/Users/<name> is.
    "home": r"(?<![A-Za-z0-9._-]{2})/(?:Users|home)/" + _NAME,
    "winhome": r"\b[A-Za-z]:\\+(?i:users)\\+" + _NAME,  # NTFS folds case
    "tailnet": r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)?\.ts\.net\b",
    "chat": r"claude\.ai/(?:(?:chat|share)/|code/session[_/])",
    "session": r"(?i)claude-session: *[0-9a-f][0-9a-f-]{7,}",
}
_USER_RULES: frozenset[str] = frozenset({"home", "winhome"})
#: A case-insensitive git grep prefilter, a superset of every rule (POSIX ERE has no lookaround);
#: hits are re-tested in Python.
_GREP = r"/(users|home)/[a-z0-9._-]|[a-z]:\\+users|\.ts\.net|claude\.ai/(chat|share|code/session)|claude-session:"
#: Never scanned: the allowlist names placeholders; the durations file is regenerated from test ids.
EXEMPT: frozenset[str] = frozenset({ALLOWLIST, ".test_durations.json"})

_HEADER = """\
# Scrub-gate allowlist (scripts/scrub_check.py, nexus-3ceda.7; Sam, ask nexus-scrubbed).
#   user <name>      a home-directory name that is not a person: a placeholder or a service account
#   <path> <count>   a file that carried hits when the gate came in; its count only falls
# Lower counts and drop files with `python3 scripts/scrub_check.py update-exceptions`; adding a file
# or raising a count needs `--allow-raise` and a reason in the commit message.
"""


def read_allowlist(path: Path) -> tuple[set[str], dict[str, int]]:
    if not path.exists():
        return set(), {}
    return parse_allowlist(path.read_text(encoding="utf-8"), str(path))


def parse_allowlist(text: str, label: str) -> tuple[set[str], dict[str, int]]:
    users: set[str] = set()
    ceilings: dict[str, int] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 2 and parts[0] == "user" and parts[1] not in users:
            users.add(parts[1])
        elif (
            len(parts) == 2
            and parts[0] != "user"
            and parts[1].isdigit()
            and parts[0] not in ceilings
        ):
            ceilings[parts[0]] = int(parts[1])
        else:
            raise SystemExit(
                f"{label}:{n}: expected one 'user <name>' or '<path> <count>' per entry, got {raw!r}"
            )
    return users, ceilings


def hits_in(text: str, users: set[str]) -> list[tuple[str, str]]:
    """Every (rule, matched text) in ``text`` that is not a placeholder home path."""
    out: list[tuple[str, str]] = []
    for rule, pattern in RULES.items():
        for m in re.finditer(pattern, text):
            # A sentence can end on the name ("/home/you."): its trailing dots are not part of it.
            if rule in _USER_RULES and (
                m.group(1) in users or m.group(1).rstrip(".") in users
            ):
                continue
            out.append((rule, m.group(0)))
    return out


def scan(
    root: Path, users: set[str], rev: str | None = None
) -> dict[str, list[tuple[int, str, str]]]:
    """Hits per tracked text file: path -> [(line number, rule, matched text)]. With ``rev``, the
    files of that commit are scanned instead of the working tree's tracked files."""
    # -z: NUL after the path and the line number, so a path holding ':' parses and is not quoted.
    # Records end in \n and nothing else: splitlines() would also split on FF, CR or U+2028 inside
    # a line and drop what follows.
    cmd = [
        "git",
        "-C",
        str(root),
        "grep",
        "-z",
        "-n",
        "-I",
        "-i",
        "-E",
        "--full-name",
        "-e",
        _GREP,
    ]
    proc = subprocess.run(
        [*cmd, *([rev] if rev else []), "--", "."], capture_output=True
    )
    if proc.returncode not in (0, 1):
        raise SystemExit(
            f"git grep failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    found: dict[str, list[tuple[int, str, str]]] = {}
    for raw in proc.stdout.decode("utf-8", errors="replace").split("\n"):
        if not raw:
            continue
        path, _, rest = raw.partition("\0")
        lineno, _, text = rest.partition("\0")
        if rev:
            path = path.removeprefix(f"{rev}:")
        if path in EXEMPT:
            continue
        for rule, match in hits_in(text, users):
            found.setdefault(path, []).append((int(lineno), rule, match))
    return found


def tracked_count(root: Path, rev: str | None = None) -> int:
    cmd = ["ls-tree", "-r", "-z", "--name-only", rev] if rev else ["ls-files", "-z"]
    out = subprocess.run(
        ["git", "-C", str(root), *cmd], check=True, capture_output=True
    ).stdout
    return sum(1 for p in out.split(b"\0") if p)


def _allowlist_at(root: Path, rev: str | None) -> tuple[set[str], dict[str, int]]:
    if not rev:
        return read_allowlist(root / ALLOWLIST)
    proc = subprocess.run(
        ["git", "-C", str(root), "show", f"{rev}:{ALLOWLIST}"], capture_output=True
    )
    if proc.returncode != 0:
        return set(), {}
    return parse_allowlist(proc.stdout.decode("utf-8"), f"{rev}:{ALLOWLIST}")


def message_hits(root: Path, base: str, rev: str, users: set[str]) -> list[str]:
    """One finding per hit in the commit messages of ``base..rev`` (no allowance: a message is new)."""
    out = subprocess.run(
        ["git", "-C", str(root), "log", "--format=%h%x00%B%x01", f"{base}..{rev}"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8", errors="replace")
    findings: list[str] = []
    for record in out.split("\x01"):
        sha, _, body = record.strip("\n").partition("\0")
        for rule, match in hits_in(body, users):
            findings.append(
                f"commit {sha} message: {rule} {match!r}; reword it (git commit --amend before pushing)"
            )
    return findings


def widening(
    old: tuple[set[str], dict[str, int]], new: tuple[set[str], dict[str, int]]
) -> list[str]:
    """What ``new`` allows that ``old`` did not: a new user name, a new file, a raised count."""
    (old_users, old_ceil), (new_users, new_ceil) = old, new
    out = [f"allowlist adds user {u}" for u in sorted(new_users - old_users)]
    for p, c in sorted(new_ceil.items()):
        if p not in old_ceil:
            out.append(f"allowlist adds {p} at {c}")
        elif c > old_ceil[p]:
            out.append(f"allowlist raises {p} {old_ceil[p]} -> {c}")
    return out


def check(root: Path, rev: str | None = None, base: str | None = None) -> list[str]:
    """Findings for the working tree's tracked files, or for commit ``rev`` (what a push sends).
    With ``base`` (the remote branch), also the range's commit messages and any allowlist widening."""
    users, ceilings = _allowlist_at(root, rev)
    errors: list[str] = []
    if base is not None:
        if rev is None:
            raise SystemExit("--base needs --rev")
        old = _allowlist_at(root, base)
        if old != (set(), {}):
            errors.extend(
                f"{w}; a widening needs the push's --skip-scrub '<reason>'"
                for w in widening(old, (users, ceilings))
            )
        errors.extend(message_hits(root, base, rev, users))
    if tracked_count(root, rev) == 0:
        errors.append("no tracked files; the scan reached nothing")
    found = scan(root, users, rev)
    counts = Counter({p: len(h) for p, h in found.items()})
    for p, hits in sorted(found.items()):
        c = ceilings.get(p)
        if c is None or len(hits) > c:
            shown = "; ".join(f"line {n}: {rule} {m!r}" for n, rule, m in hits[:5])
            more = f" (+{len(hits) - 5} more)" if len(hits) > 5 else ""
            limit = "no allowance" if c is None else f"its ceiling of {c}"
            errors.append(
                f"{p}: {len(hits)} scrub hits, over {limit}: {shown}{more}. Use a placeholder "
                "(/Users/you/..., ~, <host>) instead; a legitimate one: "
                "python3 scripts/scrub_check.py update-exceptions --allow-raise, with the reason "
                "in the commit message"
            )
    for p, c in sorted(ceilings.items()):
        if counts[p] < c:
            errors.append(
                f"{p}: {counts[p]} scrub hits, below its ceiling of {c}; lower it in this commit: "
                "python3 scripts/scrub_check.py update-exceptions"
            )
    return errors


def update_exceptions(root: Path, allow_raise: bool) -> list[str]:
    users, old = read_allowlist(root / ALLOWLIST)
    counts = {p: len(h) for p, h in scan(root, users).items()}
    refusals: list[str] = []
    if not allow_raise:
        for p, n in sorted(counts.items()):
            if p not in old:
                refusals.append(f"{p}: new entry at {n} hits")
            elif n > old[p]:
                refusals.append(f"{p}: raise {old[p]} -> {n}")
    if refusals:
        return refusals
    body = "".join(f"user {u}\n" for u in sorted(users))
    body += "".join(f"{p} {n}\n" for p, n in sorted(counts.items()))
    (root / ALLOWLIST).write_text(_HEADER + body, encoding="utf-8", newline="\n")
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("check")
    ck.add_argument(
        "--rev",
        help="scan this commit's files, not the working tree (the push gate uses it)",
    )
    ck.add_argument(
        "--base",
        help="with --rev: also scan the commit messages of BASE..REV and refuse allowlist widening",
    )
    up = sub.add_parser("update-exceptions")
    up.add_argument("--allow-raise", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "update-exceptions":
        refusals = update_exceptions(args.root, args.allow_raise)
        for r in refusals:
            sys.stderr.write(f"refused: {r}\n")
        if refusals:
            sys.stderr.write(
                "ceilings only fall; pass --allow-raise for a legitimate addition\n"
            )
            return EXIT_FOUND
        sys.stdout.write(f"wrote {ALLOWLIST}\n")
        return EXIT_OK
    for ref in (args.rev, args.base):
        if (
            ref
            and subprocess.run(
                [
                    "git",
                    "-C",
                    str(args.root),
                    "rev-parse",
                    "-q",
                    "--verify",
                    f"{ref}^{{commit}}",
                ],
                capture_output=True,
            ).returncode
        ):
            sys.stderr.write(f"error: {ref!r} is not a commit\n")
            return EXIT_FOUND
    errors = check(args.root, args.rev, args.base)
    for e in errors:
        sys.stderr.write(f"error: {e}\n")
    sys.stdout.write(f"scrub_check: {len(errors)} errors\n")
    return EXIT_FOUND if errors else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
