# Catalog Link Types

Reference for the typed edges that connect documents in the nx
catalog. Every link is `(from_tumbler, to_tumbler, link_type,
created_by)`. The `link_type` field is a string drawn from the set
defined here. The catalog stores edges as JSONL plus a SQLite cache
(see `src/nexus/catalog/catalog.py`); the link types are inspected
by `Catalog.graph()`, `Catalog.graph_many()` (RDR-078 P3), and the
purpose registry resolver (`docs/catalog-purposes.md`).

This is a **closed set** at any given release — adding a new link
type requires updating the catalog, the purpose resolver's known-set
guard, and (where appropriate) the link-generator passes. Plans that
reference an unknown link type degrade gracefully (warn + drop) so
the introduction of a new type at a future release doesn't break old
plans.

## Set

| Type | Direction | Source | Typical use | When to traverse |
|------|-----------|--------|-------------|------------------|
| `cites` | from → to | bib enrichment, authoring | Citation: source explicitly references target. | Reference chain, "what does this paper / RDR cite". |
| `supersedes` | from → to | RDR lifecycle, manual | Replacement: from is the new artefact, to is the deprecated one. | Decision evolution: walk from current → historical. |
| `quotes` | from → to | bib enrichment, manual | Verbatim quotation: from contains a direct quote of to. | Stricter than `cites` — surface direct textual reuse. |
| `relates` | from ↔ to | manual, llm-linker (advisory) | Soft semantic association without a stronger label. | Exploratory survey traversal; lower confidence than typed links. |
| `comments` | from → to | manual | Annotation / commentary: from comments on to. | Find downstream commentary on a doc. |
| `implements` | from → to | rdr-implements pass, manual | Authored implementation: from is design; to is code. | Walk from RDR/spec → implementing modules. |
| `implements-heuristic` | from → to | code-rdr heuristic pass | Heuristic implementation guess (path-or-symbol match). | Same as `implements` but lower confidence; OK for surveys, suspect for audits. |

Cardinality: every type except `relates` is directional. `relates`
edges are stored from-low-to-high tumbler order so the catalog dedups
both directions to one row; the BFS treats them as bidirectional via
`direction='both'` (default).

## Provenance

Every link carries a `created_by` field on the edge row. Conventional
values:

- `bib_enricher` — Semantic Scholar lookup created a `cites` from a
  citation match.
- `rdr_implements_pass` — generator pass walked accepted RDRs and
  emitted strict `implements` to known code paths.
- `code_rdr_heuristic` — looser pass; emitted `implements-heuristic`
  when only a path/symbol substring matched.
- `manual` — user authored via `nx catalog link`.
- `auto_linker` — created at storage time from T1 link-context
  (RDR-053 storage-boundary auto-linker).
- `llm_linker` — Claude-suggested edge; advisory only, manual review
  recommended before traversing in a `verb:review` pass.

When auditing a graph traversal, prefer `created_by IN ('bib_enricher',
'rdr_implements_pass', 'manual')` over the heuristic creators —
RDR-077 hub detection works the same way.

## Where types are used

- `nexus.catalog.catalog.Catalog.graph()` — single-seed, single-type BFS.
- `nexus.catalog.catalog.Catalog.graph_many()` — multi-seed, multi-type
  BFS; fan-out per (seed, type) and merge with first-seen-wins node
  dedup, `(from, to, type)` edge dedup, and a 500-node cap on the
  merged frontier (RDR-078 P3, nexus-05i.5).
- `nexus.plans.purposes.resolve_purpose()` — purpose names map to
  link-type lists (see `docs/catalog-purposes.md`).
- `nexus.mcp.core.traverse` — MCP tool that resolves seeds, accepts
  either `link_types` or `purpose` (mutually exclusive — SC-16),
  and dispatches to `graph_many`.

## Adding a new type

1. Add the string to the `tumbler.py` `link_type` enumeration
   docstring (currently free-form on `CatalogLink.link_type`).
2. Update `_KNOWN_LINK_TYPES` in `src/nexus/plans/purposes.py` so
   plans can reference the new type via a purpose without being
   silently dropped.
3. Decide on `created_by` provenance: extend the link-generator
   passes (`src/nexus/catalog/link_generator.py`) or document a
   manual workflow.
4. Update `nx/plans/purposes.yml` if any existing purpose should
   include the new type (additive; consider whether to ship a new
   purpose name instead to avoid changing the meaning of an existing
   one).
