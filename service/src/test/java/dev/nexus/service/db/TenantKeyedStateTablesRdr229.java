// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;

import static dev.nexus.service.db.TenantKeyedStateTables.RESULT;

/**
 * The RDR-229 engine-wide entries of {@link TenantKeyedStateTables}: the boot-time completion check (P3.1), the per-leaf
 * unique-index pass (P3.3b), the id-keyed index pass (P3.4) and the per-transaction registry-row reads (P3.5a), each holding no per-tenant state. Split out so the table stays
 * under the test file-size cap.
 */
final class TenantKeyedStateTablesRdr229 {

    private TenantKeyedStateTablesRdr229() { }

    private static final String LEAF_INDEX_PASS_ENGINE_WIDE = "the RDR-229 P3.3b per-leaf unique-index pass is engine-wide, never a "
        + "tenant's: it reads pg_catalog (partition trees and index states of nexus.chunks and nexus.taxonomy_centroids) and "
        + "inserts two rows into nexus.data_moves (a global registry with no tenant column) on the engine's admin connection, "
        + "and holds relation and index names and counts, no tenant id and no row of any tenant; ";

    /** RDR-229 P3.3b: {@code LeafIndexPass}, {@code LeafIndexCatalog}: every collection they hold is per-pass bookkeeping or a catalog snapshot. */
    static void fillLeafIndexPass(Map<String, String> notKeyed) {
        notKeyed.put("TwinKeyUpsert.Pair.nameKey", "a table's name-key column names (constants of the checked pair list); no tenant or collection value");
        notKeyed.put("TwinKeyUpsert.Pair.idKey", "a table's id-key column names (constants of the checked pair list); no tenant or collection value");
        notKeyed.put("TwinKeyUpsert.PAIRS", "the checked name/id pair list, derived once from ArbiterTwins.ALL (constants); engine-wide, no tenant or collection value");
        notKeyed.put("LeafIndexPass.Result.registered", LEAF_INDEX_PASS_ENGINE_WIDE + "the labels of the swap moves one pass "
            + "inserted (rdr229-swap-<table>), returned to the caller and not kept");
        notKeyed.put("LeafIndexPass.Session.registered", LEAF_INDEX_PASS_ENGINE_WIDE + "the labels this pass has inserted so far; "
            + "the session lives for one pass and is closed with its connection");
        notKeyed.put("LeafIndexPass.backoffs", LEAF_INDEX_PASS_ENGINE_WIDE + "the leaves whose build failed, by leaf relation name, with a "
            + "failure count and the time the next attempt may start; bounded by the leaf count, in memory only, and read and "
            + "written on the pass's own thread under its pass lock");
        notKeyed.put("LeafIndexPass.beforeDdl", LEAF_INDEX_PASS_ENGINE_WIDE + "a test seam: a consumer that sees each DDL statement "
            + "(its kind, relation and text) before the pass sends it; null in production");
        notKeyed.put("LeafIndexCatalog.Tree.models", LEAF_INDEX_PASS_ENGINE_WIDE + "a snapshot of one table's model partitions, "
            + "read per step and dropped with it");
        notKeyed.put("LeafIndexCatalog.Tree.leaves", LEAF_INDEX_PASS_ENGINE_WIDE + "a snapshot of one table's leaf partitions "
            + "(relation names; a leaf's tenant is in its bound, which nothing here reads), read per step and dropped with it");
    }

    private static final String ID_UNIQUE_INDEX_PASS_ENGINE_WIDE = "the RDR-229 P3.4 id-keyed index pass is engine-wide, never a "
        + "tenant's: it reads pg_catalog (index states of the nexus tables it indexes) and builds indexes with CREATE INDEX CONCURRENTLY "
        + "on the engine's admin connection, and holds table and index names and counts, no tenant id and no row of any tenant "
        + "(a duplicate's key values go to the engine log, never into these fields); ";

