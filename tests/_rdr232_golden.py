# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 golden-set support (nexus-jypmj.1.1): the self-contained ART RDR-084 fixture and the
observers that turn a run into ``bench.answer_shape_metrics.RunObservation`` records.

The fixture reproduces, on the engine substrate and without reading any live corpus, the evidence
mix of the 2026-10-23 incident: an ART RDR-084 and a Nexus RDR-084 under two repo owners, plus
more than 300 code chunks reachable from them through ``implements`` and ``relates`` links, so
the runner as it was before RDR-232, running the factual-lookup plan, gathers a code-dominated
evidence set.

Everything here is deterministic: documents, pieces and links are generated from fixed templates
and a fixed index, never from a clock or an RNG, and the substrate embeds with its bundled local
model. The text is synthetic (invented section content, the real RDR number and subject), so no
private project content enters the repository.

Observation never calls a model. Plan selection is the matcher over the seeded builtin library
(local ONNX embeddings); a run executes the factual-lookup plan with ``continuation_cut_at_step``
set at its first operator step, so the retrieval steps run for real and the rank/generate steps
(``claude -p``) never dispatch.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
BUILTIN_PLANS = REPO / "conexus" / "plans" / "builtin"
GOLDEN_PATH = REPO / "bench" / "queries" / "golden_v1.yaml"

#: The model token of the collections the fixture writes (the local posture's embedder).
MODEL_TOKEN = "bge-base-en-v15-768__v1"

ART_PROJECT = "ART"
NEXUS_PROJECT = "Nexus"

#: The fixture's repo owner names, and the T2 projects the RDR tooling would use for them
#: (``<repo>_rdr``). The lifecycle stream (nexus-jypmj.1.7) derives the project from the owner's name.
ART_OWNER_NAME = "art-rdr232-fixture"
NEXUS_OWNER_NAME = "nexus-rdr232-fixture"
ART_T2_PROJECT = f"{ART_OWNER_NAME}_rdr"
NEXUS_T2_PROJECT = f"{NEXUS_OWNER_NAME}_rdr"

#: The RDR-084 T2 records of each project: same titles, different content, so a stream that read the
#: wrong project is visible in the text. Invented content.
ART_T2_RECORDS: dict[str, str] = {
    "084": "id: RDR-084\nprefix: ART\ntitle: LIST PARSE Canonical Consolidation\nstatus: accepted\n"
           "accepted_date: 2026-03-10\n",
    "084-gate-latest": "outcome: PASSED\ndate: 2026-03-09\nround: 3\nsummary: ART list-parse gate passed with "
                       "observation O1 on adapter removal order\n",
}
NEXUS_T2_RECORDS: dict[str, str] = {
    "084": "id: RDR-084\nprefix: NEX\ntitle: Plan Library Hygiene for Grown Plans\nstatus: draft\n",
    "084-gate-latest": "outcome: BLOCKED\ndate: 2026-03-02\nround: 1\nsummary: Nexus plan-hygiene gate blocked on "
                       "the merge rule\n",
}
ART_REF = "RDR-084@ART"
NEXUS_REF = "RDR-084@Nexus"

#: Facet ids of the ART RDR-084 request, as labeled in golden_v1.yaml (G06).
F_ARCH, F_NUM, F_EVID, F_GATE, F_IMPL, F_OPEN = (
    "locked-architecture", "numerical-conventions", "evidence",
    "gate-history", "implementation-state", "open-by-design",
)

