# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-rlkgu: per-record NexusError catch tripwire, mechanically enforced.

Context (substantive-critic diagnosis, T1 3fe5e91c; nexus-hb10j
adjudication): nexus-hb10j was the THIRD occurrence of the same
wrapper-gap class -- a per-record ingest loop's narrow except tuple lets
a newly-introduced NexusError subclass (ChunkLandingUnverifiedError,
IndexRunVerifyRefused) propagate uncaught and abort the WHOLE batch
instead of just the offending record. Prior occurrences: nexus-9800y
(dt.py file-backed branch), the tp8yk-era commands/index.py sites,
nexus-hb10j (dt.py --dt-content branch). Root-cause-after-3-patches: the
structural cause is not "no shared idiom" (the four near-identical catch
blocks have differing return/log shapes) -- it's "new NexusError
subclasses keep being introduced without an audit of every per-record
catch site". Full extraction wouldn't close that loop; this tripwire
does, by making the audit MANDATORY at introduction time.

Two independent, complementary gates:

1. **Loop coverage** (``test_every_per_record_loop_site_is_covered_or_
   allowlisted``): AST-enumerates every call to an index_*-family
   function (``index_pdf``, ``index_markdown``, ``_index_record``,
   ``_index_dt_content_record``) that occurs lexically inside a
   ``for``/``async for`` loop in ``src/nexus/commands/dt.py`` and
   ``src/nexus/commands/index.py``. Each site must be covered by an
   enclosing ``try`` whose handlers include either
   ``nexus.errors.PER_RECORD_SURVIVABLE_EXCEPTIONS`` by name, or a broad
   ``except Exception``/``except BaseException``/bare ``except`` (safe by
   construction -- a superclass catch cannot miss any NexusError
   subclass, known or future) -- or be listed in ``_LOOP_ALLOWLIST``
   below with a specific reason. A call made straight-line (no enclosing
   loop) is NOT a candidate site at all: those are single-file commands
   (``index_pdf_cmd``'s dry-run/monitor/else branches, ``index_md_cmd``)
   where converting the exception to a ``click.ClickException`` IS the
   correct behavior -- there is no batch to protect, so no allowlist
   entry is needed or wanted for them.

2. **Classification completeness** (``test_every_nexuserror_subclass_is_
   classified``): AST-derives every class defined in ``errors.py`` that
   is ``NexusError`` or a (transitive) subclass of it, and requires each
   one to be explicitly classified as either record-level (a member of
   ``nexus.errors.PER_RECORD_SURVIVABLE_EXCEPTIONS``) or command-level
   (listed in ``_COMMAND_LEVEL_REASONS`` below with a reason it is NOT
   per-record-raisable). This is the FIRST line of defense: it fires the
   instant a new NexusError subclass is added anywhere, before anyone
   even touches a dt.py/index.py catch site -- exactly what makes
   "occurrence 4" fail loud at introduction time rather than at the next
   production incident. Deliberately does NOT try to prove which
   subclasses doc_indexer's call stack can actually raise (that would be
   whole-program dataflow analysis, out of scope -- see
   test_vw594_fence_coverage_gate.py's identical scoping decision); it
   requires a human decision and a written reason for every subclass
   instead of guessing the answer.

Design decisions:

* ``PER_RECORD_SURVIVABLE_EXCEPTIONS`` lives in ``src/nexus/errors.py``,
  next to the exception classes it names -- both dt.py and index.py
  already import individual members from that module at every existing
  catch site, so importing one more name from the same module adds zero
  new edges to the import graph. ``commands/_helpers.py`` was the other
  candidate (it already hosts cross-command shared catch-adjacent
  helpers like ``reset_identity_drop_collectors``), but it imports
  ``nexus.config``/``nexus.db`` and is a commands-layer module -- putting
  a pure exception-type tuple there would make errors.py's own classes
  reachable only via a roundabout commands-layer re-export for no
  benefit.
* Sync mechanism (gate 2 above): a hand-maintained, AST-verified
  completeness check over ALL NexusError subclasses, not an attempt to
  infer the "true" per-record-raisable set from doc_indexer's call graph.
  The classification audit is the deliverable, not an automated guess.

KILL CONTROLS (documented per this repo's TDD/tripwire convention -- see
test_vw594_fence_coverage_gate.py, test_cli_runner_output_invariant.py):
every positive claim above ("the detector would catch X") is backed by a
``tmp_path`` fixture test proving the detector actually fires on a
synthetic offending shape, and a matching negative-control test proving
it does NOT fire on the corresponding safe shape. See the
``test_kill_control_*`` / ``test_*_is_not_flagged`` /
``test_call_outside_loop_is_not_a_candidate`` functions below.
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

from nexus.errors import PER_RECORD_SURVIVABLE_EXCEPTIONS

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"
ERRORS_FILE = SRC_ROOT / "errors.py"

#: index_*-family functions a per-record/per-file BATCH loop calls to
#: ingest one record. A call to one of these occurring LEXICALLY INSIDE a
#: `for`/`async for` loop is what makes a site "per-record" for this
#: tripwire -- the same names called straight-line (no enclosing loop)
#: are single-file commands, where converting the exception to a
#: click.ClickException and aborting IS the correct, intentional
#: behavior (there is no batch to protect).
_TARGET_FUNCS: frozenset[str] = frozenset({
    "index_pdf", "index_markdown", "_index_record", "_index_dt_content_record",
})

#: except-handler type-expression names that catch every
#: PER_RECORD_SURVIVABLE_EXCEPTIONS member -- current AND future. A bare
#: except, an Exception/BaseException catch, or a catch naming the
#: shared tuple directly all qualify: each is a superclass (or the exact
#: set) of every NexusError subclass that could ever be added to the
#: tuple, so none of them can be silently defeated by a new subclass the
#: way a hand-enumerated ``except (TypeA, TypeB)`` list can.
_SAFE_HANDLER_NAMES: frozenset[str] = frozenset({
    "PER_RECORD_SURVIVABLE_EXCEPTIONS", "Exception", "BaseException",
})

#: Files swept for per-record loop call sites, relative to SRC_ROOT.
#: Started as the bead's two named files (dt.py/index.py); widened by
#: substantive-critic SIGNIFICANT (nexus-a1zv2, round-2 review of
#: nexus-rlkgu, T2 [21492]): db/embed_migrate.py's ``_default_reindex``
#: has an IDENTICAL-shape unguarded per-record loop, and it was invisible
#: to this tripwire purely because it lived outside the original scope
#: list -- an inherited scope claim, not a verified one. Scope is now
#: exhaustive over every module found to call an index_*-family function
#: from a loop, not just the two the bead happened to name.
#: ``nexus.indexer_utils.run_file_loop`` (nx index repo's per-file loop,
#: with its own documented first-exception-aborts contract) is still
#: outside this list -- it is not in ANY of these three files, so it
#: remains naturally excluded rather than needing an allowlist carve-out.
_SCAN_RELPATHS: tuple[str, ...] = (
    "commands/dt.py", "commands/index.py", "db/embed_migrate.py",
)


@dataclass(frozen=True)
class _Site:
    rel_path: str
    function: str
    lineno: int
    covered: bool


@dataclass(frozen=True)
class _LoopAllowlistEntry:
    reason: str


#: Escape hatch for a per-record loop call site that is NOT covered by
#: the shared-tuple/broad-Exception mechanism but is still safe (or at
#: least dispositioned) for a documented reason (mirrors
#: test_vw594_fence_coverage_gate.py's _ALLOWLIST shape: keyed by call
#: site, reason mandatory).
#:
#: dt.py/index.py have zero entries -- the nexus-rlkgu refactor made
#: every real per-record loop site there pass by construction (the two
#: dt.py sites now catch PER_RECORD_SURVIVABLE_EXCEPTIONS by name;
#: index.py's --dir batch loop already used a broad ``except Exception``).
#:
#: db/embed_migrate.py's two sites (nexus-a1zv2, T2 [21492]) are
#: GENUINELY unguarded -- ``_default_reindex``'s per-record loop has NO
#: try/except at all around its index_pdf/index_markdown calls. They are
#: allowlisted, not fixed, because the module is unwired: no production
#: caller imports embed_migrate (grep-verified against src/nexus/ at
#: nexus-a1zv2 triage time; the only src/ hit is a docstring MENTION in
#: catalog/http_catalog_client.py, not an import), and
#: tests/test_kmo9h_catalog_gate_census.py already documents it as
#: "Chroma-era tool on frozen sources; dies at P4b" -- the module's own
#: docstring claim of "via nx init" integration is FALSE as of this
#: triage. Disposition (delete vs. rewire vs. leave dead) is tracked as
#: nexus-a1zv2, not resolved here -- this entry's job is only to make the
#: tripwire's scope claim exhaustive rather than silently inherited.
_LOOP_ALLOWLIST: dict[tuple[str, str, int], _LoopAllowlistEntry] = {
    ("db/embed_migrate.py", "_default_reindex", 280): _LoopAllowlistEntry(
        reason=(
            "unwired module, docstring claims nx init integration "
            "falsely — disposition tracked as nexus-a1zv2"
        ),
    ),
    ("db/embed_migrate.py", "_default_reindex", 282): _LoopAllowlistEntry(
        reason=(
            "unwired module, docstring claims nx init integration "
            "falsely — disposition tracked as nexus-a1zv2"
        ),
    ),
}


def _handler_is_safe(handler: ast.ExceptHandler) -> bool:
    """True if this ONE except handler alone would catch every
    PER_RECORD_SURVIVABLE_EXCEPTIONS member, current and future."""
    t = handler.type
    if t is None:
        return True  # bare `except:`
    names: list[str] = []
    if isinstance(t, ast.Name):
        names = [t.id]
    elif isinstance(t, ast.Tuple):
        names = [elt.id for elt in t.elts if isinstance(elt, ast.Name)]
    return any(n in _SAFE_HANDLER_NAMES for n in names)


def _try_is_safe(try_node: ast.Try) -> bool:
    return any(_handler_is_safe(h) for h in try_node.handlers)


def _find_loop_call_sites(
    root: pathlib.Path, rel_paths: tuple[str, ...],
) -> list[_Site]:
    """AST-enumerate every call to a ``_TARGET_FUNCS`` name that occurs
    lexically inside a ``for``/``async for`` loop, across *rel_paths*
    (relative to *root*). Records whether the NEAREST enclosing
    ``try``/``except`` between the call and its loop already safely
    covers ``PER_RECORD_SURVIVABLE_EXCEPTIONS`` (see ``_try_is_safe``).

    Honest limit (matches this repo's other AST tripwires --
    test_vw594_fence_coverage_gate.py, test_cli_runner_output_invariant.py):
    no whole-program dataflow. Lexical ancestry only -- a call routed
    through an intermediate helper that itself wraps the try/except is
    invisible to this scan, same documented scope as those two gates.
    """
    sites: list[_Site] = []
    for rel in rel_paths:
        path = root / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        stack: list[ast.AST] = []

        class _Visitor(ast.NodeVisitor):
            def generic_visit(self, node: ast.AST) -> None:
                stack.append(node)
                super().generic_visit(node)
                stack.pop()

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                if isinstance(func, ast.Name) and func.id in _TARGET_FUNCS:
                    in_loop = any(
                        isinstance(a, (ast.For, ast.AsyncFor)) for a in stack
                    )
                    if in_loop:
                        try_node = next(
                            (a for a in reversed(stack) if isinstance(a, ast.Try)),
                            None,
                        )
                        covered = try_node is not None and _try_is_safe(try_node)
                        function = next(
                            (
                                a.name for a in reversed(stack)
                                if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))
                            ),
                            "<module>",
                        )
                        sites.append(_Site(
                            rel_path=rel, function=function,
                            lineno=node.lineno, covered=covered,
                        ))
                self.generic_visit(node)

        _Visitor().visit(tree)
    return sites


