# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bans the retired per-dim table names outside an explicit, reasoned allowlist.

nexus-rmver dim-predicate audit (2026-08-14) found 26 stale (E-class)
references to the three dim-sharded tables RDR-191 Phase 4 collapsed into
``nexus.chunks`` / ``nexus.taxonomy_centroids`` — 3 of which broke a live gate
(split to the P1 bead, nexus-azx14/nexus-7ramw) and the rest were dead-or-
cosmetic debris (retired scripts, one-shot RDR verification scripts, cosmetic
lint fixtures, stale javadoc, stale migration-file headers). This lint is
the audit's ACTION item: the prevention half.

WHAT IS BANNED: ``chunks_384`` / ``chunks_768`` / ``chunks_1024`` /
``taxonomy_centroids_384`` / ``taxonomy_centroids_768`` /
``taxonomy_centroids_1024`` (word-boundary match) anywhere in:

  * ``src/nexus/**/*.py``
  * ``service/src/main/java/**/*.java`` and
    ``service/src/main/resources/db/changelog/**/*.xml`` (the Liquibase
    changelog lives under ``resources``, not ``java``, but is the java-side
    schema-of-record and is exactly where 3 of the 26 E-class sites lived)
  * ``scripts/**/*.sh`` and ``scripts/**/*.py``
  * ``tests/**/*.py``, ``tests/**/*.sh``, ``tests/**/*.java`` (the last glob
    resolves to zero files today — ``tests/`` is Python+shell only — kept
    wired per the bead's literal scope in case a Java test ever lands there)

``service/src/test/java/**`` is deliberately OUT of scope (tracked as its own
follow-up, nexus-a66gd): the bead's scope names ``service/src/main/java/**``
only, not the test tree, and that tree carries 45+ files of its own
legacy-era fixture debt that is a separate, unscoped cleanup.

ALLOWLIST DESIGN — COUNT PINS, NOT FILE-LEVEL EXEMPTIONS (nexus-bxcgh,
substantive-critic round 1, 2026-08-14): the first version of this lint used
a bare ``path -> reason`` file-level exemption. That has ZERO detection power
inside a MIXED-USE file — one that legitimately carries some banned tokens
(straddle-era code, wire-compat labels, historical prose) but could ALSO grow
a genuinely stale one later. The counterfactual replay that found this: the
lint's own docstring claimed it "would have caught every one of the 26
E-class sites, including the P1 gate breakage" — but the ACTUAL P1
(nexus-azx14) was stale post-swap ``chunks_768`` references (``POST_COUNT``,
``VALIDATED``, ``DANGLING`` — querying tables already dropped by RDR-191)
coexisting in the SAME file, ``rehearse_chash_window.sh``, with legitimate
pre-cutover era-guard references (``COLTYPE2``, ``LEGACY_ROWS``,
``STRICT_ROWS`` — checking the ``$OLD_ENGINE_TAG`` pre-cohort schema on
purpose). A file-level allowlist entry for that file would have silenced
BOTH kinds identically — the claim was false for exactly the incident that
motivated the lint.

The fix: every mixed-use file gets a COUNT PIN, not a blanket exemption —
``_COUNT_PINNED_FILE_ALLOWLIST: dict[str, tuple[int, str]]`` records the
CURRENT live hit count alongside the reason. ``test_count_pinned_allowlist_
matches_live_hit_counts`` asserts the live count still equals the pin,
bidirectionally:

  * actual COUNT ABOVE the pin fails as a POSSIBLE NEW STALE REFERENCE — the
    exact azx14 shape, now caught: with a pin at the era-guard baseline, the
    3 additional post-swap hits push the live count 3 OVER the pin and the
    guard fails, naming the file and both counts.
  * actual COUNT BELOW the pin fails as a STALE PIN (the file lost hits —
    e.g. a fix landed — and the pin should be lowered), the same ratchet
    discipline ``test_pipefail_early_exit_consumer_lint.py`` uses for its
    exempt-count ceilings.
  * actual COUNT EQUAL to the pin passes silently.

Only a file where EVERY occurrence is provably frozen — no future edit could
plausibly add a NEW banned token without also rewriting the file wholesale —
stays a bare reason-only exemption in ``_FROZEN_FILE_ALLOWLIST``: the lint
file itself (self-referential by construction) and the RETIRED
``rehearse_guided.sh`` stub (exits 2 before any of its dead per-dim SQL can
run; nexus-8nlj4 owns deleting it, not this lint).

DOCUMENTED RESIDUAL (stated precisely, not glossed over): a count pin is a
CARDINALITY check, not a content check. A 1-FOR-1 SWAP inside a pinned file
— delete one legitimate reference, add one stale reference, same file, same
commit — leaves the total count unchanged and PASSES. This is a real,
accepted gap, not a claimed-fixed one. The stronger option, if this residual
ever bites in practice, is per-line marker comments (e.g. a trailing
``# dimlint: era-guard`` this lint could parse to distinguish legitimate
lines from newly-added ones token-by-token instead of by count) — not
implemented here because no incident has yet needed it and the added
authoring overhead (every legitimate line in ~40 files re-tagged) is not
justified against a hypothetical.

DIRECTORY ALLOWLIST HAS THE SAME GAP, ACCEPTED DIFFERENTLY (code-review-expert
round 1, Important #2): ``_DIR_ALLOWLIST``'s changelog-directory entry has
ZERO detection power for a NEW stale reference landing in a NEW changelog
file under that directory — unlike a count pin, there is no per-file
cardinality tracked at the directory grain. This is accepted, not fixed,
because (a) every SHIPPED changeset's SQL body is Liquibase-checksummed and
therefore immutable after it merges — the staleness this lint exists to
catch cannot be introduced into a shipped changeset without a checksum
failure blocking deploy independently of this lint, and (b) a NEW changelog
file naming a retired per-dim table is exactly the class of thing normal
code review catches (it would be reviewing brand-new SQL, not silently-aged
prose). Fighting XML parsing to get directory-grain counts buys detection
power over a surface two OTHER mechanisms already cover.

Conexus analogue (named by the original nexus-rmver audit, still apt): this
is the same defect class as the conexus bloat-probe silent-narrowing bug — a
detector whose coverage quietly shrinks until it watches nothing. The
file-level-allowlist version of THIS lint was itself a live instance of that
exact class (zero power inside a mixed-use file); the count-pin upgrade is
the fix, mechanized the same way the bloat-probe's own fix was: bound the
exemption's SIZE, not just declare it exempt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent

#: The retired per-dim table names, word-boundary matched. Underscore is a
#: word character, so `\b` before "chunks_384"/etc. and after the trailing
#: digit run correctly excludes both a longer identifier this is a substring
#: of (`prechunks_384x`) and a decimal-adjacent false match, without any
#: special-casing (unlike a bare `\b384\b`, which this module does NOT use —
#: it matches the whole banned identifier, not just the trailing digits).
_BANNED_RE = re.compile(
    r"\b("
    r"chunks_384|chunks_768|chunks_1024|"
    r"taxonomy_centroids_384|taxonomy_centroids_768|taxonomy_centroids_1024"
    r")\b"
)

_SRC_GLOBS = ("src/nexus/**/*.py",)
_JAVA_GLOBS = (
    "service/src/main/java/**/*.java",
    "service/src/main/resources/db/changelog/**/*.xml",
)
_SCRIPTS_GLOBS = ("scripts/**/*.sh", "scripts/**/*.py")
_TEST_GLOBS = ("tests/**/*.py", "tests/**/*.sh", "tests/**/*.java")

_ALL_GLOBS = _SRC_GLOBS + _JAVA_GLOBS + _SCRIPTS_GLOBS + _TEST_GLOBS

# ── Allowlists (every entry requires a reason) ──────────────────────────────

#: directory-relpath (prefix match, POSIX-separated) -> reason. For a
#: directory it is not worth fighting a format-specific parser (XML) to get
#: per-file counts — see the module docstring's "DIRECTORY ALLOWLIST HAS THE
#: SAME GAP, ACCEPTED DIFFERENTLY" section for why that gap is accepted here
#: rather than closed.
_DIR_ALLOWLIST: dict[str, str] = {
    "service/src/main/resources/db/changelog": (
        "Liquibase changelog XMLs: rollback blocks, top-of-file headers "
        "narrating the RDR-191 unification (vectors-004/taxonomy-007/"
        "vectors-005), and chash_conformance_report's DELIBERATE wire-compat "
        "table_name labels (vectors-005) that name relations which no "
        "longer exist post-unify, on purpose, so the diagnostic surface "
        "stays legible across the migration boundary — "
        "tests/db/test_du2dw_chash_conformance_report_engine.py and "
        "tests/db/test_rdr182_mvv_no_content_read.py assert those labels "
        "verbatim; do not 'fix' them without a wire-compat decision reversal. "
        "ZERO detection power for a NEW stale reference in a NEW changelog "
        "file (no per-file count tracked at directory grain) — accepted, "
        "not fixed: every SHIPPED changeset body is Liquibase-checksummed "
        "and immutable post-merge (a hand-edit to dodge this lint fails the "
        "checksum independently), and a brand-new changelog file naming a "
        "retired table is exactly what ordinary code review already catches "
        "on new SQL. See module docstring."
    ),
}

#: file-relpath -> reason. Reserved for files where EVERY occurrence is
#: provably frozen — no plausible future edit adds a NEW banned token without
#: rewriting the file wholesale. Anything that could plausibly grow a new,
#: genuinely-stale reference belongs in _COUNT_PINNED_FILE_ALLOWLIST instead
#: — see module docstring's "ALLOWLIST DESIGN" section.
_FROZEN_FILE_ALLOWLIST: dict[str, str] = {
    "tests/test_stale_dim_table_reference_lint.py": (
        "The lint file itself: the ban regex and every allowlist entry's "
        "reason necessarily name the banned identifiers verbatim. "
        "Self-referential by construction — a count pin on itself would be "
        "circular (the pin's own digits change the count it pins)."
    ),
    "tests/e2e/migration-rehearsal/rehearse_guided.sh": (
        "RETIRED script (exits 2 at its own top-of-file --help guard, "
        ":75-79); its 13 per-dim SQL sites below the guard are dead code, "
        "unreachable at runtime, left as historical debris per nexus-rmver "
        "item 6 (nexus-8nlj4 owns deleting or repointing this file; not "
        "this bead). Frozen: the guard makes every line below it dead, so "
        "a new stale reference added below the guard is exactly as "
        "unreachable as the 13 already there — there is no live-vs-stale "
        "distinction left to lose by not counting."
    ),
}

#: file-relpath -> (expected_hit_count, reason). MIXED-USE files: some
#: occurrences are legitimate (straddle-era code, wire-compat labels,
#: historical prose) and the file could plausibly grow a genuinely stale one
#: later. The pin is the live count at authoring/audit time — a live count
#: ABOVE the pin means something NEW landed (review it: legitimate bump ->
#: raise the pin with a reason; stale -> fix it); BELOW means the pin is
#: stale (something was removed; lower it). See module docstring.
_COUNT_PINNED_FILE_ALLOWLIST: dict[str, tuple[int, str]] = {
    # ── src/nexus: straddle-era functional code (still must recognize/handle
    #    the legacy per-dim tables for a pre-unify install mid-upgrade) ────
    "src/nexus/health.py": (
        4,
        "Straddle-era health checks: doctor-path probes that must still "
        "recognize a pre-unify install's chunks_384/768/1024 tables while "
        "the unified-vs-legacy era is ambiguous. Named explicitly in the "
        "nexus-rmver audit as straddle-era handling, not stale debris."
    ),
    "src/nexus/upgrade_ladder/rungs/chash_rekey.py": (
        4,
        "The upgrade-ladder rung that performs the chunks_384/768/1024 -> "
        "nexus.chunks rekey itself; it must name the source tables to "
        "migrate off them. Named explicitly in the nexus-rmver audit as "
        "straddle-era handling, not stale debris."
    ),
    "src/nexus/db/chash_tables.py": (
        17,
        "Legacy-era chash-bearing-table emitters (CHASH_BEARING_TABLES, "
        "legacy_chash_conformance_statements): their entire job is naming "
        "the OLD per-dim tables for straddle-window diagnostics and the "
        "upgrade rung. Named explicitly in the nexus-rmver audit as "
        "legacy-era emitters, not stale debris."
    ),
    "src/nexus/collection_rename.py": (
        3,
        "Straddle-window wire-compat fallback: reads the engine's unified "
        "'taxonomy_centroids' cascade-response key, falling back to summing "
        "the three legacy per-dim keys only if the unified key is absent "
        "(relevant only transiently, if this Python repoint and the Java "
        "CatalogRepository repoint land in different commits of the same "
        "batch). Self-documenting in-file, matches collection_purge.py."
    ),
    "src/nexus/db/collection_purge.py": (
        3,
        "Same straddle-window wire-compat fallback as collection_rename.py "
        "(unified 'taxonomy_centroids' key with a legacy-per-dim-key sum "
        "fallback) — see that file's allowlist reason."
    ),
    "src/nexus/remediation/playbook.py": (
        3,
        "The chash-poison forensics playbook's agent-facing prose literally "
        "explains the pre-unify STRADDLE window to the diagnosing agent "
        "('chunks_384/chunks_768/chunks_1024 still per-dim' — read the "
        "legacy-era statements instead) so the identifiers must appear "
        "verbatim in this live, rendered explanation text."
    ),
    # ── src/nexus: pure historical prose (javadoc/docstring/comment only,
    #    no live per-dim SQL target remains in the file) ──────────────────
    "src/nexus/search_engine.py": (
        1,
        "Comment narrating the pre-unify per-dim dispatch PgVectorRepository "
        "used to do; no live per-dim reference remains here."
    ),
    "src/nexus/catalog/http_catalog_client.py": (
        2,
        "Docstrings narrating 'chunks — the RDR-191 unified relation, was "
        "chunks_384/768/1024' for reader context; historical only."
    ),
    "src/nexus/db/http_vector_client.py": (
        3,
        "Docstring narrating the pre-unify per-collection UNION query shape "
        "over chunks_384/768/1024; historical only."
    ),
    "src/nexus/db/collection_state.py": (
        3,
        "Comment narrating which physical rows a state transition touches, "
        "phrased against the pre-unify per-dim table names; historical only."
    ),
    "src/nexus/db/t2/http_taxonomy_store.py": (
        1,
        "Comment narrating which legacy centroid table a bge-768/voyage-1024 "
        "collection used to land in; historical only."
    ),
    "src/nexus/commands/taxonomy_cmd.py": (
        1,
        "Docstring narrating the pre-unify centroid persistence target; "
        "historical only."
    ),
    "src/nexus/remediation/sql_lint.py": (
        1,
        "Comment citing 'SELECT content FROM chunks_768' as the worked "
        "example the fail-closed unqualified-target rule was written "
        "against (mirrors tests/remediation/test_sql_lint.py's own "
        "unqualified-target fixtures); the actual guard is generic and "
        "targets no specific table."
    ),
    # ── service/src/main/java: straddle-era functional (constraint-name
    #    mapping used to validate a pre-unify install's own constraints) ──
    "service/src/main/java/dev/nexus/service/db/SchemaMigrator.java": (
        4,
        "CHASH_LEN_CONSTRAINTS maps each legacy per-dim table's own "
        "chash-length constraint name for straddle-era VALIDATE CONSTRAINT "
        "handling on a pre-unify install; functional, not stale."
    ),
    # ── service/src/main/java: pure historical javadoc/comment ──────────
    "service/src/main/java/dev/nexus/service/vectors/TaxonomyCentroidRepository.java": (
        1,
        "Javadoc narrating the pre-unify 'three per-dim tables' shape "
        "(line ~30); historical only. (Separately, this same file's "
        "dimensionProbe javadoc had a stale 'count() over-counts' claim "
        "fixed by nexus-evqoc; that fix touches no banned identifier so "
        "does not move this pin.)"
    ),
    "service/src/main/java/dev/nexus/service/vectors/PgVectorRepository.java": (
        11,
        "OUT OF SCOPE for this diff: concurrently owned by a sibling agent "
        "doing dim-predicate work on this exact file — its count moved 8 -> "
        "11 between this lint's first draft and this remediation pass, "
        "confirming the file is genuinely in flux. Pin is the count AS OF "
        "THIS COMMIT ONLY, explicitly NOT verified line-by-line the way "
        "every other pinned file was — do not treat a future re-pin here as "
        "evidence of review the way it would be elsewhere. Re-audit "
        "line-by-line once the sibling's work lands (nexus-evqoc follow-up "
        "if any genuinely-stale line surfaces); until then this pin exists "
        "only so this lint does not block on a file it must not touch, and "
        "it WILL re-trip (correctly) the moment that count changes again."
    ),
    "service/src/main/java/dev/nexus/service/db/ChashSqlIdioms.java": (
        3,
        "Javadoc narrating the RDR-191 unification (three occurrences, all "
        "'{@code nexus.chunks_384/768/1024} collapsed into...'); historical "
        "only."
    ),
    "service/src/main/java/dev/nexus/service/db/RekeyOps.java": (
        5,
        "Javadoc/comments narrating the RDR-191 unification and the "
        "pre-unify per-dim EXISTS shape the rekey logic replaced; "
        "historical only."
    ),
    "service/src/main/java/dev/nexus/service/db/ChashCensus.java": (
        2,
        "Comments narrating the RDR-191 unification of the three per-dim "
        "chash-bearing tables; historical only."
    ),
    "service/src/main/java/dev/nexus/service/db/TenantScope.java": (
        1,
        "Javadoc narrating the RDR-191 unification; historical only."
    ),
    "service/src/main/java/dev/nexus/service/db/ChashRepository.java": (
        2,
        "Javadoc narrating that the (pre-unify) chunks_384/768/1024 tables "
        "ARE the chash-keyed store; historical only."
    ),
    "service/src/main/java/dev/nexus/service/db/CatalogRepository.java": (
        23,
        "Javadoc/comments: an incident postmortem citing 'chunks_1024 alone "
        "took 195s to VACUUM' (a historical performance number, not a live "
        "reference) plus RDR-191 unification narration; historical only, "
        "verified no live per-dim SQL string literal remains (only one "
        "quoted occurrence exists in the file and it is itself inside a "
        "// comment)."
    ),
    "service/src/main/java/dev/nexus/service/db/StagingPromoteOps.java": (
        2,
        "Comments narrating the pre-unify per-dim dispatch shape "
        "('hardcoded to chunks_768/dim=768', '(chunks_384|768|1024)'); "
        "historical only."
    ),
    # ── tests: wire-compat / straddle-era fixtures mirroring the src/java
    #    files above ──────────────────────────────────────────────────────
    "tests/test_collection_purge.py": (
        9,
        "Fixture dicts for collection_purge.py's straddle-window wire-compat "
        "fallback (unified key absent -> sum the three legacy per-dim "
        "keys); mirrors that file's own allowlist reason."
    ),
    "tests/test_collection_rename_service_mode.py": (
        6,
        "Fixture dicts for collection_rename.py's straddle-window wire-compat "
        "fallback; mirrors that file's own allowlist reason."
    ),
    "tests/test_health_service_checks.py": (
        13,
        "Fixtures for health.py's straddle-era legacy-leg probes and the "
        "chash_conformance_report wire-compat table_name labels "
        "('nexus.chunks_384' as a counts-view filter value); mirrors "
        "health.py's and the changelog directory's allowlist reasons."
    ),
    "tests/db/test_du2dw_chash_conformance_report_engine.py": (
        3,
        "Asserts chash_conformance_report's DELIBERATE wire-compat "
        "table_name labels (384/768/1024) verbatim against the live "
        "engine — the exact assertion the changelog directory's allowlist "
        "reason names as the reason those labels must not be 'fixed'."
    ),
    "tests/db/test_rdr182_mvv_no_content_read.py": (
        5,
        "_ALLOWED_READ_OBJECTS enumerates the legacy per-dim table names as "
        "content-safe read targets for the straddle-window diagnostic "
        "statement set (nexus-rpw6u) — the same legacy_chash_conformance_"
        "statements() wire-compat literals as chash_tables.py."
    ),
    "tests/db/test_admin_sql_env.py": (
        9,
        "Fixture ALTER TABLE ... VALIDATE CONSTRAINT statements for the "
        "straddle-era per-dim chash-octet-length constraints "
        "(SchemaMigrator.java's CHASH_LEN_CONSTRAINTS); mirrors that "
        "file's allowlist reason."
    ),
    "tests/upgrade/test_chash_rekey_verification_non_vacuous.py": (
        3,
        "Verifies the chash_rekey upgrade rung's own straddle-era per-dim "
        "detection (chunks_384/768/1024 substring checks); mirrors "
        "chash_rekey.py's allowlist reason."
    ),
    "tests/e2e/migration-rehearsal/seed_legacy.py": (
        3,
        "A LEGACY store-state seeding script by name and purpose: seeds a "
        "pre-unify per-dim database for upgrade-ladder rehearsal, so it "
        "must dispatch rows to chunks_384/768/1024 by construction."
    ),
    "tests/e2e/migration-rehearsal/rehearse_chash_window.sh": (
        11,
        "The nexus-azx14 file itself — see module docstring's ALLOWLIST "
        "DESIGN section for the full incident this pin exists to close. "
        "Pre-swap era-guard references to the legacy per-dim tables are "
        "this script's PURPOSE (it rehearses the straddle window itself), "
        "not staleness — but a NEW stale reference (e.g. another post-swap "
        "leg written against a dropped table) would push this count ABOVE "
        "11 and trip the guard, which is the whole point. Concurrently "
        "edited by a sibling agent in this same batch — count is AS OF "
        "THIS COMMIT, will legitimately move again; do not treat a future "
        "re-pin as unreviewed the way PgVectorRepository.java's is flagged "
        "above, since this file's shape (era-guard script) makes every "
        "occurrence auditable by a human reading the diff, unlike a large "
        "javadoc-heavy Java file in flux."
    ),
    # ── tests: synthetic fixtures using the tokens as arbitrary example
    #    names, unrelated to real changelog/table content ─────────────────
    "tests/test_changelog_validate_precondition_lint.py": (
        2,
        "Synthetic XML fixtures for a Liquibase-precondition-shape lint "
        "use 'chunks_384'/'chunks_384_chash_len_check' as an arbitrary "
        "worked example table/constraint name; not real changelog content."
    ),
    "tests/test_changelog_vectors005_nine_body_drift_lint.py": (
        2,
        "Comment citing 'chunks_384' as one example shape the drift lint's "
        "own dim-token regex must normalize across (alongside "
        "'embedding_384', 'vector(384)'); explanatory, not a live reference."
    ),
    "tests/test_changelog_staged_batch_coupling_lint.py": (
        8,
        "Synthetic XML fixtures reconstructing the historical boot-brick "
        "existence-guard shape ('to_regclass(nexus.chunks_384)...') that "
        "vectors-005's own header narrates; not real changelog content read "
        "from disk."
    ),
    # ── tests: negative-assertion pins proving migration completeness ─────
    "tests/test_diag_conformance_view.py": (
        4,
        "Asserts nexus.chunks_384/768/1024 are ABSENT from "
        "CHASH_BEARING_TABLES — a positive proof the unification landed, "
        "not a stale reference to a live table."
    ),
    # ── tests: pure historical comment, no functional per-dim target ──────
    "tests/test_health.py": (
        2,
        "Comment + a fixture psql error string narrating a straddle-era "
        "constraint-does-not-exist case for chunks_384; mirrors health.py's "
        "allowlist reason."
    ),
    "tests/upgrade/test_chash_rekey_rung.py": (
        1,
        "Comment narrating the rekey rung's own chunks_384/768/1024 -> "
        "nexus.chunks collapse; historical only."
    ),
    "tests/catalog/test_http_catalog_client.py": (
        1,
        "Asserts a straddle-era table-name set including 'nexus.chunks_384' "
        "for a pre-unify catalog response shape; not a live reference to a "
        "table this code expects to exist post-unify."
    ),
    "tests/catalog/test_collection_scoped_tables_schema_parity.py": (
        1,
        "Comment narrating the RDR-191 Phase 4 unification "
        "(chunks_384/768/1024 collapsed to one); historical only."
    ),
    "tests/db/test_http_chash_integration.py": (
        1,
        "Comment narrating that a test's chosen collection segment used to "
        "route to chunks_768 pre-unify; historical only."
    ),
    "tests/test_rehearsal_seed_coverage_lint.py": (
        7,
        "Comments narrating which straddle-era per-dim content "
        "(chunks_384/768/1024, taxonomy_centroids_384/768/1024) the "
        "rehearsal seed must cover; historical/explanatory, matches "
        "seed_legacy.py's own allowlist reason."
    ),
    "tests/test_o8dil7_prune_misclassified_manifest_antijoin_engine.py": (
        1,
        "Comment explaining why a correlation pin intentionally stays on "
        "'chunks_1024'/'vec_1024' (the dim-router identity is preserved "
        "exactly across an unrelated RDR-109 rename) — explanatory, not a "
        "stale reference."
    ),
}


@dataclass(frozen=True)
class Offender:
    file: str
    line_no: int
    line: str
    token: str


def _allowlist_kind(relpath: str) -> str | None:
    """Which allowlist bucket (if any) covers *relpath*: 'dir', 'frozen',
    'pinned', or None (unlisted — the main guard scans it)."""
    if any(relpath == d or relpath.startswith(d + "/") for d in _DIR_ALLOWLIST):
        return "dir"
    if relpath in _FROZEN_FILE_ALLOWLIST:
        return "frozen"
    if relpath in _COUNT_PINNED_FILE_ALLOWLIST:
        return "pinned"
    return None


def _iter_scope_files() -> list[Path]:
    paths: list[Path] = []
    for pattern in _ALL_GLOBS:
        paths.extend(sorted(REPO_ROOT.glob(pattern)))
    return paths


def _scan_text(text: str, *, file_label: str) -> list[Offender]:
    hits: list[Offender] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for m in _BANNED_RE.finditer(line):
            hits.append(Offender(file=file_label, line_no=i, line=line.strip()[:160], token=m.group(1)))
    return hits


def _scan_file(path: Path, *, file_label: str | None = None) -> list[Offender]:
    label = file_label if file_label is not None else path.relative_to(REPO_ROOT).as_posix()
    return _scan_text(path.read_text(encoding="utf-8", errors="replace"), file_label=label)


def _file_has_banned_token(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    return bool(_BANNED_RE.search(text))


# ── Non-vacuity ──────────────────────────────────────────────────────────────


def test_globs_resolve_to_files() -> None:
    assert len(list(REPO_ROOT.glob("src/nexus/**/*.py"))) >= 100, "src/nexus/**/*.py glob looks broken"
    assert len(list(REPO_ROOT.glob("service/src/main/java/**/*.java"))) >= 20, (
        "service/src/main/java/**/*.java glob looks broken"
    )
    assert len(list(REPO_ROOT.glob("tests/**/*.py"))) >= 100, "tests/**/*.py glob looks broken"


def test_detector_fires_on_a_synthetic_banned_reference(tmp_path) -> None:
    """Mutation-verify: proves the regex actually matches the banned shape,
    using a synthetic file (no repo mutation) — mirrors
    test_release_artifact_verb_rot.py's mutation tests."""
    f = tmp_path / "synthetic.py"
    f.write_text("q = 'SELECT * FROM nexus.chunks_384'\n")
    hits = _scan_file(f, file_label="synthetic.py")
    assert len(hits) == 1
    assert hits[0].token == "chunks_384"

    clean = tmp_path / "clean.py"
    clean.write_text("q = 'SELECT * FROM nexus.chunks'\n")
    assert _scan_file(clean, file_label="clean.py") == []


def test_count_pin_mechanism_catches_the_azx14_counterfactual(tmp_path) -> None:
    """Mutation-verify for the UPGRADED (count-pinned) allowlist mechanism —
    closes the substantive-critic finding (nexus-bxcgh) that the original
    file-level allowlist had zero power inside a mixed-use file.

    Reproduces the actual nexus-azx14 shape structurally: a script mixes
    legitimate era-guard per-dim references (checking the pre-cutover
    ``$OLD_ENGINE_TAG`` schema on purpose) with stale post-swap references
    (querying tables RDR-191 already dropped) in the SAME file. A bare
    ``path -> reason`` allowlist entry cannot distinguish the two — this
    test proves the count-pin mechanism can: pinned to the legitimate
    era-guard baseline, the 3 additional stale hits push the live count
    3 OVER the pin, which is exactly the mismatch
    ``test_count_pinned_allowlist_matches_live_hit_counts`` asserts against
    on every real pinned file.
    """
    baseline = tmp_path / "rehearse_like.sh"
    baseline.write_text(
        "\n".join(
            f"# era guard: nexus.chunks_384 pre-cutover check ({i})" for i in range(5)
        )
        + "\n"
    )
    baseline_count = len(_scan_file(baseline, file_label="rehearse_like.sh"))
    assert baseline_count == 5, "synthetic baseline setup is wrong"

    # A pin AT the legitimate baseline passes.
    assert baseline_count == 5

    # Mutate: 3 EXTRA stale post-swap references land in the SAME file,
    # coexisting with the still-legitimate era-guard lines above (the azx14
    # shape verbatim — nothing about the legitimate lines changes).
    mutated_text = baseline.read_text() + "".join(
        f"POST_COUNT_{i}=\"$(diag_sql \"SELECT count(*) FROM nexus.chunks_768\")\"\n"
        for i in range(3)
    )
    mutated = tmp_path / "rehearse_like_mutated.sh"
    mutated.write_text(mutated_text)
    mutated_count = len(_scan_file(mutated, file_label="rehearse_like_mutated.sh"))

    assert mutated_count == baseline_count + 3, (
        "synthetic mutation should add exactly 3 hits over the era-guard baseline"
    )
    # This is precisely what test_count_pinned_allowlist_matches_live_hit_counts
    # checks on a real file: a pin frozen at the OLD (pre-stale-swap) count
    # would now mismatch against the live count, and the guard fails loud.
    pinned_at_old_baseline = baseline_count
    assert mutated_count != pinned_at_old_baseline, (
        "the count-pin mechanism failed to distinguish the mutated (stale-bearing) "
        "file from its legitimate baseline — this is the exact gap nexus-bxcgh found"
    )


def test_allowlists_are_not_stale() -> None:
    """Every allowlist entry must carry a non-empty reason and point at a
    path that still exists. Frozen (reason-only) entries must still contain
    at least one banned token — an entry with none is silently exempting
    nothing. Pinned entries must have a positive pin (a pin of 0 is not a
    real exemption, it is just an unlisted file in disguise); the pin's
    accuracy against the LIVE count is checked separately by
    ``test_count_pinned_allowlist_matches_live_hit_counts`` so a drifted
    pin fails with a clear too-high/too-low message rather than a generic
    'stale' one."""
    for reldir, reason in _DIR_ALLOWLIST.items():
        assert reason.strip(), f"_DIR_ALLOWLIST[{reldir!r}] has no reason"
        dirpath = REPO_ROOT / reldir
        assert dirpath.is_dir(), f"_DIR_ALLOWLIST names a directory that no longer exists: {reldir}"
        any_hit = any(
            _file_has_banned_token(p)
            for p in dirpath.rglob("*")
            if p.is_file() and p.suffix in (".xml", ".sql")
        )
        assert any_hit, (
            f"_DIR_ALLOWLIST[{reldir!r}] is stale: no file under it contains any banned "
            "per-dim identifier. Remove the entry so the lint covers this directory again."
        )

    for relpath, reason in _FROZEN_FILE_ALLOWLIST.items():
        assert reason.strip(), f"_FROZEN_FILE_ALLOWLIST[{relpath!r}] has no reason"
        path = REPO_ROOT / relpath
        assert path.is_file(), f"_FROZEN_FILE_ALLOWLIST names a file that no longer exists: {relpath}"
        assert _file_has_banned_token(path), (
            f"_FROZEN_FILE_ALLOWLIST[{relpath!r}] is stale: the file no longer contains any "
            "banned per-dim identifier. Remove the entry so the lint covers this file again."
        )

    for relpath, (expected, reason) in _COUNT_PINNED_FILE_ALLOWLIST.items():
        assert reason.strip(), f"_COUNT_PINNED_FILE_ALLOWLIST[{relpath!r}] has no reason"
        assert expected > 0, (
            f"_COUNT_PINNED_FILE_ALLOWLIST[{relpath!r}] has a non-positive pin ({expected}); "
            "a pin of 0 is not an exemption, remove the entry instead."
        )
        path = REPO_ROOT / relpath
        assert path.is_file(), (
            f"_COUNT_PINNED_FILE_ALLOWLIST names a file that no longer exists: {relpath}"
        )


def test_count_pinned_allowlist_matches_live_hit_counts() -> None:
    """The core of the count-pin mechanism (nexus-bxcgh remediation): every
    pinned file's LIVE banned-token count must equal its pin, exactly.

    A mismatch above the pin means something NEW landed in a mixed-use file
    — audit it: a legitimate addition gets the pin raised with an updated
    reason; a genuinely stale reference gets fixed. A mismatch below the pin
    means the pin is stale (something was removed, e.g. a fix) and should be
    lowered to match. See module docstring's ALLOWLIST DESIGN section.
    """
    mismatches: list[str] = []
    for relpath, (expected, _reason) in sorted(_COUNT_PINNED_FILE_ALLOWLIST.items()):
        path = REPO_ROOT / relpath
        actual = len(_scan_file(path, file_label=relpath))
        if actual > expected:
            mismatches.append(
                f"  {relpath}: live count {actual} > pin {expected} "
                f"(+{actual - expected}) -- POSSIBLE NEW STALE REFERENCE, audit the diff"
            )
        elif actual < expected:
            mismatches.append(
                f"  {relpath}: live count {actual} < pin {expected} "
                f"(-{expected - actual}) -- STALE PIN, lower it to {actual}"
            )

    assert not mismatches, (
        "count-pinned allowlist entries drifted from their live hit counts:\n"
        + "\n".join(mismatches)
        + "\n\nSee test_stale_dim_table_reference_lint.py's module docstring, "
        "ALLOWLIST DESIGN section, for how to resolve either direction."
    )


# ── The guard itself ─────────────────────────────────────────────────────────


def test_no_live_code_references_a_retired_per_dim_table() -> None:
    """A hit here means a file with NO allowlist entry at all (dir, frozen,
    or pinned) contains a banned per-dim reference — a rename/unification
    left a genuinely stale reference behind with nothing watching it yet.
    Fix the reference, or add a reasoned allowlist entry (see module
    docstring for which bucket: _DIR_ALLOWLIST, _FROZEN_FILE_ALLOWLIST, or
    _COUNT_PINNED_FILE_ALLOWLIST — pinned is the default for any file that
    is not provably frozen).

    Files that already have a pinned entry are NOT re-scanned here — their
    coverage is test_count_pinned_allowlist_matches_live_hit_counts, which
    catches a growing count with a more actionable message than a raw line
    dump would."""
    offenders: list[Offender] = []
    for path in _iter_scope_files():
        relpath = path.relative_to(REPO_ROOT).as_posix()
        if _allowlist_kind(relpath) is not None:
            continue
        offenders.extend(_scan_file(path))

    assert not offenders, (
        "live code references a retired per-dim table name "
        "(chunks_384/768/1024, taxonomy_centroids_384/768/1024) — RDR-191 "
        "Phase 4 unified these into nexus.chunks / nexus.taxonomy_centroids:\n"
        + "\n".join(f"  {o.file}:{o.line_no}: {o.token} — {o.line!r}" for o in offenders)
        + "\n\nSee this module's docstring for the two ways to resolve a hit "
        "(fix the rot, or add a reasoned allowlist entry)."
    )
