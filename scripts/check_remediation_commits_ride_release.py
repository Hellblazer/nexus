#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Release gate: do the commits a remediation bead is sequenced behind actually
ride the release being cut? (nexus-fix9t)

Root cause this closes: nexus-3n7pr's remediation plan was explicitly
sequenced "after the client release ships" -- the assumption being that
whatever fix the remediation depended on would be an ancestor of that
release by the time it shipped. 7.7.0 shipped; the manifest_backfill
safety fixes (nexus-gvmbo / nexus-b91tv) landed on develop at commit
5f59ede70, but v7.7.0 was CUT BEFORE that commit
(``git merge-base --is-ancestor 5f59ede70 v7.7.0`` is false). Nothing
checked this at release time, so the *installed* ``nx`` at 7.7.0 carried
the PRE-FIX destructive ``manifest_backfill`` module -- the exact module
the remediation plan assumed was already safe. Found retroactively by the
strategic-planner via the same ``git merge-base --is-ancestor`` check this
script now runs mechanically, before every release, instead of relying on
someone remembering to re-verify a "ships after X" sequencing note by hand.

This is the bead-remediation analogue of ``check_engine_release_floor.py``'s
source-ancestry arm (``check_source_ancestry`` / nexus-hs4xl): version or
tag agreement does not imply SOURCE agreement -- a release can be current by
every other measure while a specific commit it was implicitly relying on is
simply not in its history. Same fail-closed doctrine throughout: "could not
verify" is never treated as "must be fine".

## What counts as a "required commit"

A bead names a required commit for THIS gate in one of two ways, scanned
across the bead's ``description``, ``notes``, ``design``,
``acceptance_criteria``, and every comment body. Two distinct bead JSON
shapes are accepted (duck-typed -- an absent field is simply skipped, not
an error): ``bd export``'s per-issue dict (comments inlined) and the
git-tracked ``.beads/issues.jsonl`` "beads-native" snapshot, which carries
no ``comments`` field at all but does carry ``design`` /
``acceptance_criteria``.

1. **Structured marker (preferred)** -- a line of the form::

       requires-commit: <sha>

   one sha per line (7-40 lowercase hex characters; case-insensitive on
   input, normalized to lowercase). This is the form bead authors should
   write going forward (see AGENTS.md / docs/contributing.md's release
   checklist entry for this gate) -- unambiguous, greppable by both this
   script and a human skimming ``bd show``, and immune to prose rewording
   breaking detection.
2. **Free-text fallback (best-effort net)** -- two natural-language forms,
   scanned case-insensitively, so a bead written before the marker
   convention existed is not silently invisible to this gate:

   - ``requires commit <sha>``
   - ``must include <sha>``

   Free-text scraping is inherently weaker than the marker (it will never
   catch every possible phrasing a bead author might use) -- it exists as a
   net, not a substitute. Prefer the marker.

A bead can name more than one required commit (one marker line per commit).
Requirements are deduplicated per ``(bead_id, sha)`` -- a sha mentioned in
both the description and three comments is checked once.

**Closed beads are never scanned.** A closed bead's remediation either
shipped or was abandoned; either way its "ships after X" sequencing note is
no longer live risk this gate needs to catch.

## Non-vacuity

Two DISTINCT guards, not conflated:

1. **Zero beads parsed from the export at all** is ALWAYS a hard failure,
   unconditionally, no flag required -- a scanner that read zero issues
   cannot tell "this repo has no beads" from "the export path is wrong /
   the file is stale or empty" (this is the class of failure that makes a
   gate WORSE than no gate: a wrong-but-parseable data source, like a
   long-stale JSONL snapshot, reports a confident green while catching
   nothing new. Verify the export is actually current before wiring this
   into an automated pipeline against anything other than a live ``bd
   export`` -- see the CI-wiring caveat in this repo's release skill /
   AGENTS.md § Cutting a release, which documents why this gate is
   currently a human/skill-run pre-tag step rather than a CI job: this
   repo's ``bd`` backend is Dolt, and the only git-tracked bead artifact,
   ``.beads/issues.jsonl``, is a legacy pre-Dolt-migration fossil (verified
   stale by ~3 months, ~5% of the live issue count, zero comments) --
   wiring THIS gate against THAT file in CI would satisfy this exact
   non-vacuity guard while still catching nothing, since the guard only
   detects "zero beads", not "stale beads").
