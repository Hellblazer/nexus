# SPDX-License-Identifier: AGPL-3.0-or-later
"""A release-only artifact must never grep for a log-event / structured
literal that nothing in production still emits.

Instance of nexus-1e2eh ("release-only procedures rot silently"), widening
the class ``test_release_artifact_verb_rot.py`` already mechanizes for ONE
token type (a deleted `nx <verb>`) to a SECOND: a deleted structlog event
name or slf4j ``event=`` literal a gate greps for.

WHY THIS IS THE SAME DEFECT, NOT A DIFFERENT ONE: both are an artifact
asserting on a symbol the code stopped producing. A release-only e2e gate is
exercised rarely (once per cut, sometimes less), so the producer can be
deleted or renamed and the gate keeps "passing" -- not because the thing it
checks is healthy, but because the assertion can no longer fail. This is the
exact vacuity shape nexus-vw594 and the 2026-08-10 release-gate audit both
independently rediscovered.

TWO LIVE ESCAPES motivated this, both confirmed at develop a7553c88 by
exhaustive inverse-grep before this module existed:

  * nexus-xm0cp: ``tests/e2e/migration-rehearsal/rehearse_shakeout.sh``
    greps ``dual_write_failed`` twice. The chash dual-write hook that used
    to emit it was retired at RDR-187 (``src/nexus/hook_registry.py``); the
    string now has ZERO producers anywhere in ``src/`` or ``service/``, so
    both assertions are permanent, silent passes.
  * nexus-x8fuq: a sibling escape in ``tests/e2e/upgrade-shakeout.sh`` (a
    demoted-*verb*-shaped regex, not a log event -- covered by
    ``test_release_artifact_verb_rot.py``'s class instead, not this one).

EXTRACTION (design decision 1 -- precision over recall; a noisy lint gets
muted, and this repo had one false vacuity finding land the same day this
module was filed). A candidate is a quoted string literal that is the FIRST
argument ``grep`` matches against on a non-comment line of a swept file, and
that literal is either:

  1. ``event=<token>`` shaped -- the slf4j convention every Java log call in
     ``service/`` uses (``log.info("event=combined_write_embed_partition
     ...")``), and the one the warm-reindex incident used
     (``event=upsert_embed_skipped``). The token after ``event=`` is
     extracted.
  2. A BARE snake_case identifier matching ``^[a-z][a-z0-9]*(_[a-z0-9]+)+$``
     -- at least two underscore-joined words, all lowercase. This is not a
     structlog-specific convention (Python's ``KeyValueRenderer`` DOES
     render ``event=...`` too, per ``logging_setup.py``, but gates grep the
     bare event name directly, e.g. ``grep -q "dual_write_failed"``, not the
     rendered ``event=`` form) -- it is the shape every real event name and
     every real structured-JSON-field name observed in this repo happens to
     share (``dual_write_failed``, ``docs_for_chashes_failed``,
     ``chunk_flush_complete``, ``schema_changeset_count``). Requiring the
     underscore is what keeps this from matching ordinary prose words
     (``builtin``, ``installed``) the way the sibling module's verb regex
     has its own noise allowlist for.

``awk`` was named as a candidate extraction site in the originating bead;
checked against the current corpus and found zero instances of an
event-shaped literal driving an ``awk`` pattern -- kept out of scope rather
than building unverifiable parsing for a case that does not exist today.

SCOPE SWEPT: ``tests/e2e/**/*.sh``, ``scripts/**/*.sh`` (widened 2026-08-10,
nexus-xm0cp Finding 3 -- the original ``scripts/*.sh`` top-level-only scope,
copied from the sibling module's own nexus-zmfan precedent without
independently re-checking whether THIS module's corpus actually matched
that precedent, missed a real hit one directory deeper:
``scripts/rdr152-sandbox/prod-copy.sh``, see the ``_DEAD_EXCLUSION_FILTERS``
section below), and ``service/native-smoke.sh`` explicitly
(release-workflow-only, outside the ``tests/e2e`` glob). ``scripts/**/*.sh``
is a strict superset of the old ``scripts/*.sh`` (Python's ``**`` matches
zero-or-more directories), so nothing already swept is dropped. NOT swept:
skill markdown, workflow YAML, ``conexus/hooks/scripts/*.sh``,
``conexus/README.md`` -- the sibling module's remaining surfaces prescribe
*commands* to run, not log-output assertions; the one hooks-script hit found
during reconnaissance (``stop_verification_hook.sh`` grepping ``bd``'s own
``in_progress`` status word) is a third-party CLI's status text, not
something ``src/`` or ``service/`` ever produces, and does not belong in a
producer search scoped to this repo's own log/API surfaces.

TWO FILES ARE DELIBERATELY EXCLUDED FOR NOW (``_IN_FLIGHT_EXCLUDE``, not
silently -- see that table): ``tests/e2e/warm-reindex-skip-gate.sh`` and
``tests/e2e/local-index-memory-gate.sh`` are being actively edited by a
sibling agent in this same session under nexus-acvi7 (the CombinedWriteService
existence-partition observability gap this exact class predicts). Sweeping a
file mid-edit races a moving target and this module's own hard constraint is
not to touch either file; the exclusion is temporary and named, not a
permanent carve-out.

PRODUCER SEARCH (design decision 2). A single \\b-bounded substring search
over the concatenated text of every ``src/nexus/**/*.py`` file (Python,
structlog: an event name is always a literal first positional arg, or an
``event=``/``log_event=`` kwarg value -- see ``store_hook.py``,
``storage_service_daemon.py``) and every ``service/src/main/java/**/*.java``
file (Java, slf4j: the token appears literally after ``event=`` inside the
log-call format string, e.g. ``NexusService.java``, ``Main.java``,
``CombinedWriteService.java``). One regex, one corpus, both languages,
because a real producer in this codebase always spells the token out in
full somewhere in its own source -- neither side builds an event name via
string concatenation for any token this module has ever had to check.
``service/src/test/**`` and ``tests/**`` are excluded from the producer
corpus on purpose: a token mentioned only in a test does not mean
production emits it.

KNOWN LIMITATION, not silently swept under: a handful of call sites DO
build an event name dynamically (``f"{log_event}_malformed_chash"`` in
``catalog/store_hook.py``, ``f"{tier}_install_activation_not_found"`` in
``daemon/installer.py``, a bare ``event`` variable in
``upgrade_ladder/runner.py``). A static \\b-bounded literal search cannot
see through these. None of today's real candidates collide with one (see
this module's own violation-sweep report) -- if a future gate greps for a
dynamically-constructed event name and trips a false positive, the fix is a
new reasoned ``_FALSE_CANDIDATE_ALLOWLIST`` entry (design decision 3),
never silently narrowing the producer regex to paper over one call site.

SECOND KNOWN LIMITATION (nexus-xm0cp Finding 4, disclosed rather than left
silent -- the mirror image of the f-string false-NEGATIVE above): the
producer corpus (:func:`_producer_corpus_text`) is the RAW file text of
every swept ``src/``/``service/`` source file, comments and docstrings
included, not just executable log-call sites. A stray comment or docstring
that merely MENTIONS a retired event name (e.g. this very module's own
docstring, which is why it lives in ``tests/`` and is excluded from the
producer glob) would register as a false "live producer", masking real
rot -- a false POSITIVE where the f-string case above is a false NEGATIVE.
Not fixed here: distinguishing an executable log call from a comment would
require a real parser per language (tokenizing/AST-walking two languages)
for a class of failure that has not yet been observed in practice (the two
literal-searching sibling modules in this repo, this one and the verb-rot
module, have both shipped on substring search without incident) --
building that machinery preemptively would be exactly the kind of
un-evidenced preventive scope this repo's own review discipline pushes
back on. If a future gate hit is ever traced to a comment-only mention
rather than a real call site, that is the trigger to build the real
parser, not a hint to add another allowlist entry (an allowlist entry
would hide the SYMPTOM at the gate that found it, not the mechanism that
produces false positives at every OTHER gate this corpus backs).

THIRD DESIGN DECISION (nexus-xm0cp Finding 3, the ``grep -v`` exclusion-
filter class): ``scripts/rdr152-sandbox/prod-copy.sh`` pipes three ETL
subcommands through ``grep -v "row_failed" | head -20`` to trim per-row
failure noise before capping the tail. ``row_failed`` has zero producers
in ``src/`` or ``service/`` -- genuine rot, not an extractor false-positive
-- but this is a DIFFERENT failure shape than every other candidate this
module has ever flagged: a dead token in a positive assertion
(``grep -q "x"``) is a permanent, silent PASS -- decorative but
misrepresenting a real check. A dead token in a ``grep -v`` NOISE FILTER is
merely INERT: the filter excludes nothing, so the unfiltered output simply
flows through to ``head -20`` unchanged -- observable directly in the
script's own output, not a manufactured confidence signal. Investigating
further: the underlying ``nx storage migrate`` subcommand these three
calls invoke does not exist AT ALL any more (confirmed live,
``nx storage migrate --help`` -> ``Error: No such command 'storage'`` --
RDR-155 P4b retired the whole migrate CLI surface), so this ETL block is
pre-existing, unrelated dead-CLI-verb rot (``test_release_artifact_verb_
rot.py``'s class, not this module's) that predates and sits outside
nexus-xm0cp's remediation scope. Rather than silently narrow this module's
guard to ignore ``grep -v`` in general (which WOULD blind it to a future
dead token in a real exclusion-driven safety filter, a case this repo has
not yet needed but should not pre-emptively give up on catching) or
silently drop these three hits, they are named, reasoned, and pinned in
``_DEAD_EXCLUSION_FILTERS`` below -- the same RATCHET shape as
``_FALSE_CANDIDATE_ALLOWLIST``, but semantically distinct: entries there
claim "not actually a log-event literal"; entries here claim "IS a dead
log-event literal, but inert-filter rot, not false-PASS rot, and out of
this bead's scope to fix". The staleness test for this table additionally
confirms every exempted candidate is still marked ``is_exclusion=True`` --
an entry can never be (ab)used to hide a genuine dead positive-assertion.

ALLOWLIST MECHANISM (design decision 4 -- the RATCHET pattern already
proven by ``test_mode_declarations_are_explicit.py`` / the sibling verb-rot
module's own tables, never a bare grandfather-everything allowlist):
``_FALSE_CANDIDATE_ALLOWLIST`` requires a reason per entry AND an exact
``_ALLOWLIST_CEILING`` that must be consciously bumped (or lowered) in the
same diff that adds (or removes) an entry --
``test_false_candidate_allowlist_is_not_stale`` enforces both the ceiling
and that the exempted token is STILL a real extracted candidate (not dead
weight silencing nothing). ``_IN_FLIGHT_EXCLUDE`` is the same shape, scoped
to the temporary file-level carve-out above, with its own ceiling test.

BUCKET (design decision 5): ``-m lint``. This census walks the filesystem
directly (``Path.glob``), so it is invocation-independent -- the same
property ``test_no_new_sqlite.py`` / ``test_storage_boundary_lint.py`` rely
on to be safely lint-bucketed (see ``test_mode_declarations_are_explicit.py``'s
docstring for the CONTRASTING case, where lint-bucketing silently blinded a
census keyed on ``pytest``'s own fixture-resolution machinery -- not
applicable here, there is no such dependency). Per AGENTS.md's CI Cost
Discipline, this keeps the sweep out of the hot per-push loop while still
running on every PR via the dedicated ``test-lint`` job. NOTE:
``test_release_artifact_verb_rot.py`` itself is NOT lint-bucketed today
(runs in the default hot loop) -- an existing inconsistency, not something
this module's placement needs to match; both are equally correct choices
for a filesystem-walking census, and reconciling the two is a separate,
out-of-scope decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent

_SH_GLOBS = ("tests/e2e/**/*.sh", "scripts/**/*.sh")
_EXTRA_SH_FILES = ("service/native-smoke.sh",)

_PY_SRC_GLOB = "src/nexus/**/*.py"
_JAVA_SRC_GLOB = "service/src/main/java/**/*.java"

#: relative-path -> reason. Files temporarily out of scope because a sibling
#: agent is actively editing them in this same session -- see module
#: docstring. NOT a permanent carve-out; ceiling below must move (up or
#: down) consciously.
_IN_FLIGHT_EXCLUDE: dict[str, str] = {
    "tests/e2e/warm-reindex-skip-gate.sh": (
        "nexus-acvi7 (open P1, active this session): CombinedWriteService's "
        "existence-partition path emits no observability at all and the gate "
        "cannot currently be made green. A sibling agent is editing this exact "
        "file this session; sweeping it here would race a moving target, and "
        "this module's own hard constraint is not to touch it. Remove this "
        "exclusion once nexus-acvi7 lands and re-sweep."
    ),
    "tests/e2e/local-index-memory-gate.sh": (
        "Concurrently edited alongside warm-reindex-skip-gate.sh in this same "
        "session under the same nexus-acvi7 remediation. Same TOCTOU "
        "rationale as that entry."
    ),
}
_IN_FLIGHT_EXCLUDE_CEILING = 2

#: ``grep``'s first quoted-literal argument on a line, stopping at the next
#: ``|``/``;``/newline so a piped second ``grep`` on the same line is a
#: separate match, not swallowed into this one. Group 1 captures the flags/
#: args segment BETWEEN ``grep`` and the literal (used to detect ``-v``,
#: see ``_GREP_INVERT_FLAG_RE`` below); group 2 is the literal itself.
_GREP_LITERAL_RE = re.compile(r"\bgrep\b([^|;\n]*?)[\"']([^\"']+)[\"']")

#: Detects an invert-match flag (``-v``, a combined short cluster like
#: ``-qv``/``-vqE``, or the GNU long form ``--invert-match``) inside a
#: grep flags/args segment. Grep has no other short flag spelled with the
#: letter ``v`` (``-v`` is unambiguous), so a bare substring-in-token check
#: is sufficient given this module's existing precision-over-recall
#: posture -- see Finding 3 design decision in the module docstring.
_GREP_INVERT_FLAG_RE = re.compile(r"(?:^|\s)(--invert-match|-[a-zA-Z]*v[a-zA-Z]*)(?=\s|$)")

#: The slf4j/production convention: ``event=<token>`` embedded inside a
#: larger literal (``event=upsert_embed_skipped``, possibly followed by more
#: pattern text after a space).
_EVENT_PREFIX_RE = re.compile(r"\bevent=([A-Za-z][A-Za-z0-9_]*)")

#: A bare literal that LOOKS like a log-event / structured-field name: pure
#: lowercase, at least two underscore-joined words. See module docstring for
#: why the underscore requirement is load-bearing for precision.
_BARE_EVENT_SHAPE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


@dataclass(frozen=True)
class Candidate:
    file: str
    line_no: int
    token: str
    line: str
    #: True when the matched ``grep`` invocation carries an invert-match
    #: flag (``-v``/``--invert-match``) -- i.e. this candidate is an
    #: EXCLUSION filter (noise-reduction), not a positive presence
    #: assertion. See Finding 3 design decision / ``_DEAD_EXCLUSION_FILTERS``.
    is_exclusion: bool = False


#: (relative-path, token) -> reason. General-purpose exemption for a
#: genuinely non-log-event candidate the shape-based extractor cannot
#: distinguish from a real one. See module docstring, design decision 3.
_FALSE_CANDIDATE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh", "small_sentinel"): (
        "small_sentinel is the synthetic fixture module's filename (see "
        "rehearse_shakeout_e2e.sh's own comment above the hit: 'query the "
        "real English phrase that genuinely appears in the sentinel "
        "function's docstring'), asserted against `nx search` RESULT "
        "CONTENT to prove semantic retrieval works -- not a log event. "
        "There is and never was a producer for it in src/ or service/; "
        "confirmed zero occurrences repo-wide outside this one assertion "
        "and the fixture file itself."
    ),
}
_ALLOWLIST_CEILING = 1


#: (relative-path, token) -> reason. Distinct from _FALSE_CANDIDATE_ALLOWLIST
#: above: these ARE real dead log-event literals (genuine rot, not an
#: extractor false-positive) but appear inside a `grep -v ... | head`
#: NOISE-REDUCTION filter, not a presence/PASS assertion. See the module
#: docstring's Finding-3 design decision for the full reasoning. Entries
#: here are cross-checked (test_dead_exclusion_filters_is_not_stale) to
#: still be `is_exclusion=True` candidates, so this table cannot be misused
#: to hide a genuine dead `grep -q`/positive-assertion rot.
_DEAD_EXCLUSION_FILTERS: dict[tuple[str, str], str] = {
    ("scripts/rdr152-sandbox/prod-copy.sh", "row_failed"): (
        "grep -v \"row_failed\" | head -20 is a noise-reduction filter "
        "ahead of the ETL output tail, not a presence assertion -- a dead "
        "token here makes the filter INERT (nothing gets excluded, so "
        "head -20 sees the raw output unfiltered), never a false PASS the "
        "way a dead `grep -q` would be. The underlying `nx storage "
        "migrate` command these three calls invoke does not exist at all "
        "any more (RDR-155 P4b retired the whole migrate CLI surface; "
        "confirmed live -- `nx storage migrate --help` -> \"Error: No "
        "such command 'storage'\"), so this whole ETL block is "
        "pre-existing, unrelated dead-CLI-verb rot (test_release_"
        "artifact_verb_rot.py's class, not this module's) outside "
        "nexus-xm0cp's remediation scope. Disclosed and pinned here "
        "rather than silently dropped or silently fixed out-of-scope."
    ),
}
_DEAD_EXCLUSION_FILTERS_CEILING = 1


def _sh_files() -> list[Path]:
    paths: list[Path] = []
    for pattern in _SH_GLOBS:
        paths.extend(sorted(REPO_ROOT.glob(pattern)))
    paths.extend(REPO_ROOT / p for p in _EXTRA_SH_FILES)
    out: list[Path] = []
    for p in paths:
        rel = str(p.relative_to(REPO_ROOT))
        if rel in _IN_FLIGHT_EXCLUDE:
            continue
        out.append(p)
    return out


def _extract_candidates(path: Path) -> list[Candidate]:
    rel = str(path.relative_to(REPO_ROOT))
    found: list[Candidate] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        if "grep" not in raw_line:
            continue
        for m in _GREP_LITERAL_RE.finditer(raw_line):
            flags_segment, literal = m.group(1), m.group(2)
            evm = _EVENT_PREFIX_RE.search(literal)
            if evm:
                token = evm.group(1)
            elif _BARE_EVENT_SHAPE_RE.match(literal):
                token = literal
            else:
                continue
            is_exclusion = bool(_GREP_INVERT_FLAG_RE.search(flags_segment))
            found.append(Candidate(
                file=rel, line_no=line_no, token=token, line=stripped[:160],
                is_exclusion=is_exclusion,
            ))
    return found


def _all_candidates() -> list[Candidate]:
    out: list[Candidate] = []
    for p in _sh_files():
        out.extend(_extract_candidates(p))
    return out


@lru_cache(maxsize=1)
def _producer_corpus_text() -> str:
    """Concatenated text of every production-source file this module
    trusts as a producer surface (Python ``src/nexus``, Java
    ``service/src/main/java``). Read once per process; ~350 files."""
    chunks: list[str] = []
    for p in sorted(REPO_ROOT.glob(_PY_SRC_GLOB)):
        chunks.append(p.read_text(encoding="utf-8", errors="replace"))
    for p in sorted(REPO_ROOT.glob(_JAVA_SRC_GLOB)):
        chunks.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def _has_producer(token: str, corpus: str | None = None) -> bool:
    text = _producer_corpus_text() if corpus is None else corpus
    return re.search(r"\b" + re.escape(token) + r"\b", text) is not None


# ── Non-vacuity ──────────────────────────────────────────────────────────────


#: file -> minimum candidate count, hand-verified against the tree at
#: authoring time (2026-08-10, develop a1b64b3c).
_ANCHOR_MIN_COUNTS: dict[str, int] = {
    # nexus-xm0cp (2026-08-10): dropped from 4 to 2 (dual_write_failed's
    # two candidates DELETED, RDR-187 retired their producer), then back up
    # to 3 (Finding 2's fix added a THIRD grep candidate, `grep
    # "vector_gateway_retry"` in the Phase D log-tail-on-failure branch --
    # a real, live-producer candidate, not a regression). Re-derived
    # programmatically against the tree at this diff via
    # `_extract_candidates`: exactly 3.
    "tests/e2e/migration-rehearsal/rehearse_shakeout.sh": 3,
    "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh": 4,
    "tests/e2e/fresh-install-mvv.sh": 2,
    "service/native-smoke.sh": 3,
    # nexus-xm0cp Finding 3: proves the widened scripts/**/*.sh scope
    # actually reaches this file (previously missed entirely by the
    # top-level-only scripts/*.sh glob). All 3 candidates are the same
    # `row_failed` exclusion-filter token -- see _DEAD_EXCLUSION_FILTERS.
    "scripts/rdr152-sandbox/prod-copy.sh": 3,
}


def test_globs_resolve_to_files() -> None:
    assert len(_sh_files()) >= 40, f"the .sh globs look broken: {len(_sh_files())} files"
    for extra in _EXTRA_SH_FILES:
        assert (REPO_ROOT / extra).is_file(), f"explicitly-swept file moved: {extra}"


def test_producer_corpus_is_not_vacuous() -> None:
    corpus = _producer_corpus_text()
    assert len(corpus) >= 500_000, f"producer corpus looks truncated/broken: {len(corpus)} chars"
    py_files = list(REPO_ROOT.glob(_PY_SRC_GLOB))
    java_files = list(REPO_ROOT.glob(_JAVA_SRC_GLOB))
    assert len(py_files) >= 200, f"{_PY_SRC_GLOB} glob looks broken: {len(py_files)} files"
    assert len(java_files) >= 50, f"{_JAVA_SRC_GLOB} glob looks broken: {len(java_files)} files"


def test_extraction_is_not_vacuous_in_aggregate() -> None:
    total = len(_all_candidates())
    assert total >= 10, (
        f"only {total} log-event-shaped grep candidates extracted across every "
        "swept surface -- the extraction regex likely broke (baseline at "
        "authoring time: 13; re-derived to 16 at nexus-xm0cp's Finding 3 "
        "scope widening + Finding 2's added retry-census grep, 2026-08-10)"
    )


@pytest.mark.parametrize("relpath,minimum", sorted(_ANCHOR_MIN_COUNTS.items()))
def test_anchor_file_extraction_is_not_vacuous(relpath: str, minimum: int) -> None:
    """A hand-verified-nonzero file that suddenly yields zero (or far fewer)
    candidates means the regex broke, not that the file went quiet."""
    path = REPO_ROOT / relpath
    assert path.is_file(), f"anchor file moved: {relpath}"
    found = _extract_candidates(path)
    assert len(found) >= minimum, (
        f"{relpath}: extractor found only {len(found)} candidate(s), expected "
        f">= {minimum}. Found: {[c.token for c in found]}"
    )


def test_producer_search_correctly_rejects_known_dead_token() -> None:
    """Pins the resolver's negative case against the REAL corpus, independent
    of any file scan: RDR-187 retired the chash dual-write hook, so this
    token must never resolve, or the resolver itself is broken."""
    assert not _has_producer("dual_write_failed"), (
        "dual_write_failed unexpectedly has a producer -- either a producer "
        "was re-added (update nexus-xm0cp) or the resolver regressed"
    )


def test_producer_search_correctly_accepts_known_live_tokens() -> None:
    for token in (
        "docs_for_chashes_failed",
        "chunk_flush_complete",
        "http_vector_upsert_chunks_request",
        "manifest_hook_batch_missing_doc_identity",
        "embedding_mode_banner",
        "egress_proxy_configured",
        "schema_changeset_count",
    ):
        assert _has_producer(token), f"{token} unexpectedly has no producer"


# ── Allowlists ───────────────────────────────────────────────────────────────


def test_in_flight_exclude_is_not_stale() -> None:
    assert len(_IN_FLIGHT_EXCLUDE) == _IN_FLIGHT_EXCLUDE_CEILING, (
        "_IN_FLIGHT_EXCLUDE ceiling must be bumped (or lowered) in the SAME "
        "diff as an entry change -- see module docstring"
    )
    for relpath, reason in _IN_FLIGHT_EXCLUDE.items():
        assert reason.strip(), f"_IN_FLIGHT_EXCLUDE[{relpath!r}] has no reason"
        assert (REPO_ROOT / relpath).is_file(), (
            f"_IN_FLIGHT_EXCLUDE names a file that no longer exists: {relpath}. Remove it."
        )


def test_dead_exclusion_filters_is_not_stale() -> None:
    """Shape, truth, AND category: every entry needs a reason, a real
    file, the token must still actually be extracted there, AND it must
    still be marked is_exclusion=True -- if a future edit turns one of
    these into a positive assertion (grep -q), this table must stop
    covering it (the entry would then be hiding a real false-PASS, exactly
    what _DEAD_EXCLUSION_FILTERS is not for)."""
    assert len(_DEAD_EXCLUSION_FILTERS) == _DEAD_EXCLUSION_FILTERS_CEILING, (
        "_DEAD_EXCLUSION_FILTERS ceiling must be bumped (or lowered) in the "
        "SAME diff as an entry change -- see module docstring"
    )
    for (relpath, token), reason in _DEAD_EXCLUSION_FILTERS.items():
        assert reason.strip(), f"_DEAD_EXCLUSION_FILTERS[({relpath!r}, {token!r})] has no reason"
        path = REPO_ROOT / relpath
        assert path.is_file(), (
            f"_DEAD_EXCLUSION_FILTERS names a file that no longer exists: {relpath}. Remove it."
        )
        matching = [c for c in _extract_candidates(path) if c.token == token]
        assert matching, (
            f"{relpath}: allowlisted token {token!r} is no longer extracted as a "
            "candidate there -- the exemption is now silencing nothing. Remove it."
        )
        assert all(c.is_exclusion for c in matching), (
            f"{relpath}: {token!r} now appears in a NON-exclusion (positive-assertion) "
            "grep call -- _DEAD_EXCLUSION_FILTERS only covers grep -v noise filters; "
            "move this to _FALSE_CANDIDATE_ALLOWLIST or fix the gate instead."
        )


def test_false_candidate_allowlist_is_not_stale() -> None:
    """Shape AND truth: every entry needs a reason and a real file, AND the
    exempted token must still actually be an extracted candidate in that
    file -- an allowlist entry silencing nothing (because the line moved or
    the token changed) is dead weight, exactly the failure mode the sibling
    verb-rot module's own staleness fix (2026-07-25) closed for its tables."""
    assert len(_FALSE_CANDIDATE_ALLOWLIST) == _ALLOWLIST_CEILING, (
        "_FALSE_CANDIDATE_ALLOWLIST ceiling must be bumped (or lowered) in "
        "the SAME diff as an entry change -- see module docstring"
    )
    for (relpath, token), reason in _FALSE_CANDIDATE_ALLOWLIST.items():
        assert reason.strip(), f"_FALSE_CANDIDATE_ALLOWLIST[({relpath!r}, {token!r})] has no reason"
        path = REPO_ROOT / relpath
        assert path.is_file(), (
            f"_FALSE_CANDIDATE_ALLOWLIST names a file that no longer exists: {relpath}. Remove it."
        )
        found_tokens = {c.token for c in _extract_candidates(path)}
        assert token in found_tokens, (
            f"{relpath}: allowlisted token {token!r} is no longer extracted as a "
            "candidate there -- the exemption is now silencing nothing. Remove it."
        )


