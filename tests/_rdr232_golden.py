# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 golden-set support (nexus-jypmj.1.1): the self-contained ART RDR-084 fixture and the
observers that turn a run into ``bench.answer_shape_metrics.RunObservation`` records.

The fixture reproduces, on the engine substrate and without reading any live corpus, the evidence
mix of the 2026-10-23 incident: an ART RDR-084 and a Nexus RDR-084 under two repo owners, plus
more than 300 code chunks reachable from them through ``implements`` and ``relates`` links, so the
UNMODIFIED runner running the factual-lookup plan gathers a code-dominated evidence set.

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
    counts = {"code_chunks": 0, "links": 0}

    with HttpCatalogClient() as cat:
        art_owner = str(cat.register_owner("art-rdr232-fixture", "repo", repo_hash="rdr232-fx-art"))
        nexus_owner = str(cat.register_owner("nexus-rdr232-fixture", "repo", repo_hash="rdr232-fx-nexus"))

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

    return Fixture(
        art_owner=art_owner, nexus_owner=nexus_owner, art_rdr=art_rdr, nexus_rdr=nexus_rdr,
        art_rdr_collection=_owner_collection("rdr", art_owner),
        nexus_rdr_collection=_owner_collection("rdr", nexus_owner),
        art_code_collection=_owner_collection("code", art_owner),
        nexus_code_collection=_owner_collection("code", nexus_owner),
        code_docs=code_docs, chunks=fx_chunks,
        code_chunk_count=counts["code_chunks"], link_count=counts["links"],
    )


# ── observers ───────────────────────────────────────────────────────────────


def builtin_plan_key(dimensions: dict[str, Any]) -> str:
    """``<verb>:<strategy>``: the plan identity golden_v1.yaml's unacceptable_plans uses (several
    builtins are all named ``default``, so the name alone does not identify one)."""
    return f"{dimensions.get('verb', '')}:{dimensions.get('strategy', '')}"


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


def observe_plan_selection(
    library, question: str, *, scope: str = "", bindings: dict[str, Any] | None = None,
) -> PlanSelection:
    """What the UNMODIFIED matcher offers and picks for *question*, called as ``nx_answer`` calls it
    (same bindings, availability set, floor, ``n`` and category verb). A plan miss selects ``None``."""
    from nexus.mcp.core import _PLAN_MATCH_MIN_CONFIDENCE, _nx_answer_caller_bindings, _nx_answer_match_is_hit
    from nexus.mcp_infra import get_t1_plan_cache
    from nexus.plans.matcher import plan_match
    from nexus.plans.verb_infer import infer_verb

    _run, available = _nx_answer_caller_bindings(question=question, scope=scope, bindings=bindings)
    cache = get_t1_plan_cache(populate_from=library)
    matches = plan_match(
        question, library=library, cache=cache, dimensions=None, scope_preference=scope,
        context=None, min_confidence=_PLAN_MATCH_MIN_CONFIDENCE, n=5,
        available_bindings=available, category_verb=infer_verb(question),
    )
    keys = [builtin_plan_key(m.dimensions) for m in matches]
    hit = bool(matches) and _nx_answer_match_is_hit(matches[0].confidence, threshold=_PLAN_MATCH_MIN_CONFIDENCE)
    return PlanSelection(keys[0] if hit else None, keys, [m.confidence for m in matches])


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


async def run_lookup_retrieval(question: str, *, scope: str = "", bindings: dict[str, Any] | None = None) -> RetrievalRun:
    """Run the factual-lookup builtin's retrieval steps through the UNMODIFIED ``plan_run``, with
    the call-shape ``nx_answer`` gives it, and stop before the first operator step (no model call)."""
    from nexus.plans.runner import plan_run

    match = builtin_match("hybrid-factual-lookup", "lookup")
    run_bindings: dict[str, Any] = {"question": question, "intent": question}
    if scope:
        run_bindings["_nx_scope"] = scope
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