    /** RDR-229 P3.4: {@code IdUniqueIndexPass}: every collection it holds is per-engine bookkeeping or a test seam. */
    static void fillIdUniqueIndexPass(Map<String, String> notKeyed) {
        notKeyed.put("IdUniqueIndexPass.Result.corrupt", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "the tables whose index build hit a "
            + "duplicate so far, by table name, returned to the caller and not kept");
        notKeyed.put("IdUniqueIndexPass.corrupt", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "the indexes whose build hit a duplicate, by "
            + "index name, with the status text (which names the table, never a tenant or a key); bounded by the number of indexes the "
            + "pass builds, in memory only (the next engine tries again), read and written under the pass lock");
        notKeyed.put("IdUniqueIndexPass.heldReasons", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "the words of the standing conditions the pass holds "
            + "(a corrupt table, a mismatched index), table and index names only, never a tenant or a key value; read by gate3_why");
        notKeyed.put("IdUniqueIndexPass.SPECS", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "the pass's fixed list of indexes (table, index "
            + "name, key columns, predicate), built from ArbiterTwins and constants; the same for every tenant and never changed");
        notKeyed.put("IdUniqueIndexPass.beforeStatement", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "a test seam: a consumer that sees "
            + "each DDL statement's text before the pass sends it; null in production");
        notKeyed.put("IdUniqueIndexPass.mismatched", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "the pass's own indexes found standing with another "
            + "definition, by index name, with the status text; constants of SPECS, no tenant or collection value");
        notKeyed.put("IdUniqueIndexPass.waitLogged", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "the twin index names whose changeset wait has been logged "
            + "once; constants of SPECS");
        notKeyed.put("IdUniqueIndexPass.backoffs", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "the indexes whose build timed out, by index name, with a "
            + "failure count and the next attempt's time; constants of SPECS");
        notKeyed.put("IdUniqueIndexPass.buildTimeout", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "the build statement's bound, a test seam; a duration");
        notKeyed.put("IdUniqueIndexPass.recheck", ID_UNIQUE_INDEX_PASS_ENGINE_WIDE + "how long a finished pass waits before it looks again, a test "
            + "seam; a duration");
    }

    static final String COMPLETION_ENGINE_WIDE = "the RDR-229 boot-time completion check is engine-wide, never a "
        + "tenant's: it reads nexus.data_moves (a global registry with no tenant column), pg_catalog and the registry's "
        + "row counts on the engine's admin connection, and a Result holds labels, table and constraint names and counts "
        + "summed over every tenant, no tenant id on the wire (its one slot that can name a collection, gate3Uncovered, "
        + "holds P3.4's id keys, model and tenant and collection id, when the PCI reconciler has planned; it is held in "
        + "memory only, never rendered on /v1/status, and replaced whole by every sample); ";

