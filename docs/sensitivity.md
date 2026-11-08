# Sensitive documents

A document marked sensitive is kept from the places nexus sends document text to a model or to an outside service, and from the places a model reads nexus output. You mark it with `--sensitive` when you add it, or afterwards with `nx catalog sensitive`. This page is for the person who runs nexus and has not read the design ([RDR-243, first-class document sensitivity](rdr/rdr-243-first-class-document-sensitivity.md)). It says what the flag is and is not, how to set, clear and list it, what each surface does with a flagged document, what the threat scope is, and what is not enforced.

Two regions below are generated from the LLM-surface registry (`src/nexus/tables/llm-surfaces.toml`), the same source as the `--sensitive` help and the table in the [privacy policy](privacy-policy.md) section 2. Edit the registry, then run `uv run python scripts/gen_privacy_policy_table.py --write`. Never edit a generated region by hand.

## What the flag is and is not

The flag is a column on the catalog document, not a metadata key. Nothing that rewrites a document's metadata can clear it. It governs two things:

- What nexus sends to a model or to an outside service: Anthropic (`claude -p` or the API), an `openai_compat` endpoint, Voyage AI, Semantic Scholar, OpenAlex, CrossRef, a remote MinerU server, the managed cloud tenant.
- What nexus prints to a reader that is, or feeds, the model of your Claude Code session: the MCP read tools, and the output of `nx search`, `nx store get` and `list`, and `nx catalog show`, `list`, `search`, `links` and `links-for-file` when Claude Code runs them. Other `nx` verbs that print a title or path are not covered (see "What it does not enforce").

It is not access control and it does not touch the filesystem. The source file stays where it is, readable by anything that can read it. It does not hide a document from you: in a terminal you see the content, with a `[sensitive]` badge. It is one-way except through an audited clear. Every other write path, including a plain re-index, can raise it and none can lower it. One sequence drops it without an audit row: delete, purge-trash, then register the same file again (below).

Protection belongs to the document at its source URI. Rename keeps it. Re-indexing the same source URI keeps it. A new row inherits the flag from a tombstoned row of the same source URI only, while that tombstone exists: a document registered with a file path and no source URI inherits nothing. `nx catalog purge-trash` removes the tombstone, and a document registered again after that starts unflagged. Nothing writes an audit row for it, so delete, purge-trash and re-register clears protection without a trace in `catalog_sensitivity_changes`. A file moved or copied to a new path is a new document and starts unflagged. `nx catalog merge` raises both rows if either was flagged. `.nxexp` export, import and the recovery bundle carry the flag.

## Setting, clearing and listing the flag

Set it when you add the document. The same `--sensitive` option exists on five verbs:

```
nx index pdf report.pdf --sensitive
nx index md notes.md --sensitive
nx dt index --uuid <UUID> --sensitive
nx dt capture ... --sensitive
nx store put ... --sensitive
```

MCP `store_put(sensitive=True)` also sets it. An agent may set the flag but cannot clear it: no MCP tool clears.

To flag a repository's files by path, pass `--sensitive-glob` to `nx index repo`. It is repeatable and uses gitignore-like matching against the path from the repo root:

```
nx index repo ~/git/myrepo --sensitive-glob '**/*.pem' --sensitive-glob secrets/
```

The run prints how many files each pattern matched and warns on stderr when a pattern matched none, so a typo does not silently index the files it meant to protect. See [the CLI reference](cli-reference.md) for the full matching rules.

Flag documents that are already indexed, by tumbler, by collection, or by a path glob:

```
nx catalog sensitive 1.2.3
nx catalog sensitive --collection knowledge__hr
nx catalog sensitive --path 'docs/private/*' --owner 1.4
```

`TUMBLER` is a dotted tumbler, never a title. `--path` matches the document's file path with `fnmatch`, where `*` also crosses `/`. A target that matches nothing is an error and changes nothing.

Clear the flag with `--clear` and a reason. A clear without `--reason` is refused:

```
nx catalog sensitive 1.2.3 --clear --reason "published 2026-11-05"
```

Every set and every clear writes a row to the engine's `catalog_sensitivity_changes` table (old value, new value, reason, actor, time). There is no CLI view of that table. It lives in the engine's `nexus` schema under tenant row-level security, and the `nexus_diag` role is denied it in the view-era grants, so the reader is the engine's service database role. A managed-cloud tenant has no such access and cannot read its own audit rows.