def _nexus_error_subclass_names(errors_file: pathlib.Path = ERRORS_FILE) -> set[str]:
    """AST-derive every class name in *errors_file* that IS ``NexusError``
    or a (possibly transitive) subclass of it, EXCLUDING ``NexusError``
    itself. Fixed-point closure over same-file ``class X(Base):``
    declarations only (no cross-module base resolution -- every
    NexusError subclass lives in this one module today; a future
    subclass defined elsewhere would need this widened, same documented
    honest-limit convention as this repo's other AST gates).
    """
    tree = ast.parse(errors_file.read_text(encoding="utf-8"), filename=str(errors_file))
    bases: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases[node.name] = [
                b.id for b in node.bases if isinstance(b, ast.Name)
            ]

    subclasses: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, base_names in bases.items():
            if name in subclasses or name == "NexusError":
                continue
            if "NexusError" in base_names or any(b in subclasses for b in base_names):
                subclasses.add(name)
                changed = True
    return subclasses


#: Record-level: members of nexus.errors.PER_RECORD_SURVIVABLE_EXCEPTIONS.
#: Kept as a plain name set here (rather than importing the tuple
#: directly into every test) so test_record_level_matches_production_
#: tuple can assert the two independently-maintained lists agree --
#: catching drift in EITHER direction.
_RECORD_LEVEL: frozenset[str] = frozenset({
    "ChunkLandingUnverifiedError", "IndexRunVerifyRefused",
    # nexus-wi1uv (occurrence 5 of the wrapper-gap class, caught by this
    # tripwire pre-merge): fires from PDFExtractor.extract() deep inside
    # index_pdf -> _pdf_chunks, reachable from dt.py's nx dt index
    # per-record loop for any .pdf record. One gated PDF must fail that
    # record only, never abort the rest of the batch.
    "ExtractionQualityError",
    # nexus-rqsh1/nexus-1sd0f: fires from index_markdown/index_pdf's
    # pre-registration chunkability guard (zero-byte or binary content),
    # reachable from batch_index_markdowns and dt.py per-record loops.
    # One unchunkable file must fail that record only; single-file
    # commands translate it to ClickException at the wrapper boundary.
    "UnchunkableContentError",
    # nexus-deyd5: fires from PDFExtractor._extract_normalized (pymupdf)
    # and _extract_with_docling when extraction completes but yields zero
    # usable text (an image-only PDF, a deliberately blank fixture, a
    # damaged text layer). Reachable from the same dt.py/index.py
    # per-record loops as ExtractionQualityError; also recognized
    # directly by nexus.indexer_utils.run_file_loop so the bulk
    # `nx index repo` walk skips this one file instead of aborting the
    # whole run.
    "UnextractableContentError",
})

