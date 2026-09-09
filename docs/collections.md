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

Give the bare subject and let the catalog render the rest:

```bash
nx store put paper.md --collection distributed-systems
nx index md notes/*.md --collection vector-search
nx index pdf --dir ~/papers/oracles --collection augur-oracle-papers
```

`distributed-systems` becomes `knowledge__distributed-systems__<model>__v1`
on your install, with the model your install actually embeds with. Never
type the four-segment form, a model token, or a version yourself. A name
that encodes a model the install does not use is exactly how local
collections once ended up labelled with a model that never embedded them
(GH #667, RDR-109). RDR-204 is retiring the encoded name as an authority
altogether; a name you type by hand today is a name that has to be
migrated tomorrow.

Subject names are lowercase, hyphen-separated, two or three words, ASCII.
The owner segment of the four-segment name (and of the grandfathered
two-segment form) admits letters, digits, hyphens, and underscores —
`[a-zA-Z0-9_-]+` — one grammar shared by the client's collection-name check
(`src/nexus/corpus.py`), the engine's attribute-backfill walk, and the Java
test helper that seeds fixtures against it (nexus-ztafa).

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
- **DEVONthink imports**: `nx dt import` chooses the subject you give it;
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
