# Choosing and Naming Collections

A collection is a named set of chunks that share one embedding model. It is
also, today, the unit that search fans out over, that ranking merges
across, that deduplication is scoped to, and that taxonomy is built per.
So the choice of collection is not a filing decision. It shapes what a
search returns and which topics exist. This page is the rule set for
making that choice. It applies to people typing `--collection`, to agents
calling `store_put`, and to skills that index on the user's behalf.

The rules are short because the system already makes most of the decision
for you. Where it does not, prefer the broad, boring choice.

## The four content types, and who creates them

| Content type | What it holds | Who creates the collection |
|---|---|---|
| `code` | Source files from an indexed repository | `nx index repo`, never by hand |
| `docs` | Prose files from an indexed repository | `nx index repo`, never by hand |
| `rdr` | RDR documents from an indexed repository | `nx index repo`, never by hand |
| `knowledge` | Everything else: papers, notes, agent findings, imported documents | You, by naming a subject |

`code`, `docs`, and `rdr` collections are owned by a repository. The
catalog assigns their owner from the repo's tumbler and renders their full
name. You do not name them, and you do not write into them directly; you
re-index the repo.

`knowledge` is the only content type a person or agent names. Everything
below is about that choice.

## Rule 1: a knowledge collection is a subject area, not a container

Name a knowledge collection after a subject a reader would browse for a
long time: `distributed-systems`, `vector-search`, `interpretability`,
`agentic-scholar-papers`. A good test is whether the same name would still
be right in two years and would still hold everything you add on that
subject.

Not a subject: a document (`the-hnsw-paper`), a session or task
(`research-2026-09-06`, `rdr-204-notes`), a source application
(`devonthink`, `browser-clips`), a person, a date, or a placeholder
(`default`, `knowledge`, `notes`, `tmp`, `test`). Two of those exist on a
production tenant today, `docs__default` and `knowledge__knowledge`, both
minted by taking a default where a subject was needed. They are the
failure this rule prevents.
A write that names one of those placeholders as its subject is refused
(nexus-0fw11) on every writer that resolves a collection name: `nx store
put`, the MCP `store_put` tool, `nx memory promote`, `nx index pdf` and
`nx index md`, and `nx dt index`. `nx store put` and `store_put` have no
default collection for the same reason. An existing placeholder collection
stays readable, its full four-segment name is still accepted for writes,
and a recovery-bundle import restores it under its recorded name.

Fine distinctions go on the document, not in a new collection. Use `--tags`
and `--category` for "postmortem", "draft", "from DEVONthink", "week 36".
The catalog decided this once already: RDR post-mortems live in the repo's
knowledge collection under `category=rdr_postmortem`, not in a
`knowledge__rdr-postmortem` collection (RDR-103, pinned decision).

## Rule 2: reuse before you create

Before writing to a new name, list what exists:

```bash
nx collection list
```

If a collection for the subject exists, write into it. Adding the eleventh
paper on consensus to `distributed-systems` is right; creating
`consensus-papers` beside it splits one subject's topics in two and makes
both collections rank against each other for every query. When you are
unsure whether a new document belongs to an existing subject, it does.

## Rule 3: type the subject, never the machinery

Give the bare subject and let the catalog do the rest:

```bash
nx store put paper.md --collection distributed-systems
nx index md notes/overview.md --collection vector-search
nx index pdf --dir ~/papers/oracles --collection augur-oracle-papers
```

`distributed-systems` is a subject, not a collection name. `nx` looks the
collection up by its attributes: content type `knowledge`, owner
`distributed-systems`, and the embedding model your install actually embeds
with (RDR-229 item 5). If no collection has those attributes, `nx`
registers one with them, and the engine gives it a label.

**A collection is a registry row, and its name is a label.** Each
collection has a `collection_id`, a number the engine assigns and never
reuses, and every table that belongs to a collection points at that id. The
row also holds the attributes: content type, owner, embedding model, model
version, dimension and lifecycle state. The name is only a label: the string
you type and read in a command. Nothing is decided from it. Which model
embeds a collection, which content type it is and whether it is quarantined
are read from the row, never from the label.