#: Command-level: every OTHER NexusError subclass, with a specific,
#: evidence-checked reason it is not per-record-raisable inside a
#: dt.py/index.py batch loop. A new NexusError subclass with no entry
#: here AND no entry in _RECORD_LEVEL fails
#: test_every_nexuserror_subclass_is_classified -- that failure IS the
#: tripwire; the fix is a deliberate classification decision, not a
#: blanket exemption.
_COMMAND_LEVEL_REASONS: dict[str, str] = {
    "CombinedWriteEmbedTimeoutError": (
        "Raised from HttpCatalogClient.write_manifest_many's chunk-"
        "carrying POST (nexus-y9t08 CRITICAL fix) -- a flush-grain "
        "catalog write invoked from indexer.py's ChunkBatcher flush path "
        "and mcp_infra.py, never from index_pdf/index_markdown/"
        "_index_record/_index_dt_content_record or any other "
        "per-record/per-file loop body in dt.py or index.py."
    ),
    "T3ConnectionError": (
        "No live `raise T3ConnectionError` site anywhere in src/nexus/ "
        "as of nexus-rlkgu -- defined but currently unused, unreachable "
        "from any call stack, let alone a per-record ingest loop."
    ),
    "IndexingError": (
        "Raised from index_pdf's streaming return_metadata path (chunks "
        "written but the metadata query found none). commands/index.py's "
        "index_pdf_cmd wraps the raw indexer in a LOCAL `def "
        "index_pdf(*args, **kwargs)` that converts IndexingError to "
        "click.ClickException at the wrapper boundary BEFORE it can "
        "reach a loop's except clause as IndexingError itself -- the "
        "--dir batch loop's broad `except Exception` absorbs the "
        "resulting ClickException regardless; single-file callers abort "
        "intentionally (no batch to protect)."
    ),
    "CredentialsMissingError": (
        "Raised at the START of doc_indexer's _index_document/index_pdf "
        "(before file_path.resolve()) as a startup-time credential-"
        "availability precondition -- would fail IDENTICALLY for every "
        "record in a batch, so per-record catch-and-continue provides no "
        "recovery value; it is a run-level precondition, not a "
        "record-level failure mode."
    ),
    "CollectionNotFoundError": (
        "Raised by a STRICT collection lookup (get_collection, not "
        "get_or_create_collection) in inmemory_vector_store.py / "
        "http_vector_client.py -- the missing-collection contract "
        "between HttpVectorClient and the purge/reidentify/backfill "
        "callers (see the class's own docstring), not doc_indexer's "
        "create-on-write per-record ingest path, which auto-creates via "
        "get_or_create_collection."
    ),
    "EmbeddingModelMismatch": (
        "Raised only from exporter.py's `nx export`/`nx import` "
        "embedding-model validation, not reachable from the ingest call "
        "stack (index_pdf/index_markdown) that nx dt index / nx index "
        "pdf --dir per-record loops invoke."
    ),
    "FormatVersionError": (
        "Raised only from exporter.py's `nx export`/`nx import` format-"
        "version checks, not reachable from the ingest call stack."
    ),
    "EmbeddingDimensionMismatch": (
        "Raised only from exporter.py's `nx export`/`nx import` "
        "dimension validation (GH #1370 D2), not reachable from the "
        "ingest call stack."
    ),
    "SourceUriNotFoundError": (
        "Raised when --source-uri is set and unresolvable -- a single-"
        "record IDENTITY precondition. index_pdf_cmd/index_md_cmd's own "
        "wrappers convert it to click.ClickException at the single-file "
        "command boundary (intentional abort, no batch to protect); "
        "neither the --dir batch loop nor nx dt index passes "
        "--source-uri per-record."
    ),
    "SourceUriCollectionMismatchError": (
        "Same shape as SourceUriNotFoundError above -- single-record "
        "identity precondition, converted to click.ClickException at the "
        "single-file command wrapper; not reachable from the --dir / "
        "nx dt index per-record loops."
    ),
    "EphemeralPathRefusedError": (
        "Same shape as SourceUriNotFoundError above (nexus-3o4lt): a "
        "single-record IDENTITY precondition raised by _repo_home_for "
        "before any chunk or catalog write, when a repo file exists only "
        "in a nested worktree. index_pdf_cmd/index_md_cmd's wrappers "
        "convert it to click.ClickException at the single-file command "
        "boundary. PDFs and DEVONthink records are exempt from the repo-"
        "owner rule by construction, so the --dir loop and nx dt index "
        "never see it; the only batch path that can (batch_index_markdowns "
        "under nx index rdr) already catches Exception per file and "
        "records the file as failed."
    ),
    "PutOversizedError": (
        "Raised only from T3Database.put (the MCP store_put / `nx store "
        "put` path, db/t3.py), which per the class's own docstring has "
        "'no chunker upstream' unlike the doc_indexer pipeline -- not "
        "reachable from nx dt index / nx index pdf --dir's doc_indexer-"
        "based per-record ingest call stack."
    ),
    "SplitConservationViolatedError": (
        "Raised only from HttpTaxonomyStore.split_topic's defense-in-"
        "depth compute_split conservation check (db/t2/"
        "http_taxonomy_store.py, nexus-i6eg8) -- a taxonomy topic-split "
        "internal invariant, not reachable from any dt.py/index.py "
        "per-record ingest loop. `nx taxonomy split` operates on ONE "
        "named topic per invocation, no batch to protect."
    ),
}


