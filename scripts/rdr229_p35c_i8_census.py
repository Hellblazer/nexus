#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5c (nexus-p2m1x.25), census invariant I8: a write stores the locked registry row's CURRENT label.

I8 (linda): every writer of a collection name column writes the label of the registry row its own write transaction locked, never the label the
request carried from the route boundary (after a one-row rename the boundary's label may be another collection's by the time the row is
written). One row per (writer, table) of ``service/src/test/resources/rdr229/p31-writer-census.tsv`` that writes a name column, with the sites
(file:line, from that census) and a verdict:

  holds   the writer files the locked row's current label (the basis names the line or the commit that makes it so)
  holds-until-P4.1   a site linda ruled safe in Phase 3 that P4.1 (nexus-p2m1x.35) must revisit (the reaper's by-label SQL calls)
  n/a     the write is not an id-addressed write of a live collection's row (a registry verb, a fidelity import, a trigger, a SQL function whose
          label argument is its caller's, which is a row of this census)

There is no ``violates`` row: a site that violated I8 is fixed in the same change, and ``tests/test_rdr229_p35c_i8_census.py`` pins that the
census covers every writer of the writer census and says nothing else.

Usage: rdr229_p35c_i8_census.py <tree-root> [--write]   (default: print totals; --write rewrites the file)
"""
import collections
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1]).resolve()
SRC = ROOT / "service/src/test/resources/rdr229/p31-writer-census.tsv"
OUT = ROOT / "service/src/test/resources/rdr229/p35c-i8-stored-label-census.tsv"

P35A = "P3.5a round 2 (tamsin)"
# (file, table-or-"*") -> (verdict, basis). A file entry with "*" covers every table of that file not listed by name.
BASIS = {
    ("TaxonomyRepository.java", "*"): ("holds", f"{P35A} 2390d20721: the label is the locked row's, from lockAndRequireWritable / ensureCollectionRegistered / labelNow; import batches (importTopic, importAssignmentsBatch, importMetaBatch, importTopicsBatch) file the label of the row they registered; assign_from_chashes: P3.5c, below. The persist paths stamp the current label but find the topic rows they replace by name in three statements (persist_rebuild and persist_discovered, around the root-topic select and the delete): W1 (tamsin) fixes those, with its own rename-gap test"),
    ("TaxonomyCentroidRepository.java", "*"): ("holds", f"{P35A} 2390d20721: upsertCentroids writes collectionRow.label() (P3.5c's id arbiter keeps it: the conflict arm also sets the name). The two statements that find a centroid's existing rows by name (the dim-transition delete and the model-change delete) are W1 (tamsin)'s, with its own rename-gap test"),
    ("AspectRepository.java", "*"): ("holds", f"{P35A} 2390d20721: writeAspect and the highlight upserts file requireWritable(...).label(); the batch imports file the label of the row they registered."),
    ("PipelineRepository.java", "*"): ("holds", f"{P35A} 7826f5767c: create files the row under the collection's CURRENT label."),
    ("ReembedJobRepository.java", "*"): ("holds", f"{P35A} 51440c6624: start reads both ends in id order and files their current labels"),
    ("RemapRepository.java", "*"): ("holds", f"{P35A} 2390d20721: record_batch reads every collection of the call FOR KEY SHARE in id order and files the rows' current labels. clearLeg matches each leg by CollectionKey.eqHistory directly (P3.5c's rule, the dangling rows of a deleted collection clear too), after the same FOR KEY SHARE reads, and does not use CollectionOwnedRows"),
    ("PgVectorRepository.java", "chunks"): ("holds", "PgVectorRepository.upsertChunksInternal: coll = registered.label() of the write's own requireWritable (P3.5a f83d55540f runs the attempt again when the row was renamed after the gate)"),
    ("TelemetryRepository.java", "*"): ("n/a", "a history row (relevance_log, search_telemetry, hook_failures): stamped by CollectionIds.stampHistory / historyLabel, unlocked by linda's ruling; a request by id stamps the row's current label"),
    ("CatalogRepository.java", "catalog_documents"): ("holds", f"the document writers take the label from DocumentCollectionRef.normalize (the locked row's current label; {P35A} f83d55540f); the move's target is the re-home's locked row (P3.5a, R2)"),
    ("CatalogRepository.java", "catalog_document_chunks"): ("holds", f"insertManifestChunkRows is handed the label registryFirst returned (CatalogRepository.registryFirst: the locked row's current label; {P35A} f83d55540f); a row whose chunk exists is named by the chunk's stored name on purpose (G17: fk_catalog_chunks_chunk ties the two names until P4.1)"),
    ("CatalogRepository.java", "chunks"): ("holds", "upsertManifestChunkVectors is called with the label registryFirst returned (the locked row's current label)"),
    ("CatalogRepository.java", "document_aspects"): ("holds", "completeMove (the document move's satellite re-home) writes the target label registryFirst returned (CatalogRepository.completeMove: the locked row's current label, with its id); the name-path rename cascade and the retag sites run only inside RenameGuard.runRename, where the new name is the locked row's (R1: rename bodies run pre-latch only)"),
    ("CatalogRepository.java", "document_highlights"): ("holds", "completeMove (the document move's satellite re-home) writes the target label registryFirst returned (CatalogRepository.completeMove: the locked row's current label, with its id); the name-path rename cascade and the retag sites run only inside RenameGuard.runRename, where the new name is the locked row's (R1: rename bodies run pre-latch only)"),
    ("CatalogRepository.java", "catalog_collections"): ("n/a", "the registry itself: the collection verbs (create, rename, supersede, delete) write the row they lock FOR UPDATE"),
    ("CatalogRepository.java", "catalog_reconcile_rows"): ("n/a", "a reconcile staging row, not a collection's data: it carries the label the reconciler read from the registry in the same pass"),
    ("CatalogRepository.java", "*"): ("n/a", "the name-path rename cascade and the retag of the move run only inside RenameGuard.runRename, where the row is locked FOR UPDATE and the new name is the row's (R1: rename bodies run pre-latch only); a history table is stamped, not locked (linda)"),
    ("ChashRepository.java", "*"): ("n/a", "renameCollection runs inside RenameGuard.runRename (R1)"),
    ("ManifestReplace.java", "catalog_document_chunks"): ("holds", "update() is handed the label of ManifestReplace.replace, which writeManifestRows passes after registryFirst (the locked row's current label); a row whose chunk exists keeps the chunk's stored name (G17)"),
    ("DepartedWriterSpans.java", "chunks"): ("n/a", "the clear sets metadata and the id only; it names the chunk row by its own stored name and id and writes no label"),
    ("CatalogIdStamps.java", "*"): ("n/a", "the re-home's id repair (P3.5a, R2): it gives the rows the move carried the id of the target the re-home locked; the move, not a request by id, chooses what it writes"),
    ("CollectionMint.java", "*"): ("n/a", "the registry itself: the mint registers the label the caller asked for"),
    ("CollectionRename.java", "*"): ("n/a", "the registry itself: the rename verb"),
    ("CollectionTombstones.java", "*"): ("n/a", "the registry itself: a tombstone's superseded_by text is its successor's label, written by the verb under its FOR UPDATE lock (repointTombstones reads the successor row FOR KEY SHARE and writes its current label)"),
    ("TenantImportLoad.java", "*"): ("n/a", "fidelity import: the file's label and the id the import resolved for it are written together; no request names an id"),
}
SQL = ("n/a", "a SQL function or trigger: its label argument is its caller's, a row of this census (the gc and restore routes pass the locked rows' current labels, P3.5a cde6b8ffd1; assign_from_chashes is fed the row's current label by TaxonomyRepository, P3.5c; the other functions run for rows the caller's transaction already locked or copy the stored name of a row they read)")
SQL_ASSIGN = ("holds", "P3.5c: TaxonomyRepository.assignFromChashesRetryingDeadlocks reads the row FOR KEY SHARE in each of its two transactions and calls the function with the row's CURRENT label (Rdr229P35cIdAddressedRenameRaceTest, own and cross pass)")


REAPER = ("holds-until-P4.1", "nexus-p2m1x.35, R1 reservation: the reaper calls the function by LABEL (ReaperRepository, ChunkReaper); linda ruled the call safe in Phase 3, out of P3.5c's scope, and it is a P4.1 precondition")


def verdict(file, table):
    if file.startswith("nexus.reaper_"):
        return REAPER
    if file.startswith("nexus."):
        return SQL_ASSIGN if file.startswith("nexus.assign_from_chashes_") else SQL
    return BASIS.get((file, table)) or BASIS.get((file, "*")) or (None, None)


def load():
    groups = collections.OrderedDict()
    for line in SRC.read_text().splitlines():
        if line.startswith("#") or line.startswith("family\t"):
            continue
        c = line.split("\t")
        if len(c) < 11 or c[6] == "-":
            continue
        site = c[2]
        file = site.split(" ")[0] if site.startswith("nexus.") else pathlib.PurePosixPath(site).name
        groups.setdefault((file, c[1]), []).append(f"{c[3]}")
    return groups


groups = load()
rows = []
for (file, table), lines in groups.items():
    v, b = verdict(file, table)
    if v is None:
        print(f"NO I8 BASIS for {file} {table} (lines {','.join(lines)})")
        sys.exit(1)
    rows.append((file, table, ",".join(dict.fromkeys(lines)), v, b))
rows.sort()
text = ("# RDR-229 P3.5c (nexus-p2m1x.25), census invariant I8: a write stores the locked registry row's CURRENT label, never the request's.\n"
        "# Generated by scripts/rdr229_p35c_i8_census.py from the writer census (p31-writer-census.tsv); the verdicts are the script's table.\n"
        "# columns: writer (java file, or SQL function), table, sites (line in that file or function body; the writer census has the rest), verdict, basis\n"
        "writer\ttable\tsites\tverdict\tbasis\n" + "".join("\t".join(r) + "\n" for r in rows))
holds = sum(1 for r in rows if r[3] == "holds")
print(f"i8 rows {len(rows)} holds {holds} n/a {len(rows) - holds}")
if "--write" in sys.argv:
    OUT.write_text(text)
sys.exit(0 if OUT.exists() and OUT.read_text() == text else 1)