    /** RDR-229 P3.1 (nexus-p2m1x.17): the completion check, its reporter and the two callbacks that wake it. */
    static void fillCompletionCheck(Map<String, String> notKeyed) {
        String bookkeeping = COMPLETION_ENGINE_WIDE + "this is the reporter's own bookkeeping, nothing a tenant's reset has to clear";
        notKeyed.put("CompletionBootWork.eventExecutor", "the executor an event's read runs on (the reporter's own "
            + "single thread, or a test's direct executor); holds no data");
        notKeyed.put("CompletionBootWork.readOwed", bookkeeping + " (a flag: an event asked for a read)");
        notKeyed.put("CompletionBootWork.lastSampleAt", bookkeeping + " (when the last read began)");
        notKeyed.put("CompletionBootWork.safetyPoll", "the interval of the slow poll behind the two events; "
            + "configuration, not keyed by tenant");
        notKeyed.put("CompletionBootWork.latest", COMPLETION_ENGINE_WIDE + "it is the last read, replaced whole by every "
            + "sample (at most one safety poll apart) and re-derived from the database, so a tenant a reset removed drops "
            + "out of the counts at the next sample");
        notKeyed.put("CompletionBootWork.DbReader.readSet", "the labels the check reads, fixed at construction "
            + "(CompletionCheck.ReadSet.p33c()); configuration, not keyed by tenant");
        notKeyed.put("CompletionBootWork.DbReader.session", DATA_MOVE_REPORT_SESSION_COMPLETION);
        notKeyed.put("CompletionBootWork.CachedCounter.kept", COMPLETION_ENGINE_WIDE + "this cache holds one violating-row "
            + "count per CHECK constraint name for ten minutes; the counts are summed over every tenant and carry no "
            + "tenant identity, and a reset only makes a count stale until it expires");
        notKeyed.put("FkTwins.Holder.ROWS", "the RDR-229 P3.3a id-twin list (db/rdr229/p33a-fk-twins.tsv), parsed once from the classpath; "
            + "table, constraint and label names, no tenant state");
        notKeyed.put("CompletionCheck.P33C_CONSTRAINTS", "the RDR-229 P3.3c labels that stand on a constraint, to the table and name of that "
            + "constraint (Rdr229SwapMove's names), built once at class load; label, table and constraint names, no tenant state");
        notKeyed.put("Rdr229SwapMove.CHILD_TABLES", "the three child tables of nexus.chunks whose id keys the chunks swap adds "
            + "(RDR-229 P3.3c), fixed at class load; table names, no tenant state");
        notKeyed.put("CompletionCheck.ReadSet.gate1Labels", "the move labels gate 1 reads, fixed at construction; "
            + "global registry labels, not keyed by tenant");
        notKeyed.put("CompletionCheck.ReadSet.gate2Labels", "the move labels gate 2 reads, fixed at construction; "
            + "global registry labels, not keyed by tenant");
        notKeyed.put("CompletionCheck.IdIndexCoverage.planned", RESULT + "; P3.4's PciGate3Coverage supplies it as id keys "
            + "(model, tenant, collection id) read from the catalog and registry by the check, and it is dropped with the read");
        notKeyed.put("CompletionCheck.IdIndexCoverage.validIdIndexed", RESULT + "; as planned");
        notKeyed.put("CompletionCheck.IdIndexCoverage.invalidIdIndexed", RESULT + "; as planned");
        notKeyed.put("CompletionCheck.Result.labels", RESULT + "; it holds registry labels and their state, no tenant's");
        notKeyed.put("CompletionCheck.Result.checks", RESULT + "; it holds constraint names and whether each is validated");
        notKeyed.put("CompletionCheck.Result.violators", RESULT + "; it holds table and constraint names and counts");
        notKeyed.put("CompletionCheck.Result.gate3Uncovered", RESULT + "; the planned id keys with no VALID id index, "
            + "replaced whole by the next sample, in memory only and never rendered on /v1/status");
        notKeyed.put("CompletionCheck.Dangling.byContentType", RESULT + "; R7's dangling-tombstone count by content type "
            + "(a few fixed labels such as code or knowledge), summed over every tenant's registry rows by one GROUP BY on "
            + "content_type: no tenant id or collection name is kept, and the next sample re-derives it");
        notKeyed.put("CompletionCheck.Dangling.selfIdByContentType", RESULT + "; the count of tombstones the stamp resolved "
            + "to their own id, by content type, summed over every tenant as byContentType is; no tenant id or name");
        notKeyed.put("CompletionBootWork.TableWait.constraints", RESULT + "; the CHECK constraint names (fixed by the "
            + "changesets) of one table whose VALIDATE waits, merged across its name columns by byTable; no tenant's");
        notKeyed.put("CompletionTenantCounts.Counts.tables", "a component of one tenant's counts record, built by one "
            + "GET /v1/catalog/rdr229_completion_counts request inside that tenant's own scoped transaction, returned and "
            + "dropped with the response; never held across requests, so a tenant's reset has nothing to clear");
        notKeyed.put("BootWorkRegistry.rdr229Completion", "the supplier of /v1/status's rdr229_completion, bound once at "
            + "boot: it renders the completion check's last read from memory, which is engine-wide counts and labels summed "
            + "over every tenant (CompletionBootWork.latest), and holds no tenant data itself");
        notKeyed.put("StatusHandler.rdr229Completion", "the same supplier, bound once at boot by the status route; a "
            + "callback over the engine-wide completion state, not keyed by tenant");
        notKeyed.put("DataMoveWorker.onMoveCompleted", "the listener the engine hands the worker at boot "
            + "(CompletionBootWork::onMoveCompleted), run when a move records completed_at; a callback, no data");
        notKeyed.put("PciReconciler.passEnded", "the listener the engine hands the reconciler at boot "
            + "(CompletionBootWork::onReconcilerPassEnded), run when a pass ends; a callback, no data");
    }

    private static final String REGISTRY_READ_PER_TRANSACTION = "the RDR-229 P3.5a registry-row read of one write: a record built inside "
        + "one request's own scoped transaction to say which collections that transaction locks, bound to typed locals there and dropped "
        + "when the transaction ends; never a field of a long-lived object, never held across requests, so a tenant's reset has "
        + "nothing to clear; ";

    /** RDR-229 P3.5a row 14 (nexus-p2m1x.23): the per-transaction sets of collection ids and labels a topic or aspect write locks. */
    static void fillRegistryRowReads(Map<String, String> notKeyed) {
        notKeyed.put("TaxonomyRegistryReads.Names.ids", REGISTRY_READ_PER_TRANSACTION + "the stored collection ids a topic write reads");
        notKeyed.put("TaxonomyRegistryReads.Names.labels", REGISTRY_READ_PER_TRANSACTION + "the labels of rows no id has reached yet, "
            + "which the same write reads by label");
        notKeyed.put("AspectDocCollectionLocks.Held.ids", REGISTRY_READ_PER_TRANSACTION + "the stored collection ids an aspect or "
            + "highlight write holds");
        notKeyed.put("TaxonomyRegistryReads.afterFirstReadHookForTests", "a test-only hook, null in production");
        notKeyed.put("AspectDocCollectionLocks.Held.labels", REGISTRY_READ_PER_TRANSACTION + "the labels of unstamped rows the same "
            + "write holds");
    }

    private static final String DATA_MOVE_REPORT_SESSION_COMPLETION = "the completion check's own read-only admin "
        + "connection (default_transaction_read_only on), kept between samples and replaced after a failure; it reads the "
        + "global registry and the catalog and carries no tenant setting";
}
