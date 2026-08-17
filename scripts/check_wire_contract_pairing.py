#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Both-halves wire-contract tripwire (nexus-1vogq).

THE BUG CLASS. ``498c92953`` (RDR-191 GATE-2: manifest ``collection`` became
NOT NULL at the store) changed both the engine's request validation AND every
``src/nexus`` client caller in the same commit -- except the raw
``cat._post("/import/chunk", ...)`` envelopes hand-built in one test file,
which carry no client-method signature for a contract change to reconcile
against and were therefore structurally invisible to a signature-diff review.
The engine tag (``engine-service-v0.1.73``) deployed to the managed service
before any client release carried the fix, so every released client (up to
and including v7.6.1) 400'd on manifest writes -- full forensic trace in T2
``nexus/rdr-191-manifest-400-caller-trace-2026-08-14`` [22490]. Sibling bead
nexus-86mx2 adds a gate leg that runs the CURRENTLY PUBLISHED client against a
candidate engine before deploy; this module is the complementary STATIC
tripwire that flags the commit class at review/release time, before a gate run
is even needed, and covers the hand-built-test-envelope blind spot the 2026-
08-14 bead comment called out explicitly.

WHAT COUNTS AS "BOTH HALVES". A commit is flagged when it touches:

    (a) the engine tree -- any path under ``service/``, and
    (b) the client wire surface -- either
        (b1) a ``src/nexus/**/*.py`` file whose name STARTS WITH ``http_``
             (the stable naming convention for every T1/T2/T3 wire client in
             this tree -- catalog, vector, pipeline, scratch, aspect queue,
             taxonomy, telemetry, ... -- see :func:`_is_client_module_path`),
             or contains one of :data:`_CLIENT_FILE_SUBSTRINGS` (the non-
             ``http_``-named modules that still own wire-contract calls:
             ``mcp_infra``, ``store_hook``, the ``indexer`` family), or
        (b2) a ``tests/**/*.py`` file whose content (AT THAT COMMIT) contains
             a raw ``_post("/manifest...`` or ``_post("/import...`` envelope
             -- the class the 2026-08-14 bead comment added: a hand-built test
             body has no method signature to diff against, so path-only
             detection on client MODULES is structurally blind to it.

RECURSIVE-GAP GUARD (2026-08-14 consolidated review, T2 [22513]). (b1)'s
coverage rule is itself just a list/pattern -- and a coverage rule that only
knows about TODAY's client modules silently escapes its own lint the moment a
NEW one starts issuing wire calls. :func:`live_client_modules_missing_coverage`
closes this by scanning the LIVE ``src/nexus/**/*.py`` tree (not a commit
diff) for the same raw-envelope idiom used in (b2), and failing loudly if any
match is NOT covered by (b1) -- see its docstring. This is why (b1) prefers
the structural ``http_`` prefix over an enumerated list: an enumerated list
of "modules that today own wire calls" is exactly the shape that reopens this
gap on the next new module; a naming CONVENTION does not.

(a) is deliberately the FULL ``service/`` tree, not the narrower
``service/src/main/java/**/http/**`` / ``changelog/**`` surface one might
reach for first. ``8c75a61a3`` (F8c indexer guard, one of the three known
members this module's non-vacuity test pins) touches ONLY a
``service/src/test/java/**`` regression test on the engine side -- no
main-code, no changelog. The T2 [22490] Q3b census already validated the
full-``service/``-tree intersection as the right methodology ("exhaustive for
single-commit pairs"); narrowing to main/http+changelog would silently drop
this exact known member. A tripwire's failure mode should be over-flagging
(cheap: write one ledger line) never under-flagging (expensive: the outage
this module exists to prevent), so the broader surface is the correct choice.

THE LEDGER CONTRACT (mirrors ``conexus/PENDING_RELEASE.md``'s shape and
``tests/test_plugin_release_drift_ledger.py``'s enforcement pattern). A
checked-in ledger (default :data:`DEFAULT_LEDGER_PATH`) declares every flagged
commit under an ``## Unshipped`` section, naming the owning bead and the
engine tag that carries its engine half. Two failure modes, symmetric with the
plugin drift ledger:

    - UNDECLARED: a flagged commit in ``<newest v* tag>..HEAD`` absent from
      the ledger's Unshipped section.
    - STALE: an Unshipped entry whose commit is now an ancestor of the newest
      published ``v*`` tag -- the client half shipped; the entry must move to
      the ``## Shipped`` history section (or be deleted), never linger.

Usage::

    uv run python scripts/check_wire_contract_pairing.py
    uv run python scripts/check_wire_contract_pairing.py --rev-range v7.6.1..HEAD

Exit codes: ``0`` ledger and detected state agree (nothing undeclared, nothing
stale); ``1`` undeclared and/or stale entries found; ``2`` git state could not
be interrogated ("could not verify" is never "must be fine").
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: House-consistent home for this ledger: alongside the other durable
#: process docs in docs/, not conexus/ (that PENDING_RELEASE.md ledger is
#: plugin-pin-specific machinery; this one is a repo-wide release-process
#: artifact with no plugin-pin dependency).
DEFAULT_LEDGER_PATH = _REPO_ROOT / "docs" / "wire-contract-pending.md"

#: The engine-side surface. Deliberately the whole service/ tree -- see the
#: module docstring's "WHAT COUNTS AS BOTH HALVES" section for why the
#: narrower main/http + changelog surface would miss a known member.
_ENGINE_PREFIX = "service/"

#: Client modules that own wire-contract method signatures beyond the
#: structural ``http_*`` naming convention (see :func:`_is_client_module_path`)
#: -- RDR-191 census, T2 [22490] Q3: the manifest/collection glue, and the
#: indexer family that drives batched manifest writes (doc_indexer.py,
#: code_indexer.py, prose_indexer.py, indexer.py, indexer_utils.py all match
#: via the "indexer" substring -- confirmed grep, all are real manifest-write
#: producers per [22490] Q2(b)).
#:
#: DELIBERATELY DOES NOT list the http_* clients (http_catalog_client,
#: http_vector_client, ...) -- 2026-08-14 consolidated review (T2 [22513]):
#: an enumerated list here is the SAME recursive gap the module docstring's
#: over-flag/never-under-flag principle exists to avoid. A NEW http_*_store.py
#: (there are ten: http_aspect_queue, http_centroid_store, http_chash_index,
#: http_document_aspects_store, http_document_highlights_store,
#: http_memory_store, http_pipeline_client, http_plan_library,
#: http_scratch_store, http_taxonomy_store, http_telemetry_store,
#: http_token_store, plus catalog/vector) would silently escape an enumerated
#: list every time one is added. ``http_`` is a STABLE, load-bearing naming
#: convention across every T1/T2/T3 wire client in src/nexus/ -- see
#: :func:`_is_client_module_path`, which recognizes it structurally.
_CLIENT_FILE_SUBSTRINGS = (
    "mcp_infra",
    "store_hook",
    "indexer",
)

#: Hand-built test envelope shape (2026-08-14 bead comment): a raw
#: ``cat._post("/manifest/...")`` or ``cat._post("/import/...")`` call has no
#: client-method signature to diff, so a commit that only edits a client
#: METHOD is structurally blind to a test file carrying one of these. Matches
#: `_post("/manifest...` and `_post('/import...` etc.
_TEST_POST_RE = re.compile(r"""_post\(\s*["']/(?:manifest|import)""")

#: Sentinel for "could not read from git" -- distinct from "read cleanly and
#: found nothing empty", same idiom as the sibling release-gate scripts.
GIT_UNAVAILABLE = object()


def _git(
    *args: str, repo_root: pathlib.Path | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root or _REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def newest_client_tag(repo_root: pathlib.Path | None = None) -> str | object:
    """Newest published ``vX.Y.Z`` client tag, or :data:`GIT_UNAVAILABLE`."""
    proc = _git("tag", "-l", "v[0-9]*", "--sort=-v:refname", repo_root=repo_root)
    if proc.returncode != 0:
        return GIT_UNAVAILABLE
    tags = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return tags[0] if tags else None


def is_ancestor(
    commit: str, ref: str, repo_root: pathlib.Path | None = None
) -> object:
    """``True``/``False``, or :data:`GIT_UNAVAILABLE` if git could not answer."""
    proc = _git(
        "merge-base", "--is-ancestor", commit, ref, repo_root=repo_root
    )
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    return GIT_UNAVAILABLE


def _is_engine_path(path: str) -> bool:
    return path.startswith(_ENGINE_PREFIX)


def _is_client_module_path(path: str) -> bool:
    if not (path.startswith("src/nexus/") and path.endswith(".py")):
        return False
    name = pathlib.PurePosixPath(path).name
    # ``http_*`` is the stable naming convention for every T1/T2/T3 wire
    # client in this tree (catalog, vector, pipeline, scratch, aspect queue,
    # taxonomy, telemetry, token, memory, plan library, ... -- verified
    # exhaustively: every src/nexus/**/http_*.py IS a wire client, by grep).
    # Structural, not enumerated -- a NEW http_*_store.py is covered on day
    # one, closing the recursive gap an enumerated list would reopen.
    if name.startswith("http_"):
        return True
    return any(substr in name for substr in _CLIENT_FILE_SUBSTRINGS)