# ── Gate 1: per-record loop coverage ────────────────────────────────────────


def test_every_per_record_loop_site_is_covered_or_allowlisted() -> None:
    """The actual tripwire for gate 1: every per-record loop call site in
    dt.py/index.py must be covered by PER_RECORD_SURVIVABLE_EXCEPTIONS
    (by name) or a broad Exception/BaseException catch, or carry an
    explicit, reasoned _LOOP_ALLOWLIST entry. An unlisted, uncovered site
    is exactly the nexus-2fyb/qo84l/9800y/hb10j regression class."""
    sites = _find_loop_call_sites(SRC_ROOT, _SCAN_RELPATHS)
    offenders = [
        s for s in sites
        if not s.covered
        and (s.rel_path, s.function, s.lineno) not in _LOOP_ALLOWLIST
    ]
    assert not offenders, (
        "nexus-rlkgu: per-record loop call site(s) to an index_*-family "
        "function with no safe except coverage and no _LOOP_ALLOWLIST "
        "entry (catch nexus.errors.PER_RECORD_SURVIVABLE_EXCEPTIONS by "
        "name, use a broad `except Exception`, or add a reasoned "
        "allowlist entry):\n  "
        + "\n  ".join(f"{s.rel_path}:{s.lineno} in {s.function}()" for s in offenders)
    )