# ── The guard itself ─────────────────────────────────────────────────────────


def test_no_release_artifact_greps_a_dead_log_event() -> None:
    """A release-only e2e gate must never grep for a log-event / structured
    literal that no production code in ``src/`` or ``service/`` still emits.

    A hit here means one of:
      1. A producer genuinely rotted (deleted/renamed) and the gate was not
         swept -- fix the gate (repoint at a live signal, or delete the dead
         assertion).
      2. The literal is a genuine non-log-event false positive of the
         shape-based extractor (e.g. a fixture identifier) -- add a reasoned
         entry to `_FALSE_CANDIDATE_ALLOWLIST` and bump `_ALLOWLIST_CEILING`
         in the same diff.
    """
    offenders: list[Candidate] = []
    for c in _all_candidates():
        if (c.file, c.token) in _FALSE_CANDIDATE_ALLOWLIST:
            continue
        if (c.file, c.token) in _DEAD_EXCLUSION_FILTERS:
            continue
        if not _has_producer(c.token):
            offenders.append(c)

    assert not offenders, (
        "release-only artifact(s) grep for a log-event/structured literal "
        "nothing in src/ or service/ still produces:\n"
        + "\n".join(f"  {o.file}:{o.line_no}: {o.token!r} — {o.line!r}" for o in offenders)
        + "\n\nSee test_no_release_artifact_greps_a_dead_log_event's own docstring "
        "for the two ways to resolve a hit."
    )