List the flagged documents with `nx catalog list --sensitive`, and read one document's card with `nx catalog show <tumbler>`. Add `--json` to either for scripts. Review the list before you clear anything. Under Claude Code both verbs print the withheld stub for a flagged row; run them in a terminal to see the rows.

### Embedding a flagged document

Embedding sends chunk text to the embedder. In local mode with a local embedder nothing leaves the machine. When the target collection embeds with Voyage AI, the flag changes where the document goes:

- Local mode, no acknowledgement: the document is written to the bge collection of the same subject, which embeds on this machine. `knowledge__S`, `docs__S`, `rdr__S` and `code__S` become `<prefix>__S__bge-base-en-v15-768__v1`.
- `--ack-external-embedder`: you accept that Voyage AI gets the text, and the document is indexed where you pointed it.
- Cloud mode: every collection embeds through Voyage and the managed tenant stores the text. `--sensitive` refuses without `--ack-external-embedder`; with it, a one-line disclosure names the endpoint, the tenant and Voyage.
- MCP `store_put(sensitive=True)` cannot acknowledge. In local mode it stores into the bge collection, and it refuses in cloud mode, and on a local install whose engine embeds with Voyage and so has no bge collection.
- A flagged document already registered in a Voyage collection is not moved. The verb refuses, naming the tumbler and the collection, unless you pass the acknowledgement, which indexes it in place. `nx index repo` has no acknowledgement: it skips such a file with a warning.

Two bulk verbs apply the same Voyage rule to documents that are already flagged. `nx store import` and `nx catalog import` (the recovery bundle) refuse to write a flagged document into a Voyage collection without `--ack-external-embedder` (`gate_sensitive_import`), and `nx collection reindex` of a Voyage collection that holds a flagged document refuses before it deletes anything (`gate_sensitive_reindex`).

`--sensitive` cannot be combined with `--enrich` (it would send the title to Semantic Scholar or CrossRef). A PDF is never sent to `pdf.mineru_server_url`, loopback included, because a loopback URL can be a tunnel; the local MinerU ladder runs instead, and the verb refuses if none is available.

### Records DEVONthink excludes from AI

`nx dt index` treats a record that DEVONthink excludes from AI ("exclude from chat", on the record, on its database, or inherited from a parent group) as sensitive, as if you had passed `--sensitive` for that record. Nothing is written back to DEVONthink. Clearing the exclusion in DEVONthink later does not unmark the nexus document; use `nx catalog sensitive --clear --reason`. The group inheritance has not been verified live (see below).

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
| Knowledge Map in SubagentStart | topic labels, into every agent's context | the model of the Claude Code session | excluded |
| `nx_answer` operators | hydrated chunk text | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_answer` bundle | bundled step inputs | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_answer` plan miss and request coverage | the question and plan text; coverage may carry search hits | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_tidy` | prefetched T3 entries, inlined | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_enrich_beads` | bead text, plus the nx MCP tools in the child process | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| `nx_plan_audit` | plan text, plus the nx MCP tools in the child process | Anthropic (`claude -p` or `api.anthropic.com`) | excluded |
| Memory rollup (`memory_consolidate`) | T2 notes | Anthropic (`claude -p` or `api.anthropic.com`) | not covered: a note is not a document |
| Voyage embedding | chunk text, at index time | Voyage AI (`api.voyageai.com`) | disclosed; needs an acknowledgement |
| Voyage rerank | the query plus candidate chunk text | Voyage AI (`api.voyageai.com`) | excluded |
| MinerU remote extraction | PDF bytes | the MinerU server at `pdf.mineru_server_url` | skipped, loudly |
| Cloud tenant storage | chunk text and embeddings | the managed nexus-service (managed-cloud mode) | disclosed; needs an acknowledgement |
| MCP read tools | content and titles of search hits and fetched documents | the model of the Claude Code session | withheld from the session model |
| `nx search`, `nx store` and `nx catalog` output, and the SubagentStart links section | result text on stdout, often read by the Bash tool of Claude Code | the model of the Claude Code session | withheld from the session model |
<!-- END GENERATED: llm-surfaces -->

## What each surface does