#: ART RDR-084 sections, one piece each: (facet it evidences, text). Invented content.
ART_RDR_PIECES: list[tuple[str | None, str]] = [
    (None, "# RDR-084: LIST PARSE Canonical Consolidation\n\nStatus: accepted. Project: ART. "
           "Problem Statement. Four modules each carried a private copy of the LIST PARSE routine "
           "that splits a serialized category list into entries. The copies disagreed on empty "
           "entries and on trailing separators, so two readers of the same record could see "
           "different category counts. This RDR consolidates them into one canonical routine."),
    (None, "## Context\n\nThe LIST PARSE routine feeds the category-weight update. A disagreement in "
           "entry count shifts which weight vector a pattern resonates with, which is why the copies "
           "could not simply be left to drift. The consolidation is scoped to parsing only; the "
           "weight update itself is untouched."),
    (F_ARCH, "## Locked Architecture\n\nOne canonical parser, art.listparse.canonical.parse_list, "
             "owns every split of a serialized category list. Callers pass the raw string and receive "
             "an immutable tuple of entries. Decision A1: the parser is a pure function with no I/O "
             "and no module state. Decision A2: callers never re-split its output."),
    (F_ARCH, "## Locked Architecture (layering)\n\nDecision A3: the four former copies become thin "
             "adapters that call the canonical parser and map its tuple into their own record types. "
             "Decision A4: the adapters live beside their callers; the canonical routine lives in "
             "art.listparse and imports nothing from the adapters, so the dependency points one way."),
    (F_NUM, "## Numerical Conventions\n\nEntry weights are IEEE-754 binary64 and are parsed with "
            "round-half-even. A trailing separator yields no entry; an empty field between two "
            "separators yields one entry of weight 0.0. Sums over entries accumulate in input order "
            "so results are bit-reproducible across the four adapters. Tolerance for the regression "
            "comparison is 1e-12 absolute."),
    (F_EVID, "## Evidence\n\nResearch finding R1: replaying 10,000 recorded category lists through "
             "the four old copies produced 37 records where at least two copies disagreed on the "
             "entry count. Research finding R2: replaying the same lists through the canonical "
             "parser produced zero disagreements against the specified conventions."),
    (F_EVID, "## Evidence (benchmarks)\n\nResearch finding R3: the canonical parser is within 4 "
             "percent of the fastest old copy on the replay corpus, so consolidation costs no "
             "measurable throughput. Research finding R4: the 37 disagreeing records were all "
             "trailing-separator cases, which the numerical conventions now settle."),
    (F_GATE, "## Gate History\n\nGate round 1 (2026-03-02): critical finding C1, the draft left the "
             "empty-field convention unspecified. Fixed by the Numerical Conventions section. "
             "Gate round 2 (2026-03-05): significant finding S1, no replay evidence for the "
             "adapters. Fixed by research findings R1 through R4."),
    (F_GATE, "## Gate History (acceptance)\n\nGate round 3 (2026-03-09): PASSED with one "
             "observation, O1, that adapter removal order be recorded. Recorded under "
             "Implementation State. Accepted 2026-03-10 by the project owner."),
    (F_IMPL, "## Implementation State\n\nComplete: canonical parser landed with 41 unit tests; "
             "adapters for the pattern store and the replay tool migrated. Pending: the adapter for "
             "the legacy import path and the adapter for the report generator. Removal order of the "
             "old copies follows observation O1: replay tool first, import path last."),
    (F_IMPL, "## Implementation State (verification)\n\nThe regression suite compares every migrated "
             "adapter against the canonical parser within the 1e-12 tolerance. The two pending "
             "adapters are tracked as separate work items; neither blocks the migrated ones."),
    (F_OPEN, "## Open by Design\n\nOpen by design: whether the canonical parser should accept "
             "escaped separators. The current data contains none, so this RDR leaves escape "
             "handling out and records the question for a later RDR. Also open: the long-term home "
             "of the replay corpus."),
]

#: Nexus RDR-084 sections (a different document under another owner; invented content).
NEXUS_RDR_PIECES: list[tuple[str | None, str]] = [
    (None, f"# RDR-084: Plan Library Hygiene for Grown Plans\n\nStatus: accepted. Project: Nexus. "
           "Grown plans saved from successful runs accumulate near-duplicates. This RDR defines "
           "when a grown plan is a duplicate and how the library retires one."),
    (None, "## Design\n\nA grown plan is a duplicate when its dimensional identity and its first "
           "retrieval step match an existing plan. The newer plan is merged into the older one's "
           "match counters and then retired."),
    (None, "## Evidence\n\nOn a library snapshot, 112 of 340 grown plans were duplicates under this "
           "rule. Retiring them left the top-1 match unchanged on 98 percent of replayed requests."),
    (None, "## Gate History\n\nGate round 1 found the merge rule ambiguous for plans with different "
           "default bindings. Gate round 2 passed after the rule compared default bindings."),
    (None, "## Implementation State\n\nThe duplicate detector is merged. Retirement is behind a "
           "flag and has not been enabled in the cloud tenant."),
    (None, "## Open Questions\n\nWhether a retired plan's counters should decay. Not decided."),
]

ART_CODE_DOCS = 90
NEXUS_CODE_DOCS = 25
PIECES_PER_CODE_DOC = 4

#: nexus-jypmj.1.8. The fixture's 17 RDR chunks are fewer than the search limit (40), so the vector
#: stream held every one of them by construction and primary and facet coverage saturated at 1.0
#: whatever the ranking did. Two additions break that, both in the ART owner's ``rdr`` collection
#: (so ``scope="rdr"`` admits them):
#:
#: * ``DISTRACTOR_RDR_DOCS`` other RDRs, one chunk each, written as near-copies of the golden
#:   request's wording (another RDR number, another subject), so they outrank the real RDR-084
#:   sections for the 40 slots: the vector stream is full (40 of more than 100 RDR chunks) and holds
#:   only some of the named source's sections.
#: * ``SECONDARY_ROLE_DOCS`` secondary documents, one chunk each, linked to the ART RDR-084 with
#:   mixed link roles (``cites`` is a citation, ``implements`` an implementation, ``relates`` a
#:   related link). They are unrelated in wording, so they never enter the vector stream; the walk
#:   reaches them. More of them than the traversal document cap (50), so the cap, the role ranking
#:   and ``dropped_cap`` run on the substrate.
DISTRACTOR_RDR_DOCS = 32
SECONDARY_ROLE_DOCS: tuple[tuple[str, int], ...] = (("cites", 22), ("implements", 20), ("relates", 20))
SECONDARY_DOCS = sum(n for _role, n in SECONDARY_ROLE_DOCS)
#: ``rank`` rows the oversized-input case searches for: more than ``runner._OPERATOR_MAX_INPUTS``.
OVERSIZED_SEARCH_LIMIT = 120

