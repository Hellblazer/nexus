# Xanadu in Nexus

Nexus borrows ideas from Ted Nelson's Project Xanadu — the original vision of a universal, interconnected document system — and adapts them for a practical problem: helping AI agents understand not just what documents say, but how they relate to each other and where specific claims come from.

To be clear: this is a linking system, not an attempt to build Xanadu. We needed permanent document addresses, typed relationships, and durable sub-document references — and Nelson's model provided all three in a form that was simple, well-studied, and easy to implement. We could have used RDF triples, property graphs, or ad-hoc foreign keys. We chose tumblers and typed links because they map cleanly onto the problems our RDR process and agent suite actually face: tracing where a decision came from, what code implements a design, and which findings have been superseded. The Xanadu lineage gives us a coherent vocabulary and a set of proven design principles without requiring us to build the full docuverse.

This document explains what we took, what we deliberately left out, and how the result works in practice. The full design rationale is in [RDR-053: Xanadu Fidelity](rdr/rdr-053-xanadu-fidelity.md), with its [post-mortem](rdr/post-mortem/053-xanadu-fidelity.md) documenting lessons learned during implementation.

## The problem: cross-document linkage in a vector database

Vector databases are good at one thing: finding chunks that are semantically similar to a query. But a chunked, embedded corpus has no structure beyond similarity. There is no way to express that a code chunk *implements* a design described in a prose chunk three collections away. There is no way to say that a research finding *supersedes* an earlier one. There is no way to follow a chain of citations from a paper through the code that applies its ideas.

This is the cross-document linkage problem. In Nexus it's especially acute: every design decision has an RDR document, code that implements it, papers that informed it, and a post-mortem that evaluates it. These live in separate collections with separate embedding models. Semantic search can find each piece individually — it cannot connect them. A code chunk does not embed anywhere near the prose that specifies what it implements.

Taxonomies and collection-level metadata help, but they operate at the wrong granularity. Knowing that a collection contains code and another contains design docs doesn't tell you *which* code implements *which* design. You need chunk-to-chunk linkage across collection boundaries.

The link graph provides this. Every typed link is an edge between two documents — and optionally between two specific chunks. The graph gives the [query planner](querying-guide.md) traversable structure: "find this RDR, follow `implements` links to the code, search those collections for the user's question."

The result is search that knows structure before it runs. The [query planner](querying-guide.md) uses the link graph to scope, route, and compose queries — finding relevant documents in the catalog first, then searching their collections. This is faster and more precise than hoping the embedding space puts related chunks close together.

This is the role Xanadu fills in Nexus. Not a hypertext system — a linking substrate that gives structure to an otherwise flat vector store.

## How the suite leverages it

The catalog is not a standalone system — it's the connective tissue between Nexus's [storage tiers](storage-tiers.md) and the AI agents that use them.

**The [RDR process](rdr-overview.md) ties it together.** A design decision starts as an RDR, picks up research findings along the way, gets accepted and implemented in code, and closes with a post-mortem. Each stage produces artifacts in different collections — the RDR lifecycle skills link them as they go. An agent can then start from any piece and walk the graph: RDR → research that informed it → code that implements it → post-mortem that evaluated it.

**Other agents do the same thing.** A research synthesizer `cites` source papers. Debugger and analyst agents `relates` findings to each other. The knowledge tidier `supersedes` duplicates when consolidating. Agents can link at the chunk level when search results provide specific passages, but most links today are document-to-document.

**The query planner uses the catalog to optimize search.** The [`query()` MCP tool](querying-guide.md) accepts catalog parameters — `author`, `content_type`, `subtree`, `follow_links` — that the planner uses to narrow the search space before the vector query runs. Instead of searching everything and hoping for relevance, the planner resolves matching documents in the catalog first, then searches only their collections. For multi-step analytical queries, the planner composes graph traversal with search to maximize the quality of retrieved content — following citation chains, crossing collection boundaries, and scoping each step to the documents that actually matter.