The table above lists the rows the registry marks as enforced in the release being built. This table lists every row of the registry, 25 in all, with the behaviour the registry gives it and the phase that enforces it, so you can read what a surface does and when. A row's phase is the release phase from which the generated text lists it as enforced (see "Release phase" below).

The behaviours are `refuse` (the surface turns the document away), `skip` (it drops the document and says so in a log line and a count), `exclude` (the read leaves the document out, at the engine or at selection), `withhold` (the output side replaces it with a stub), `disclose` (the verb prints a disclosure and needs your acknowledgement), and two `n/a` values for surfaces that are not documents or carry no content.

| Surface | Behaviour | Phase | What happens to a sensitive document |
| --- | --- | --- | --- |
| Aspect extraction: enqueue hook | `skip` | 1 | No task is queued. The log line `aspect_enqueue_skipped_sensitive` names the tumbler and the surface. |
| Aspect extraction: worker drain | `skip` | 1 | The flag is read again for each claimed task, since it may have been set after the enqueue. A task for a flagged document is dropped before anything is read or sent (`aspect_worker_skipped_sensitive`). A catalog that cannot answer sends the claim back to the retry ladder. |
| Aspect extraction: `claude -p` | `refuse` | 1 | `extract_aspects` refuses the document. The dispatch root also requires the caller to be inside the sensitivity scope. |
| Aspect extraction: API | `refuse` | 1 | The same refusal. There is no exemption for a loopback `openai_compat` endpoint, because a loopback URL can be a tunnel to another host. |
| `nx enrich aspects` | `skip` | 1 | Dropped at selection. The summary counts them: `N sensitive skipped`. |
| `nx enrich aspects --validate-sample` | `skip` | 1 | Flagged documents leave the sample frame, and the sample is drawn from the eligible ones. |
| `nx enrich bib` | `skip` | 1 | Skipped, logged as `enrich_bib_skipped_sensitive`. A title is content too. |
| `nx dt index --enrich`, `nx dt capture --enrich` | `skip` | 1 | `--enrich` together with `--sensitive` is refused. A run that meets a flagged catalog row skips its CrossRef lookup (`enrich_dt_crossref_skipped_sensitive`). |
| Taxonomy labels | `skip` | 1 | A flagged document never appears as a sample name. A topic with a flagged member keeps its terms label and skips the LLM label, logged as `taxonomy_label_skipped_sensitive`. |
| `nx taxonomy review --auto` | `skip` | 1 | A topic with a flagged member is not sent to the model (`taxonomy_review_skipped_sensitive`). |
| Knowledge Map in SubagentStart | `exclude` | 3 | `nx taxonomy discover` leaves flagged chunks out of clustering and term weighting, so no term only a flagged document carries becomes a topic label. A topic built before the flag keeps its old terms; setting the flag replaces that topic's label with `label withheld` until you rebuild it with `nx taxonomy discover --force <collection>`. |
| `nx_answer` operators | `exclude` | 2 | The run enters the sensitivity scope. Retrieval passes `sensitivity=exclude` to the engine, which drops flagged chunks inside the search. Hydration refuses a flagged id the catalog names, with a `sensitive_document_refused` log line. A refused exclude read fails the plan. |
| `nx_answer` bundle | `exclude` | 2 | As the operators row: bundled step inputs share its hydration. |
| `nx_answer` plan miss and request coverage | `exclude` | 2 | The scope is required and the reads exclude. |
| `nx_tidy` | `exclude` | 2 | The prefetch excludes flagged entries. The summary says how many the catalog check withheld after the engine's own filter, and says so when the catalog check was unavailable. |
| `nx_enrich_beads` | `exclude` | 2 | The posture is exported to the child process, so the child's nested nx MCP server reads with exclude. The child's file tools are denied the flagged source files (see "The `claude -p` children" below). |
| `nx_plan_audit` | `exclude` | 2 | As `nx_enrich_beads`. |
| Memory rollup (`memory_consolidate`) | `n/a-not-a-document` | 1 | Not enforced. A T2 note is not a document and carries no flag, so a note that quotes a flagged document goes to the LLM. See "What it does not enforce". |
| Voyage embedding | `disclose` | 1 | Local mode routes the document to a bge collection unless you pass `--ack-external-embedder`. Cloud mode refuses without it. |
| Voyage rerank | `exclude` | 3 | The engine keeps flagged candidates out of the Voyage reranker. A flagged row is not scored and keeps the position it had in vector order; the unflagged rows are reranked into the remaining positions. A flagged result can therefore sit above or below where a reranker would have put it. |
| MinerU remote extraction | `skip` | 1 | The remote leg is skipped and the local subprocess ladder runs. The verb refuses only when no local extractor exists. |
| Cloud tenant storage | `disclose` | 1 | Cloud mode needs `--ack-external-embedder`; with it a one-line disclosure names the managed tenant and Voyage. |
| MCP read tools | `withhold` | 3 | The default replaces each flagged hit with a stub and counts it. `sensitivity.mcp_output: flag` shows the content with a badge. See "Output posture". |
| `nx search`, `nx store` and `nx catalog` output, and the SubagentStart links section | `withhold` | 3 | Under Claude Code the hit is the stub. In a terminal it prints with a badge. See "Output posture". |
| Install ping | `n/a-no-content` | 1 | Sends the install id, version and mode and no content. Listed so the registry covers every outbound call. |