def _touched_paths(
    commit: str, repo_root: pathlib.Path | None = None
) -> list[str]:
    proc = _git("show", "--name-only", "--format=", commit, repo_root=repo_root)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git show --name-only {commit}: {proc.stderr.strip()}"
        )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _file_content_at(
    commit: str, path: str, repo_root: pathlib.Path | None = None
) -> str | None:
    """File content at ``commit``, or ``None`` if unreadable (deleted, binary).

    A read failure here is a soft "not a test envelope match", not a hard
    error -- a commit that DELETES the only offending test file, for example,
    should not crash the detector.
    """
    proc = _git("show", f"{commit}:{path}", repo_root=repo_root)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _is_client_test_envelope(
    commit: str, path: str, repo_root: pathlib.Path | None = None
) -> bool:
    if not (path.startswith("tests/") and path.endswith(".py")):
        return False
    content = _file_content_at(commit, path, repo_root=repo_root)
    if content is None:
        return False
    return bool(_TEST_POST_RE.search(content))


def live_client_modules_missing_coverage(
    repo_root: pathlib.Path | None = None,
) -> list[str]:
    """Drift guard for :data:`_CLIENT_FILE_SUBSTRINGS` / :func:`_is_client_module_path`
    (2026-08-14 consolidated review, T2 [22513] -- "the recursive gap"): a
    substring/naming list that only covers TODAY's known client modules
    silently escapes its own lint the moment a new module starts issuing
    wire calls. This scans the LIVE ``src/nexus/**/*.py`` tree (not a single
    commit's diff) with the same ``_TEST_POST_RE`` envelope idiom used for
    test-file detection, and returns every file that issues a raw
    ``_post("/manifest...`` / ``_post("/import...`` wire call but whose path
    :func:`_is_client_module_path` does NOT recognize as a client module --
    i.e. a module :func:`flagged_commits` would silently fail to credit as
    the client half of a both-halves commit.

    Empty list is the passing state. A non-empty return names real gaps in
    the coverage rule -- fix by widening :func:`_is_client_module_path`
    (prefer a structural rule, like the ``http_`` naming convention it
    already recognizes, over appending to the enumerated
    :data:`_CLIENT_FILE_SUBSTRINGS` list, which is exactly how this gap
    recurs).
    """
    root = repo_root or _REPO_ROOT
    src_dir = root / "src" / "nexus"
    offenders: list[str] = []
    if not src_dir.is_dir():
        return offenders
    for file_path in sorted(src_dir.rglob("*.py")):
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _TEST_POST_RE.search(content):
            continue
        rel = file_path.relative_to(root).as_posix()
        if not _is_client_module_path(rel):
            offenders.append(rel)
    return offenders