2. **Zero requirements found among beads that WERE parsed** is a
   LEGITIMATE green by default -- most releases have no open remediation
   bead with a commit dependency. ``--require-at-least N`` is the opt-in
   escalation for a caller that wants to assert "this run must have found
   at least N real requirements" (e.g. a fixture-pinned CI run with a
   known-nonzero count); it stays opt-in because defaulting it on would
   false-red the common, correct case.

Every run prints a one-line pipeline summary (beads parsed / scanned /
requirements found) before the ancestor-check verdict, so a log shows the
non-vacuity evidence on a green run too, not only on failure.

## Usage::

    uv run python scripts/check_remediation_commits_ride_release.py --release-ref develop
    uv run python scripts/check_remediation_commits_ride_release.py --release-ref v7.7.0
    uv run python scripts/check_remediation_commits_ride_release.py --release-ref develop \\
        --bd-export-json /tmp/beads.jsonl   # offline / self-test, no live bd dependency

Exit codes: ``0`` all required commits ride the release (or zero requirements
found among the parsed beads and ``--require-at-least`` is unset /
satisfied), ``1`` a required commit is not an ancestor of the release ref
(named in the message), ``2`` unverifiable (``bd export`` failed, ZERO beads
were parsed at all, git could not be consulted, ``--require-at-least`` was
not met, ``--bd-export-json`` names a file that does not exist, or
``--verify-snapshot`` rejected the snapshot -- "could not check" is never
"must be fine").

## Snapshot verification (``--verify-snapshot``, nexus-fehi3)

``bd export`` needs live ``bd``/Dolt access this repo's CI runner does not
have (see the Non-vacuity section above for why wiring this gate against
the stale tracked ``.beads/issues.jsonl`` was rejected -- nexus-fix9t).
Instead, the release skill's pre-tag human step runs::

    uv run python scripts/check_remediation_commits_ride_release.py \\
        --write-snapshot .release-gates/remediation-snapshot.json

which invokes a live ``bd export`` and commits the result onto the release
branch alongside the version-bump commit (Step 7). ``release.yml`` then
replays the real gate against that exact committed file at tag-publish
time::

    python3 scripts/check_remediation_commits_ride_release.py \\
        --release-ref vX.Y.Z \\
        --bd-export-json .release-gates/remediation-snapshot.json \\
        --verify-snapshot

``--verify-snapshot`` adds two checks on top of the ordinary
``--bd-export-json`` load, both fail-closed (exit 2, never a silent pass):

1. **Committed on the release ref.** ``git cat-file -e <release-ref>:<path>``
   must resolve -- a snapshot present on disk but never ``git add``ed/
   committed (or committed on a different ref) is not proof of anything
   about *this* release.
2. **Not stale.** The newest ``updated_at`` timestamp across every parsed
   bead must be no older than the commit date of ``--release-base-ref``
   (default ``<release-ref>^``, i.e. the commit immediately preceding this
   release -- for the standard ``gh pr merge --merge`` shape this is
   exactly main's tip before the release PR landed, since Step 7 always
   pre-merges ``origin/main`` into the release branch before committing).
   A snapshot whose most recent bead activity predates that point was
   evidently captured before -- not at -- this release's cut.

A missing ``--bd-export-json`` file is always an explicit UNVERIFIABLE
message and exit 2 (never a raw traceback), independent of
``--verify-snapshot``.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

_CLOSED_STATUS = "closed"

#: Structured marker (preferred form -- see module docstring). One sha per
#: line; 7-40 lowercase-or-uppercase hex characters (git accepts abbreviated
#: shas from 7 chars; 40 is a full sha1). Case-insensitive on input, the
#: captured sha is normalized to lowercase before ancestry checking.
_MARKER_RE = re.compile(r"(?im)^\s*requires-commit:\s*([0-9a-fA-F]{7,40})\b")

#: Free-text fallback forms (best-effort net -- see module docstring). Named
#: labels are surfaced in gate output so a red line tells the reader WHICH
#: form matched, not just that something did.
_FREE_TEXT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("free-text:requires-commit", re.compile(r"(?i)\brequires\s+commit\s+([0-9a-fA-F]{7,40})\b")),
    ("free-text:must-include", re.compile(r"(?i)\bmust\s+include\s+([0-9a-fA-F]{7,40})\b")),
]

