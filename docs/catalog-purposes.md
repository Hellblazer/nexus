# Catalog Purposes Registry

A **purpose** is a human-named alias for a list of catalog link types
that the `traverse` operator should walk. Plan templates declare
`purpose: <name>` instead of an explicit `link_types: [...]` list so
the same template stays correct as new link types ship in the catalog.

The registry lives at `nx/plans/purposes.yml` and is loaded once per
process by `nexus.plans.purposes.resolve_purpose`. Project overrides
may add purposes in `.nexus/plans/purposes.yml` (additive — name
collisions surface at load time).

Companion: `docs/catalog-link-types.md` enumerates the link-type set
that purposes refer to.

## Shipped purposes

| Name | Resolves to | When to use |
|------|-------------|-------------|
| `find-implementations` | `implements`, `implements-heuristic` | Walk from a design / RDR doc to the code modules that implement it. Used by `verb:research` and `verb:analyze`. |
| `decision-evolution` | `supersedes`, `cites` | Walk back through the decision history that authored a doc — RDRs that supersede / amend / cite each other. Used by `verb:review` and `verb:debug` for "what did we decide and why?". |
| `reference-chain` | `cites`, `quotes` | Walk citation chains across docs / papers. Used by `verb:analyze` when synthesising across a reference graph. |
| `documentation-for` | `implements`, `implements-heuristic`, `cites` | Inverse of `find-implementations` — walk from code or an artefact back to the docs that describe it. Used by `verb:document` to locate doc-coverage gaps. |
| `soft-relations` | `relates`, `comments` | Walk loose semantic associations. Lower-confidence than `find-implementations`; useful for exploratory survey traversals. |
| `all-implementations` | `implements` | Strict-only implementation links — excludes `implements-heuristic`. Use when an audit needs only authored / verified mappings. |

## Resolution semantics

`resolve_purpose("find-implementations")` returns the list of link
types **after filtering against the catalog's known set**:

- Unknown purpose name → empty list. `traverse` will short-circuit
  with no edges followed.
- Unknown link type within a known purpose → dropped with a
  structured warning `purpose_unknown_link_type`. The valid subset
  is returned. This lets `purposes.yml` reference future link types
  (`semantic-implements`, etc.) defensively without breaking older
  releases that don't have them yet.

The `traverse` MCP tool enforces SC-16: passing both `link_types`
and `purpose` returns `{"error": "..."}` instead of running a
half-resolved walk.

## Adding a new purpose

1. Add an entry to `nx/plans/purposes.yml`:
   ```yaml
   <new-purpose-name>:
     description: >-
       One-line prose: when should an authoring agent reach for this purpose?
     link_types:
       - <type1>
       - <type2>
   ```
2. Confirm every link type in the list is in
   `_KNOWN_LINK_TYPES` (`src/nexus/plans/purposes.py`) — otherwise
   the resolver will warn-and-drop at runtime.
3. Reference the purpose by name in any new plan template:
   ```yaml
   - tool: traverse
     args:
       seeds: $step1.tumblers
       purpose: <new-purpose-name>
       depth: 2
   ```
4. Optional: update the purpose-resolution test to add the new name
   to the "shipped purposes" assertion in
   `tests/test_purposes_resolve.py`.

## Project-scope overrides

A project may ship its own `.nexus/plans/purposes.yml` to add purposes
that don't make sense at the global plugin tier (e.g.
`security-review-walk` for a security-team-specific traversal
shape). The loader merges project entries on top of the plugin
defaults; name collisions surface at load time.