@dataclass(frozen=True)
class FlaggedCommit:
    sha: str
    subject: str
    engine_paths: tuple[str, ...]
    client_paths: tuple[str, ...]


def flagged_commits(
    rev_range: str, repo_root: pathlib.Path | None = None
) -> list[FlaggedCommit]:
    """Commits in ``rev_range`` that touch both the engine and client wire
    surfaces. Raises :class:`RuntimeError` on unreadable git state -- callers
    that want a soft failure should catch it (the CLI does, turning it into
    exit code 2)."""
    proc = _git("log", "--format=%H\x01%s", rev_range, repo_root=repo_root)
    if proc.returncode != 0:
        raise RuntimeError(f"git log {rev_range}: {proc.stderr.strip()}")
    out: list[FlaggedCommit] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition("\x01")
        paths = _touched_paths(sha, repo_root=repo_root)
        engine_paths = tuple(p for p in paths if _is_engine_path(p))
        if not engine_paths:
            continue
        client_paths = tuple(
            p
            for p in paths
            if _is_client_module_path(p)
            or _is_client_test_envelope(sha, p, repo_root=repo_root)
        )
        if client_paths:
            out.append(
                FlaggedCommit(
                    sha=sha,
                    subject=subject,
                    engine_paths=engine_paths,
                    client_paths=client_paths,
                )
            )
    return out


