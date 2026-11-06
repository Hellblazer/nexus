# Sensitive documents

A document marked `--sensitive` is kept from the surfaces that send document text to a model or to an outside service. This page is the Phase 1 stub of RDR-243 ([first-class document sensitivity](rdr/rdr-243-first-class-document-sensitivity.md)); Phase 3 completes it. Its two generated regions come from the LLM-surface registry (`src/nexus/tables/llm-surfaces.toml`), the same source as the `--sensitive` help and the table in the [privacy policy](privacy-policy.md) section 2. Run `uv run python scripts/gen_privacy_policy_table.py --write` after a registry change.

## What `--sensitive` enforces in this release

<!-- BEGIN GENERATED: llm-surfaces (scripts/gen_privacy_policy_table.py; edit the registry, not this region) -->
Each row is a feature that sends document-derived text off the machine or to a model. A destination applies only where your mode and configuration reach it. The last column says what `--sensitive` does to a document in this release.

| Surface | What it sends | Where | With `--sensitive` |
| --- | --- | --- | --- |
| Aspect extraction: enqueue hook | the document's identity, queued for later extraction (the worker reads the content at drain) | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| Aspect extraction: worker drain | document content read at drain from its chunks or its file, passed to the LLM | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| Aspect extraction: `claude -p` | document text, passed to `claude -p` | Anthropic (`claude -p` or `api.anthropic.com`) | refused |
| Aspect extraction: API | document text, sent to the Anthropic API or an `openai_compat` endpoint | Anthropic (`claude -p` or `api.anthropic.com`); the endpoint in `llm.openai_compat.base_url` | refused |
| `nx enrich aspects` | document text, through the aspect extraction above | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| `nx enrich aspects --validate-sample` | an extracted aspect plus its source evidence | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| `nx enrich bib` | the title, plus arXiv ids and DOIs scanned from chunks | Semantic Scholar; OpenAlex | skipped, loudly |
| `nx dt index --enrich`, `nx dt capture --enrich` | a DOI | CrossRef, through DEVONthink | skipped, loudly |
| Taxonomy labels | topic terms plus sample document names | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| `nx taxonomy review --auto` | topic label, terms and document names | Anthropic (`claude -p` or `api.anthropic.com`) | skipped, loudly |
| Memory rollup (`memory_consolidate`) | T2 notes | Anthropic (`claude -p` or `api.anthropic.com`) | not covered: a note is not a document |
| Voyage embedding | chunk text, at index time | Voyage AI (`api.voyageai.com`) | disclosed; needs an acknowledgement |
| MinerU remote extraction | PDF bytes | the MinerU server at `pdf.mineru_server_url` | skipped, loudly |
| Cloud tenant storage | chunk text and embeddings | the managed nexus-service (managed-cloud mode) | disclosed; needs an acknowledgement |
<!-- END GENERATED: llm-surfaces -->

## What it does not enforce

<!-- BEGIN GENERATED: llm-surfaces-pending (scripts/gen_privacy_policy_table.py; edit the registry, not this region) -->
Not yet enforced: search results (MCP read tools; `nx search` and `nx query` output) are still shown to the session model until the output posture ships; Knowledge Map in SubagentStart still reaches the model of the Claude Code session until it excludes sensitive documents (it carries topic labels, into every agent's context); `nx_answer` operators still reaches Anthropic (`claude -p` or `api.anthropic.com`) until it excludes sensitive documents (it carries hydrated chunk text); `nx_answer` bundle still reaches Anthropic (`claude -p` or `api.anthropic.com`) until it excludes sensitive documents (it carries bundled step inputs); `nx_answer` plan miss and request coverage still reaches Anthropic (`claude -p` or `api.anthropic.com`) until it excludes sensitive documents (it carries the question and plan text; coverage may carry search hits); `nx_tidy` still reaches Anthropic (`claude -p` or `api.anthropic.com`) until it excludes sensitive documents (it carries prefetched T3 entries, inlined); `nx_enrich_beads` still reaches Anthropic (`claude -p` or `api.anthropic.com`) until it excludes sensitive documents (it carries bead text, plus the nx MCP tools in the child process); `nx_plan_audit` still reaches Anthropic (`claude -p` or `api.anthropic.com`) until it excludes sensitive documents (it carries plan text, plus the nx MCP tools in the child process); Voyage rerank still reaches Voyage AI (`api.voyageai.com`) until it excludes sensitive documents (it carries the query plus candidate chunk text).
<!-- END GENERATED: llm-surfaces-pending -->

Flagging a document does not recall a model call that is already in flight: a request sent before the flag was set has its text at the model. The drain and `extract_aspects` read the flag from the catalog again before each task, so a task queued before the flag is dropped when it is next claimed, but one a worker has already handed to the model is not stopped.

A T2 note that quotes a sensitive document is not covered, and RDR-243 proposes no mitigation for it: memory rollup (`memory_consolidate`) sends notes to the LLM, and a note is not a document, so it carries no flag.