def test_scanner_finds_the_known_per_record_loop_sites() -> None:
    """Non-vacuity / kill-control proof for the scanner itself, pinning
    the exact sites found on the live tree right now: two within dt.py's
    index_cmd (the --dt-content branch and the file-backed branch, both
    guarded via the SAME `for uuid, path in records:` loop), one within
    index.py's index_pdf_cmd (the --dir batch loop), and two within
    db/embed_migrate.py's _default_reindex (nexus-a1zv2 — genuinely
    unguarded, allowlisted rather than covered). If this count ever drops
    to zero, test_every_per_record_loop_site_is_covered_or_allowlisted
    above would pass VACUOUSLY -- this test is what catches that."""
    sites = _find_loop_call_sites(SRC_ROOT, _SCAN_RELPATHS)
    found = sorted({(s.rel_path, s.function) for s in sites})
    assert found == [
        ("commands/dt.py", "index_cmd"),
        ("commands/index.py", "index_pdf_cmd"),
        ("db/embed_migrate.py", "_default_reindex"),
    ], found
    assert len(sites) == 5, sites
    uncovered = {(s.rel_path, s.function, s.lineno) for s in sites if not s.covered}
    assert uncovered == {
        ("db/embed_migrate.py", "_default_reindex", 280),
        ("db/embed_migrate.py", "_default_reindex", 282),
    }, (
        "the set of genuinely-uncovered sites changed -- either a real "
        "coverage regression (fix it) or embed_migrate.py's line numbers "
        "shifted (update _LOOP_ALLOWLIST's keys to match): "
        f"{uncovered}"
    )
    # And every uncovered site above is exactly the allowlisted set --
    # the actual gate (test_every_per_record_loop_site_is_covered_or_
    # allowlisted) is what turns an UNLISTED uncovered site red; this
    # assertion just pins today's known-uncovered set stays == today's
    # allowlist, so the two can't silently drift apart from each other.
    assert uncovered == set(_LOOP_ALLOWLIST), (uncovered, set(_LOOP_ALLOWLIST))


