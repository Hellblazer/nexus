---
title: "RDR-103: Catalog as Collection-Name Authority"
id: RDR-103
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-03
accepted_date:
related_issues: [nexus-qpet, nexus-qpet.4, nexus-2r71]
related_tests: []
implementation_notes: ""
related: [RDR-101]
---

# RDR-103: Catalog as Collection-Name Authority

Formalizes the collection-name tuple with canonical embedding model names, and consolidates naming authority in `Catalog`.

RDR-101 Phase 6 introduced the conformant collection-name schema `<content_type>__<owner_id>__<embedding_model>__v<n>` and the `strict_collection_naming` opt-in flag, but stopped short of moving naming authority into the catalog. The indexer family still constructs collection names from repo-path hashes (`registry._collection_name(repo)` returns `code__nexus-8c2e74c0`); the catalog records whatever name the indexer chose. Doctor's `--collections-drift` check fires every release shakedown because fresh indexes continue to emit legacy 2-segment names. The prophylactic-review remediation (epic `nexus-qpet`) deferred two beads (`nexus-2r71` strict-flip, `nexus-qpet.4` indexer rewrite) because doing them piecemeal would thread a flag through 30+ call sites without addressing the underlying split: naming authority lives in two places.

This RDR consolidates naming authority in `Catalog`. The collection name becomes a formal tuple computed from `(content_type, owner, embedding_model, model_version)`; the indexer asks the catalog for a collection rather than constructing one and asking the catalog to record it. The strict-naming check collapses from a 30-site flag-threading exercise to a one-line property of a single helper. Embedding-model identity becomes part of the collection's surface, matching the user-visible reality that switching embedding models necessarily creates a new collection (the vectors are not compatible).

## Problem Statement

The current collection-name surface has three structural problems that compound over time:

1. **Authority is split.** `registry.py:64-91`'s `_collection_name` / `_docs_collection_name` / `_rdr_collection_name` build names from `_repo_identity()` (a git-remote-URL hash). The indexer passes the constructed name to `db.get_or_create_collection(name)`. The catalog learns the name only at hook-time when a `Document` is registered with `physical_collection=name`. Two writers pick names; the catalog passively records both choices.

2. **Embedding model is implicit.** A `code__nexus-8c2e74c0` collection embedded with `voyage-code-3` and a separate `code__nexus-8c2e74c0` collection embedded with a future `voyage-code-3.5` would have the same physical name. Switching models requires either re-indexing in place (which mixes incompatible vectors during the transition) or operator-driven rename. The conformant `__voyage-code-3__v1` schema makes the model part of the identity but only *projects* the value the indexer happened to use; nothing forces consistency.

3. **Strict-naming enforcement scales linearly with caller count.** Phase 6 shipped `T3Database.strict_collection_naming` as an opt-in flag. Flipping the default means every existing caller must explicitly opt out (~30 production sites + ~26 test mocks per the `nexus-qpet` audit). Threading the flag is preparation work with no user-visible value at the time of the threading commit. Catalog-side authority replaces this with one helper: the helper always emits conformant names; non-conformant names are unreachable from new writes regardless of any flag state.

The migration path for existing legacy collections is the design question that has stalled progress on this in past attempts. The operator has a working catalog with hundreds of legacy-named collections, real chunks in T3 keyed off those names. Any migration that requires a corpus-wide re-index is a non-starter.

## Context