#: Sentinel for "bd export could not be run / parsed". Distinct from an
#: empty (but successfully parsed) export, which is itself suspicious for a
#: repo with any history at all but is NOT treated as a hard failure by this
#: constant -- see ``check_requirements``'s zero-requirements branch, which
#: is a legitimate green unless ``--require-at-least`` says otherwise.
_EXPORT_UNAVAILABLE = object()

#: Sentinel for "git could not resolve one of the two revisions". Distinct
#: from ``False`` (git resolved both revisions and confirmed non-ancestry) --
#: an unresolvable sha is a gate FAILURE (folded into the red exit, not a
#: silent skip), never a pass.
_UNVERIFIABLE = object()


@dataclass(frozen=True)
class Requirement:
    """One (bead, required-commit) pair found by the scan."""

    bead_id: str
    bead_title: str
    sha: str
    source: str  # "structured-marker" | one of the _FREE_TEXT_PATTERNS labels
    locus: str  # "description" | "notes" | "comment <id>"


def find_requirements_in_text(text: str, locus: str) -> list[tuple[str, str, str]]:
    """Return ``(sha, source, locus)`` tuples found in ``text``.

    Deduplicated within this single ``text`` blob by sha, preferring the
    structured marker's classification over a free-text match on the same
    sha (marker scanned first; ``dict.setdefault`` keeps the first hit).
    """
    found: dict[str, tuple[str, str, str]] = {}
    for m in _MARKER_RE.finditer(text):
        sha = m.group(1).lower()
        found.setdefault(sha, (sha, "structured-marker", locus))
    for label, pattern in _FREE_TEXT_PATTERNS:
        for m in pattern.finditer(text):
            sha = m.group(1).lower()
            found.setdefault(sha, (sha, label, locus))
    return list(found.values())


#: Simple string-valued loci scanned on every bead, keyed by the field name
#: bd stores them under. Two DISTINCT bead JSON shapes exist in this repo's
#: tooling and both are scanned by the same field list, duck-typed via
#: ``dict.get`` (nexus-fix9t code-review round): ``bd export``'s per-issue
#: dict (comments inlined, ``design``/``acceptance_criteria`` present on
#: ~9% of issues) and the git-tracked ``.beads/issues.jsonl`` "beads-native"
#: snapshot (no ``comments`` field at all, but the same ``design`` /
#: ``acceptance_criteria`` fields). Neither shape needs bespoke handling:
#: a field absent from one shape is simply absent from the dict, and
#: ``.get(..., "")`` treats that identically to "present but empty".
_SIMPLE_TEXT_FIELDS = ("description", "notes", "design", "acceptance_criteria")


def extract_bead_requirements(bead: dict) -> list[Requirement]:
    """All required-commit requirements named anywhere in ``bead``.

    Scans :data:`_SIMPLE_TEXT_FIELDS` plus every comment's ``text`` -- the
    loci a bead author plausibly writes a sequencing note in. ``comments``
    is only ever present on the ``bd export`` shape (the git-tracked
    ``.beads/issues.jsonl`` snapshot carries no comment bodies at all); its
    absence here is silent and correct, not a partial read -- there is
    nothing to scan that the source data doesn't have. Results are
    deduplicated per sha across ALL loci of this one bead -- a sha
    mentioned in both the description and two comments is a single
    requirement, not three.
    """
    bead_id = bead.get("id") or "?"
    title = bead.get("title") or ""

    texts: list[tuple[str, str]] = []
    for field in _SIMPLE_TEXT_FIELDS:
        value = bead.get(field) or ""
        if value:
            texts.append((field, value))
    for comment in bead.get("comments") or []:
        body = comment.get("text") or ""
        if body:
            texts.append((f"comment:{comment.get('id') or '?'}", body))

    seen: dict[str, Requirement] = {}
    for locus, text in texts:
        for sha, source, found_locus in find_requirements_in_text(text, locus):
            seen.setdefault(
                sha,
                Requirement(bead_id=bead_id, bead_title=title, sha=sha, source=source, locus=found_locus),
            )
    return list(seen.values())


