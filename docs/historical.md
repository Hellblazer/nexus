# Origins and Inspirations

Nexus synthesizes patterns from four tools into a unified semantic search and knowledge system for Claude Code agents.

## Lineage

| Tool | What Nexus borrowed | What Nexus replaced |
|---|---|---|
| **mgrep** | UX patterns, citation format, Claude Code plugin structure | — |
| **SeaGOAT** | Git frecency scoring (`exp(-0.01 * days_passed)`), hybrid ripgrep+vector search, persistent server pattern | — |
| **Arcaneum** | PDF extraction + chunking pipeline (PyMuPDF, OCR, semantic markdown splitting) | Qdrant (replaced by ChromaDB), fastembed/local ONNX (replaced by Voyage AI) |
| **Mixedbread** | Original cloud vector store for indexed collections | Replaced entirely by self-hosted ChromaDB indexing pipeline |

## Design decisions at inception

**Self-hosted ingest, cloud storage.** Embeddings and long-term storage are cloud-backed (Voyage AI, ChromaDB Cloud). Ingest, chunking, and retrieval logic run locally — no raw content leaves the machine.

**Three storage tiers.** T1 (ephemeral ChromaDB) for session scratch, T2 (SQLite + FTS5) for persistent memory and plan library, T3 (ChromaDB Cloud + Voyage AI) for permanent knowledge.

**No raw content storage.** Unlike Arcaneum (which persists content to local disk for re-indexability), Nexus stores vectors + chunk text only. The source repositories are the ground truth.

**North star.** Agents should be able to index, search, remember, and synthesize — cheaply, without vendor lock-in, without a Swiss army knife.

## Evolution

The project has grown significantly beyond the original spec. Key additions since inception:

- **Catalog system** (RDR-049, RDR-050, RDR-052): Xanadu-inspired document registry with tumbler addressing, typed link graph, and catalog-first query routing
- **31-language AST chunking** via tree-sitter (RDR-028)
- **PDF extraction pipeline** with auto-detect routing: Docling, MinerU, PyMuPDF (RDR-044)
- **Contextualized embeddings** (CCE) via Voyage AI for prose and knowledge collections
- **Agent ecosystem**: 17 specialized agents orchestrated by skills, with structured relay protocol
- **Beads**: Dolt-backed issue tracker for multi-session work with dependency tracking

For current architecture, see [architecture.md](architecture.md). For the RDR decision record, see [docs/rdr/](rdr/).