**New collections get a minted label.** When `nx` registers a collection,
the engine sets its label to `c` followed by its id (`c4812`). The label
says nothing about the collection; listings show the attributes beside it
(`nx collection list`, `nx catalog show`). Collections created before this
change keep their old four-segment names
(`knowledge__distributed-systems__voyage-context-3__v1`) as labels; those
names are no longer parsed for meaning either.

**A label is unique among a tenant's live collections.** Two live
collections cannot answer to one label, so a label in a command names one
collection. A superseded collection keeps its label for history and
releases it for reuse. To give a collection a readable label, rename it:

```bash
nx collection rename c4812 distributed-systems-papers
```

A rename changes one row; the collection's id, chunks and catalog documents
stay where they are. A label may be any non-empty UTF-8 string without
control characters, up to 255 bytes, except the minted form `c<digits>`,
which only the engine assigns.

Never type a four-segment name, a model token or a version to create a
collection. A name that encoded a model the install did not use is how local
collections once ended up labelled with a model that never embedded them
(GH #667, RDR-109). With the subject alone, `nx` finds or creates the right
collection for the model it embeds with.

Subject names are lowercase, hyphen-separated, two or three words, ASCII.

The `owner_id` column is not the name's owner segment for a repo
collection (nexus-6pbwx). A collection's owner is its source, set once at
registration: the repo owner for a repo's `code`, `docs` and `rdr`
collections, the corpus curator for a docs corpus, the knowledge curator
for knowledge. An owner that a registration set stands: a collection
registered under a curator keeps it, and documents that arrive later do not
replace it (`catalog-051` amends `catalog-044`). Only a non-tumbler owner (a
slug such as `arcaneum-2ad2825c`, an empty value or a placeholder) is still
derived from the collection's documents, hyphenated (`1-15`, what
`owner_segment_for_tumbler` gives), when the first document lands, unless
another live collection already holds that owner for the same content
type, embedding model and version: then the collection keeps its slug and
the engine logs a notice naming both (`catalog-051`). It takes the
documents' owner once that place frees, as when a rename or a supersede
retires the other collection. A collection with no documents keeps the
value it was registered with. A
`knowledge` collection is different: its owner is its subject, whatever
tumbler its documents live under.
`catalog-044-collection-owner-from-documents.xml` carries the rule and a
one-off repair of existing rows.

An `.nxexp` import resolves the owner of a collection it registers in this
order: the owner on the collection's existing row; then the export file's
owner header (the repo owner with that `repo_hash`, registered with an empty
root that the first `nx index repo` on this machine fills in); then a
`<slug>-<hash8>` stem in the name (always for a `code__` collection; for a
`docs__` or `rdr__` one when the target has a `code__` collection of the stem
or a repo owner with that hash); then the corpus curator for a
`docs` corpus; then the knowledge curator, with a warning in the log and in
the import summary. A name whose owner segment is a tumbler, not a corpus
name, has no corpus curator and takes the knowledge curator. A repo's
collections that an old bulk import left under a curator are re-owned to the
repo owner by the registered data move `rdr229-reown-repo-stems-from-curator`,
not by later documents. The move leaves a collection with its curator when
another live collection of the same type, repo owner, embedding model and
version already exists, and records each such collection in `nexus.gc_audit`
(operation `rdr229_reown_skipped`, the reason in `details`). A name whose stem
is all digits, such as `docs__2024__...`, is read as a tumbler too, so it
takes the knowledge curator rather than a corpus curator.

## Rule 4: a collection holds many documents

A collection is meant to hold dozens to thousands of documents. A
collection with one document has the cost of a partition and none of the
benefit: no cross-document ranking inside it, one more shard for every
fan-out, and its own taxonomy that will never find a second member.

The bare-corpus fan-out drops a collection with fewer than three chunks
when a sibling under the same prefix is not thin (nexus-rbhci), so a
tiny collection created for one note can vanish from `search` results
while its content is fine. Put the note in the broad subject collection
instead.

## Rule 5: where each kind of source goes

- **Papers and PDFs**: `nx index pdf --collection <subject>` into
  `knowledge`. Never into `docs`; `docs` is repo prose.
