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

The current collection-name surface has three structural gaps that compound over time, plus a migration constraint that has stalled prior remediation attempts.

#### Gap 1: Authority is split

`registry.py:64-91`'s `_collection_name` / `_docs_collection_name` / `_rdr_collection_name` build names from `_repo_identity()` (a path-derived hash, not remote-derived). The indexer passes the constructed name to `db.get_or_create_collection(name)`. The catalog learns the name only at hook-time when a `Document` is registered with `physical_collection=name`. Two writers pick names; the catalog passively records both choices. There is no single point that enforces conformance, so any indexer (today or future) is free to mint legacy-shaped names regardless of catalog state.

#### Gap 2: Embedding model is implicit

A `code__nexus-8c2e74c0` collection embedded with `voyage-code-3` and a separate `code__nexus-8c2e74c0` collection embedded with a future `voyage-code-3.5` would have the same physical name. Switching models requires either re-indexing in place (which mixes incompatible vectors during the transition) or operator-driven rename. The conformant `__voyage-code-3__v1` schema makes the model part of the identity but only *projects* the value the indexer happened to use; nothing forces consistency, and no path bumps the version when the model changes.

#### Gap 3: Strict-naming enforcement scales linearly with caller count

Phase 6 shipped `T3Database.strict_collection_naming` as an opt-in flag. Flipping the default means every existing caller must explicitly opt out (~30 production sites + ~26 test mocks per the `nexus-qpet` audit). Threading the flag is preparation work with no user-visible value at the time of the threading commit. Catalog-side authority replaces this with one helper: the helper always emits conformant names; non-conformant names are unreachable from new writes regardless of any flag state.

#### Gap 4: Migration path constrains the design

The operator has a working catalog with hundreds of legacy-named collections, real chunks in T3 keyed off those names. Any migration that requires a corpus-wide re-index is a non-starter. Prior attempts at this rewrite stalled on this constraint; the design must use the existing `Catalog.rename_collection` primitive (atomic 1:1 T3-then-catalog rename with rollback) rather than retargeting documents.

## Context

