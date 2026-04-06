# Xanadu in Nexus

Nexus borrows ideas from Ted Nelson's Project Xanadu — the original vision of a universal, interconnected document system — and adapts them for a practical problem: helping AI agents understand not just what documents say, but how they relate to each other and where specific claims come from.

This document explains what we took from Xanadu, what we deliberately left out, and how the result works in practice.

## The problem Xanadu solves for us

Semantic search finds relevant content. But it can't answer questions like: "What paper does this code implement?" or "Has this finding been superseded?" or "Which specific passage in that design doc led to this architecture decision?"

These are relationship questions. They require knowing which documents exist, how they connect, and — critically — being able to point at specific passages that survive when documents are re-indexed. Nelson's Xanadu addressed all three of these problems in the 1960s. We address them today with a narrower, more pragmatic system.

## What we borrowed

### Tumbler addressing

Every document in Nexus gets a permanent hierarchical address called a tumbler. The format is `store.owner.document`, optionally extended with `.chunk` for sub-document addressing. For example, `1.2.5` means store 1, owner 2 (a repository or knowledge source), document 5. Adding `.3` gives you chunk 3 of that document.

Tumblers are assigned once and never reused. If you delete document `1.2.5` and compact the catalog, the number 5 is retired. The next document under that owner gets `1.2.6`. This means any reference to a tumbler — in a link, in an agent's memory, in a conversation — remains valid indefinitely, even across re-indexing, deletion, and compaction.

Nelson's tumblers were far more ambitious: variable-depth addresses with transfinitesimal arithmetic for inserting new addresses between existing ones. We use fixed-depth integer segments with standard comparison operators instead. This is simpler, covers our actual use cases, and avoids the complexity of Nelson's number space. The trade-off is documented as deviation D1 in RDR-053.

### Typed links between documents

Nelson envisioned a universal link graph where every connection between documents is typed, bidirectional, and permanent. Nexus ships with seven built-in link types — five that agents create automatically (`cites`, `implements`, `implements-heuristic`, `supersedes`, `relates`) and two for human annotation (`quotes`, `comments`). The link type field is a free-form string at the API level, so custom types can be added without code changes; the CLI offers the seven built-in types as a convenience.

A debugger agent creates `relates` links between a root cause analysis and prior findings about the same subsystem. A developer agent creates `implements` links between code and the design document it realizes. The knowledge tidier creates `supersedes` links when consolidating duplicate findings. Citation links are auto-generated from Semantic Scholar metadata during enrichment.

Every link carries `created_by` provenance, so you can always distinguish auto-generated links from manual ones, and filter by creator.

### Append-only storage

Nelson's docuverse was explicitly append-only — bytes are never truly deleted (Literary Machines 4/42). Nexus follows this principle: both the document registry and link graph are stored as append-only JSONL files, with SQLite as a disposable query cache rebuilt automatically from the JSONL truth. Git tracks the JSONL history, giving version control for free. Tombstones mark deletions without erasing the original record. This is why tumbler permanence works — even after deletion and compaction, the append log preserves the fact that an address was once assigned.

### Span transclusion

Nelson's most radical idea was transclusion: the ability to include content from one document in another by reference, not by copy. The referenced content stays in its original location; the inclusion is a live pointer.

Nexus doesn't implement full transclusion, but it does implement span-addressed links — links that point not just to a document but to a specific passage within it. This is the mechanism that lets an agent say "this finding cites lines 42-57 of that paper" rather than just "this finding cites that paper."

We support three span formats, each with different durability characteristics:

**Positional spans** (`42-57` for line ranges, `3:100-250` for chunk character ranges) are simple and human-readable. But they break when the document is re-indexed — if the content shifts, the line numbers point to the wrong text.

**Content-addressed spans** (`chash:<sha256hex>`) identify a passage by the SHA-256 hash of its chunk text. These survive re-indexing as long as the chunk text doesn't change. They are the preferred format for any reference that needs to last.

