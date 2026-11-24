// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.nio.file.InvalidPathException;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;

/**
 * RDR-254: the two catalog move transactions, behind {@code POST /v1/catalog/documents/move} and
 * {@code POST /v1/catalog/owners/move}. A move keeps a document's (or an owner's) identity when its path moves:
 * the row is re-pointed at the new path, and a row that was registered at the new path in the meantime is
 * absorbed into it.
 *
 * <p>Each move is ONE transaction ({@link TenantScope#withTenant}); a dry run runs the same code, reads the same
 * counts and rolls back (a {@link DryRun} thrown from inside the transaction), so the report of a dry run is the
 * report of the write. A move writes catalog, aspect and highlight rows only: never T3, and never a chunk manifest.
 * Every refusal is a {@link CatalogRepository.MergeRefused} (the handler's 409) thrown before the first write.
 */
public final class CatalogMoves {

    /** What a move did (or, for a dry run, would do). Counts are rows. */
    private static final class Counts {
        int repointed;
        int absorbed;
        int trashed;
        int skipped;
        int unchanged;
        int aspectRowsRewritten;
        int aspectRowsDeleted;
        int highlightRowsRewritten;
        int highlightRowsDeleted;
        int linksRemapped;
        int linksCollapsed;
        int linksDropped;
        int ownersFolded = -1;
        final List<String> absorbedTumblers = new ArrayList<>();