def test_loop_allowlist_reasons_are_non_vacuous() -> None:
    for key, entry in _LOOP_ALLOWLIST.items():
        assert entry.reason.strip(), f"{key}: empty reason"
        assert len(entry.reason.strip()) > 20, (
            f"{key}: allowlist reason too short to be a real justification "
            f"({entry.reason!r})"
        )


def test_loop_allowlist_has_no_stale_entries() -> None:
    sites = _find_loop_call_sites(SRC_ROOT, _SCAN_RELPATHS)
    live_uncovered = {
        (s.rel_path, s.function, s.lineno) for s in sites if not s.covered
    }
    stale = sorted(set(_LOOP_ALLOWLIST) - live_uncovered)
    assert not stale, (
        "nexus-rlkgu: stale _LOOP_ALLOWLIST entry (site is now covered, "
        f"renamed, or removed): {stale}"
    )


# ── Gate 1 kill controls (tmp_path fixtures) ────────────────────────────────


def test_kill_control_narrow_except_is_flagged(tmp_path: pathlib.Path) -> None:
    """THE regression shape, reproduced: a per-record loop catching only
    an unrelated narrow exception type around a target-function call."""
    fixture = tmp_path / "fixture_offender.py"
    fixture.write_text(
        "def index_pdf(*a, **kw):\n"
        "    ...\n"
        "\n"
        "def batch(records):\n"
        "    for uid, path in records:\n"
        "        try:\n"
        "            index_pdf(path)\n"
        "        except RuntimeError as exc:\n"
        "            print(exc)\n"
    )
    sites = _find_loop_call_sites(tmp_path, ("fixture_offender.py",))
    assert len(sites) == 1
    assert not sites[0].covered