This RDR originated from the RDR-101 Phase 6 prophylactic-review remediation arc (epic `nexus-qpet`, branch `feature/rdr-101-remediation`, PR #491). Two of the nine child beads were deferred when their pre-condition surface area exposed the underlying authority-split problem:

- `nexus-2r71` (flip strict_collection_naming default to True, irreversible). Required threading `strict=False` through 30+ callers as a defensive pre-condition. The threading-without-flip has zero user-visible value; the flip-with-threading needs the indexer to actually emit conformant names first.
- `nexus-qpet.4` (indexers emit conformant collection names, filed during the deferral). Would touch `registry.py`, `indexer.py`, `doc_indexer.py`, `pipeline_stages.py`, `commands/index.py`. Each site duplicates the same naming logic with slight variations.

Both beads block on the same architectural decision: where naming authority lives. The skill-level fix is "rewrite each indexer to construct conformant names." The RDR-level fix is "the catalog constructs the name; the indexer asks for it." Doing the former without the latter just reshuffles the duplicate logic.

RDR-101 Phase 6 defined the conformant schema regex (`src/nexus/corpus.py:46-51`) and the parse helper (`parse_conformant_collection_name`). It also added `Catalog.tumbler_to_collection_segment` (a tumbler dotted form like `1.5` becomes the conformant segment `1-5`). The infrastructure for the rewrite is in place; only the authority shift is pending.

## Research Findings

### Current collection-name construction

Survey via `grep -rn 'f"code__\|f"docs__\|f"rdr__\|f"knowledge__\|_collection_name\|_make_collection' src/nexus/`:

- `src/nexus/registry.py:64`: `_collection_name(repo)` constructs `code__<basename>-<sha256[:8]>` from repo path.
- `src/nexus/registry.py:76`: `_docs_collection_name(repo)`, same shape with `docs__` prefix.
- `src/nexus/registry.py:85`: `_rdr_collection_name(repo)`, same shape with `rdr__` prefix.
- `src/nexus/doc_indexer.py:599, 1102, 1463`: inline `f"docs__{corpus}"` for the standalone PDF / markdown indexers.
- `src/nexus/indexer.py:617, 625, 1003, 1005, 1213, 1399, 1592`: consumers of the `registry.py` helpers.

Each call site duplicates the same content-type-prefix logic. None consults the catalog for the owner segment; all derive owner-like identity from repo-path or corpus-string state.

### Tuple definition (current state)

`src/nexus/corpus.py:46-93`:

```python
_CONFORMANT_COLLECTION_RE = re.compile(
    r"^(?P<ct>code|docs|rdr|knowledge)"
    r"__(?P<owner>[a-zA-Z0-9-]+)"
    r"__(?P<model>[a-z][a-z0-9-]*)"
    r"__v(?P<ver>\d+)$"
)
```

The schema is defined but not formalized as a Python type. `parse_conformant_collection_name` returns a `dict[str, str]` with canonical keys but no validation beyond the regex. There is no "build a name from a tuple" inverse helper today.

### Embedding-model identity (current state)

`src/nexus/corpus.py`'s `voyage_model_for_collection` and `index_model_for_collection`: the model is derived from the content-type prefix (`code__` maps to `voyage-code-3`, `docs__` maps to `voyage-context-3`). The model is consistent within a content type but not pinned per-collection. A future model bump within a content type (e.g. `voyage-code-3` to `voyage-code-4`) requires either a global cutover or per-collection branch handling that doesn't exist.

### Migration path (existing infrastructure)

Verified primitives already on develop:

- `Catalog.rename_collection` (Phase 6): atomic T3-then-catalog rename with rollback. Already tested (`tests/test_catalog_rename_collection.py`, 14 cases).
- `Catalog.update_documents_collection_batch` (`nexus-qpet.3`): single-flock per-document re-point.
- `nx catalog migrate-fallback` (Phase 6): full per-document retarget path. Overkill for the legacy-to-conformant case where every document of one repo lands in the same target.

The `rename_collection` verb is the right primitive: legacy-to-conformant is a 1:1 rename, not a 1:N retarget.

## Proposed Solution

### Tuple as Python type

Define a frozen dataclass that encodes the conformant tuple:

```python
@dataclass(frozen=True, slots=True)
class CollectionName:
    """Formal tuple identity for a T3 collection.

    The four-segment canonical form
    ``<content_type>__<owner_id>__<embedding_model>__v<n>`` is the
    sole conformant shape per RDR-101 §"Collection naming". Legacy
    2-segment names (``code__nexus-8c2e74c0``) are
    grandfathered-readable but never freshly constructed.
    """
    content_type: str       # one of CONTENT_TYPES (code|docs|rdr|knowledge)
    owner_id: str           # tumbler segment (dots-to-hyphens)
    embedding_model: str    # canonical voyage-* name; pinned at construction
    model_version: int      # monotonically-bumped on incompatible changes

    def render(self) -> str:
        return (
            f"{self.content_type}__{self.owner_id}__"
            f"{self.embedding_model}__v{self.model_version}"
        )

    @classmethod
    def parse(cls, name: str) -> "CollectionName":
        # delegates to nexus.corpus.parse_conformant_collection_name
        ...
```

Lives in `src/nexus/catalog/collection_name.py` (new module so the type is importable from both `Catalog` and the indexer family without circulating through `corpus.py`).

### Canonical embedding names

A new module-level constant in `src/nexus/corpus.py` enumerates the embedding models that may appear in a `CollectionName.embedding_model` segment:

```python
CANONICAL_EMBEDDING_MODELS: frozenset[str] = frozenset({
    "voyage-context-3",
    "voyage-code-3",
    # additions go here; never silently expand the regex without
    # bumping a CollectionName.model_version somewhere.
})
```

The `_CONFORMANT_COLLECTION_RE` model group (`[a-z][a-z0-9-]*`) stays permissive at the regex level so legacy collections with unexpected models still parse, but `CollectionName.parse` validates the model against `CANONICAL_EMBEDDING_MODELS` and raises a clear error on unknown models. The "what's our canonical embedding identity" question gets a single answer.

### Catalog as naming authority

Add `Catalog.collection_for(content_type, owner, embedding_model)` that returns a `CollectionName`. Behaviour:

1. Owner is a `Tumbler` (or string-castable to one). The catalog converts to the segment via the existing `tumbler_to_collection_segment` helper.
2. Embedding model is validated against `CANONICAL_EMBEDDING_MODELS`.
3. The catalog looks up the highest existing `model_version` for `(content_type, owner_id, embedding_model)` in the `collections` projection. New `(c, o, m)` tuple returns `v1`. Existing tuple at `vN` returns `vN` (re-index of the same model continues to land in the same collection).
4. Caller writes to the collection using the rendered name. The collection is registered in the catalog at the point of first write via the existing `register_collection` flow.

Indexer family change: replace every `registry._collection_name(repo)` and `f"docs__{corpus}"` call site with `cat.collection_for(content_type, repo_owner, model).render()`. The repo-owner lookup uses the existing `Catalog.owner_for_repo(repo_hash)` path; if no owner exists yet, the indexer registers one (the same upfront `_catalog_hook_repo` flow that already exists for the doc_id-resolver closure).

The strict-naming enforcement collapses to a property of `CollectionName.render`: any name produced by `render()` is conformant by construction, no flag needed. The `T3Database.strict_collection_naming` flag stays as a defensive guard against direct callers (tests, plugins, future code paths) that try to pass non-conformant strings, but it becomes a backstop, not a contract surface.

### Version-bump semantics

`model_version` bumps when an existing `(content_type, owner_id, embedding_model)` tuple needs a fresh collection without colliding with the existing one. Trigger conditions:

1. **Operator-initiated re-index with explicit version flag** (`nx index repo --bump-version`). Catalog assigns `vN+1`. Existing `vN` collection remains for the migration window; operator supersedes via existing verbs when ready.
2. **Embedding-model schema break** within the same model name (e.g. dimensionality change). Detected at write-time by the embedder; raises a clear error pointing at the bump verb.

What does NOT bump the version: routine re-index of the same model, content-type changes (those produce a different `content_type` segment), repo identity changes (those produce a different `owner_id`).

### Repo-owner identity stability

`_repo_identity()` in `src/nexus/registry.py` derives the repo hash from `git config --get remote.origin.url`. Cross-clone of the same repo (different absolute path, same remote) produces the same hash, the same tumbler, the same `owner_id` segment, and therefore the same conformant collection name. This invariant is implicit today; the conformant name surface makes it user-visible and an explicit test locks it in.

### Migration path

On first `nx index` after RDR-103 lands, the indexer:

1. Computes the conformant target via `cat.collection_for(...)`.
2. Looks up the legacy name via the existing repo registry (`_collection_name(repo)`).
3. If the legacy collection exists in T3 and the conformant target does not: invoke `Catalog.rename_collection(legacy, conformant)` atomically. Single one-line operator message: `Upgraded legacy collection X to Y.`
4. If the conformant target already exists: index proceeds against it directly; legacy collection (if present) is left for the operator to clean up via existing tools (`nx t3 gc`, `nx catalog rename-collection`, etc.).

The migration is one-shot per repo per content-type. Operators with hundreds of legacy collections see one upgrade message per collection, then the message stops appearing on subsequent indexes.

## Alternatives Considered

**(a) Thread `strict=False` through 30 callers (the `nexus-2r71` original plan).** Rejected because it preserves the authority split: naming logic still duplicated across registry + 4 indexer modules + commands. Threading is preparation for a flip that re-creates the same problem at the next caller boundary.

**(b) Keep current shape, just lock the prefix content-type.** Rejected because it leaves embedding-model identity implicit and gives no path to model bumps.

**(c) New module-level naming helper called from each indexer.** A weaker version of the proposal that doesn't centralize authority in the catalog. Rejected because the catalog needs to be the source of truth for `model_version` bumps and owner-tumbler mapping; co-locating the helper outside the catalog forces a catalog round-trip on every name construction anyway.

**(d) Re-index everything to conformant names instead of renaming.** Rejected as operator-hostile (terabytes of T3 chunks re-embedded). The rename verb is the surgical primitive.

## Trade-offs

**Positive:**

- Single source of truth for collection names; `nexus-2r71` becomes a 5-line patch to the helper instead of 30-site threading.
- Embedding-model identity becomes user-visible, matching reality.
- Repo-owner identity stability locks across clones.
- Migration is one-shot per repo, not corpus-wide re-index.
- Tests stop hardcoding `code__nexus-XXXXX` strings; they use `CollectionName(...)` directly.

**Costs:**

- One new module (`catalog/collection_name.py`) and one new method on `Catalog` (`collection_for`).
- Indexer family touches 4 modules at ~5 sites each. Mechanical.
- Test churn: every test that hardcodes a legacy collection name needs updating. Estimated ~20-30 test sites.
- Migration message is operator-visible, which means it has to be documented and locked into a test (the operator should not see it twice for the same collection).

**Risks:**

- Repo-identity drift: if `_repo_identity()` ever changes its hash scheme (it has, historically), the owner segment shifts underneath the operator. Mitigation: the existing `Catalog.owner_for_repo` already maps stable across hash schemes via the catalog row.
- Embedding-model expansion: every new canonical model needs a manual addition to `CANONICAL_EMBEDDING_MODELS`. Mitigation: forcing the addition is the point; silent model proliferation is the failure mode this prevents.

## Implementation Plan

### Prerequisites

- RDR accepted and gated.
- `nexus-qpet` remediation arc (PR #491) merged.
- Phase 6 shipped on develop (already done as of 2026-05-03).

### Phase 1: tuple type + canonical model set

1. New module `src/nexus/catalog/collection_name.py` with `CollectionName` dataclass plus `render` and `parse`.
2. New constant `CANONICAL_EMBEDDING_MODELS` in `src/nexus/corpus.py`.
3. Tests: round-trip render-parse, model validation, segment validation. Lock the regex contract against the new type.

### Phase 2: catalog naming authority

4. Add `Catalog.collection_for(content_type, owner, embedding_model)` returning `CollectionName`.
5. Internal version-bump helper that reads the highest existing `model_version` from the projection.
6. Tests: new (c, o, m) returns v1; existing returns same vN; explicit `--bump` returns vN+1.

### Phase 3: indexer rewrite

7. Replace `registry._collection_name` family with calls to `cat.collection_for(...).render()`. Keep the legacy helpers exported with a deprecation warning for one release cycle so external plugins have a window.
8. Update `doc_indexer.py:599/1102/1463` inline name construction.
9. Update `pipeline_stages.py`, `indexer.py`, `commands/index.py`.

### Phase 4: migration on first index

10. In `_catalog_hook_repo`: detect legacy collection, compute conformant target, invoke `rename_collection` if needed.
11. Operator-visible one-line "upgraded" message; idempotent (no second message on subsequent indexes).
12. Lock in a test that re-indexing after the upgrade does not re-emit the message.

### Phase 5: strict-flip + flag drop

13. Flip `T3Database.strict_collection_naming` default to True (`nexus-2r71` collapses to this commit).
14. Remove the flag in a follow-up.
15. Drop the legacy registry helpers after their deprecation window.

### Day 2 operations

- `nx catalog doctor --collections-drift` exits 0 cleanly on a fresh greenfield index.
- Operators with legacy collections see one upgrade message per collection on the first index after the upgrade.
- A failing model bump (e.g. dimensionality change) raises a clear `nx index --bump-version` hint instead of corrupting vectors.

## Validation

- `tests/test_catalog_collection_name.py` (new): locks the tuple type contract.
- `tests/test_catalog_collection_for.py` (new): locks the catalog naming-authority contract including version-bump semantics.
- `tests/test_indexer_conformant_names.py` (new): locks the indexer family on conformant naming.
- `tests/test_collection_name_migration.py` (new): locks the legacy-to-conformant rename on first index.
- Existing tests update to use `CollectionName(...)` instead of hardcoded legacy strings (~20-30 sites).

## Open Questions

- Should `CollectionName` be exposed in `nexus.catalog`'s public `__all__`, or kept module-internal? Plugins consuming chunk metadata may want to introspect the tuple.
- Operator UX for the upgrade message: stderr line per collection is fine for ~10 collections. Operators with hundreds of collections (the current state for the project) may want a single summary line with a count plus a `--quiet` flag.
- Deprecation window for the legacy registry helpers (one release? two?). The helpers are not part of the public API but external plugins may import them.