The `link_audit()` system distinguishes between the two: positional spans that may have gone stale (because the document was re-indexed after the link was created) and content-hash spans that no longer resolve (because the chunk was deleted or its text changed). Each stale entry includes a reason so operators can distinguish "chunk was deleted" from "infrastructure was unreachable."

## What we left out

Nelson's Xanadu was a complete alternative to the file system. Nexus is a catalog that sits alongside existing storage. The deliberate departures are documented in RDR-053's deviations register:

**No tumbler arithmetic.** Nelson's system could insert new addresses between existing ones using transfinitesimal ADD and SUBTRACT operations. We use simple integer comparison with -1 sentinel padding for cross-depth ordering. This means `1.1.3` sorts before `1.1.3.0` (parent before child), and `sorted()` on a list of tumblers produces the correct document ordering. The consequence is that span widths cannot be computed by tumbler subtraction; if span-weighted reranking is needed later, it will require ad-hoc arithmetic at the segment level. The migration path to full arithmetic is known but costs approximately 30 call sites.

**No byte-level addressing.** Nelson's spans could reference arbitrary byte ranges within any document version. Our spans reference chunks — the semantic units produced by the indexing pipeline. This is coarser but matches how the system actually stores and retrieves content.

**No version tracking.** Xanadu preserved every version of every document. Nexus tracks the current state via content hashes and detects when documents change (for staleness detection), but does not store historical versions. Git handles version history for source files; the catalog tracks the current indexed state.

**No meta-links.** Nelson's links lived in the tumbler address space alongside documents — you could annotate a link, cite a link, or create trust provenance on a citation. In Nexus, links are a separate relation table, not addressable entities. This forecloses annotations on annotations — a pattern Nelson cared about deeply — but keeps the link schema simple.

**No federation.** Nelson's docuverse was inherently distributed — multiple stores cooperating across a network. Nexus is single-user, single-machine. The catalog is a local git repository; T3 can be local (ONNX) or cloud (ChromaDB Cloud), but there is no multi-user catalog federation. The tumbler's store segment (always `1` today) exists to leave the door open, but no federation protocol is implemented.

**TTL expiry.** Nelson insisted that all addresses remain valid forever. Nexus supports time-to-live expiry on knowledge entries — an expired tumbler becomes unresolvable. The tumbler number is still retired (never reused), but the content is gone. This is a pragmatic concession for managing knowledge base growth.

## How the suite leverages it

The catalog is not a standalone system — it's the connective tissue between Nexus's storage tiers and the AI agents that use them.

**Agents create links as they work.** Seven core agent types and all RDR lifecycle skills are wired to create catalog links. Their tool signatures include `from_span`/`to_span` parameters with `chash:` support; agents create spans when they have chunk references available from search results. Most links today are document-to-document (no span), but the infrastructure supports chunk-level precision when needed. When a developer agent implements a design doc, it creates an `implements` link. When a research synthesizer cites a source, it creates a `cites` link. The debugger, deep-analyst, codebase-analyzer, and architect-planner create `relates` and `cites` links between findings.

**The query system uses the catalog for routing.** The `query()` MCP tool accepts catalog parameters — `author`, `content_type`, `subtree`, `follow_links` — that scope semantic search to relevant collections before the vector query runs. Asking for papers by a specific author first resolves matching documents in the catalog, then searches only their physical collections. This is faster and more precise than searching everything.

**Link audit maintains graph health.** `nx catalog link-audit` verifies every content-hash span resolves in T3, detects orphaned links to deleted documents, and flags positional spans that may have gone stale. For retroactive span support, `nx collection backfill-hash` adds content hashes to existing chunk metadata without re-embedding, and `nx catalog backfill` does the same across all collections as part of a full catalog re-population.

**Tumbler ordering enables span overlap detection.** The comparison operators on tumblers power `spans_overlap()`, which detects when two positional span references cover the same passage. This is the foundation for future conflict detection in the link graph.

The result is a system where every indexed document has a permanent address, every relationship between documents is typed and traceable, and every reference to a specific passage can be verified against the actual content. It's not Nelson's Xanadu — it's smaller, simpler, and built for AI agents rather than human hypertext — but the core ideas are the same ones Nelson articulated sixty years ago.