_SECONDARY_ROLE_OF_LINK_TYPE = {"cites": "citation", "implements": "implementation", "relates": "related"}


def _distractor_piece(i: int) -> str:
    n = 101 + i
    subject = f"WIDGET QUEUE {i:02d}"
    return (
        f"Synthesize RDR-{n:03d}, {subject} Canonical Consolidation. State the locked architecture and "
        "numerical conventions, the evidence and gate history supporting them, the implementation work "
        "recorded as complete or pending, and what remains open by design. Distinguish facts at the "
        f"RDR's acceptance from later records, cite source documents. This RDR is about {subject}, not "
        "any other record."
    )


def _secondary_piece(role: str, i: int) -> str:
    return (
        f"Kiln log {role} {i:02d}: the harbour lamp schedule moved the firing window by {i + 3} minutes "
        "and the glaze batch cooled on the stone bench before inspection."
    )


def _art_code_pieces(i: int) -> list[str]:
    """Four pieces for ART code module *i*; each names the RDR and subject in its docstring, as
    real implementing code does, so it is lexically close to a request about RDR-084."""
    out = []
    for j, (fn, what) in enumerate([
        ("parse_list", "splits a serialized category list into entries"),
        ("normalize_entry", "applies the numerical conventions to one entry weight"),
        ("sum_weights", "accumulates entry weights in input order"),
        ("adapt_record", "maps the canonical tuple into this module's record type"),
    ]):
        out.append(
            f"# art/listparse/mod_{i:02d}.py\n"
            f"def {fn}_{i:02d}(raw):\n"
            f'    """RDR-084 LIST PARSE canonical consolidation, adapter {i:02d}: {what}. '
            f'Implements the locked architecture and the numerical conventions of RDR-084."""\n'
            f"    return _canonical_{j}(raw, module={i})\n"
        )
    return out


def _nexus_code_pieces(i: int) -> list[str]:
    out = []
    for j, what in enumerate([
        "computes the dimensional identity of a grown plan",
        "compares first retrieval steps of two plans",
        "merges match counters into the surviving plan",
        "retires the duplicate plan row",
    ]):
        out.append(
            f"# nexus/plans/hygiene_{i:02d}.py\n"
            f"def step_{j}_{i:02d}(plan):\n"
            f'    """Plan library hygiene (RDR-084, Nexus): {what}."""\n'
            f"    return _hygiene_{j}(plan, slot={i})\n"
        )
    return out


@dataclass
class Fixture:
    """What :func:`build_rdr084_fixture` wrote, plus the oracle labels for each chunk."""

    art_owner: str
    nexus_owner: str
    art_rdr: str
    nexus_rdr: str
    art_rdr_collection: str
    nexus_rdr_collection: str
    art_code_collection: str
    nexus_code_collection: str
    code_docs: list[str] = field(default_factory=list)
    #: chash -> (source ref, corpus prefix, facet ids) for every chunk the fixture wrote.
    chunks: dict[str, tuple[str, str, frozenset[str]]] = field(default_factory=dict)
    code_chunk_count: int = 0
    link_count: int = 0
    #: nexus-jypmj.1.8: the distractor RDR documents, the secondary documents with the link role each
    #: was given, and per chunk the collection it was written to and (secondary chunks) its role.
    distractor_docs: list[str] = field(default_factory=list)
    secondary_docs: dict[str, str] = field(default_factory=dict)
    chunk_collection: dict[str, str] = field(default_factory=dict)
    secondary_role_of_chash: dict[str, str] = field(default_factory=dict)

    def collection_of(self, chash: str) -> str:
        return self.chunk_collection.get(chash, "")

    def evidence_item(self, chash: str):
        from bench.answer_shape_metrics import EvidenceItem

        src, corpus, facets = self.chunks.get(chash, ("", "", None))
        if not corpus:  # a chunk the fixture did not write: unattributed, unlabeled
            return EvidenceItem(source="", corpus="", facets=None)
        return EvidenceItem(source=src, corpus=corpus, facets=facets)


def chash_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _owner_collection(kind: str, owner: str) -> str:
    # Conformant shape: <content_type>__<owner_id>__<embedding_model>__v<n>, owner_id the owner
    # tumbler's first two segments joined by a hyphen.
    return f"{kind}__{owner.replace('.', '-')}__{MODEL_TOKEN}"