        Report report(boolean dryRun) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("dry_run", dryRun);
            m.put("repointed", repointed);
            m.put("absorbed", absorbed);
            m.put("trashed", trashed);
            m.put("skipped", skipped);
            m.put("unchanged", unchanged);
            m.put("aspect_rows_rewritten", aspectRowsRewritten);
            m.put("aspect_rows_deleted", aspectRowsDeleted);
            m.put("highlight_rows_rewritten", highlightRowsRewritten);
            m.put("highlight_rows_deleted", highlightRowsDeleted);
            m.put("links_remapped", linksRemapped);
            m.put("links_collapsed", linksCollapsed);
            m.put("links_dropped", linksDropped);
            if (ownersFolded >= 0) m.put("owners_folded", ownersFolded);
            m.put("absorbed_tumblers", List.copyOf(absorbedTumblers));
            return new Report(m);
        }
    }

    /** A move's answer: the counts as the route's JSON object. */
    public record Report(Map<String, Object> asMap) {
        public Map<String, Object> toMap() {
            return asMap;
        }
    }

    /** Thrown from inside the transaction to roll a dry run back; carries the counts the run reached. */
    private static final class DryRun extends RuntimeException {
        private static final long serialVersionUID = 1L;
        final transient Counts counts;

        DryRun(Counts counts) {
            super("dry run", null, false, false);
            this.counts = counts;
        }
    }

    /** The audit reason of a sensitivity raise made by an absorb. */
    private static final String SENS_REASON_MOVE = "move absorbed";
    /** Bounded like {@code CatalogRepository.MAX_ALIAS_HOPS}. */
    private static final int MAX_ALIAS_HOPS = 16;

    private static final org.slf4j.Logger LOG = org.slf4j.LoggerFactory.getLogger(CatalogMoves.class);

    private final CatalogRepository repo;

    public CatalogMoves(CatalogRepository repo) {
        this.repo = repo;
    }

    private Report run(String tenant, boolean dryRun, Function<DSLContext, Counts> work) {
        try {
            return repo.tenantScope().withTenant(tenant, ctx -> {
                Counts c = work.apply(ctx);
                if (dryRun) throw new DryRun(c);
                return c.report(false);
            });
        } catch (DryRun d) {
            return d.counts.report(true);
        }
    }

    private static CatalogRepository.MergeRefused refuse(String message) {
        return new CatalogRepository.MergeRefused(message);
    }

    private static final java.util.regex.Pattern DRIVE_PATH = java.util.regex.Pattern.compile("^[A-Za-z]:[\\\\/].*", java.util.regex.Pattern.DOTALL);

    /**
     * An absolute path, or the handler's 400: a relative path would be written into rows as if it named a place. A shape
     * check, independent of the engine host's OS (a Linux engine must accept a Windows client's path): absolute is a leading
     * {@code /}, a drive letter, a colon and a separator ({@code C:\} or {@code C:/}), or a leading {@code \\\\} (UNC).
     * {@code C:docs} (drive-relative) and {@code docs/a.pdf} are relative.
     */
    private static void requireAbsolute(String field, String path) {
        boolean absolute = path.startsWith("/") || path.startsWith("\\\\") || DRIVE_PATH.matcher(path).matches();
        if (!absolute) throw new IllegalArgumentException("'" + field + "' must be an absolute path, got " + path);
    }

    /** Test-only: called with the tumblers of the owner move's document rows in the order they were locked. */
    static volatile java.util.function.Consumer<List<String>> lockOrderHookForTests;

    /** The owner prefix of a document tumbler: its first two segments. */
    static String ownerPrefixOf(String tumbler) {
        String[] seg = tumbler.split("\\.");
        return seg.length < 2 ? tumbler : seg[0] + "." + seg[1];
    }

    // ══════════════════════════════════════════════════════════════════════════
    // DOCUMENT MOVE
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Move curator document {@code tumbler} (S) to {@code newSourceUri} / {@code newFilePath} (P): the Document move of
     * RDR-254 Technical Design. A live row T other than S holding {@code newSourceUri} is absorbed into S first; then S's
     * {@code source_uri} and {@code file_path} are set; then S's aspect and highlight rows follow.
     *
     * @throws IllegalArgumentException a blank argument (the handler's 400)
     * @throws CatalogRepository.MergeRefused S is missing, trashed or not a curator document; T belongs to another owner;
     *         the absorb would close an alias cycle; an aspect row of another document sits at the new path
     */
    public Report moveDocument(String tenant, String tumbler, String newSourceUri, String newFilePath, boolean dryRun) {
        if (tumbler == null || tumbler.isBlank()) throw new IllegalArgumentException("'tumbler' required");
        if (newSourceUri == null || newSourceUri.isBlank()) throw new IllegalArgumentException("'new_source_uri' required");
        if (newFilePath == null || newFilePath.isBlank()) throw new IllegalArgumentException("'new_file_path' required");
        if (!newSourceUri.startsWith("file://")) {
            throw new IllegalArgumentException("'new_source_uri' must start with file:// (a curator document's URI is the raw "
                + "file://<absolute path>), got " + newSourceUri);
        }
        requireAbsolute("new_file_path", newFilePath);
        return run(tenant, dryRun, ctx -> documentMove(ctx, tenant, tumbler, newSourceUri, newFilePath));
    }

    private Counts documentMove(DSLContext ctx, String tenant, String s, String newUri, String newPath) {
        // The holder of the new URI, read before the locks and re-read after them: a concurrent move or registration
        // that changed it between the two is refused, not guessed at.
        String candidate = liveHolderOf(ctx, tenant, newUri);
        // gate 3 is read once for the whole move (RDR-229 P3.5c): from the latch the collections of the documents are found, locked and matched
        // by their registry ids, never by the stored label a one-row rename leaves stale
        final CollectionKey ck = CollectionKey.of(repo.gate3());
        // RDR-229 P3.5a lock order: the registry rows of the collections these documents are in come BEFORE any document row is locked
        // (a retiring verb holds the registry row and then wants child rows; a child row first and the registry row after it can close
        // a cycle with it). rekeyHighlights below needs the row of S's collection; it is read here, unlocked-then-locked, and checked
        // again once the document rows are held.
        lockCollectionsOfDocuments(ctx, tenant, ck, CATALOG_DOCUMENTS.TUMBLER.in(candidate == null ? List.of(s) : List.of(s, candidate)));
        CatalogRepository.lockDocumentRowsSorted(ctx, tenant, true, s, candidate);
        String t = liveHolderOf(ctx, tenant, newUri);
        if (!java.util.Objects.equals(candidate, t)) {
            throw refuse("the holder of " + newUri + " changed while the move was starting (" + candidate + " then " + t
                + "); run it again");
        }
        requireCollectionsHeld(ctx, tenant, ck, CATALOG_DOCUMENTS.TUMBLER.in(t == null ? List.of(s) : List.of(s, t)));

        var sRow = ctx.select(CATALOG_DOCUMENTS.SOURCE_URI, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.ALIAS_OF,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
            .from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(s))
                   .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
            .fetchOne();
        if (sRow == null) {
            throw refuse("document not found (absent, trashed, or not visible to this tenant): " + s);
        }
        String ownerType = ctx.select(CATALOG_OWNERS.OWNER_TYPE).from(CATALOG_OWNERS)
            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(ownerPrefixOf(s))))
            .fetchOne(CATALOG_OWNERS.OWNER_TYPE);
        if (!"curator".equals(ownerType)) {
            throw refuse(s + " is not a curator document (its owner is " + (ownerType == null ? "missing" : "a " + ownerType + " owner")
                + "): a repository file's path is relative to its owner's root, so move the owner (owners/move) instead");
        }
        String collection = sRow.value4() == null ? "" : sRow.value4();
        Long collectionId = sRow.value5();
        String sAlias = sRow.value3() == null ? "" : sRow.value3();
        if (!sAlias.isBlank() && !sAlias.equals(t)) {
            throw refuse(s + " is an alias of " + sAlias + ": a document that points at another is not given a live URI of its "
                + "own; move " + sAlias + " instead");
        }
        Counts c = new Counts();

        if (t != null && !t.equals(s)) {
            if (!ownerPrefixOf(t).equals(ownerPrefixOf(s))) {
                throw refuse(t + " already holds " + newUri + " but belongs to owner " + ownerPrefixOf(t) + ", not "
                    + ownerPrefixOf(s) + ": a move absorbs a row only of the document's own owner");
            }
            // No alias-cycle check here: S may not be an alias of another row (refused above), so absorbing T into S
            // cannot close a loop. The owner fold, where an S may be an alias, checks its graph (planFold).
            Boolean tSensitive = ctx.select(CATALOG_DOCUMENTS.SENSITIVE).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(t))
                       .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                .fetchOne(CATALOG_DOCUMENTS.SENSITIVE);
            absorb(ctx, tenant, c, s, t, new AbsorbFacts(true, Boolean.TRUE.equals(tSensitive), true, t.equals(sAlias)));
            trashAbsorbed(ctx, tenant, List.of(t));
            moveAspects(ctx, tenant, ck, c, s, t, collection, collectionId, newUri, newPath);
            rekeyHighlights(ctx, tenant, ck, c, s, t, sRow.value4(), collectionId, newUri);
        } else {
            moveAspects(ctx, tenant, ck, c, s, null, collection, collectionId, newUri, newPath);
        }

        // Step 2: S itself. T's source_uri was cleared by the absorb, so the live-URI index lets S take it.
        if (!newUri.equals(sRow.value1()) || !newPath.equals(sRow.value2())) {
            ctx.update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, newUri)
                .set(CATALOG_DOCUMENTS.FILE_PATH, newPath)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CatalogIdStamps.keepDocumentId())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(s))
                       .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                .execute();
            c.repointed++;
        }
        // Step 4's last part: S's highlights row names the new URI.
        c.highlightRowsRewritten += ctx.update(DOCUMENT_HIGHLIGHTS)
            .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, newUri)
            .set(DOCUMENT_HIGHLIGHTS.COLLECTION_ID, CatalogIdStamps.keepHighlightId())
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(s))
                   .and(DOCUMENT_HIGHLIGHTS.SOURCE_URI.ne(newUri)))
            .execute();
        afterWritesHook();
        return c;
    }

    /** Test-only: runs after a move's last write, inside the transaction; a test throws from it to prove the rollback. */
    static volatile Runnable afterWritesHookForTests;

    private static void afterWritesHook() {
        Runnable h = afterWritesHookForTests;
        if (h != null) h.run();
    }

    /**
     * The registry rows (FOR KEY SHARE, one statement, id order) of the collections the documents matching {@code docCond} are filed in,
     * read BEFORE the document rows are locked. The document read here is unlocked, so a document that moves collection before its row is
     * locked is caught by {@link #rekeyHighlights}. A label with no registry row is left to the check that follows (the 422 as before).
     */
    private static void lockCollectionsOfDocuments(DSLContext ctx, String tenant, CollectionKey ck, org.jooq.Condition docCond) {
        var docs = ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(docCond)).fetch();
        List<String> matched = docs.stream().map(r -> r.value1()).toList();
        if (ck.byId()) {
            // From gate 3 by collection ids, ALL of them in ONE id-ordered statement (a retiring verb's order, before any document row is locked):
            // the documents' own collections, and the collections of the rows the move writes besides: the documents' aspect and highlight
            // rows (each carries its own collection) and the documents that name a matched one as alias_of. A lock that cannot be taken fails
            // the move, before any write: an id no registry row holds is UnknownCollectionIdException, a retired row CollectionRetiredIdException
            // (nothing is skipped; linda 2026-11-23). The ids are read first, unlocked, then locked at once; requireCollectionsHeld checks them
            // again once the document rows are held.
            CollectionRegistry.requireIdsAndLabelsForWrite(ctx, tenant, collectionIdsOf(ctx, tenant, docs, matched), List.of());
            return;
        }
        // Before the latch, by label, the same set the by-id branch locks: the documents' own labels and those of the rows the move writes besides
        // (the documents' aspect and highlight rows, and the documents that name a matched one as alias_of), all in the one call (RDR-229 P3.5a row 14)
        List<String> labels = labelsOf(ctx, tenant, docs, matched);
        if (labels.isEmpty()) {
            return;
        }
        try {
            CollectionRegistry.requireAllForWrite(ctx, tenant, labels);
        } catch (UnregisteredCollectionException unregistered) {
            // see below: a label with no registry row is no lock to take
        }
        // Before the latch, labels are exact, so an unregistered physical_collection has no registry row: there is no lock to take, and no
        // verb can race the move (every verb needs the row). After the latch, an unresolved label is a stale label for a row that exists,
        // so skipping its lock would misdirect the move: refuse (the by-id branch above has no such skip) (linda 2026-11-23). The rekey that
        // needs the row answers as it always did. A single unregistered label is skipped quietly by the lock (a multi-label call raises), so
        // the documents are named here either way: an unregistered collection is a data problem.
        java.util.Set<String> registered = new java.util.HashSet<>(ctx.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.in(labels)).fetch(CATALOG_COLLECTIONS.NAME));
        for (var d : docs) {
            if (d.value2() != null && !d.value2().isEmpty() && !registered.contains(d.value2())) {
                LOG.warn("event=catalog_move_unregistered_collection tenant={} doc_id={} collection={}: the move goes ahead without a registry lock",
                    tenant, d.value1(), d.value2());
            }
        }
    }

    /** {@link #collectionIdsOf} by label, for the move before the latch: the distinct non-empty labels, in the order they are first met. */
    private static List<String> labelsOf(DSLContext ctx, String tenant,
            org.jooq.Result<? extends org.jooq.Record3<String, String, Long>> docs, List<String> matched) {
        java.util.Set<String> labels = new java.util.LinkedHashSet<>();
        docs.forEach(r -> labels.add(r.value2()));
        for (List<String> chunk : chunks(matched)) {
            labels.addAll(ctx.selectDistinct(DOCUMENT_ASPECTS.COLLECTION).from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.DOC_ID.in(chunk))).fetch(DOCUMENT_ASPECTS.COLLECTION));
            labels.addAll(ctx.selectDistinct(DOCUMENT_HIGHLIGHTS.COLLECTION).from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.DOC_ID.in(chunk))).fetch(DOCUMENT_HIGHLIGHTS.COLLECTION));
            labels.addAll(ctx.selectDistinct(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.ALIAS_OF.in(chunk))).fetch(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION));
        }
        return labels.stream().filter(l -> l != null && !l.isEmpty()).toList();
    }

    /**
     * The collection ids a move on {@code matched} (the documents of {@code docs}) writes in: those documents' own, and those of their aspect and
     * highlight rows and of the documents whose alias_of names one of them (any owner, any collection).
     */
    private static java.util.SortedSet<Long> collectionIdsOf(DSLContext ctx, String tenant,
            org.jooq.Result<? extends org.jooq.Record3<String, String, Long>> docs, List<String> matched) {
        java.util.SortedSet<Long> ids = new java.util.TreeSet<>();
        docs.forEach(r -> { if (r.value3() != null) ids.add(r.value3()); });
        for (List<String> chunk : chunks(matched)) {
            ids.addAll(ctx.selectDistinct(DOCUMENT_ASPECTS.COLLECTION_ID).from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.DOC_ID.in(chunk)).and(DOCUMENT_ASPECTS.COLLECTION_ID.isNotNull()))
                .fetch(DOCUMENT_ASPECTS.COLLECTION_ID));
            ids.addAll(ctx.selectDistinct(DOCUMENT_HIGHLIGHTS.COLLECTION_ID).from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.DOC_ID.in(chunk)).and(DOCUMENT_HIGHLIGHTS.COLLECTION_ID.isNotNull()))
                .fetch(DOCUMENT_HIGHLIGHTS.COLLECTION_ID));
            ids.addAll(ctx.selectDistinct(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.ALIAS_OF.in(chunk)).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID.isNotNull()))
                .fetch(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID));
        }
        return ids;
    }

    /**
     * Once the document rows are held: every collection the move writes in must be one whose registry row this transaction already holds (taken
     * by {@link #lockCollectionsOfDocuments} before any document row). A document re-homed, or an aspect, highlight or alias row filed under a
     * collection that arrived between that read and the document locks, names one it does not hold; the move is refused (run it again), never
     * carried on under a lock it does not have, and nothing was written (linda 2026-11-23, CM-FIX). Before gate 3 the labels are exact and
     * the pre-latch skip stays as it is.
     */
    private static void requireCollectionsHeld(DSLContext ctx, String tenant, CollectionKey ck, org.jooq.Condition docCond) {
        if (!ck.byId()) {
            return;
        }
        var docs = ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(docCond)).fetch();
        java.util.SortedSet<Long> ids = collectionIdsOf(ctx, tenant, docs, docs.stream().map(r -> r.value1()).toList());
        if (ids.isEmpty()) {
            return;
        }
        var names = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.COLLECTION_ID.in(ids))).fetchMap(CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.NAME);
        for (long id : ids) {
            String label = names.get(id);
            if (label == null || !(WriteRegistryMemo.get(ctx, tenant, "#" + id) != null || CollectionRegistry.holdsRow(ctx, tenant, label))) {
                throw refuse("a collection the move writes in (id " + id + ") changed while the move was starting and its registry row is not held; run it again");
            }
        }
    }

    /** The tumbler of the LIVE document holding {@code sourceUri}, or null. */
    private static String liveHolderOf(DSLContext ctx, String tenant, String sourceUri) {
        return ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.SOURCE_URI.eq(sourceUri))
                   .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
            .fetchOne(CATALOG_DOCUMENTS.TUMBLER);
    }

    /**
     * Refuse, before any write, absorbs that would leave an {@code alias_of} cycle. {@code sOf} maps each absorbed row T
     * to the row S that keeps its identity. The absorb re-points EVERY row that names a T (live or trashed, any owner) at
     * its S, and clears an S's pointer that names its own T; so the final graph is the stored one with each target
     * replaced by its S. A cycle must pass through a re-pointed edge, hence through an S that some row pointed at T
     * from: {@code starts} are those S, and each is walked (every row, bounded) in the FINAL graph. Checking pair by
     * pair against the stored graph would miss a cycle that two absorbs close together (A/x names B/y, A/y names B/x).
     */
    private static void refuseAliasCycles(DSLContext ctx, String tenant, Map<String, String> sOf, List<String> starts) {
        for (String start : starts) {
            List<String> chain = new ArrayList<>();
            chain.add(start);
            String cursor = start;
            for (int hop = 0; hop < MAX_ALIAS_HOPS; hop++) {
                String next = ctx.select(CATALOG_DOCUMENTS.ALIAS_OF).from(CATALOG_DOCUMENTS)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(cursor)))
                    .fetchOne(CATALOG_DOCUMENTS.ALIAS_OF);
                if (next == null || next.isBlank()) break;
                String finalNext = sOf.getOrDefault(next, next);
                if (finalNext.equals(cursor)) break;          // the row names its own absorbed twin: the absorb clears it
                chain.add(finalNext.equals(next) ? next : next + " (absorbed into " + finalNext + ")");
                if (finalNext.equals(start)) {
                    throw refuse("absorbing into " + start + " would create an alias cycle: " + String.join(" -> ", chain)
                        + " -> " + start + "; settle the alias_of rows on that chain first");
                }
                if (chain.size() > MAX_ALIAS_HOPS) break;
                cursor = finalNext;
            }
        }
    }

    /**
     * What an absorb of one row must do, known by the caller before the call: the document move reads it for its one
     * row, the owner fold reads it for every row of B in a few set-based queries, so a fold of thousands of rows does not
     * probe links, aliases and the sensitive flag once per row.
     */
    private record AbsorbFacts(boolean hasLinks, boolean sensitive, boolean aliased, boolean selfAliased) {}

    /**
     * Absorb T into S: the steps of {@code mergeDocuments} with S as the canonical row (RDR-254 Technical Design, Absorb).
     * Links go onto S; a sensitive T raises S; every other row naming T as {@code alias_of} is pointed at S, and S's own
     * pointer is cleared when it names T. T's {@code source_uri} is cleared and T trashed by {@link #trashAbsorbed}, which
     * the caller runs for all its rows at once, before S takes T's URI. T's chunk manifest stays on the trashed row. Aspect
     * and highlight rows are the caller's: the document move and the owner fold treat them differently.
     */
    private void absorb(DSLContext ctx, String tenant, Counts c, String s, String t, AbsorbFacts facts) {
        if (facts.hasLinks()) {
            var lc = repo.remapLinksForMerge(ctx, tenant, t, s);
            c.linksRemapped += lc.remapped();
            c.linksCollapsed += lc.collapsed();
            c.linksDropped += lc.dropped();
        }
        if (facts.sensitive()) {
            CatalogRepository.raiseSensitivity(ctx, tenant, List.of(s), SENS_REASON_MOVE + " " + t);
        }
        if (facts.aliased()) {
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, s)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CatalogIdStamps.keepDocumentId())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.ALIAS_OF.eq(t))
                       .and(CATALOG_DOCUMENTS.TUMBLER.ne(s)))
                .execute();
        }
        if (facts.selfAliased()) {
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, "")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CatalogIdStamps.keepDocumentId())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(s))
                       .and(CATALOG_DOCUMENTS.ALIAS_OF.eq(t)))
                .execute();
        }
        c.absorbed++;
        c.absorbedTumblers.add(t);
    }

    /** Clear the {@code source_uri} of the absorbed rows and trash them, in statements of at most {@link #CHUNK} rows. */
    private static void trashAbsorbed(DSLContext ctx, String tenant, List<String> absorbed) {
        for (List<String> chunk : chunks(absorbed)) {
            ctx.update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "")
                .set(CATALOG_DOCUMENTS.DELETED_AT, DSL.currentOffsetDateTime())
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CatalogIdStamps.keepDocumentId())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.in(chunk))
                       .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                .execute();
        }
    }

    /** Rows per set-based statement: far under PostgreSQL's 32,767 bind-parameter limit. */
    private static final int CHUNK = 1000;

    private static <T> List<List<T>> chunks(List<T> all) {
        List<List<T>> out = new ArrayList<>();
        for (int i = 0; i < all.size(); i += CHUNK) out.add(all.subList(i, Math.min(all.size(), i + CHUNK)));
        return out;
    }

    /**
     * Step 3 of the Document move: S's aspect rows follow the document. If T has an aspect row at the new path in S's
     * collection it is the newer extraction: S's old row goes first, then T's row is re-keyed to S, so the unique key
     * (tenant, collection, source_path) never holds two rows. Every other aspect row of T is deleted. Then S's rows are
     * pointed at the new path; another document's row already there is a refusal (written nowhere).
     */
    private void moveAspects(DSLContext ctx, String tenant, CollectionKey ck, Counts c, String s, String t, String collection,
                             Long collectionId, String newUri, String newPath) {
        // S's collection: the stored label before gate 3, the collection id from it (the label a one-row rename leaves stale matches no row)
        org.jooq.Condition inS = ck.byId() ? DOCUMENT_ASPECTS.COLLECTION_ID.eq(collectionId) : DOCUMENT_ASPECTS.COLLECTION.eq(collection);
        if (t != null) {
            boolean tHasNewer = ctx.fetchExists(DOCUMENT_ASPECTS,
                DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.DOC_ID.eq(t))
                    .and(inS).and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(newPath)));
            if (tHasNewer) {
                c.aspectRowsDeleted += ctx.deleteFrom(DOCUMENT_ASPECTS)
                    .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.DOC_ID.eq(s))
                           .and(inS).and(DOCUMENT_ASPECTS.SOURCE_PATH.ne(newPath)))
                    .execute();
                c.aspectRowsRewritten += ctx.update(DOCUMENT_ASPECTS)
                    .set(DOCUMENT_ASPECTS.DOC_ID, s).set(DOCUMENT_ASPECTS.SOURCE_URI, newUri)
                    .set(DOCUMENT_ASPECTS.COLLECTION_ID, CatalogIdStamps.keepAspectId())
                    .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.DOC_ID.eq(t))
                           .and(inS).and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(newPath)))
                    .execute();
            }
            c.aspectRowsDeleted += ctx.deleteFrom(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.DOC_ID.eq(t)))
                .execute();
        }
        var sRows = ctx.select(DOCUMENT_ASPECTS.ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH,
                DOCUMENT_ASPECTS.SOURCE_URI, DOCUMENT_ASPECTS.COLLECTION_ID)
            .from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.DOC_ID.eq(s)))
            .fetch();
        for (var r : sRows) {
            if (newPath.equals(r.value3()) && newUri.equals(r.value4())) continue;
            var other = ctx.select(DOCUMENT_ASPECTS.ID, DOCUMENT_ASPECTS.DOC_ID).from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant)
                       .and(ck.byId() ? DOCUMENT_ASPECTS.COLLECTION_ID.eq(r.value5()) : DOCUMENT_ASPECTS.COLLECTION.eq(r.value2()))
                       .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(newPath)).and(DOCUMENT_ASPECTS.ID.ne(r.value1())))
                .fetchOne();
            if (other != null) {
                String otherDoc = other.value2() == null ? "" : other.value2();
                boolean live = !otherDoc.isBlank() && ctx.fetchExists(CATALOG_DOCUMENTS,
                    CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(otherDoc))
                        .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()));
                if (live) {
                    throw refuse("an aspect row of live document " + otherDoc + " already sits at " + newPath + " in "
                        + r.value2() + "; remove it first with: nx enrich delete " + shellQuote(r.value2()) + " "
                        + shellQuote(newPath) + " (or move " + otherDoc + " off that path)");
                }
                // A row of a trashed (or vanished) document at the new path is superseded: the path now belongs to S.
                c.aspectRowsDeleted += ctx.deleteFrom(DOCUMENT_ASPECTS)
                    .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.ID.eq(other.value1()))).execute();
            }
            c.aspectRowsRewritten += ctx.update(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.SOURCE_PATH, newPath).set(DOCUMENT_ASPECTS.SOURCE_URI, newUri)
                .set(DOCUMENT_ASPECTS.COLLECTION_ID, CatalogIdStamps.keepAspectId())
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.ID.eq(r.value1())))
                .execute();
        }
    }

    private static String shellQuote(String v) {
        return "'" + v.replace("'", "'\\''") + "'";
    }

    /**
     * Step 4 of the Document move (and of the owner fold): T's highlights row is re-keyed to S, in S's collection and
     * under {@code rowUri} (S's URI after the move), when S has none; otherwise it is deleted.
     */
    private void rekeyHighlights(DSLContext ctx, String tenant, CollectionKey ck, Counts c, String s, String t, String collection,
                                 Long collectionId, String rowUri) {
        // From gate 3 the row of S's collection is the one its id names, and the label it is written under is that row's CURRENT label
        // (the locked row's, never the stale name S carries); before it, the stored label
        String label = collection;
        if (ck.byId() && collectionId != null) {
            label = ctx.select(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME)
                .from(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
                .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                    .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.COLLECTION_ID.eq(collectionId)))
                .fetchOne(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME);
            if (label == null) {
                throw refuse("the collection of " + s + " (id " + collectionId + ") has no registry row; nothing was written for it");
            }
        }
        boolean hasCollection = ck.byId() ? collectionId != null : collection != null && !collection.isBlank();
        if (hasCollection && !CollectionRegistry.holdsRow(ctx, tenant, label)
                && (ck.byId() || ctx.fetchExists(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS,
                    dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                        .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(label))))) {
            // the document's collection changed between the read before the locks and the read under them: the registry row of the new
            // one cannot be taken now, after the document rows, without inverting the order. Nothing was written for it yet.
            throw refuse("the collection of " + s + " changed while the move was starting (now " + label + "); run it again");
        }
        boolean sHas = ctx.fetchExists(DOCUMENT_HIGHLIGHTS,
            DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(s)));
        if (!sHas) {
            // S's own collection when it has one; a NULL physical_collection leaves the row's collection as it is
            // (the column is NOT NULL, and an empty string would name no collection).
            var q = ctx.update(DOCUMENT_HIGHLIGHTS)
                .set(DOCUMENT_HIGHLIGHTS.DOC_ID, s).set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, rowUri);
            // RDR-229 P3.1: the id follows the name; with no collection to move to the row keeps its own.
            var set = !hasCollection
                ? q.set(DOCUMENT_HIGHLIGHTS.COLLECTION_ID, CatalogIdStamps.keepHighlightId())
                : ck.byId()
                    ? q.set(DOCUMENT_HIGHLIGHTS.COLLECTION, label).set(DOCUMENT_HIGHLIGHTS.COLLECTION_ID, collectionId)   // the row locked above, by its id
                    : q.set(DOCUMENT_HIGHLIGHTS.COLLECTION, collection)
                        .set(DOCUMENT_HIGHLIGHTS.COLLECTION_ID, CollectionIds.require(ctx, tenant, collection));   // P3.5a: read FOR KEY SHARE, not an unlocked name lookup
            c.highlightRowsRewritten += set
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(t)))
                .execute();
        } else {
            c.highlightRowsDeleted += ctx.deleteFrom(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(t)))
                .execute();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // OWNER MOVE
    // ══════════════════════════════════════════════════════════════════════════

    /** One catalog document row as the owner move reads it, once, under its lock. */
    private record DocRow(String tumbler, String sourceUri, String filePath, String aliasOf, boolean live,
                          boolean sensitive, String collection, Long collectionId) {}

    /** A planned rewrite of one root-derived row of A. */
    private record Rewrite(String tumbler, String oldUri, String newUri, boolean live) {}

    /**
     * Move repo owner {@code owner} (A) to {@code newRoot} (R) with repo hash {@code newRepoHash} (H): the Owner move of
     * RDR-254 Technical Design. (1) Any other owner B holding H, live or deactivated, is folded into A: each live row of
     * B is absorbed into the live row of A with the same {@code file_path}, or trashed when A has none; B is deactivated
     * and its hash cleared. (2) A's {@code repo_root} and {@code repo_hash} are set (A keeps its name). (3) A's documents,
     * live and trashed, that are root-derived (a {@code file://} URI ending in {@code /<file_path>}, a relative
     * {@code file_path}) get the {@code source_uri} a fresh registration at R gives them
     * ({@link CatalogRepository#deriveSourceUri}); every other row is skipped and counted. (4) Their aspect and highlight
     * rows follow.
     *
     * @throws IllegalArgumentException a blank argument (the handler's 400)
     * @throws CatalogRepository.MergeRefused A is missing, not a repo owner, or deactivated; a fold would close an alias
     *         cycle; a live row outside A (after the fold) already holds a URI the rewrite would give an A row
     */
    public Report moveOwner(String tenant, String owner, String newRoot, String newRepoHash, boolean dryRun) {
        if (owner == null || owner.isBlank()) throw new IllegalArgumentException("'owner' required");
        if (newRoot == null || newRoot.isBlank()) throw new IllegalArgumentException("'new_root' required");
        if (newRepoHash == null || newRepoHash.isBlank()) throw new IllegalArgumentException("'new_repo_hash' required");
        requireAbsolute("new_root", newRoot);
        return run(tenant, dryRun, ctx -> ownerMove(ctx, tenant, owner, newRoot, newRepoHash));
    }

    private Counts ownerMove(DSLContext ctx, String tenant, String a, String root, String hash) {
        // The other owner holding the hash, found first so both owner rows can be locked together in prefix order (the
        // C order every lock here uses), not in the order of their roles. Re-read under the lock: a registration that
        // took the hash in between is refused, not folded half-seen.
        String bPeek = ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX).from(CATALOG_OWNERS)
            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.REPO_HASH.eq(hash))
                   .and(CATALOG_OWNERS.TUMBLER_PREFIX.ne(a)))
            .fetchOne(CATALOG_OWNERS.TUMBLER_PREFIX);
        var lockCond = CATALOG_OWNERS.TUMBLER_PREFIX.eq(a);
        if (bPeek != null) lockCond = lockCond.or(CATALOG_OWNERS.TUMBLER_PREFIX.eq(bPeek));
        var aOwner = ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.DEACTIVATED_AT,
                CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.REPO_HASH)
            .from(CATALOG_OWNERS)
            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(lockCond))
            .orderBy(CATALOG_OWNERS.TUMBLER_PREFIX.collate(DSL.name("C")))
            .forUpdate().fetch().stream().filter(r -> a.equals(r.value1())).findFirst().orElse(null);
        if (aOwner == null) throw refuse("owner not found (absent or not visible to this tenant): " + a);
        if (!"repo".equals(aOwner.value2())) {
            throw refuse(a + " is a " + aOwner.value2() + " owner: only a repo owner moves with its root (a curator "
                + "document moves with documents/move)");
        }
        if (aOwner.value3() != null) {
            throw refuse(a + " is deactivated: reactivate it first (nx catalog owners --execute reactivate)");
        }
        String b = ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX).from(CATALOG_OWNERS)
            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.REPO_HASH.eq(hash))
                   .and(CATALOG_OWNERS.TUMBLER_PREFIX.ne(a)))
            .fetchOne(CATALOG_OWNERS.TUMBLER_PREFIX);
        if (!java.util.Objects.equals(bPeek, b)) {
            throw refuse("the owner holding " + hash + " changed while the move was starting (" + bPeek + " then " + b
                + "); run it again");
        }

        // Every document of A and B, read once under a row lock in tumbler order, the C order lockDocumentRowsSorted
        // sorts in (a database collation that ignores punctuation would order 1.2.10 and 1.2.9 differently from it, and
        // two writers of the same rows in different orders can deadlock).
        org.jooq.Condition cond = CATALOG_DOCUMENTS.TUMBLER.like(a + ".%");
        if (b != null) cond = cond.or(CATALOG_DOCUMENTS.TUMBLER.like(b + ".%"));
        // RDR-229 P3.5a lock order: the registry rows of the collections these documents are in, before their rows are locked (see documentMove)
        final CollectionKey ck = CollectionKey.of(repo.gate3());   // read once for the whole move (RDR-229 P3.5c)
        lockCollectionsOfDocuments(ctx, tenant, ck, cond);
        List<DocRow> aDocs = new ArrayList<>();
        List<DocRow> bDocs = new ArrayList<>();
        List<String> lockOrder = new ArrayList<>();
        for (var r : ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.SOURCE_URI, CATALOG_DOCUMENTS.FILE_PATH,
                    CATALOG_DOCUMENTS.ALIAS_OF, CATALOG_DOCUMENTS.DELETED_AT, CATALOG_DOCUMENTS.SENSITIVE,
                    CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
                .from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(cond))
                .orderBy(CATALOG_DOCUMENTS.TUMBLER.collate(DSL.name("C")))
                .forUpdate().fetch()) {
            var d = new DocRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5() == null,
                Boolean.TRUE.equals(r.value6()), r.value7() == null ? "" : r.value7(), r.value8());
            (ownerPrefixOf(d.tumbler()).equals(a) ? aDocs : bDocs).add(d);
            lockOrder.add(d.tumbler());
        }
        var hook = lockOrderHookForTests;
        if (hook != null) hook.accept(List.copyOf(lockOrder));
        requireCollectionsHeld(ctx, tenant, ck, cond);

        // The plan: what step 3 rewrites, and the refusal for a URI another live row holds. Both are decided before the
        // first write, so a refusal (and its dry run) leaves nothing behind.
        Counts c = new Counts();
        List<Rewrite> plan = new ArrayList<>();
        for (DocRow d : aDocs) {
            String nu = rootDerivedUri(d, root);
            if (nu == null) c.skipped++;
            else if (nu.equals(d.sourceUri())) c.unchanged++;
            else plan.add(new Rewrite(d.tumbler(), d.sourceUri(), nu, d.live()));
        }
        refuseHeldUris(ctx, tenant, plan, bDocs);

        FoldPlan foldPlan = b == null ? null : planFold(ctx, tenant, aDocs, bDocs);   // refuses an alias cycle: still no write
        if (b != null) {
            fold(ctx, tenant, ck, c, foldPlan, plan);
            ctx.update(CATALOG_OWNERS)
                .set(CATALOG_OWNERS.DEACTIVATED_AT,
                    DSL.coalesce(CATALOG_OWNERS.DEACTIVATED_AT, DSL.currentOffsetDateTime()))
                .set(CATALOG_OWNERS.REPO_HASH, (String) null)
                .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(b)))
                .execute();
            c.ownersFolded = 1;
        } else {
            c.ownersFolded = 0;
        }
        if (!root.equals(aOwner.value4()) || !hash.equals(aOwner.value5())) {
            ctx.update(CATALOG_OWNERS)
                .set(CATALOG_OWNERS.REPO_ROOT, root).set(CATALOG_OWNERS.REPO_HASH, hash)
                .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(a)))
                .execute();
        }
        rewrite(ctx, tenant, c, plan);
        afterWritesHook();
        return c;
    }

    /**
     * The URI a fresh registration at {@code root} would give {@code d}, or null when {@code d} is not root-derived: a
     * {@code file://} URI that ends in {@code /<file_path>} with a relative {@code file_path}. The old root is never
     * needed, so the rewrite works after registration has already overwritten it.
     */
    private static String rootDerivedUri(DocRow d, String root) {
        String fp = d.filePath();
        String su = d.sourceUri();
        if (fp == null || fp.isBlank() || su == null || !su.startsWith("file://")) return null;
        Path p;
        try {
            p = Paths.get(fp);
        } catch (InvalidPathException e) {
            return null;
        }
        if (p.isAbsolute()) return null;
        String norm = p.normalize().toString();
        if (norm.isEmpty() || norm.startsWith("..") || !su.endsWith("/" + norm)) return null;
        String derived = CatalogRepository.deriveSourceUri("", fp, root);
        return derived.isEmpty() ? null : derived;
    }

    /**
     * Refuse the move when a live row that will still be there after the fold already holds a URI the rewrite would
     * give an A row (the unique index on live URIs would refuse the write; this names the row instead, before any).
     * A's own rows that are themselves being rewritten, and B's live rows (all absorbed or trashed by the fold), do not
     * count. Two live A rows that would take the same URI are refused too.
     */
    private static void refuseHeldUris(DSLContext ctx, String tenant, List<Rewrite> plan, List<DocRow> bDocs) {
        Map<String, String> taker = new HashMap<>();
        for (Rewrite r : plan) {
            if (!r.live()) continue;
            String other = taker.put(r.newUri(), r.tumbler());
            if (other != null) {
                throw refuse(other + " and " + r.tumbler() + " would both take " + r.newUri()
                    + ": two live documents of one owner share a file_path");
            }
        }
        Set<String> moving = new HashSet<>();
        plan.forEach(r -> moving.add(r.tumbler()));
        Set<String> folded = new HashSet<>();
        for (DocRow d : bDocs) if (d.live()) folded.add(d.tumbler());
        for (List<String> chunk : chunks(new ArrayList<>(taker.keySet()))) {
            for (var h : ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.SOURCE_URI.in(chunk))
                           .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                    .fetch()) {
                if (moving.contains(h.value1()) || folded.contains(h.value1())) continue;
                throw refuse("live document " + h.value1() + " (owner " + ownerPrefixOf(h.value1()) + ") already holds "
                    + h.value2() + ", which the move would give " + taker.get(h.value2())
                    + "; move or trash " + h.value1() + " first");
            }
        }
    }

    /** What the fold will do, read before the first write: the pairs (B row, A row), B's rows with no counterpart, and the facts. */
    private record FoldPlan(List<DocRow> pairsT, List<DocRow> pairsS, List<String> orphans, Set<String> linked,
                            Set<String> aliased, Set<String> withHighlights) {}

    /**
     * Read the fold: each live row of B is paired with the live row of A with the same {@code file_path}, or is an orphan
     * (a file added after the move: the next index run registers it under A). What an absorb must do per row is read in
     * a few set-based queries, and the alias graph the fold leaves is checked here, before any write.
     */
    private static FoldPlan planFold(DSLContext ctx, String tenant, List<DocRow> aDocs, List<DocRow> bDocs) {
        Map<String, DocRow> counterpart = new HashMap<>();
        for (DocRow d : aDocs) {
            if (d.live() && d.filePath() != null && !d.filePath().isBlank()) counterpart.putIfAbsent(d.filePath(), d);
        }
        List<DocRow> pairsT = new ArrayList<>();
        List<DocRow> pairsS = new ArrayList<>();
        List<String> orphans = new ArrayList<>();
        for (DocRow t : bDocs) {
            if (!t.live()) continue;
            DocRow s = t.filePath() == null ? null : counterpart.get(t.filePath());
            if (s == null) {
                orphans.add(t.tumbler());
            } else {
                pairsT.add(t);
                pairsS.add(s);
            }
        }
        List<String> ts = pairsT.stream().map(DocRow::tumbler).toList();
        Set<String> aliased = aliasTargetsAmong(ctx, tenant, ts);
        Map<String, String> sOf = new HashMap<>();
        List<String> starts = new ArrayList<>();
        for (int i = 0; i < pairsT.size(); i++) {
            String t = pairsT.get(i).tumbler();
            String s = pairsS.get(i).tumbler();
            sOf.put(t, s);
            if (aliased.contains(t) && !starts.contains(s)) starts.add(s);
        }
        refuseAliasCycles(ctx, tenant, sOf, starts);
        return new FoldPlan(pairsT, pairsS, orphans, linkedAmong(ctx, tenant, ts), aliased,
            highlightDocsAmong(ctx, tenant, ts));
    }

    /**
     * Step 1: fold B into A. Each paired row of B is absorbed into its row of A; B's aspect rows of an absorbed row are
     * deleted (they sit in B's collection, never in A's); B's highlights row moves to A's row only when A's has none;
     * B's orphans are trashed.
     */
    private void fold(DSLContext ctx, String tenant, CollectionKey ck, Counts c, FoldPlan fp, List<Rewrite> plan) {
        Map<String, String> newUriOf = new HashMap<>();
        plan.forEach(r -> newUriOf.put(r.tumbler(), r.newUri()));
        List<DocRow> pairsT = fp.pairsT();
        List<DocRow> pairsS = fp.pairsS();
        List<String> ts = pairsT.stream().map(DocRow::tumbler).toList();
        for (int i = 0; i < pairsT.size(); i++) {
            DocRow t = pairsT.get(i);
            DocRow s = pairsS.get(i);
            absorb(ctx, tenant, c, s.tumbler(), t.tumbler(),
                new AbsorbFacts(fp.linked().contains(t.tumbler()), t.sensitive(), fp.aliased().contains(t.tumbler()),
                    t.tumbler().equals(s.aliasOf())));
            if (fp.withHighlights().contains(t.tumbler())) {
                rekeyHighlights(ctx, tenant, ck, c, s.tumbler(), t.tumbler(), s.collection(), s.collectionId(),
                    newUriOf.getOrDefault(s.tumbler(), s.sourceUri()));
            }
        }
        for (List<String> chunk : chunks(ts)) {
            c.aspectRowsDeleted += ctx.deleteFrom(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.DOC_ID.in(chunk)))
                .execute();
        }
        trashAbsorbed(ctx, tenant, ts);
        for (List<String> chunk : chunks(fp.orphans())) {
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, DSL.currentOffsetDateTime())
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CatalogIdStamps.keepDocumentId())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.in(chunk))
                       .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                .execute();
        }
        c.trashed += fp.orphans().size();
    }

    /** The tumblers among {@code tumblers} that any link touches. */
    private static Set<String> linkedAmong(DSLContext ctx, String tenant, List<String> tumblers) {
        Set<String> out = new HashSet<>();
        for (List<String> chunk : chunks(tumblers)) {
            for (var r : ctx.select(CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER).from(CATALOG_LINKS)
                    .where(CATALOG_LINKS.TENANT_ID.eq(tenant)
                           .and(CATALOG_LINKS.FROM_TUMBLER.in(chunk).or(CATALOG_LINKS.TO_TUMBLER.in(chunk))))
                    .fetch()) {
                out.add(r.value1());
                out.add(r.value2());
            }
        }
        return out;
    }

    /** The tumblers among {@code tumblers} that some row names as its {@code alias_of}. */
    private static Set<String> aliasTargetsAmong(DSLContext ctx, String tenant, List<String> tumblers) {
        Set<String> out = new HashSet<>();
        for (List<String> chunk : chunks(tumblers)) {
            out.addAll(ctx.selectDistinct(CATALOG_DOCUMENTS.ALIAS_OF).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.ALIAS_OF.in(chunk)))
                .fetch(CATALOG_DOCUMENTS.ALIAS_OF));
        }
        return out;
    }

    /** The tumblers among {@code tumblers} that have a highlights row. */
    private static Set<String> highlightDocsAmong(DSLContext ctx, String tenant, List<String> tumblers) {
        Set<String> out = new HashSet<>();
        for (List<String> chunk : chunks(tumblers)) {
            out.addAll(ctx.select(DOCUMENT_HIGHLIGHTS.DOC_ID).from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.DOC_ID.in(chunk)))
                .fetch(DOCUMENT_HIGHLIGHTS.DOC_ID));
        }
        return out;
    }

    /**
     * Steps 3 and 4: write the planned URIs onto the document rows, and onto their aspect and highlight rows that carried
     * the old URI (an aspect row's {@code source_path}, the relative {@code file_path}, stays). One JDBC batch per table.
     */
    private static void rewrite(DSLContext ctx, String tenant, Counts c, List<Rewrite> plan) {
        if (plan.isEmpty()) return;
        // The id expressions carry no bind parameter (CatalogIdStamps), so the bind order of the templates is unchanged.
        var docs = ctx.batch(ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "")
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CatalogIdStamps.keepDocumentId())
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq("").and(CATALOG_DOCUMENTS.TUMBLER.eq(""))));
        var asp = ctx.batch(ctx.update(DOCUMENT_ASPECTS).set(DOCUMENT_ASPECTS.SOURCE_URI, "")
            .set(DOCUMENT_ASPECTS.COLLECTION_ID, CatalogIdStamps.keepAspectId())
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq("").and(DOCUMENT_ASPECTS.DOC_ID.eq(""))
                   .and(DOCUMENT_ASPECTS.SOURCE_URI.eq(""))));
        var hl = ctx.batch(ctx.update(DOCUMENT_HIGHLIGHTS).set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, "")
            .set(DOCUMENT_HIGHLIGHTS.COLLECTION_ID, CatalogIdStamps.keepHighlightId())
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq("").and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(""))
                   .and(DOCUMENT_HIGHLIGHTS.SOURCE_URI.eq(""))));
        for (Rewrite r : plan) {
            docs.bind(r.newUri(), tenant, r.tumbler());
            asp.bind(r.newUri(), tenant, r.tumbler(), r.oldUri());
            hl.bind(r.newUri(), tenant, r.tumbler(), r.oldUri());
        }
        docs.execute();
        for (int n : asp.execute()) c.aspectRowsRewritten += Math.max(n, 0);
        for (int n : hl.execute()) c.highlightRowsRewritten += Math.max(n, 0);
        c.repointed += plan.size();
    }
}