@dataclass(frozen=True)
class LedgerEntry:
    sha: str
    bead: str
    note: str
    engine_tag: str | None = None
    shipped_in: str | None = None


@dataclass
class Ledger:
    unshipped: dict[str, LedgerEntry] = field(default_factory=dict)
    shipped: dict[str, LedgerEntry] = field(default_factory=dict)


#: Bullet shape mirrors tests/test_plugin_release_drift_ledger.py's
#: backtick-span idiom: exact spans, one canonical parser feeds both the
#: undeclared and stale checks so they can never drift apart.
_UNSHIPPED_RE = re.compile(
    r"^- `([0-9a-f]{7,40})` -- bead (\S+) -- engine tag `([^`]+)` -- (.+)$"
)
_SHIPPED_RE = re.compile(
    r"^- `([0-9a-f]{7,40})` -- bead (\S+) -- shipped in `([^`]+)` -- (.+)$"
)


def parse_ledger(path: pathlib.Path) -> Ledger:
    ledger = Ledger()
    if not path.is_file():
        return ledger
    section: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Unshipped"):
            section = "unshipped"
            continue
        if line.startswith("## Shipped"):
            section = "shipped"
            continue
        if line.startswith("## "):
            section = None
            continue
        if section == "unshipped":
            m = _UNSHIPPED_RE.match(line)
            if m:
                sha, bead, tag, note = m.groups()
                ledger.unshipped[sha] = LedgerEntry(
                    sha=sha, bead=bead, note=note, engine_tag=tag
                )
        elif section == "shipped":
            m = _SHIPPED_RE.match(line)
            if m:
                sha, bead, tag, note = m.groups()
                ledger.shipped[sha] = LedgerEntry(
                    sha=sha, bead=bead, note=note, shipped_in=tag
                )
    return ledger


def _sha_declared(sha: str, unshipped: dict[str, LedgerEntry]) -> bool:
    """A flagged full 40-char sha is declared if any ledger key is a prefix
    of it (ledger entries may use abbreviated shas)."""
    return any(sha.startswith(key) for key in unshipped)


@dataclass(frozen=True)
class EvalResult:
    undeclared: tuple[FlaggedCommit, ...]
    stale: tuple[LedgerEntry, ...]

    @property
    def ok(self) -> bool:
        return not self.undeclared and not self.stale