## Output posture

This is the output side of the flag: what the model of your Claude Code session is shown.

**MCP read tools.** The default is `withhold`. A tool reads the hit, then replaces each flagged one with a single line and no title:

```
[sensitive] 1.2.3: content withheld
```

The tumbler is the hit's first sensitive owner in tumbler order, left out when none resolves. The result states how many were withheld (`1 sensitive hit withheld`), so a result list does not get shorter without saying so. The text never leaves the tool. The tools covered are `search`, `query`, `store_get`, `store_get_many`, `store_list`, the scoped searches (`search_metadata_scoped`, `search_topic_scoped`, `search_graph_hop`, `search_aspect_scoped`) and the catalog `show`, `list`, `search` and `links` tools. A flagged document's catalog row keeps its tumbler, content type, collection, chunk count and index time and loses its title, author, path, URI and bibliographic fields. An edge that touches a flagged document keeps its tumblers, type and spans and loses its `meta`.

Under `sensitivity.mcp_output: flag` the hit is returned with its content and a `[sensitive]` badge, and a line is added to the reader instruction asking the model not to quote, summarize or carry the content into any other tool, file or message unless you asked for that. Set it with:

```
nx config set sensitivity.mcp_output flag
```

Only `withhold` and `flag` are accepted. The key is read from the global `config.yml` only, never from a repository's `.nexus.yml`, so a cloned repository cannot switch withholding off. Any other value, a missing key and a config that fails to parse all mean `withhold`. `nx doctor` and `nx catalog doctor --sensitivity-mcp-output` report the active value and warn on `flag`.

**The CLI.** The CLI has no switch. When Claude Code started the process (`CLAUDECODE` or `CLAUDE_CODE_ENTRYPOINT` is set to a non-blank value) the verbs withhold, because what Claude Code's Bash tool reads reaches the same model. In a terminal they print the content with a `[sensitive]` badge and, in the prose formats, the reader line. The verbs are `nx search`, `nx store get` and `list`, and `nx catalog show`, `list`, `search`, `links` and `links-for-file`. Other verbs are not covered; see "What it does not enforce". `nx search --json` keeps the array's length: a withheld hit becomes `{"sensitive": true, "sensitive_owner": "<tumbler>", "stub": "<the line>"}`, and every item carries a `sensitive` boolean. `sensitivity.mcp_output` does not move the CLI.

## Threat scope

The flag protects against accidental exposure by tools and agents that behave as designed: an extraction job that would have sent the document to Anthropic, an indexing run that would have sent it to an external embedder, a search whose result a model would have read. It is not a defence against a hostile local user, and it does not stop a model with shell access that sets out to get around it.

What a determined process can still do:

- Anything that unsets `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` before running `nx search` gets the terminal posture, with content.
- A process that can edit your global `config.yml` can set `sensitivity.mcp_output` to `flag`.
- A process that can run `nx catalog sensitive --clear --reason ...` can clear the flag. The clear leaves an audit row, which is the only trace.
- A model with a Bash tool can read any file your account can read, including the source file of a flagged document.
- Anything that can run `nx catalog delete` and `nx catalog purge-trash` and then index the same file again ends with an unflagged document and no audit row.