- **Web pages**: convert with pandoc to Markdown, then
  `nx index md --collection <subject>`.
- **Agent findings and notes**: `store_put` or `nx store put` with a
  `--title` (so a re-put replaces rather than duplicates) into the subject
  collection, with `--tags` for provenance.
- **Code, repo docs, RDRs**: `nx index repo <path>`. Nothing else.
- **DEVONthink imports**: `nx dt index --collection <subject>` chooses the subject you give it;
  the same rules apply.
- **Test and shakedown artifacts**: give them a TTL (`--ttl 7d`) or delete
  the collection when the run is over. `knowledge__shakedown` on a
  production tenant is residue, not a subject.

## Rule 6: lifecycle goes through the catalog

Rename, supersede, and delete only with the `nx collection` verbs. They
cascade through the manifest, aspects, highlights, and topics; a delete
that skips them leaves rows that fail loud on a foreign key later
(RDR-164). Never write to or delete a `quarantine-*` collection; the orphan
GC owns those and restores from them.

A `quarantine-*` collection is a registered row like any other, under a name
no parser accepts, so the two list routes (`/v1/catalog/collections/list`
and `/v1/vectors/stats`) take a `lifecycle_state` filter (`live`,
`quarantine`, `dormant`, `disputed`; anything else is a 400). The default is
the full inventory, which is what doctor, gc, backfill and export read.
Code that picks a search target, parses a name, or discovers topics reads
the live view only: `HttpVectorClient.list_live_collections()`,
`live_collection_rows()`, `HttpCatalogClient.list_collections("live")`, or
`nexus.mcp_infra.get_live_collection_names()` for the MCP fan-out
(nexus-bc7ps). An explicit `corpus=<name>` naming a registered non-live
collection is excluded the same way, since an explicit name in an MCP call
is the model's navigation, not a person's choice. A routing module that
calls the bare listing fails the lint in
`tests/test_bc7ps_routing_uses_live_listing.py`.

Do not create a second collection to change embedding model. The model is
an install-level fact, and switching it mints the sibling for you at the
next write (see "Local mode with Voyage" in the CLI reference).

## Checking the rules

```bash
nx collection shape            # every rule above, one finding per violation, read-only
nx collection shape --json
```

`shape` reads the catalog rows, the vector stats, and the document counts,
and reports each violation with a proposed action. Two things it cannot see
and does not pretend to: a person-shaped subject (no signal to detect), and
tags or categories used only once (tags live on chunk metadata, not on the
catalog document, so that check would be a scan of every chunk; deferred on
nexus-ger23). It never renames,
merges, or deletes; those stay yours. For a suspected duplicate it names
`nx collection merge-candidates`, which ranks pairs by shared-topic
overlap, as the evidence to confirm with, and for one collection's depth
`nx collection audit NAME` and `nx collection health` are the RDR-087
views. Run `shape` before and after any curation pass; a clean run says how
many collections it examined, so an empty result is never mistaken for a
skipped one.

## Reading is different from writing

`--collection` and the `collection` parameter are write-side choices. For
search, name the corpus (`knowledge`, `code`, `docs`, `rdr`, or `all`) and
let the fan-out find the collections; name a specific collection only when
you already know the answer lives there. Filtering by `where` on tags or
category is the way to narrow inside a corpus.

## What the collection decides today, and why this page exists

The collection is the grain of the fan-out floor, the per-collection
top-N-then-merge, the dedup boundary (identical text in two collections is
stored twice and returned twice), per-collection taxonomy (RDR-075 exists
to project topics across collections), and code-versus-prose scoring by
prefix. Every one of those inherits your naming choice. RDR-204 moves the
collection's attributes out of its name and into catalog columns, and its
Context section names the follow-on work that moves each of those
mechanisms to the grain it actually wants. Until then, the rules above are
how to keep the grain from working against you.

See also: [Storage Tiers § Collections](storage-tiers.md#collections) for
the naming shape and model table, [Repo Indexing](repo-indexing.md) for the
code, docs, and rdr pipeline, and
[RDR-204](rdr/rdr-204-embedding-profile-and-collection-authority.md).
