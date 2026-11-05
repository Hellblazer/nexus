# Sensitive documents

A document marked `--sensitive` is kept from the surfaces that send document text to a model or to an outside service. This page is the Phase 1 stub of RDR-243 ([first-class document sensitivity](rdr/rdr-243-first-class-document-sensitivity.md)); Phase 3 completes it. Its two generated regions come from the LLM-surface registry (`src/nexus/tables/llm-surfaces.toml`), the same source as the `--sensitive` help and the table in the [privacy policy](privacy-policy.md) section 2. Run `uv run python scripts/gen_privacy_policy_table.py --write` after a registry change.

## What `--sensitive` enforces in this release

<!-- BEGIN GENERATED: llm-surfaces (scripts/gen_privacy_policy_table.py; edit the registry, not this region) -->
Each row is a feature that sends document-derived text off the machine or to a model. A destination applies only where your mode and configuration reach it. The last column says what `--sensitive` does to a document in this release.

| Surface | What it sends | Where | With `--sensitive` |
| --- | --- | --- | --- |
| Aspect extraction: enqueue hook | full document content, queued for later extraction | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| Aspect extraction: worker drain | queued or on-disk document content, passed to the LLM | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| Aspect extraction: `claude -p` | document text, passed to `claude -p` | Anthropic (`claude -p` or `api.anthropic.com`) | refused |
| Aspect extraction: API | document text, sent to the Anthropic API or an `openai_compat` endpoint | Anthropic (`claude -p` or `api.anthropic.com`); the endpoint in `llm.openai_compat.base_url` | refused |
| `nx enrich aspects` | document text, through the aspect extraction above | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| `nx enrich aspects --validate-sample` | an extracted aspect plus its source evidence | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| `nx enrich bib` | the title, plus arXiv ids and DOIs scanned from chunks | Semantic Scholar; OpenAlex | skipped, loudly |
| `nx dt index --enrich`, `nx dt capture --enrich` | a DOI | CrossRef, through DEVONthink | skipped, loudly |
| Taxonomy labels | topic terms plus sample document names | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| `nx taxonomy review --auto` | topic label, terms and document names | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| `nx_answer` operators | hydrated chunk text | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_answer` bundle | bundled step inputs | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_answer` plan miss and request coverage | the question and plan text; coverage may carry search hits | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_tidy` | prefetched T3 entries, inlined | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_enrich_beads` | bead text, plus the nx MCP tools in the child process | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_plan_audit` | plan text, plus the nx MCP tools in the child process | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| Memory rollup (`memory_consolidate`) | T2 notes | Anthropic (`claude -p` or `api.anthropic.com`) | not covered: a note is not a document |
| Voyage embedding | chunk text, at index time | Voyage AI (`api.voyageai.com`) | disclosed; needs an acknowledgement |
| MinerU remote extraction | PDF bytes | the MinerU server at `pdf.mineru_server_url` | skipped, loudly |
| Cloud tenant storage | chunk text and embeddings | the managed nexus-service (managed-cloud mode) | disclosed; needs an acknowledgement |
<!-- END GENERATED: llm-surfaces -->

## What it does not enforce

<!-- BEGIN GENERATED: llm-surfaces-pending (scripts/gen_privacy_policy_table.py; edit the registry, not this region) -->
Not yet enforced: search results (MCP read tools; `nx search` and `nx query` output) are still shown to the session model until the output posture ships; Voyage rerank still sends the query plus candidate chunk text to Voyage AI until it excludes sensitive documents.
<!-- END GENERATED: llm-surfaces-pending -->

A T2 note that quotes a sensitive document is not covered, and RDR-243 proposes no mitigation for it: memory rollup (`memory_consolidate`) sends notes to the LLM, and a note is not a document, so it carries no flag.