**Link audit maintains graph health.** [`nx catalog link-audit`](catalog.md#link-health) verifies that content-hash spans resolve, detects orphaned links to deleted documents, and flags positional spans that may have gone stale. Existing collections can be backfilled with content hashes without re-embedding.

## What we borrowed

### Tumbler addressing

Every document in Nexus gets a permanent hierarchical address called a tumbler. The format is `store.owner.document`, optionally extended with `.chunk` for sub-document addressing. For example, `1.2.5` means store 1, owner 2 (a repository or knowledge source), document 5. Adding `.3` gives you chunk 3 of that document. See the [catalog guide](catalog.md#tumbler-addressing) for the full segment table and examples.

Tumblers are assigned once and never reused. If you delete document `1.2.5` and compact the catalog, the number 5 is retired. The next document under that owner gets `1.2.6`. This means any reference to a tumbler — in a link, in an agent's memory, in a conversation — remains valid indefinitely.

Nelson's tumblers were more ambitious — they supported inserting new addresses between existing ones using a specialized number system. We use simple integer segments instead. This covers our actual use cases and avoids significant complexity. The trade-off is documented in [RDR-053](rdr/rdr-053-xanadu-fidelity.md).

### Typed links between documents

Nelson envisioned a universal link graph where every connection between documents is typed, bidirectional, and permanent. Nexus ships with seven built-in link types — five that agents create automatically (`cites`, `implements`, `implements-heuristic`, `supersedes`, `relates`) and two for human annotation (`quotes`, `comments`). The link type field is a free-form string at the API level, so custom types can be added without code changes. See the [catalog guide](catalog.md#link-types) for when to use each type.

A debugger agent creates `relates` links between a root cause analysis and prior findings. A developer agent creates `implements` links between code and the design document it realizes. Citation links are auto-generated from Semantic Scholar metadata during [enrichment](cli-reference.md#nx-enrich).

Every link carries `created_by` provenance, so you can always distinguish auto-generated links from manual ones and filter by creator.

### Append-only storage

Nelson's docuverse was explicitly append-only — bytes are never truly deleted. Nexus follows this principle: both the document registry and link graph are stored as append-only JSONL files, with SQLite as a disposable query cache rebuilt from the JSONL truth. Git tracks the history, giving version control for free. Tombstones mark deletions without erasing the original record. This is why tumbler permanence works — even after deletion and compaction, the append log preserves the fact that an address was once assigned.

### Span transclusion

Nelson's most radical idea was transclusion: including content from one document in another by reference, not by copy. The referenced content stays in its original location; the inclusion is a live pointer.

Nexus doesn't implement full transclusion, but it does implement span-addressed links — links that point not just to a document but to a specific passage within it. This lets an agent say "this finding cites lines 42-57 of that paper" rather than just "this finding cites that paper." See the [span lifecycle section](catalog.md#span-lifecycle-and-staleness) for the full durability guide.

**Positional spans** (`42-57` for line ranges, `3:100-250` for chunk character ranges) are simple and human-readable. But they break when the document is re-indexed — if the content shifts, the line numbers point to the wrong text.

**Content-addressed spans** (`chash:<sha256hex>`) identify a passage by the hash of its text. They survive re-indexing as long as the content doesn't change. Use these for any reference that needs to last. Existing collections can be [backfilled](cli-reference.md#nx-collection) without re-embedding.

The [link audit](catalog.md#link-health) system tracks both: positional spans that may have gone stale after re-indexing, and content-hash spans that no longer resolve. Content-addressed spans are validated when created — a hash that doesn't resolve in the collection is rejected immediately.

## What we left out

Nelson's Xanadu was a complete alternative to the file system. Nexus is a catalog that sits alongside existing storage. The deliberate departures are documented in [RDR-053's deviations register](rdr/rdr-053-xanadu-fidelity.md):

**No tumbler arithmetic.** Nelson's system could insert new addresses between existing ones using a specialized number system. We use simple integer comparison instead. Parent documents sort before their children; `sorted()` on a list of tumblers gives the right order. The trade-off: span widths can't be computed by subtraction. If span-weighted reranking is needed later, that would require additional work — documented in [RDR-053](rdr/rdr-053-xanadu-fidelity.md).

**No byte-level addressing.** Nelson's spans could reference arbitrary byte ranges within any document version. Our spans reference chunks — the semantic units produced by the [indexing pipeline](repo-indexing.md). This is coarser but matches how the system actually stores and retrieves content.

**No version tracking.** Xanadu preserved every version of every document. Nexus tracks the current state via content hashes and detects when documents change, but does not store historical versions. Git handles version history for source files; the catalog tracks the current indexed state.

**No meta-links.** Nelson's links lived in the address space alongside documents — you could annotate a link, cite a link, or create trust provenance on a citation. In Nexus, links are a separate relation table, not addressable entities. This forecloses annotations on annotations but keeps the link schema simple.

**No federation.** Nelson's docuverse was inherently distributed — multiple stores cooperating across a network. Nexus is single-user, single-machine. The catalog is a local git repository; the vector store can be local or cloud, but there is no multi-user catalog federation. The tumbler's store segment (always `1` today) leaves the door open.

**TTL expiry.** Nelson insisted that all addresses remain valid forever. Nexus supports time-to-live expiry on knowledge entries — an expired tumbler becomes unresolvable. The tumbler number is still retired (never reused), but the content is gone. A pragmatic concession for managing growth.

## Further reading

- [Document Catalog guide](catalog.md) — full user guide for the catalog system
- [Querying Guide](querying-guide.md) — `nx search` vs `query()` MCP vs `/nx:query` skill
- [Architecture](architecture.md) — module map and design decisions
- [RDR-049: Git-Backed Catalog](rdr/rdr-049-git-backed-catalog.md) — catalog architecture design
- [RDR-051: Link Lifecycle](rdr/rdr-051-link-lifecycle.md) — full CRUD for typed links
- [RDR-052: Catalog-First Query Routing](rdr/rdr-052-catalog-first-query-routing.md) — three-path dispatch
- [RDR-053: Xanadu Fidelity](rdr/rdr-053-xanadu-fidelity.md) — tumbler arithmetic + content-addressed spans