This RDR originated from the RDR-101 Phase 6 prophylactic-review remediation arc (epic `nexus-qpet`, branch `feature/rdr-101-remediation`, PR #491). Two of the nine child beads were deferred when their pre-condition surface area exposed the underlying authority-split problem:

- `nexus-2r71` (flip strict_collection_naming default to True, irreversible). Required threading `strict=False` through 30+ callers as a defensive pre-condition. The threading-without-flip has zero user-visible value; the flip-with-threading needs the indexer to actually emit conformant names first.
- `nexus-qpet.4` (indexers emit conformant collection names, filed during the deferral). Would touch `registry.py`, `indexer.py`, `doc_indexer.py`, `pipeline_stages.py`, `commands/index.py`. Each site duplicates the same naming logic with slight variations.

Both beads block on the same architectural decision: where naming authority lives. The skill-level fix is "rewrite each indexer to construct conformant names." The RDR-level fix is "the catalog constructs the name; the indexer asks for it." Doing the former without the latter just reshuffles the duplicate logic.

RDR-101 Phase 6 defined the conformant schema regex (`src/nexus/corpus.py:46-51`) and the parse helper (`parse_conformant_collection_name`). The tumbler-to-segment translation lives as a module-private helper `_owner_segment_for_tumbler` at `src/nexus/commands/catalog.py:606` (it converts `1.5.42` to `1-5` by calling `_owner_prefix_of` from `nexus.catalog.synthesizer` and replacing dots with hyphens). It is currently used only by the migrate-fallback verb. Promoting it to a `Catalog` method (or to a module-level helper in `nexus.catalog`) is part of this RDR's scope; the logic exists but its location is not yet a contract surface.

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
- `collections.superseded_by` column (Phase 6 schema): set by `Catalog.supersede_collection` and by `Catalog.rename_collection` internally. Reverse lookup of "what is the conformant successor of legacy name X?" is a single SQL query.

The `rename_collection` verb is the right primitive: legacy-to-conformant is a 1:1 rename, not a 1:N retarget.

### Affected user-facing and internal surfaces

Surveys (verified):

- `grep -rn 'physical_collection\b' src/nexus/ | wc -l`: 246 occurrences. Most are projector reads or test assertions; the field is widely consumed.
- `grep -rn '_collection_name\|_docs_collection_name\|_rdr_collection_name' src/ nx/ sn/`: 78 callers across the repo, plugins, and skills. All must switch to the new path or stop importing.
- MCP surface (`src/nexus/mcp/core.py`): `collection_info`, `store_get`, `store_put`, `search`, and several other tools accept a literal `name` argument.
- Search routing in `src/nexus/search/` and CLI commands accept collection names directly.

### Plugin layer surfaces

The `nx/` plugin tree carries its own references. Two kinds:

**Active name-constructing code** (treat like the indexer family):

- `nx/hooks/scripts/rdr_hook.py:58, 269`: writes `target = f"rdr__{repo_name}"`. Constructs a legacy 2-segment name; needs to go through `Catalog.collection_for(...)` like the indexer family does.
- `nx/skills/rdr-close/SKILL.md`: writes post-mortems to `knowledge__rdr_postmortem__{repo}` (lines 220, 231, 244, 278, 296, 309). **Decision (pinned)**: this collapses into the conformant `knowledge__<owner_id>__<model>__v1` shape, where `owner_id` is the repo's tumbler segment (same as every other knowledge collection for that repo). The `rdr_postmortem` semantic is encoded at the document level via the `category` field (or `tags`), NOT in the collection name. Rationale: the conformant schema's `owner_id` segment is a single tumbler-derived identifier; mixing in a literal `rdr_postmortem-` prefix would require a new escape rule and would diverge from how every other curated knowledge collection is named. Document-level routing via `where={"category": "rdr_postmortem"}` is already a supported retrieval pattern. This pulls `rdr_hook.py` and `rdr-close/SKILL.md` into Phase 3 (indexer rewrite) scope, not Phase 6.

**Documentation and examples** (search-and-replace pass):

- `nx/agents/codebase-deep-analyzer.md:96`: example `code__<repo>`.
- `nx/agents/deep-research-synthesizer.md:112, 116`: examples `knowledge__art`, `code__<repo>`.
- `nx/agents/_shared/ERROR_HANDLING.md:97`, `_shared/CONTEXT_PROTOCOL.md:131`: example shapes in agent docstrings.
- `nx/skills/nexus/SKILL.md`, `nexus/reference.md`, `review/SKILL.md`, `debug/SKILL.md`, `rdr-gate/SKILL.md`: example collection names in skill instructions.
- `nx/commands/pdf-process.md:39`: example `--collection knowledge__<corpus>`.

Compatibility implication accepted as breakage: with a user base of two operators, scripted MCP calls and saved CLI invocations referencing legacy names are expected to fail loudly post-rename. The operator updates the call once. No transparent-redirect shim; no deprecation chain follow. The surface audit above stands as a guide for *what to update*, not as a list of compat surfaces to preserve.

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

**Parse-vs-readable contract**: `CollectionName.parse(name)` is for new conformant names only and raises `ValueError` on legacy 2-segment names or unknown embedding models. Code paths that receive a `physical_collection` field from catalog rows (where legacy values still appear during the migration window) must gate with `is_conformant_collection_name(name)` before calling `CollectionName.parse(name)`. Legacy collection names remain valid T3 strings and remain readable per RDR-101 Phase 6's grandfathering invariant; they are simply not valid `CollectionName` instances. `is_conformant_collection_name` is the bridge for any caller that needs to operate generically across both shapes.

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

Indexer family change: replace every `registry._collection_name(repo)` and `f"docs__{corpus}"` call site with `cat.collection_for(content_type, repo_owner, model).render()`. The repo-owner lookup uses the existing `Catalog.owner_for_repo(repo_hash)` path (`catalog.py:1093-1097`); if no owner exists yet, the indexer registers one (the same upfront `_catalog_hook_repo` flow that already exists for the doc_id-resolver closure). The tumbler-to-segment translation is the existing `_owner_segment_for_tumbler` (`commands/catalog.py:606`), promoted to a `Catalog` method as part of this RDR so it has a stable import path.

The strict-naming enforcement collapses to a property of `CollectionName.render`: any name produced by `render()` is conformant by construction, no flag needed. The `T3Database.strict_collection_naming` flag stays as a defensive guard against direct callers (tests, plugins, future code paths) that try to pass non-conformant strings, but it becomes a backstop, not a contract surface.

### Version-bump semantics

`model_version` bumps when an existing `(content_type, owner_id, embedding_model)` tuple needs a fresh collection without colliding with the existing one. Trigger conditions:

1. **Operator-initiated re-index with explicit version flag** (`nx index repo --bump-version`). Catalog assigns `vN+1`. Existing `vN` collection remains for the migration window; operator supersedes via existing verbs when ready.
2. **Embedding-model schema break** within the same model name (e.g. dimensionality change). Detected at write-time by the embedder; raises a clear error pointing at the bump verb.

A model-NAME change (e.g. Voyage renames `voyage-code-3` to `voyage-code-3-fast`) is NOT a version bump on the existing tuple. The new name is a different `embedding_model` value, so `Catalog.collection_for(c, o, "voyage-code-3-fast")` finds no existing row and naturally returns `v1` for the new tuple. The old `(c, o, "voyage-code-3")` `v1` collection persists alongside until the operator supersedes it. Operator workflow when adding a new model to `CANONICAL_EMBEDDING_MODELS`:

- For each affected `(content_type, owner_id)` pair, run `nx catalog supersede-collection <old> <new>` once the new collection has been populated.
- The doctor's `--collections-drift` check surfaces any orphaned old-model collections that have not been superseded.

What does NOT bump the version: routine re-index of the same model, content-type changes (those produce a different `content_type` segment), repo identity changes (those produce a different `owner_id`), model-name changes (those produce a different `embedding_model` segment and a fresh `v1` per the workflow above).

### Repo-owner identity stability

`_repo_identity()` in `src/nexus/registry.py:17-48` derives the repo hash from `git rev-parse --git-common-dir` (resolving worktrees to their main repo path) then SHA-256 of the resolved path string, truncated to 8 hex characters. Identity is **path-derived, not remote-derived**: two clones of the same repo on different absolute paths produce different `repo_hash` values, different `Catalog.owner_for_repo` rows, different tumblers, and therefore different conformant `owner_id` segments.

What stays stable:

- Worktrees of the same repo (the `--git-common-dir` resolution).
- Re-indexing the same repo at the same path (deterministic SHA-256).
- The owner row in `Catalog.owner_for_repo` once it has been minted: even if `_repo_identity`'s scheme changes in a future release, existing rows keyed by the old hash continue to resolve via the row's `repo_root` column (`catalog_db.py:26-28`).

What does NOT stay stable:

- Cross-machine clones (different absolute paths produce different hashes).
- Renamed repo directories (the basename component of the rendered path changes the hash).

The conformant name surface exposes this previously-implicit behavior. Users who clone the same repo to two machines today already get separate `code__nexus-XXXXXXXX` collections with different `XXXXXXXX` hashes; the conformant rewrite continues that semantics with the suffix replaced by tumbler-derived `owner_id`. A separate decision (out of scope for this RDR) could shift `_repo_identity` to hash the git remote URL for cross-machine identity, but that would invalidate every existing repo's tumbler and is not part of the catalog-as-naming-authority refactor.

Validation: a regression test asserts that two `_repo_identity` calls against the same path return the same hash, and that `--git-common-dir` resolution makes worktrees produce the main repo's hash.

### Migration path

On first `nx index` after RDR-103 lands, the indexer:

1. Computes the conformant target via `cat.collection_for(content_type, repo_owner, current_model)`. The `current_model` is **the indexer's current canonical model for the content-type prefix**, NOT a value parsed from the legacy collection name. This sidesteps the case where a legacy collection's embedding model is unknown to `CANONICAL_EMBEDDING_MODELS` (e.g. an old `voyage-3` from before the canonical set existed): the migration always renames into a current-model conformant target.
2. Looks up the legacy name via the existing repo registry (`_collection_name(repo)`).
3. If the legacy collection exists in T3 and the conformant target does not: invoke `Catalog.rename_collection(legacy, conformant)` atomically. Single one-line operator message: `Upgraded legacy collection X to Y.`
4. If the conformant target already exists AND the legacy collection is absent: index proceeds against the conformant target; no message. (This is the steady state on subsequent indexes.)
5. If both exist: the conformant target was created by a prior partial run. Skip the rename and index against the conformant target. The legacy collection is left for the operator to clean up via existing tools (`nx t3 gc`, `nx catalog rename-collection`, etc.).

The migration is one-shot per repo per content-type. Operators with hundreds of legacy collections see one upgrade message per collection, then the message stops appearing on subsequent indexes.

Idempotency check: the absence of the legacy collection in T3 is the sufficient signal. After `rename_collection` succeeds, the legacy collection no longer exists, so step 3's precondition fails and the indexer falls through to step 4. No second message.

### Legacy-name compatibility: accept the breakage

User base is two operators. Scripted MCP calls and saved CLI invocations that reference legacy collection names will fail with `Collection not found` after the rename. The operator updates the call. No transparent-redirect shim, no deprecation chain follow.

Rationale: building and maintaining a `superseded_by` chain-follow at every read surface (MCP tools, CLI commands, search routing) costs more lines than the one-time rename audit it would replace. Loud breakage on the read side is a feature at this scale.

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
- Test churn: every test that hardcodes a legacy collection name needs updating. Surveyed via `grep -rn 'code__nexus-\|docs__nexus-\|rdr__nexus-' tests/`: 64 hits across 12 distinct files. Many are cluster references (`code__nexus-XXXXXXXX-foo`) rather than literal sites needing rewrites, but the cleanup surface is non-trivial.
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

### Phase 3: indexer rewrite (src + plugin)

7. Replace `registry._collection_name` family with calls to `cat.collection_for(...).render()`. Drop the legacy helpers as soon as call sites switch (no deprecation window; user base is two operators).
8. Update `doc_indexer.py:599/1102/1463` inline name construction.
9. Update `pipeline_stages.py`, `indexer.py`, `commands/index.py`.
10. Update `nx/hooks/scripts/rdr_hook.py:58, 269` to use the same naming authority.
11. Update `nx/skills/rdr-close/SKILL.md` so post-mortems land in the conformant `knowledge__<owner_id>__<model>__v1` shape with `category="rdr_postmortem"` set at the document level. Migrate any existing `knowledge__rdr_postmortem__*` collections via `nx catalog rename-collection` as a one-time operator step; document the rename in the skill's migration note.

### Phase 4: migration on first index

12. In `_catalog_hook_repo`: detect legacy collection, compute conformant target, invoke `rename_collection` if needed.
13. Operator-visible one-line "upgraded" message; idempotent (legacy-absent in T3 is the sufficient signal; subsequent indexes fall through to step 4 of the migration path with no message).
14. Lock in a test that re-indexing after the upgrade does not re-emit the message and that a partial-state "both exist" recovery skips the rename and indexes against the conformant target.

### Phase 5: strict-flip + flag drop

15. Flip `T3Database.strict_collection_naming` default to True (`nexus-2r71` collapses to this commit).
16. Remove the flag in a follow-up.
17. Drop the legacy registry helpers from the codebase.

### Phase 6: plugin-layer documentation pass

18. Sweep `nx/agents/*.md`, `nx/agents/_shared/*.md`, `nx/skills/*/SKILL.md`, `nx/skills/*/reference.md`, `nx/commands/*.md` for legacy-shape illustrative examples (`code__<repo>`, `knowledge__art`, `rdr__nexus`, etc.). Replace with conformant examples that reflect the post-RDR-103 reality.
19. Update `nx/skills/nexus/SKILL.md` and `reference.md` collection-naming sections to describe the canonical 4-segment shape as the public contract.

### Day 2 operations

- `nx catalog doctor --collections-drift` exits 0 cleanly on a fresh greenfield index.
- Operators with legacy collections see one upgrade message per collection on the first index after the upgrade.
- A failing model bump (e.g. dimensionality change) raises a clear `nx index --bump-version` hint instead of corrupting vectors.

## Validation

- `tests/test_catalog_collection_name.py` (new): locks the tuple type contract.
- `tests/test_catalog_collection_for.py` (new): locks the catalog naming-authority contract including version-bump semantics.
- `tests/test_indexer_conformant_names.py` (new): locks the indexer family on conformant naming.
- `tests/test_collection_name_migration.py` (new): locks the legacy-to-conformant rename on first index and the idempotency of the upgrade message.
- `tests/test_repo_identity_stability.py` (new or extension of an existing test): locks the path-derived identity invariant; same path returns the same hash; worktrees resolve to the main repo's hash.
- Existing tests update to drop hardcoded legacy strings. Survey: 64 hits across 12 distinct test files (`grep -rn 'code__nexus-\|docs__nexus-\|rdr__nexus-' tests/`). Many are cluster references not literal sites; the load-bearing rewrite count is smaller but the audit is the count of files that need a pass.

## Open Questions

- Should `CollectionName` be exposed in `nexus.catalog`'s public `__all__`, or kept module-internal? Plugins consuming chunk metadata may want to introspect the tuple.
- Operator UX for the upgrade message: stderr line per collection is fine for ~10 collections. Operators with hundreds of collections (the current state for the project) may want a single summary line with a count plus a `--quiet` flag.
- When promoting `_owner_segment_for_tumbler` to a `Catalog` method, should `synthesizer._owner_prefix_of` (a private symbol it currently imports) be promoted alongside, or inlined into the new method? Removing the cross-module private-symbol import is the right cleanup; the choice is about where the canonical implementation lives.
- Test churn audit: of the 64 hits across 12 files for hardcoded legacy collection patterns, how many are load-bearing assertions vs. comment references? The Phase 3 audit answers this before the rewrite begins.