def evaluate(
    flagged: list[FlaggedCommit],
    ledger: Ledger,
    newest_tag: str | object,
    repo_root: pathlib.Path | None = None,
) -> EvalResult | object:
    """Compare detected flagged commits against the ledger.

    Returns :data:`GIT_UNAVAILABLE` if staleness could not be verified (a
    ledger entry's ``is_ancestor`` check failed to answer) -- never silently
    treats "could not verify" as "must be clean".
    """
    undeclared = tuple(
        c for c in flagged if not _sha_declared(c.sha, ledger.unshipped)
    )
    stale: list[LedgerEntry] = []
    if newest_tag not in (None, GIT_UNAVAILABLE):
        for entry in ledger.unshipped.values():
            ancestor = is_ancestor(entry.sha, str(newest_tag), repo_root=repo_root)
            if ancestor is GIT_UNAVAILABLE:
                return GIT_UNAVAILABLE
            if ancestor:
                stale.append(entry)
    return EvalResult(undeclared=undeclared, stale=tuple(stale))


def _format_report(
    result: EvalResult, scanned_range: str, newest_tag: str, ledger_path: pathlib.Path
) -> str:
    lines: list[str] = []
    if result.undeclared:
        lines.append(
            f"UNDECLARED ({len(result.undeclared)}): commit(s) in "
            f"{scanned_range} touch both the engine tree (service/) and the "
            f"client wire surface but are absent from {ledger_path}'s "
            "## Unshipped section:"
        )
        for c in result.undeclared:
            lines.append(f"  {c.sha}  {c.subject}")
            lines.append(f"    engine: {', '.join(c.engine_paths[:3])}"
                          f"{' ...' if len(c.engine_paths) > 3 else ''}")
            lines.append(f"    client: {', '.join(c.client_paths[:3])}"
                          f"{' ...' if len(c.client_paths) > 3 else ''}")
        lines.append(
            "Add a line to the ## Unshipped section naming the owning bead "
            "and the engine tag carrying this commit's engine half."
        )
    if result.stale:
        if lines:
            lines.append("")
        lines.append(
            f"STALE ({len(result.stale)}): ## Unshipped entries whose commit "
            f"is now an ancestor of {newest_tag} -- the client half shipped, "
            f"move these to ## Shipped in {ledger_path}:"
        )
        for e in result.stale:
            lines.append(f"  {e.sha}  bead {e.bead}  ({e.note})")
    return "\n".join(lines)


def check(
    rev_range: str | None = None,
    ledger_path: pathlib.Path | None = None,
    repo_root: pathlib.Path | None = None,
) -> int:
    root = repo_root or _REPO_ROOT
    path = ledger_path or DEFAULT_LEDGER_PATH

    newest = newest_client_tag(repo_root=root)
    if newest is GIT_UNAVAILABLE:
        print(
            "WIRE-CONTRACT CHECK FAILED: could not read v* tags from git. "
            "Cannot verify -- treat as a failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    if newest is None and rev_range is None:
        print(
            "WIRE-CONTRACT CHECK FAILED: zero v* tags visible. Either the "
            "checkout has no tags (CI: fetch-tags: true) or the tag "
            "namespace changed.",
            file=sys.stderr,
        )
        return 2

    resolved_range = rev_range or f"{newest}..HEAD"
    try:
        flagged = flagged_commits(resolved_range, repo_root=root)
    except RuntimeError as exc:
        print(f"WIRE-CONTRACT CHECK FAILED: {exc}", file=sys.stderr)
        return 2

    ledger = parse_ledger(path)
    result = evaluate(flagged, ledger, newest, repo_root=root)
    if result is GIT_UNAVAILABLE:
        print(
            "WIRE-CONTRACT CHECK FAILED: could not verify ledger-entry "
            "staleness against git. Treat as a failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2

    if not result.ok:
        print(
            _format_report(result, resolved_range, str(newest), path),
            file=sys.stderr,
        )
        return 1

    print(
        f"wire-contract ledger clean: {len(flagged)} both-halves commit(s) "
        f"in {resolved_range}, all declared and current against {path}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rev-range",
        default=None,
        help="Commit range to scan (default: <newest published v* tag>..HEAD).",
    )
    parser.add_argument(
        "--ledger",
        type=pathlib.Path,
        default=None,
        help=f"Ledger path (default: {DEFAULT_LEDGER_PATH}).",
    )
    args = parser.parse_args(argv)
    return check(rev_range=args.rev_range, ledger_path=args.ledger)


if __name__ == "__main__":
    sys.exit(main())