# ── Mutation-verify ──────────────────────────────────────────────────────────
#
# Proves the resolver actually distinguishes a live token from a missing
# one, using the corpus text directly (no source-file mutation, no git side
# effects) -- mirrors the sibling verb-rot module's mutation tests.


def test_mutation_a_removed_producer_is_detected() -> None:
    corpus = _producer_corpus_text()
    assert re.search(r"\bchunk_flush_complete\b", corpus), "fixture assumption broken: no baseline hit"
    mutated = corpus.replace("chunk_flush_complete", "chunk_flush_complete_XXRENAMEDXX")
    assert not _has_producer("chunk_flush_complete", corpus=mutated), (
        "resolver did not notice the removed producer"
    )
    # Unmutated view (a fresh call) proves the mutation was local to this test.
    assert _has_producer("chunk_flush_complete", corpus=_producer_corpus_text())


def test_mutation_extraction_detects_grep_invert_flag() -> None:
    """Direct proof the -v / --invert-match detector actually fires, not
    just that it happens not to matter on today's corpus (Finding 3)."""
    assert _GREP_INVERT_FLAG_RE.search(" -v ") is not None
    assert _GREP_INVERT_FLAG_RE.search(" -qvE ") is not None
    assert _GREP_INVERT_FLAG_RE.search(" -Ev ") is not None
    assert _GREP_INVERT_FLAG_RE.search(" --invert-match ") is not None
    assert _GREP_INVERT_FLAG_RE.search(" -qE ") is None
    assert _GREP_INVERT_FLAG_RE.search(" -c ") is None
    # End-to-end through the real extractor: the known live prod-copy.sh
    # hit must be marked is_exclusion, and a positive-assertion candidate
    # (e.g. rehearse_shakeout.sh's docs_for_chashes_failed, `grep -q`) must
    # NOT be.
    prod_copy = REPO_ROOT / "scripts/rdr152-sandbox/prod-copy.sh"
    row_failed_hits = [c for c in _extract_candidates(prod_copy) if c.token == "row_failed"]
    assert row_failed_hits, "expected row_failed to still be extracted from prod-copy.sh"
    assert all(c.is_exclusion for c in row_failed_hits)

    shakeout = REPO_ROOT / "tests/e2e/migration-rehearsal/rehearse_shakeout.sh"
    docs_hits = [c for c in _extract_candidates(shakeout) if c.token == "docs_for_chashes_failed"]
    assert docs_hits, "expected docs_for_chashes_failed to still be extracted from rehearse_shakeout.sh"
    assert not any(c.is_exclusion for c in docs_hits), (
        "docs_for_chashes_failed is a `grep -q` positive assertion -- it must never be "
        "classified as an exclusion filter"
    )


def test_mutation_extraction_rejects_non_event_shaped_literals() -> None:
    """Direct proof the shape filter actually filters, not just that it
    happens not to fire on today's corpus."""
    assert _EVENT_PREFIX_RE.search("event=some_token more text") is not None
    assert _BARE_EVENT_SHAPE_RE.match("dual_write_failed") is not None
    # "a_b" is intentionally ACCEPTED by the shape regex (it IS
    # underscore-joined-words shaped) -- the boundary this regex enforces is
    # the underscore, not a minimum word count.
    assert _BARE_EVENT_SHAPE_RE.match("a_b") is not None
    for non_event in ("Traceback", "T2 database not found", "builtin", "installed"):
        assert _BARE_EVENT_SHAPE_RE.match(non_event) is None, (
            f"{non_event!r} unexpectedly matched the event-shape regex"
        )