def build_rdr084_fixture() -> Fixture:
    """Write the fixture into the ambient tenant (the caller holds ``t2_service_env``)."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.catalog.note_write import write_note
    from nexus.catalog.store_hook import note_content_hash, note_manifest_metadata

    fx_chunks: dict[str, tuple[str, str, frozenset[str]]] = {}
    fx_collection: dict[str, str] = {}
    secondary_role_of_chash: dict[str, str] = {}
    counts = {"code_chunks": 0, "links": 0}

    with HttpCatalogClient() as cat:
        art_owner = str(cat.register_owner(ART_OWNER_NAME, "repo", repo_hash="rdr232-fx-art"))
        nexus_owner = str(cat.register_owner(NEXUS_OWNER_NAME, "repo", repo_hash="rdr232-fx-nexus"))

        def put(owner: str, kind: str, title: str, file_path: str, pieces: list[str],
                source: str, facets: list[frozenset[str]]) -> str:
            collection = _owner_collection(kind, owner)
            ctype = "rdr" if kind == "rdr" else "code"
            tumbler = str(cat.register(
                owner, title, content_type=ctype, file_path=file_path,
                physical_collection=collection, chunk_count=len(pieces),
            ))
            _first, metas = note_manifest_metadata(pieces)
            write_note(
                catalog_doc_id=tumbler, collection=collection, pieces=pieces,
                content_hash=note_content_hash("".join(pieces), metas), title=title, cat=cat,
            )
            for piece, fs in zip(pieces, facets, strict=True):
                fx_chunks[chash_of(piece)] = (source, kind, fs)
                fx_collection[chash_of(piece)] = collection
            return tumbler

        # The code documents register FIRST and the RDRs last, so each RDR sorts after the code
        # that implements it within its owner. The live ART RDR-084 is tumbler 1.2.4686, late in its
        # owner, and traverse reads manifests in node order, so with a 300-id cap the RDR's chunks
        # are the ones cut: the incident kept 339 code references and 1 RDR reference.
        art_code: list[str] = []
        nexus_code: list[str] = []
        for i in range(ART_CODE_DOCS):
            pieces = _art_code_pieces(i)
            art_code.append(put(
                art_owner, "code", f"art/listparse/mod_{i:02d}.py",
                f"src/art/listparse/mod_{i:02d}.py", pieces, "", [frozenset()] * len(pieces)))
            counts["code_chunks"] += len(pieces)
        for i in range(NEXUS_CODE_DOCS):
            pieces = _nexus_code_pieces(i)
            nexus_code.append(put(
                nexus_owner, "code", f"nexus/plans/hygiene_{i:02d}.py",
                f"src/nexus/plans/hygiene_{i:02d}.py", pieces, "", [frozenset()] * len(pieces)))
            counts["code_chunks"] += len(pieces)
        code_docs = art_code + nexus_code

        art_rdr = put(
            art_owner, "rdr", "RDR-084-list-parse-canonical-consolidation",
            "docs/rdr/RDR-084-list-parse-canonical-consolidation.md",
            [t for _f, t in ART_RDR_PIECES], ART_REF,
            [frozenset({f} if f else ()) for f, _t in ART_RDR_PIECES],
        )
        nexus_rdr = put(
            nexus_owner, "rdr", "rdr-084-plan-library-hygiene",
            "docs/rdr/rdr-084-plan-library-hygiene.md",
            [t for _f, t in NEXUS_RDR_PIECES], NEXUS_REF,
            [frozenset()] * len(NEXUS_RDR_PIECES),
        )

        # Half implement the RDR, half only relate to it: both are factual-evidence edges.
        for docs, rdr in ((art_code, art_rdr), (nexus_code, nexus_rdr)):
            for i, t in enumerate(docs):
                cat.link(t, rdr, "implements" if i % 2 == 0 else "relates", "rdr232-fixture")
                counts["links"] += 1

        # nexus-jypmj.1.8: distractor RDRs (beyond the search limit) and secondary in-scope documents.
        distractors: list[str] = []
        for i in range(DISTRACTOR_RDR_DOCS):
            distractors.append(put(
                art_owner, "rdr", f"RDR-{101 + i:03d}-widget-queue-{i:02d}",
                f"docs/rdr/RDR-{101 + i:03d}-widget-queue-{i:02d}.md", [_distractor_piece(i)],
                f"RDR-{101 + i:03d}@ART", [frozenset()]))
        secondary: dict[str, str] = {}
        for link_type, n in SECONDARY_ROLE_DOCS:
            for i in range(n):
                piece = _secondary_piece(link_type, i)
                t = put(
                    art_owner, "rdr", f"RDR-2{len(secondary):02d}-kiln-{link_type}-{i:02d}",
                    f"docs/rdr/RDR-2{len(secondary):02d}-kiln-{link_type}-{i:02d}.md", [piece],
                    f"SEC-{link_type}-{i:02d}@ART", [frozenset()])
                secondary[t] = _SECONDARY_ROLE_OF_LINK_TYPE[link_type]
                secondary_role_of_chash[chash_of(piece)] = _SECONDARY_ROLE_OF_LINK_TYPE[link_type]
                cat.link(t, art_rdr, link_type, "rdr232-fixture")
                counts["links"] += 1

    # The RDRs' T2 lifecycle records (nexus-jypmj.1.7): the ambient tenant's, under each owner's
    # `<repo>_rdr` project.
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    memory = HttpMemoryStore()
    try:
        for project, records in ((ART_T2_PROJECT, ART_T2_RECORDS), (NEXUS_T2_PROJECT, NEXUS_T2_RECORDS)):
            for title, content in records.items():
                memory.put(project=project, title=title, content=content, tags="rdr")
    finally:
        memory.close()

    return Fixture(
        art_owner=art_owner, nexus_owner=nexus_owner, art_rdr=art_rdr, nexus_rdr=nexus_rdr,
        art_rdr_collection=_owner_collection("rdr", art_owner),
        nexus_rdr_collection=_owner_collection("rdr", nexus_owner),
        art_code_collection=_owner_collection("code", art_owner),
        nexus_code_collection=_owner_collection("code", nexus_owner),
        code_docs=code_docs, chunks=fx_chunks,
        code_chunk_count=counts["code_chunks"], link_count=counts["links"],
        distractor_docs=distractors, secondary_docs=secondary,
        chunk_collection=fx_collection, secondary_role_of_chash=secondary_role_of_chash,
    )


# ── observers ───────────────────────────────────────────────────────────────


#: nexus-jypmj.1.14. Plan 473 of the 2026-10-23 incident is the tenant's seeded copy of the builtin
#: ``lookup:hybrid-factual-lookup`` (RDR-232 line 1606: "plan 473's template"). The fixture library holds
#: only the 12 builtins, and the local matcher over them selects ``research:default`` for the incident
#: request, so the baseline scored it as the right shape. The clone below is a GROWN-shaped copy of 473
#: (saved the way ``nx_answer``'s plan-grow path saves a plan: tags ``ad-hoc,grown``, scope ``personal``,
#: a scope anchor) so the matcher has a lookup-shaped plan to select for the incident request, and the
#: label key names it. (The RDR calls 473 builtin; "grown" describes how this copy is stored, which is
#: what the Phase 2 gate has to handle: RDR-232 line 1408, an unknown-kind grown plan with lookup-shaped
#: history and a synthesis request.)
GROWN_473_STRATEGY = "hybrid-factual-lookup-473"
#: ``unacceptable_plans`` entry that names the clone. A ``<verb>:<strategy>`` key cannot: a grown plan's
#: strategy is a slug of its originating question, not a template name, so the key carries a ``grown:``
#: prefix that no builtin verb can collide with, and the golden loader test knows it as the one
#: grown key the labels may use.
GROWN_473_KEY = f"grown:{GROWN_473_STRATEGY}"
#: The scope anchor the grow path gives a plan grown from a ``scope="rdr"`` call.
GROWN_473_SCOPE_TAGS = "rdr"
GROWN_LABEL_KEYS: frozenset[str] = frozenset({GROWN_473_KEY})


def builtin_plan_key(dimensions: dict[str, Any]) -> str:
    """``<verb>:<strategy>``: the plan identity golden_v1.yaml's unacceptable_plans uses (several
    builtins are all named ``default``, so the name alone does not identify one)."""
    return f"{dimensions.get('verb', '')}:{dimensions.get('strategy', '')}"


def plan_label_key(dimensions: dict[str, Any], tags: str = "") -> str:
    """The key a golden label uses for a library plan: ``<verb>:<strategy>`` for a template-seeded
    plan, ``grown:<strategy>`` for a grown one (tags carry the comma token ``grown``, as the matcher
    reads them)."""
    if "grown" in [t.strip() for t in (tags or "").split(",")]:
        return f"grown:{dimensions.get('strategy', '')}"
    return builtin_plan_key(dimensions)


#: The plan-side measures the grown clone can move (the other five are evidence-side or later-phase).
PLAN_SIDE_MEASURES: tuple[str, ...] = ("eligible_plan_recall", "wrong_shape_admission_rate")
FLOOR_READING = (
    "builtins-only floor: the library holds the builtin templates only, so the incident request (G06) "
    "selects research:default and scores as the right shape. Not the incident's selection; see "
    "plan_side_measures.with_grown_473_clone."
)


def grown_clone_caveat() -> str:
    """The construction caveat (``BASELINE_CAVEATS``) that travels with the grown-clone figures."""
    from bench.answer_shape_metrics import BASELINE_CAVEATS

    (caveat,) = [c for c in BASELINE_CAVEATS if "grown clone of plan 473" in c]
    return caveat


def plan_side_report(floor: dict, with_clone: dict) -> dict:
    """The report's ``plan_side_measures``: both readings, each carrying what it is. The grown-clone
    block carries its construction caveat so a figure copied out of the report keeps it."""
    from bench.answer_shape_metrics import measures_to_json

    def block(measures: dict) -> dict:
        return {n: json.loads(measures_to_json({n: measures[n]}))[n] for n in PLAN_SIDE_MEASURES}

    return {
        "builtins_only": {"reading": FLOOR_READING, "measures": block(floor)},
        "with_grown_473_clone": {"caveat": grown_clone_caveat(), "measures": block(with_clone)},
    }


def measures_report(measures: dict) -> dict:
    """The report's top-level ``measures``: the seven measures, with the two plan-side entries labeled
    as the builtins-only floor (they are taken over a library that cannot select the incident's plan)."""
    from bench.answer_shape_metrics import measures_to_json

    out = json.loads(measures_to_json(measures))
    for name in PLAN_SIDE_MEASURES:
        out[name]["plan_library"] = FLOOR_READING
    return out


def selection_drift(
    floor: dict[str, dict], with_clone: dict[str, dict],
) -> tuple[list[str], list[str]]:
    """Which requests the clone moved: (qids whose SELECTED plan differs, qids whose candidate SET
    differs), each sorted. Inputs are ``{qid: {"selected": ..., "candidates": [...]}}``. A request in
    only one of the two maps counts as moved in both."""
    selected: list[str] = []
    candidates: list[str] = []
    for qid in sorted(set(floor) | set(with_clone)):
        a, b = floor.get(qid), with_clone.get(qid)
        if a is None or b is None or a["selected"] != b["selected"]:
            selected.append(qid)
        if a is None or b is None or set(a["candidates"]) != set(b["candidates"]):
            candidates.append(qid)
    return selected, candidates


def builtin_descriptions() -> set[str]:
    """Every builtin template's match description, whitespace-normalized: the values ``plans.query``
    holds for the seeded builtin rows, which the seed loader reconciles on."""
    from nexus.plans.seed_loader import desired_row_for_template

    out: set[str] = set()
    for path in sorted(BUILTIN_PLANS.glob("*.yml")):
        row = desired_row_for_template(yaml.safe_load(path.read_text(encoding="utf-8")))
        out.add(" ".join(str(row.query).split()))
    return out


def seed_grown_473_clone(library, *, origin_question: str) -> int:
    """Save the grown clone of plan 473 into *library*; returns its row id.

    What this clone measures, and what it does not. It is stored ``ad-hoc,grown`` and carries no
    answer-kind declaration, so it exercises ONE Phase 2 exit clause (RDR-232 lines ~487-498): unknown-grown
    plans cannot bypass the gate. It does NOT exercise the clause "473 is ineligible" (a builtin-tagged row
    that declares lookup): the builtin is not selected for the incident request in this fixture. Plan 473's
    builtin origin is inferred from RDR-232 line 1606 and T2 nexus_rdr/189-research-11; no live read was
    made. Phase 2 (nexus-jypmj.2.1) declares ``lookup`` on the builtin template this clone copies; the clone
    is stamped with ``grown_plan_json(..., route_kind=None, route_source=None)``, exactly what ``nx_answer``
    stores for a grown plan today, and the test file pins that it decodes (unknown, grown).

    The plan body, binding declarations, default bindings and description are the seeded builtin's own
    (``desired_row_for_template``, the loader's row builder), so the clone runs exactly as 473 ran.
    What makes it grown is how ``nx_answer`` saves one: tags ``ad-hoc,grown``, scope ``personal``, the
    strategy slug in place of the template's, and the originating call's scope as its anchor. Its match
    text is the builtin's description (473's own wording, no exemplars added). Its ``query`` is
    *origin_question*, as the grow path stores the question that grew the plan; pass the incident request
    and ``plan_match`` selects the clone for a verbatim repeat of it (confidence 1.0) and for no other
    request. That is how the fixture reproduces the incident's selection: it says nothing about how 473
    matched live. *origin_question* must differ from the builtin's description: ``plans.query`` is the
    seed loader's reconcile key, and a clone saved under it overwrites the builtin row, so it is refused
    (``ValueError``) when it equals any builtin's description after whitespace normalization.
    """
    from nexus.plans.answer_kind import grown_plan_json
    from nexus.plans.schema import canonical_dimensions_json
    from nexus.plans.seed_loader import desired_row_for_template

    if " ".join(origin_question.split()) in builtin_descriptions():
        raise ValueError(
            "origin_question equals a builtin plan's description: plans.query is the seed loader's "
            "reconcile key, so the clone would overwrite that builtin row"
        )

    template = yaml.safe_load((BUILTIN_PLANS / "hybrid-factual-lookup.yml").read_text(encoding="utf-8"))
    row = desired_row_for_template(template)
    # Phase 2 (nexus-jypmj.2.1) declares `lookup` on the template this clone copies. nx_answer saves a grown
    # plan whose route was unknown as {unknown, grown} (grown_plan_json replaces the template's declaration),
    # so the clone is stamped the same way: it stays an unknown-kind grown plan and keeps measuring
    # "unknown-grown plans cannot bypass the gate". TestPhase2Tripwire pins the stamp.
    return int(library.save_plan(
        query=origin_question,
        plan_json=grown_plan_json(row.plan_json, route_kind=None, route_source=None),
        outcome="success",
        tags="ad-hoc,grown", project="", ttl=None, scope="personal", scope_tags=GROWN_473_SCOPE_TAGS,
        verb="lookup", name=GROWN_473_STRATEGY,
        dimensions=canonical_dimensions_json(
            {"verb": "lookup", "scope": "personal", "strategy": GROWN_473_STRATEGY}),
        default_bindings=row.default_bindings,
        match_description=" ".join(str(row.query).split()),
    ))


def builtin_match(strategy: str, verb: str):
    """A ``Match`` for a builtin plan template, built from its YAML (the runner input)."""
    from nexus.plans.match import Match

    for path in sorted(BUILTIN_PLANS.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        dims = data["dimensions"]
        if dims.get("strategy") == strategy and dims.get("verb") == verb:
            plan = data["plan_json"]
            return Match(
                plan_id=0, name=data["name"], description=" ".join(str(data["description"]).split()),
                confidence=1.0, dimensions=dims, tags=data.get("tags", ""),
                plan_json=json.dumps(plan),
                required_bindings=list(data.get("required_bindings", [])),
                optional_bindings=list(data.get("optional_bindings", [])),
                default_bindings=dict(data.get("default_bindings", {})),
                parent_dims=None,
            )
    raise LookupError(f"no builtin plan {verb}:{strategy}")


def seed_builtin_plans():
    """Load the builtin plan templates into the ambient tenant's plan library; returns the library."""
    from nexus.db.t2.http_plan_library import HttpPlanLibrary
    from nexus.plans.seed_loader import load_seed_directory

    lib = HttpPlanLibrary()
    result = load_seed_directory(BUILTIN_PLANS, library=lib)
    assert not result.errors, f"seed_loader errors: {result.errors}"
    assert len(result.inserted) + len(result.skipped_existing) >= 10, (
        f"builtin plan seed came back with {result.inserted!r} / {result.skipped_existing!r}"
    )
    return lib


@dataclass
class PlanSelection:
    selected: str | None
    candidates: list[str]
    confidences: list[float | None]
    #: The plan ids the matcher offered, in order (nexus-jypmj.2.3).
    plan_ids: list[int] = field(default_factory=list)


def observe_plan_selection(
    library, question: str, *, scope: str = "", bindings: dict[str, Any] | None = None, eligibility=None,
    min_confidence: float | None = None,
) -> PlanSelection:
    """What the matcher offers and picks for *question*, called as ``nx_answer`` calls it
    (same bindings, availability set, floor, ``n`` and category verb). A plan miss selects ``None``.
    *eligibility* is the answer-kind filter (nexus-jypmj.2.3); none, as before, is the unmodified matcher.
    *min_confidence* replaces the production floor (a test that wants every candidate the matcher examined)."""
    from nexus.mcp.core import _PLAN_MATCH_MIN_CONFIDENCE, _nx_answer_caller_bindings, _nx_answer_match_is_hit
    from nexus.mcp_infra import get_t1_plan_cache
    from nexus.plans.matcher import plan_match
    from nexus.plans.verb_infer import infer_verb

    _run, available = _nx_answer_caller_bindings(question=question, scope=scope, bindings=bindings)
    cache = get_t1_plan_cache(populate_from=library)
    matches = plan_match(
        question, library=library, cache=cache, dimensions=None, scope_preference=scope,
        context=None, min_confidence=_PLAN_MATCH_MIN_CONFIDENCE if min_confidence is None else min_confidence, n=5,
        available_bindings=available, category_verb=infer_verb(question), eligibility=eligibility,
    )
    keys = [plan_label_key(m.dimensions, m.tags) for m in matches]
    hit = bool(matches) and _nx_answer_match_is_hit(matches[0].confidence, threshold=_PLAN_MATCH_MIN_CONFIDENCE)
    return PlanSelection(
        keys[0] if hit else None, keys, [m.confidence for m in matches], [m.plan_id for m in matches],
    )


def library_match(library, question: str, *, scope: str = "", bindings: dict[str, Any] | None = None,
                  dimensions: dict[str, Any]):
    """The seeded library's own ``Match`` (real plan id, stored default bindings) the matcher offers for
    *question* under *dimensions*, asked as ``nx_answer`` asks (same bindings, availability set and
    ``n``) but with the confidence floor open. Used to pin the plan of a production journey."""
    from nexus.mcp.core import _nx_answer_caller_bindings
    from nexus.mcp_infra import get_t1_plan_cache
    from nexus.plans.matcher import plan_match
    from nexus.plans.verb_infer import infer_verb

    _run, available = _nx_answer_caller_bindings(question=question, scope=scope, bindings=bindings)
    matches = plan_match(
        question, library=library, cache=get_t1_plan_cache(populate_from=library), dimensions=dimensions,
        scope_preference=scope, context=None, min_confidence=0.0, n=5, available_bindings=available,
        category_verb=infer_verb(question),
    )
    assert matches, f"the seeded library offers no plan for {dimensions}"
    return matches[0]


#: The first operator step of the factual-lookup plan (0-based index into plan_json steps): the
#: rank step. Cutting here runs the four retrieval steps and withholds rank and generate.
LOOKUP_PLAN_FIRST_OPERATOR_STEP = 4


@dataclass
class RetrievalRun:
    """Pre-reduction evidence of one factual-lookup run: the references each retrieval stream
    produced and what was hydrated for the reducer."""

    search_ids: list[str]
    search_collections: list[str]
    traverse_ids: list[str]
    traverse_ids_truncated: dict[str, int] | None
    vector_contents: list[str]
    graph_contents: list[str]
    resolved_search_corpus: str
    cut_applied: bool
    #: ``PlanResult.scope_diagnostics``: what the caller's scope did to each retrieval step.
    scope_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    #: The traverse step's policy report and per-id collections (nexus-jypmj.1.5).
    traverse_report: dict[str, Any] = field(default_factory=dict)
    traverse_chunk_collections: list[str] = field(default_factory=list)


async def run_lookup_retrieval(
    question: str, *, scope: str = "", bindings: dict[str, Any] | None = None, owners: str = "",
) -> RetrievalRun:
    """Run the factual-lookup builtin's retrieval steps through ``plan_run``, with the call-shape
    ``nx_answer`` gives it, and stop before the first operator step (no model call). *owners* is the
    ``_nx_owners`` binding ``nx_answer`` sets for an explicit owner (nexus-jypmj.1.18)."""
    from nexus.plans.runner import plan_run

    match = builtin_match("hybrid-factual-lookup", "lookup")
    run_bindings: dict[str, Any] = {"question": question, "intent": question}
    if scope:
        run_bindings["_nx_scope"] = scope
    if owners:
        run_bindings["_nx_owners"] = owners
    run_bindings.update(bindings or {})
    result = await plan_run(
        match, run_bindings, continuation_cut_at_step=LOOKUP_PLAN_FIRST_OPERATOR_STEP,
    )
    s = result.steps
    search, traverse = s[0], s[1]
    corpus = next((r.get("corpus", "") for r in result.resolved_step_args if r.get("tool") == "search"), "")
    return RetrievalRun(
        search_ids=list(search.get("ids", [])),
        search_collections=list(search.get("collections", [])),
        traverse_ids=list(traverse.get("ids", [])),
        traverse_ids_truncated=traverse.get("ids_truncated"),
        vector_contents=list(s[2].get("contents", [])),
        graph_contents=list(s[3].get("contents", [])),
        resolved_search_corpus=corpus,
        cut_applied=result.continuation_cut_applied,
        scope_diagnostics=[dict(d) for d in result.scope_diagnostics],
        traverse_report=dict(traverse.get("traversal") or {}),
        traverse_chunk_collections=list(traverse.get("chunk_collections") or []),
    )


# ── observations ────────────────────────────────────────────────────────────


def evidence_from_ids(fx: Fixture, chashes: list[str]) -> list:
    """Evidence items for *chashes*, first occurrence kept (a chunk referenced by two streams is
    one piece of evidence), each labeled from the fixture's oracle."""
    seen: set[str] = set()
    out = []
    for c in chashes:
        if c not in seen:
            seen.add(c)
            out.append(fx.evidence_item(c))
    return out


def evidence_from_contents(fx: Fixture, contents: list[str]) -> list:
    """Evidence items for hydrated chunk *contents* (a stored piece hashes to its chunk id)."""
    return evidence_from_ids(fx, [chash_of(c) for c in contents])


def count_by_corpus(items: list) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        key = it.corpus or "unattributed"
        out[key] = out.get(key, 0) + 1
    return out


def request_call_shape(q) -> tuple[str, dict[str, Any] | None]:
    """The ``scope`` and ``bindings`` a caller would give ``nx_answer`` for golden request *q*: the
    request's own scope, and for an ``rdr`` scope the ``content_type`` binding the recorded ART call
    carried. Everything else is the bare question."""
    if q.scope == "rdr":
        return "rdr", {"content_type": "rdr"}
    return q.scope, None


def golden_source_bindings(q, owners: dict[str, str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """The reserved ``nx_answer`` bindings for golden request *q*'s ``required_sources`` and the
    labels no ``SourceRef`` can name (nexus-jypmj.1.8).

    Each label goes through ``SourceRef.parse_labeled``: ``RDR-084@ART`` is ``source=RDR-084`` plus the
    project ``ART``, which *owners* maps to an owner tumbler (``owner`` binding; a project with no
    mapping is not guessed and travels as the ``project`` binding). A bare id (``RDR-049``) has no
    qualifier, so its binding carries no owner: where sibling files share the number it resolves to
    the file whose frontmatter carries the id (nexus-xho6c). A label that is not
    an RDR id or a tumbler (the paper title ``HybridRAG``, G05) is returned in the second list and is
    scored by its label only.
    """
    from nexus.plans.evidence import SourceRef, SourceRefError

    sources: list[str] = []
    qualifiers: set[str] = set()
    label_only: list[str] = []
    for label in q.required_sources:
        try:
            ref, project = SourceRef.parse_labeled(label)
        except SourceRefError:
            label_only.append(label)
            continue
        sources.append(ref.value)
        if project:
            qualifiers.add(project)
    out: dict[str, Any] = {}
    if sources:
        out["source"] = sources[0] if len(sources) == 1 else sources
    if len(qualifiers) == 1:
        (project,) = qualifiers
        owner = (owners or {}).get(project)
        if owner:
            out["owner"] = owner
        else:
            out["project"] = project
    return out, label_only


def head_sha() -> str:
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def tree_is_dirty() -> bool:
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
        check=True, capture_output=True, text=True,
    )
    return bool(out.stdout.strip())