def scan_beads(beads: list[dict]) -> list[Requirement]:
    """Requirements across every NON-CLOSED bead in ``beads``.

    Closed beads are skipped outright (module docstring: a closed bead's
    sequencing note is no longer live risk). Status comparison is
    case-insensitive; a bead with no ``status`` field is treated as
    non-closed (conservatively scanned rather than silently skipped).
    """
    requirements: list[Requirement] = []
    for bead in beads:
        status = (bead.get("status") or "").strip().lower()
        if status == _CLOSED_STATUS:
            continue
        requirements.extend(extract_bead_requirements(bead))
    return requirements


def load_beads_from_export_json(path: pathlib.Path) -> list[dict]:
    """Parse a ``bd export`` JSONL file (or an equivalent hand-built fixture)."""
    beads: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            beads.append(json.loads(line))
    return beads


def write_snapshot(path: pathlib.Path, repo_root: pathlib.Path) -> int:
    """Run a live ``bd export`` and write it to ``path`` as bd-export-shaped
    JSONL -- the human pre-tag half of the snapshot flow (nexus-fehi3). The
    caller (release skill Step 0b) then ``git add``s + commits ``path`` onto
    the release branch alongside the version-bump commit, so
    ``--verify-snapshot`` can later confirm it is genuinely on that ref.

    Returns ``0`` on success, ``2`` (same UNVERIFIABLE doctrine as the rest
    of this module) if ``bd export`` itself could not be run -- no partial
    file is left behind in that case.
    """
    beads = run_bd_export(repo_root)
    if beads is _EXPORT_UNAVAILABLE:
        print(
            "remediation-commit snapshot: `bd export` could not be run or returned "
            "unparseable output -- no snapshot written. Confirm `bd` is installed and "
            ".beads/ is initialized in this repo.",
            file=sys.stderr,
        )
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for bead in beads:
            f.write(json.dumps(bead) + "\n")
    print(f"remediation-commit snapshot: wrote {len(beads)} bead(s) to {path}")
    return 0