These routes are inferred from how the checks work, not shown by a test. The doctor row `sensitivity_mcp_output` shows the current config value only, which is the second route. Nothing records the first route. The audit table records the third.

### The `claude -p` children nexus starts

Sensitivity governs what nexus reads and prints, not the filesystem. A `claude -p` child that nexus starts with the `Read`, `Grep` or `Glob` tools (the `nx_enrich_beads` and `nx_plan_audit` dispatches) could open a flagged document's source file from its working tree without passing through any nexus filter. Each such dispatch therefore reads the catalog's sensitive documents and starts the child with a `--disallowedTools` deny rule for each source file (`Read(//abs/path)`, which Claude Code applies to `Read`, `Grep` and `Glob`; a Windows drive path is written in Claude Code's POSIX form, `Read(//c/...)`). Past 100 rules the rules become one per directory; when the catalog cannot be listed, or a path cannot be written as a rule, the dispatch removes `Read`, `Grep` and `Glob` from the child.

This is best effort, and has two limits. Claude Code states that a `Read` deny rule applies to `Grep` and `Glob` as "a best-effort" match, so a search tool can still list or match a denied file's content in some forms; and a child with a `Bash` tool can read any file the operator's account can. Nexus closes neither. A copy of the content at a path the catalog does not name is also outside the rule.

## Capabilities and old engines

The client refuses to act on a flag the engine might drop. It asks the engine's `GET /version` for three boolean keys, and only a real JSON `true` counts. An absent key, a `false` and a failed probe all read as unsupported. The probe for the two vector keys does not remember a failure, so the next read asks again. The catalog key is different: `HttpCatalogClient` caches its whole `GET /version` answer for 5 minutes in each client process (`_version_flags`, `_CAPABILITY_TTL_S`), a failed probe included. A process that probed while the engine was down keeps reading `catalog_sensitivity_supported` as false until the 5 minutes pass.

- `catalog_sensitivity_supported`. Without it, every `--sensitive` write (`index pdf`, `index md`, `dt index`, `dt capture`, `store put`, MCP `store_put`, `index repo --sensitive-glob`) and `nx catalog sensitive` refuse before they write, with a message that begins `... refused` and ends `Nothing was written.` An engine without the column would drop the flag without an error, so the refusal is the safe answer.
- `vector_sensitivity_exclude_supported`. Without it, a read that must exclude flagged chunks raises `SensitivityExcludeUnavailable` and never falls back to an unfiltered read. Upgrade the engine.
- `rerank_sensitive_exclusion_supported`. Without it, a search that may return flagged rows does not ask for a rerank, because the engine would send their text to the Voyage reranker. The search runs in vector order and reports the degrade.

In short: the `--sensitive` write verbs and `nx catalog sensitive` need the catalog key. The rows that exclude flagged chunks inside a read (`nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit` and the Knowledge Map's `nx taxonomy discover`) need the vector key, and fail rather than read unfiltered without it. Voyage rerank exclusion needs the rerank key; without it the search skips the rerank. The output posture, the child file deny and the DEVONthink import need no key of their own. The posture takes the flag from the hit's `sensitive` field when the engine sends one and from the catalog's owner list otherwise, and treats a hit it cannot classify as sensitive.

`nx upgrade` converges a local engine. The engine that answers all three keys is engine-service-v0.1.182, which conexus 7.98.0 pins.

## What it does not enforce

<!-- BEGIN GENERATED: llm-surfaces-pending (scripts/gen_privacy_policy_table.py; edit the registry, not this region) -->
Every phase-gated surface is enforced in this release. Not covered by any phase, described below: Memory rollup (`memory_consolidate`) (census row 18).
<!-- END GENERATED: llm-surfaces-pending -->

The generated line above is derived from the registry's release phase: below the last phase it names every surface the release does not yet cover, and at the last phase it names the one row no phase covers. The list below is every gap the design records, including the ones no phase closes.

- **Memory rollup (census row 18).** Memory rollup (`memory_consolidate`) sends T2 notes to the LLM. A T2 note that quotes a sensitive document is not covered, and RDR-243 proposes no mitigation for it: a note is not a document, so it carries no flag. This is not enforced and stays that way. Keep flagged text out of notes you roll up.
- **Release phase.** `RELEASE_ENFORCED_PHASE` is 3, the last phase, from conexus 7.98.0, which pins engine-service-v0.1.182. The generated tables and the `--sensitive` help list every row a phase enforces: phase 1 (the write-side refusals and skips), phase 2 (the engine read filter and the model-bound surfaces: `nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`) and phase 3 (the output posture, rerank exclusion, the Knowledge Map, the DEVONthink import, the child file deny). A phase 2 or 3 behaviour still needs the engine key it depends on (see "Capabilities and old engines"): against an older engine it refuses or degrades as that section says.
- **DEVONthink group inheritance.** The import treats a record as excluded when any ancestor group has the exclusion set, as a walk over the record's parents. That walk has not been verified against a live DEVONthink 4: DEVONthink 4.4.2 hung on every database query on 2026-11-07, so the tests assert only the AppleScript text and the fail-closed paths. A property DEVONthink cannot read fails closed on DEVONthink 4 or later (the record is treated as excluded and a `dt_exclusion_unreadable_fail_closed` warning names it), so a wrong property name over-excludes and does not leak. Before DEVONthink 4 there is no such property.
- **The cloud edge key.** The public conexus edge trims `/version` to an allowlist, and that allowlist is not in this repository, so this page cannot say what it carries today. As of 2026-11-07 all three keys are in the conexus edge allowlist source (conexus PR #681 and the commit before it) and reach cloud clients with the next edge image redeploy, tracked as nexus-rfvyy.7.5. Until the live edge carries them, a cloud client cannot confirm `catalog_sensitivity_supported` and so refuses `--sensitive` writes and exclude reads. The refusal is the safe direction; it also means cloud mode cannot use the flag yet.
- **`traverse` and `catalog_link_query`.** Both list tumblers, chunk hashes and collection names, not text. A hash is not content, so they are outside the output withhold. The edge `meta` they carry is withheld for an edge that touches a withheld document; the spans stay. Hydrating a hash goes through `store_get_many`, which is withheld.
- **Engine routes that serve every row.** `/get-all-metadata` (ids and metadata, titles among them) and `/get-embeddings` (ids and vectors) take no `sensitivity` and serve flagged chunks too. This is by design: their callers need every row. The indexer's staleness cache and `--force` orphan cleanup must see a flagged document's rows, or they re-embed it on every run and leave its orphans behind, and an export must carry flagged chunks or lose them. The other callers read vectors only: doctor, topic assignment and projection, and the search's diversity rerank over hits an exclude search returned; the topic labels a model writes skip flagged documents, and the doctor's embedding probe re-embeds sampled text through the collection's own embedder (the local model, or Voyage for a collection Voyage already embedded). `/store-list` has no client; `store_list` withholds on the client side. `/reapable` lists chunks with no manifest row in their own collection, but its label (owner title, tumbler, and the first 80 characters of the text) can name a live document in another collection that holds the same text, so the engine withholds the whole label when a live flagged document anywhere holds it; the gc quarantine sample and the reaper's refusal rows in `gc_audit` are withheld the same way. That takes an engine that carries nexus-rfvyy.9; an older engine shows the label, and the refusal rows an older engine wrote to `gc_audit` keep the labels they were written with. `tests/test_rfvyy9_unfiltered_route_callers.py` lists every caller of these routes with its reason and fails on a new one.
- **PDF staging rows.** The PDF pipeline's staging tables (`pdf_pages` and `pdf_chunks`) hold page and chunk text while a PDF is indexed and carry no flag. They are out of scope. They are not an outbound call, and the review of the output side found no tool that sends their content to a model or prints it. That is a reading of the code, not a pinned test.
- **The audit table has no CLI view.** `catalog_sensitivity_changes` has rows from the first set, but no verb lists them, `nexus_diag` is denied it, and a managed-cloud tenant cannot reach it. Only the engine's service database role reads it.
- **Delete, purge-trash, re-register.** The flag carries over through a tombstoned row of the same source URI. After `nx catalog purge-trash` removes it, registering the file again creates an unflagged document and writes no audit row, so the loss of protection leaves no record. A document with no source URI inherits nothing even before the purge.
- **Repo runs and Voyage collections.** `nx index repo --sensitive-glob` flags the files it matches, but a repo run has no `--ack-external-embedder`. When the target collection embeds with Voyage, the matched files are skipped with a warning (`sensitive_*_skipped_external_embedder`) and not indexed at all.
- **CLI verbs outside the output posture.** Only the verbs named under "Output posture" withhold. These print the title or path of a flagged document under Claude Code with no stub: `nx catalog delete` (the prompt and the result), `nx catalog reconcile` (the LOST and empty lines), `nx catalog orphans --no-links`, `nx catalog trash`, `nx catalog gc`, `nx catalog prune-stale`, `nx catalog remediate-paths`, `nx catalog reconcile-stale`, the ghost list of `nx catalog doctor`, the topic views of `nx taxonomy` (representative document titles), and `nx enrich aspects`, which prints the file name of a flagged document it skips. A flagged document that leaked aspects before it was flagged also shows its source path in the aspect listing and show views. The sensitivity doctor rows print tumblers and collection names, not titles, and `nx collection` prints collection names and counts. Run the verbs above in a terminal, not through a model.
- **A model with a shell.** A process that unsets `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT`, or any Bash tool that runs `nx` or reads the source file, defeats the CLI posture. These routes are listed under "Threat scope".
- **The child's file tools.** The deny rules on a `claude -p` child are best effort for `Grep` and `Glob`, and a child with a `Bash` tool is not restricted (see above).
- **Title, author, corpus and path are discoverable.** The stub hides a flagged document's fields, but the lookups still match on them, and the stub carries the tumbler. `catalog_search` runs full-text over title, author, corpus and file path, and its `author`, `file_path`, `corpus` and `owner` filters match the same fields, so a hit or a miss tells a model whether a word is in a flagged document's title, author, corpus name or path. `catalog_show(title=)` and `catalog_links` accept an exact title and answer for a flagged document with its tumbler, and `store_get` accepts an exact title the same way. The withheld counts on `search` and `query` also show that a query matched something flagged. Treat the title, author and path of a flagged document as discoverable by a model that has these tools.

Flagging a document does not recall a model call that is already in flight: a request sent before the flag was set has its text at the model. The drain and `extract_aspects` read the flag from the catalog again before each task, so a task queued before the flag is dropped when it is next claimed, but one a worker has already handed to the model is not stopped. A document that leaked before it was flagged is caught by the doctor rows below.

## Doctor rows

Four rows report on the flag. `nx doctor` prints them in its sweep, and `nx catalog doctor` runs one at a time with its own option:

| Row | Option on `nx catalog doctor` | Result |
| --- | --- | --- |
| `sensitive_in_aspect_eligible` | `--sensitive-in-aspect-eligible` | WARN for a flagged document whose collection has an aspect extractor or already holds aspect rows. Nothing has leaked yet; the document is exposed to the next index run. |
| `sensitive_with_aspects` | `--sensitive-with-aspects` | FAIL (exit 1) for a flagged document that already has an aspect row or a queued aspect task. It leaked before it was flagged. The row prints `nx enrich delete --tumbler <tumbler>` for each. This is the only row that gates the sweep. |
| `sensitive_in_external_embedder` | `--sensitive-in-external-embedder` | WARN for a flagged document in a collection embedded by an external service: a Voyage model token, or a legacy name with none. |
| `sensitivity_mcp_output` | `--sensitivity-mcp-output` | Reports `withhold` or `flag`. WARN on `flag`, on a value it does not recognise, or when the config cannot be read. |

The three document rows read "not applicable", never a pass, on a tenant with no flagged documents, on an engine without `catalog_sensitivity_supported`, and when the catalog cannot be read: a row that examined nothing proves nothing. `sensitivity_mcp_output` is a config row and is always applicable.

## When something is refused

- `DispatchOutsideSensitivityScope` names a dispatch root that was called outside the sensitivity scope. It is a bug in nexus or in code you added: wrap the surface that reaches the root and add its registry row.
- A skipped document leaves a structured log line naming the tumbler and the surface, and the command's summary counts the skips.
- An old engine, or a probe that failed: see "Capabilities and old engines". The message names the engine version and the minimum when the engine answered without the key, and says it could not confirm support when it got no answer at all.
- A `--sensitive` write into a Voyage collection without the acknowledgement refuses before anything is written, and the message says which flag to pass or which collection to use.