def test_kill_control_unguarded_call_is_flagged(tmp_path: pathlib.Path) -> None:
    """No try/except at all around the per-record call -- the most
    extreme version of the same gap."""
    fixture = tmp_path / "fixture_unguarded.py"
    fixture.write_text(
        "def index_pdf(*a, **kw):\n"
        "    ...\n"
        "\n"
        "def batch(records):\n"
        "    for uid, path in records:\n"
        "        index_pdf(path)\n"
    )
    sites = _find_loop_call_sites(tmp_path, ("fixture_unguarded.py",))
    assert len(sites) == 1
    assert not sites[0].covered


def test_broad_except_is_not_flagged(tmp_path: pathlib.Path) -> None:
    """Negative control: a broad `except Exception` is safe by
    construction and must not be flagged (matches index.py's --dir
    batch loop, unchanged by the nexus-rlkgu refactor)."""
    fixture = tmp_path / "fixture_broad.py"
    fixture.write_text(
        "def index_pdf(*a, **kw):\n"
        "    ...\n"
        "\n"
        "def batch(records):\n"
        "    for uid, path in records:\n"
        "        try:\n"
        "            index_pdf(path)\n"
        "        except Exception as exc:\n"
        "            print(exc)\n"
    )
    sites = _find_loop_call_sites(tmp_path, ("fixture_broad.py",))
    assert len(sites) == 1
    assert sites[0].covered


def test_shared_tuple_except_is_not_flagged(tmp_path: pathlib.Path) -> None:
    """Negative control: the actual nexus-rlkgu fix shape now live in
    dt.py's two per-record catch sites."""
    fixture = tmp_path / "fixture_tuple.py"
    fixture.write_text(
        "def index_pdf(*a, **kw):\n"
        "    ...\n"
        "\n"
        "def batch(records):\n"
        "    for uid, path in records:\n"
        "        try:\n"
        "            index_pdf(path)\n"
        "        except PER_RECORD_SURVIVABLE_EXCEPTIONS as exc:\n"
        "            print(exc)\n"
    )
    sites = _find_loop_call_sites(tmp_path, ("fixture_tuple.py",))
    assert len(sites) == 1
    assert sites[0].covered


def test_call_outside_loop_is_not_a_candidate(tmp_path: pathlib.Path) -> None:
    """Negative control: a single-file command shape (target function
    called straight-line, no enclosing for-loop) is not even a
    candidate site -- matches index_pdf_cmd's dry-run/monitor/else
    branches and index_md_cmd, where an abort-on-failure except IS the
    intended behavior (no batch to protect)."""
    fixture = tmp_path / "fixture_single_file.py"
    fixture.write_text(
        "def index_pdf(*a, **kw):\n"
        "    ...\n"
        "\n"
        "def single(path):\n"
        "    try:\n"
        "        index_pdf(path)\n"
        "    except RuntimeError as exc:\n"
        "        raise ClickException(str(exc)) from exc\n"
    )
    sites = _find_loop_call_sites(tmp_path, ("fixture_single_file.py",))
    assert sites == []