def is_path_committed_at_ref(path: pathlib.Path, ref: str, repo_root: pathlib.Path) -> bool:
    """``True`` iff ``path`` (any path, absolute or relative) is tracked by
    git AT ``ref`` -- i.e. committed on that exact ref, not merely present
    on disk. A path outside ``repo_root`` cannot be "on this ref" by
    definition and is ``False`` without invoking git at all.
    """
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    try:
        out = subprocess.run(
            ["git", "cat-file", "-e", f"{ref}:{rel.as_posix()}"],
            cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def resolve_commit_date(ref: str, repo_root: pathlib.Path) -> object:
    """The commit date of ``ref`` as a timezone-aware :class:`datetime`, or
    :data:`_UNVERIFIABLE` if git could not resolve it."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", ref],
            cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _UNVERIFIABLE
    if out.returncode != 0 or not out.stdout.strip():
        return _UNVERIFIABLE
    try:
        return datetime.fromisoformat(out.stdout.strip())
    except ValueError:
        return _UNVERIFIABLE


def newest_bead_updated_at(beads: list[dict]) -> datetime | None:
    """The most recent ``updated_at`` timestamp among ``beads``, or ``None``
    if none carry a parseable one. Missing or malformed values are skipped,
    not errors -- the caller decides whether "no timestamps at all" is
    itself a failure (it is, for ``--verify-snapshot``: see
    :func:`verify_snapshot_is_fresh`)."""
    timestamps: list[datetime] = []
    for bead in beads:
        raw = bead.get("updated_at")
        if not raw:
            continue
        try:
            timestamps.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            continue
    return max(timestamps) if timestamps else None


def verify_snapshot_is_fresh(
    beads: list[dict],
    snapshot_path: pathlib.Path,
    release_ref: str,
    release_base_ref: str,
    repo_root: pathlib.Path,
) -> int | None:
    """The two ``--verify-snapshot`` checks (module docstring). Returns an
    exit code (always ``2``, UNVERIFIABLE) if either check fails, or
    ``None`` if both pass and the caller should proceed to the ordinary
    ancestor-check gate."""
    if not is_path_committed_at_ref(snapshot_path, release_ref, repo_root):
        print(
            f"REMEDIATION-COMMIT GATE UNVERIFIABLE: snapshot {snapshot_path} is not "
            f"committed on {release_ref} -- a snapshot present on disk but never "
            "`git add`ed/committed (or committed on a different ref) proves nothing "
            "about this release. Run the release skill's pre-tag `--write-snapshot` "
            "step and commit its output before tagging.",
            file=sys.stderr,
        )
        return 2

    newest = newest_bead_updated_at(beads)
    if newest is None:
        print(
            "REMEDIATION-COMMIT GATE UNVERIFIABLE: the snapshot carries no parseable "
            "`updated_at` timestamp on any bead -- cannot establish freshness. Treat as "
            "a failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2

    base_date = resolve_commit_date(release_base_ref, repo_root)
    if base_date is _UNVERIFIABLE:
        print(
            f"REMEDIATION-COMMIT GATE UNVERIFIABLE: could not resolve --release-base-ref "
            f"{release_base_ref!r} against git -- cannot establish the staleness floor.",
            file=sys.stderr,
        )
        return 2

    if newest < base_date:
        print(
            f"REMEDIATION-COMMIT GATE UNVERIFIABLE: snapshot is STALE -- newest bead "
            f"updated_at ({newest.isoformat()}) is older than {release_base_ref}'s commit "
            f"date ({base_date.isoformat()}). The snapshot was evidently captured before, "
            "not at, this release's cut -- re-run the release skill's pre-tag "
            "`--write-snapshot` step and re-commit.",
            file=sys.stderr,
        )
        return 2

    return None


def run_bd_export(repo_root: pathlib.Path) -> object:
    """Read-only ``bd export`` against ``repo_root``'s beads DB.

    ``bd export`` (unlike ``bd list --json``) inlines full comment bodies
    for every issue in one call -- exactly the ``description`` + ``notes`` +
    ``comments`` shape :func:`extract_bead_requirements` scans, without an
    N+1 ``bd show --include-comments`` per bead. No ``--status`` filter
    exists on ``bd export`` (checked against ``bd export --help``, bd
    1.0.5), so this reads every issue and :func:`scan_beads` filters closed
    ones out client-side.

    Returns the parsed bead list, or :data:`_EXPORT_UNAVAILABLE` if ``bd``
    could not be invoked, exited non-zero, or produced a line that does not
    parse as JSON -- fail-closed, same doctrine as
    ``check_engine_release_floor.py``'s ``_TAGS_UNAVAILABLE``: "could not
    check" is never "must be fine".
    """
    try:
        out = subprocess.run(
            ["bd", "export", "-C", str(repo_root)],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _EXPORT_UNAVAILABLE
    if out.returncode != 0:
        return _EXPORT_UNAVAILABLE
    beads: list[dict] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            beads.append(json.loads(line))
        except json.JSONDecodeError:
            return _EXPORT_UNAVAILABLE
    return beads


def is_ancestor(sha: str, ref: str, repo_root: pathlib.Path) -> object:
    """``True``/``False``, or :data:`_UNVERIFIABLE` if git could not resolve ``sha`` or ``ref``.

    ``git merge-base --is-ancestor A B`` exits 0 when A is an ancestor of
    (or equal to) B, 1 when it is verifiably NOT an ancestor, and a
    different code (typically 128) when either revision does not resolve --
    a bad/mistyped sha, or a shallow checkout missing the commit. The third
    case is UNVERIFIABLE, not a silent pass, and not conflated with the
    real "not an ancestor" answer.
    """
    try:
        out = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, ref],
            cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _UNVERIFIABLE
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    return _UNVERIFIABLE


def check_requirements(
    requirements: list[Requirement],
    release_ref: str,
    repo_root: pathlib.Path,
    require_at_least: int = 0,
) -> int:
    """Ancestor-check every requirement against ``release_ref``.

    Returns ``0`` (every requirement's sha is an ancestor, or zero
    requirements found and ``require_at_least`` is satisfied), ``1`` (at
    least one requirement's sha is verifiably not an ancestor, or could not
    be resolved by git at all -- both fold into the same red exit since
    both mean "this release cannot be trusted to carry that commit"), or
    ``2`` (zero requirements found but ``require_at_least`` demands more --
    the non-vacuity guard).
    """
    if not requirements:
        if require_at_least > 0:
            print(
                f"REMEDIATION-COMMIT GATE UNVERIFIABLE: 0 remediation beads scanned, but "
                f"--require-at-least {require_at_least} demands at least that many. A "
                "scanner that silently finds nothing is not proof there is nothing to "
                "find -- check the bd export path / marker syntax before trusting a green.",
                file=sys.stderr,
            )
            return 2
        print(
            "remediation-commit gate: 0 remediation beads scanned (no open/in_progress/"
            "blocked/deferred bead names a required commit via `requires-commit:` or the "
            "recognized free-text forms) -- nothing to check, gate passes vacuously."
        )
        return 0

    failures: list[tuple[Requirement, str]] = []
    for req in requirements:
        result = is_ancestor(req.sha, release_ref, repo_root)
        if result is True:
            continue
        if result is False:
            failures.append((req, f"{req.sha} is NOT an ancestor of {release_ref}"))
        else:
            failures.append(
                (req, f"could not resolve {req.sha} against {release_ref} (unknown revision, "
                      "or a shallow checkout missing the commit)")
            )

    if failures:
        print(
            f"REMEDIATION-COMMIT GATE FAILED: {len(failures)} of {len(requirements)} "
            f"remediation commit(s) do not ride {release_ref}:",
            file=sys.stderr,
        )
        for req, reason in failures:
            print(
                f"  {req.bead_id} ({req.bead_title!r}) requires {req.sha} "
                f"[{req.source}, {req.locus}]: {reason}\n"
                f"    Remedy: re-sequence {req.bead_id} to run after a release that DOES "
                f"carry {req.sha}, or include {req.sha} in {release_ref} before cutting.",
                file=sys.stderr,
            )
        return 1

    bead_count = len({req.bead_id for req in requirements})
    print(
        f"remediation-commit gate: {len(requirements)} remediation commit(s) across "
        f"{bead_count} bead(s) all ride {release_ref}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--release-ref",
        default=None,
        metavar="REF",
        help="Branch or tag being cut/verified (e.g. develop, v7.7.0). Every open "
        "remediation bead's required commit(s) must be an ancestor of this ref. Required "
        "unless --write-snapshot is given (that mode does not run the gate).",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        type=pathlib.Path,
        metavar="PATH",
        help="Repo root for the `bd export` / `git merge-base` invocations (default: this "
        "script's parent repo).",
    )
    parser.add_argument(
        "--bd-export-json",
        default=None,
        type=pathlib.Path,
        metavar="PATH",
        help="Read beads from this JSONL file (one bd-export-shaped issue per line) instead "
        "of invoking `bd export` live. For offline runs and the self-test -- no live bd "
        "dependency.",
    )
    parser.add_argument(
        "--require-at-least",
        type=int,
        default=0,
        metavar="N",
        help="Fail (non-vacuity) unless at least N remediation-commit requirements are "
        "found across all scanned beads. 0 (default) allows a legitimate zero-finding "
        "green -- most releases have no open remediation bead with a commit dependency.",
    )
    parser.add_argument(
        "--write-snapshot",
        default=None,
        type=pathlib.Path,
        metavar="PATH",
        help="SNAPSHOT-WRITE MODE (nexus-fehi3): run a live `bd export` and write it to "
        "PATH, then exit -- does not run the gate and does not require --release-ref. "
        "The human pre-tag half of the CI-replay flow (release skill Step 0b); commit "
        "PATH onto the release branch afterward.",
    )
    parser.add_argument(
        "--verify-snapshot",
        action="store_true",
        help="SNAPSHOT-VERIFY MODE (nexus-fehi3): requires --bd-export-json. Additionally "
        "asserts the snapshot file is committed on --release-ref (not merely present on "
        "disk) and is not stale (see --release-base-ref). Fails closed (exit 2) on either "
        "check failing. This is what release.yml's CI replay passes.",
    )
    parser.add_argument(
        "--release-base-ref",
        default=None,
        metavar="REF",
        help="Only meaningful with --verify-snapshot: the staleness floor -- the snapshot's "
        "newest bead updated_at must be no older than this ref's commit date. Default "
        "`<release-ref>^`, the commit immediately preceding this release (main's tip "
        "before the release PR landed, for the standard merge shape).",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root or pathlib.Path(__file__).resolve().parent.parent

    if args.write_snapshot is not None:
        return write_snapshot(args.write_snapshot, repo_root)

    if args.release_ref is None:
        parser.error("--release-ref is required (unless --write-snapshot is given)")
    if args.verify_snapshot and args.bd_export_json is None:
        parser.error("--verify-snapshot requires --bd-export-json")

    if args.bd_export_json is not None:
        if not args.bd_export_json.exists():
            print(
                f"REMEDIATION-COMMIT GATE UNVERIFIABLE: --bd-export-json "
                f"{args.bd_export_json} does not exist. A missing snapshot means the "
                "pre-tag human step (or the offline caller) did not produce it -- this is "
                "never a pass.",
                file=sys.stderr,
            )
            return 2
        beads = load_beads_from_export_json(args.bd_export_json)
    else:
        beads = run_bd_export(repo_root)
        if beads is _EXPORT_UNAVAILABLE:
            print(
                "REMEDIATION-COMMIT GATE UNVERIFIABLE: `bd export` could not be run or "
                "returned unparseable output. Cannot scan beads for required-commit "
                "markers -- treat as a failed gate, not a pass. Confirm `bd` is installed "
                "and .beads/ is initialized in this repo, or pass --bd-export-json for an "
                "offline run.",
                file=sys.stderr,
            )
            return 2

    if args.verify_snapshot:
        release_base_ref = args.release_base_ref or f"{args.release_ref}^"
        rc = verify_snapshot_is_fresh(beads, args.bd_export_json, args.release_ref, release_base_ref, repo_root)
        if rc is not None:
            return rc

    return run_gate(beads, args.release_ref, repo_root, require_at_least=args.require_at_least)


def run_gate(
    beads: list[dict],
    release_ref: str,
    repo_root: pathlib.Path,
    require_at_least: int = 0,
) -> int:
    """Pipeline glue between a loaded bead list and :func:`check_requirements`.

    Two DISTINCT non-vacuity guards, deliberately not conflated (nexus-fix9t
    code-review round):

    1. **Zero beads parsed at all** (``len(beads) == 0``) is ALWAYS a hard
       failure, unconditionally -- a scanner that read zero issues cannot
       distinguish "this repo genuinely has no beads" from "the export path
       is wrong / the file is empty / corrupt". There is no legitimate
       all-zero bead count for a repo old enough to be release-gated, so
       this never needs a flag to opt into.
    2. **Zero requirements found among the beads that WERE parsed** (no bead
       carries a ``requires-commit:`` marker or a recognized free-text
       form) is a LEGITIMATE green by default -- most releases have no open
       remediation bead with a commit dependency. ``--require-at-least``
       (handled inside :func:`check_requirements`) is the OPT-IN escalation
       for a caller that wants to assert a known-nonzero count (e.g. a
       fixture-pinned CI run); it must stay opt-in, since turning it on by
       default would false-red the common, correct case of a release with
       zero live commit-sequenced remediations.

    Always prints a one-line pipeline summary (parsed / scanned / found
    counts) BEFORE the ancestor-check verdict, so a CI log shows the
    non-vacuity evidence even on a green run, not just on failure.
    """
    if not beads:
        print(
            "REMEDIATION-COMMIT GATE UNVERIFIABLE: 0 beads parsed from the export. A "
            "scanner that reads zero issues cannot prove there is nothing to find -- "
            "this is ALWAYS a failed gate, never treated as a legitimate empty repo. "
            "Check the bd export path, or that the export file is not empty/stale.",
            file=sys.stderr,
        )
        return 2

    scanned = [b for b in beads if (b.get("status") or "").strip().lower() != _CLOSED_STATUS]
    requirements = scan_beads(beads)
    print(
        f"remediation-commit gate: {len(beads)} bead(s) parsed, {len(scanned)} beads "
        f"scanned (non-closed), {len(requirements)} requirement(s) found", flush=True)
    return check_requirements(
        requirements, release_ref, repo_root, require_at_least=require_at_least
    )


if __name__ == "__main__":
    raise SystemExit(main())