# ── Gate 2: NexusError subclass classification completeness ───────────────


def test_every_nexuserror_subclass_is_classified() -> None:
    """The actual tripwire for gate 2: every NexusError subclass defined
    in errors.py must be classified as record-level (_RECORD_LEVEL) or
    command-level (_COMMAND_LEVEL_REASONS). This is the FIRST line of
    defense -- a brand-new per-record-raisable NexusError subclass fails
    here even if nobody has touched a dt.py/index.py catch site yet."""
    all_names = _nexus_error_subclass_names()
    classified = _RECORD_LEVEL | set(_COMMAND_LEVEL_REASONS)
    unclassified = sorted(all_names - classified)
    assert not unclassified, (
        "nexus-rlkgu: NexusError subclass(es) with no per-record "
        "classification. Add each to nexus.errors."
        "PER_RECORD_SURVIVABLE_EXCEPTIONS (if a per-record ingest loop "
        "can raise it and must catch-and-continue) or to "
        "_COMMAND_LEVEL_REASONS in this test file with a specific reason "
        f"it is NOT per-record-raisable: {unclassified}"
    )


def test_classification_has_no_stale_entries() -> None:
    all_names = _nexus_error_subclass_names()
    classified = _RECORD_LEVEL | set(_COMMAND_LEVEL_REASONS)
    stale = sorted(classified - all_names)
    assert not stale, (
        f"classified name(s) no longer defined in errors.py: {stale}"
    )


def test_command_level_reasons_are_non_vacuous() -> None:
    assert len(_COMMAND_LEVEL_REASONS) >= 1, (
        "_COMMAND_LEVEL_REASONS is empty -- every non-record-level "
        "NexusError subclass needs a reason here"
    )
    for name, reason in _COMMAND_LEVEL_REASONS.items():
        assert reason.strip(), f"{name}: empty reason"
        assert len(reason.strip()) > 20, (
            f"{name}: reason too short to be a real justification "
            f"({reason!r})"
        )


def test_record_level_matches_production_tuple() -> None:
    """The test-local _RECORD_LEVEL name set and the production
    PER_RECORD_SURVIVABLE_EXCEPTIONS tuple are two independently-
    maintained lists (one drives the loop-coverage handler check, one
    IS the shared catch tuple dt.py imports) -- this pins them to stay
    in sync in EITHER direction."""
    assert {c.__name__ for c in PER_RECORD_SURVIVABLE_EXCEPTIONS} == _RECORD_LEVEL


# ── Gate 2 kill control ─────────────────────────────────────────────────────


def test_kill_control_new_subclass_is_discovered_by_scanner(
    tmp_path: pathlib.Path,
) -> None:
    """Proves _nexus_error_subclass_names actually notices a brand-new
    NexusError subclass -- including one that subclasses ANOTHER
    NexusError subclass rather than NexusError directly (transitive
    closure) -- against a synthetic errors.py, without mutating the real
    one. This is the mechanism that makes 'occurrence 4' fail loud at
    introduction time; if discovery were vacuous, gate 2 would never
    fire on a real new subclass."""
    fixture = tmp_path / "errors_fixture.py"
    fixture.write_text(
        "class NexusError(Exception):\n    pass\n\n"
        "class ChunkLandingUnverifiedError(NexusError):\n    pass\n\n"
        "class BrandNewError(NexusError):\n    pass\n\n"
        "class IndirectError(BrandNewError):\n    pass\n"
    )
    names = _nexus_error_subclass_names(fixture)
    assert names == {"ChunkLandingUnverifiedError", "BrandNewError", "IndirectError"}

    # And the completeness check itself would flag the unclassified pair:
    classified = _RECORD_LEVEL | set(_COMMAND_LEVEL_REASONS)
    unclassified = names - classified
    assert unclassified == {"BrandNewError", "IndirectError"}
