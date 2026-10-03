// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-wbfpw.71 and the producer half of nexus-wbfpw.68 (Sam, 2026-10-03: option (b)).
 *
 * <p>Before this, deleting or renaming an ORIGIN collection left its {@code quarantine-} rows behind with no
 * audit row and no way to reach them (the delete of the sibling was unaudited, nothing expired rows whose origin
 * was gone, and a rename left the rows tagged for a name that no longer exists). Four engine paths also moved or
 * deleted a {@code quarantine-} collection's chunks with no guard at all. This pins the replacement behaviour:
 * <ul>
 *   <li>A: delete of an origin takes its quarantine rows in the same transaction, one audit row per sibling;
 *   <li>B: delete of a {@code quarantine-} collection is allowed and audited;
 *   <li>C: rename of an origin retags its quarantine rows, one audit row per sibling, sibling names unchanged;
 *   <li>D: store-delete, rename and rehome refuse a {@code quarantine-} name and name the sanctioned verbs.
 * </ul>
 * Real PostgreSQL throughout. Each test works in its own tenant.
 */
class QuarantineCollectionLifecycleTest extends AtomicWriteTestBase {

    private static final String DELETE_OP = "collection_delete_quarantine";
    private static final String SIBLING_DELETE_OP = "quarantine_collection_delete";
    private static final String RETAG_OP = "quarantine_retag";

    private PgVectorRepository vectors;
    private ChashRepository chashRepo;

    @BeforeAll
    void wire() {
        vectors = new PgVectorRepository(tenantScope, embedder, embedder);
        chashRepo = new ChashRepository(tenantScope);
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    private String newTenant() {
        return "qcl" + seq.incrementAndGet();
    }

    private String col(String tag) {
        return "knowledge__" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private static String reaperSibling(String origin) {
        return "quarantine-" + origin;
    }

    private void register(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection);
        }
    }

    /**
     * The sibling the client derives from the origin's catalog ROW: {@code quarantine-<content_type>__<row owner>
     * __<model>__<version>}. The fixture registry rows carry no model_version, so the name's own version segment
     * stands in for it, as in {@code QuarantineRestoreIntegrationTest}; the engine matches the sibling by its
     * registered attributes and never builds this name.
     */
    private String rowDerivedSibling(String tenant, String origin) throws Exception {
        try (Connection su = pg.createConnection("")) {
            String owner = DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.OWNER_ID)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(origin)))
                .fetchOne(0, String.class);
            assertThat(owner).as("fixture: the origin has a catalog row").isNotNull();
            String[] seg = origin.split("__");
            return "quarantine-" + seg[0] + "__" + owner + "__" + seg[2] + "__" + seg[3];
        }
    }

    private void setOwner(String tenant, String collection, String owner) throws Exception {
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.OWNER_ID, owner)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(collection)))
                .execute();
            assertThat(n).isEqualTo(1);
        }
    }

    /** One quarantined chunk in {@code sibling}; {@code originTag} null means an untagged row (the reaper's older shape). */
    private String qrow(String tenant, String sibling, String seed, String originTag) throws Exception {
        String hex = Chash.ofText(sibling + "/" + seed).toHex();
        var meta = new java.util.LinkedHashMap<String, Object>();
        meta.put("title", seed);
        meta.put("quarantined_at", "2026-09-01T00:00:00Z");
        if (originTag != null) meta.put("origin_collection", originTag);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, sibling);
            PgContainerHelper.insertChunks(ctx, tenant, sibling, List.of(hex), List.of(seed + " text"),
                List.of(new float[384]), List.of(meta));
        }
        return hex;
    }

    private int count(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(CHUNKS,
                CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)));
        }
    }

    private boolean registered(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(collection)));
        }
    }

    private String tag(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection"))
                .from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                    .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())))
                .fetchOne(0, String.class);
        }
    }

    private String quarantinedAt(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "quarantined_at"))
                .from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                    .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())))
                .fetchOne(0, String.class);
        }
    }

    private List<Map<String, Object>> audit(String tenant, String operation) {
        return repo.listGcAudit(tenant, null, operation, 500, 0);
    }

    private static Map<String, Object> only(List<Map<String, Object>> rows, String collection) {
        var hit = rows.stream().filter(r -> collection.equals(r.get("collection"))).toList();
        assertThat(hit).as("exactly one audit row for " + collection + " in " + rows).hasSize(1);
        return hit.get(0);
    }

    @SuppressWarnings("unchecked")
    private static List<String> chashes(Map<String, Object> row) {
        return (List<String>) row.get("chashes");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> details(Map<String, Object> row) {
        return (Map<String, Object>) row.get("details");
    }

    // ── A: delete of an origin takes its quarantine rows ─────────────────────

    @Test
    void deleteOfAnOriginTakesItsTaggedAndUntaggedRowsFromEverySibling_andAuditsEachSibling() throws Exception {
        String t = newTenant();
        String x = col("a1x");
        String other = col("a1other");
        register(t, x);
        register(t, other);
        setOwner(t, x, "curator-9");
        String reaper = reaperSibling(x);
        String derived = rowDerivedSibling(t, x);
        assertThat(derived).as("fixture: the row-derived sibling is not the reaper's").isNotEqualTo(reaper);
        String unrelated = "quarantine-" + col("a1z");

        String tagged = qrow(t, reaper, "tagged", x);
        String untaggedInReaper = qrow(t, reaper, "untagged-reaper", null);
        String othersRow = qrow(t, reaper, "others", other);
        String taggedInDerived = qrow(t, derived, "tagged-derived", x);
        String untaggedInDerived = qrow(t, derived, "untagged-derived", null);
        String strangers = qrow(t, unrelated, "stranger-untagged", null);
        String strangersTagged = qrow(t, unrelated, "stranger-tagged", other);

        Map<String, Integer> counts = repo.deleteCollection(t, x);

        assertThat(counts.get("quarantine_chunks")).as("the rows taken from quarantine, reported").isEqualTo(4);
        assertThat(count(t, reaper)).as("another origin's row in the shared sibling stays").isEqualTo(1);
        assertThat(tag(t, reaper, othersRow)).isEqualTo(other);
        assertThat(count(t, derived)).as("the row-derived sibling is emptied").isZero();
        assertThat(count(t, unrelated)).as("an unrelated sibling is untouched, untagged row included").isEqualTo(2);
        assertThat(tag(t, unrelated, strangersTagged)).isEqualTo(other);
        assertThat(strangers).isNotNull();
        assertThat(tagged).isNotEqualTo(untaggedInReaper);
        assertThat(untaggedInDerived).isNotNull();
        assertThat(taggedInDerived).isNotNull();

        var rows = audit(t, DELETE_OP);
        assertThat(rows).as("one audit row per sibling that lost rows").hasSize(2);
        var inReaper = only(rows, reaper);
        assertThat(inReaper.get("chash_count")).isEqualTo(2);
        assertThat(chashes(inReaper)).containsExactlyInAnyOrder(tagged, untaggedInReaper);
        assertThat(details(inReaper)).containsEntry("origin_collection", x).containsEntry("count", 2);
        var inDerived = only(rows, derived);
        assertThat(inDerived.get("chash_count")).isEqualTo(2);
        assertThat(chashes(inDerived)).containsExactlyInAnyOrder(taggedInDerived, untaggedInDerived);
        assertThat(details(inDerived)).containsEntry("origin_collection", x).containsEntry("count", 2);
        assertThat(inDerived.get("dry_run")).isEqualTo(false);
    }

    /** Registers a quarantine sibling with the given attributes, the way the engine's own registration writes them. */
    private void registerSibling(String tenant, String name, String contentType, String owner, String model)
            throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).insertInto(CATALOG_COLLECTIONS,
                    CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE,
                    CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                    CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                .values(tenant, name, contentType, owner, model, "quarantine")
                .onConflictDoNothing().execute();
        }
    }

    @Test
    void anUntaggedRowIsTheOriginsWhenItsSiblingIsRegisteredWithTheOriginsAttributes_underEitherRegistrationShape()
            throws Exception {
        // The client's sibling is found by the origin's catalog ROW, never by a name the engine builds. The older
        // registration functions wrote content_type "quarantine-<ct>", the later ones copy the origin's row.
        String t = newTenant();
        String x = col("a7x");
        register(t, x);
        String owner = x.split("__")[1];
        String model = x.split("__")[2];
        String copied = "quarantine-copied-" + seq.incrementAndGet();
        String legacy = "quarantine-legacy-" + seq.incrementAndGet();
        String otherModel = "quarantine-other-model-" + seq.incrementAndGet();
        String otherOwner = "quarantine-other-owner-" + seq.incrementAndGet();
        registerSibling(t, copied, "knowledge", owner, model);
        registerSibling(t, legacy, "quarantine-knowledge", owner, model);
        registerSibling(t, otherModel, "knowledge", owner, "bge-base-en-v15-768");
        registerSibling(t, otherOwner, "knowledge", "someone-else", model);
        qrow(t, copied, "in-copied", null);
        qrow(t, legacy, "in-legacy", null);
        qrow(t, otherModel, "in-other-model", null);
        qrow(t, otherOwner, "in-other-owner", null);

        Map<String, Integer> counts = repo.deleteCollection(t, x);

        assertThat(counts.get("quarantine_chunks")).isEqualTo(2);
        assertThat(count(t, copied)).isZero();
        assertThat(count(t, legacy)).isZero();
        assertThat(count(t, otherModel)).as("a sibling of another model is not the origin's").isEqualTo(1);
        assertThat(count(t, otherOwner)).as("a sibling of another owner is not the origin's").isEqualTo(1);
    }

    @Test
    void aSiblingLeftEmptyIsUnregistered_andASiblingStillHoldingAnotherOriginsRowsIsNot() throws Exception {
        String t = newTenant();
        String x = col("a2x");
        String other = col("a2other");
        register(t, x);
        register(t, other);
        setOwner(t, x, "curator-8");
        String reaper = reaperSibling(x);
        String derived = rowDerivedSibling(t, x);
        qrow(t, reaper, "mine", x);
        qrow(t, reaper, "theirs", other);
        qrow(t, derived, "mine-too", x);
        assertThat(registered(t, derived)).isTrue();

        repo.deleteCollection(t, x);

        assertThat(registered(t, derived)).as("an emptied sibling is unregistered, as the ghost sweep would").isFalse();
        assertThat(registered(t, reaper)).as("a sibling that still holds another origin's rows stays").isTrue();
        assertThat(dev.nexus.service.db.CollectionRegistry.isKnown(t, derived))
            .as("and the registry cache no longer vouches for the unregistered name").isFalse();
    }

    @Test
    void deleteOfAnOriginWithNoCatalogRowStillTakesTheRowsTaggedForIt() throws Exception {
        // The production shape (nexus-wbfpw.65's census): the origin has no registry row in any state.
        String t = newTenant();
        String dead = col("a3dead");
        String sibling = reaperSibling(col("a3host"));
        String mine = qrow(t, sibling, "dead-origin", dead);
        String keep = qrow(t, sibling, "live-origin", col("a3live"));

        Map<String, Integer> counts = repo.deleteCollection(t, dead);

        assertThat(counts.get("quarantine_chunks")).isEqualTo(1);
        assertThat(count(t, sibling)).isEqualTo(1);
        assertThat(mine).isNotEqualTo(keep);
        assertThat(only(audit(t, DELETE_OP), sibling).get("chash_count")).isEqualTo(1);
    }

    @Test
    void deleteIsTenantScoped_anotherTenantsRowsTaggedForTheSameNameStay() throws Exception {
        String t = newTenant();
        String u = newTenant();
        String x = col("a4x");
        register(t, x);
        register(u, x);
        String sibling = reaperSibling(x);
        qrow(t, sibling, "t-row", x);
        qrow(u, sibling, "u-row", x);

        repo.deleteCollection(t, x);

        assertThat(count(t, sibling)).isZero();
        assertThat(count(u, sibling)).as("the other tenant's quarantine row is not this delete's").isEqualTo(1);
        assertThat(audit(u, DELETE_OP)).isEmpty();
    }

    @Test
    void deleteOfAnOriginWithNoQuarantineRowsWritesNoQuarantineAudit_andReportsZero() throws Exception {
        String t = newTenant();
        String x = col("a5x");
        register(t, x);

        Map<String, Integer> counts = repo.deleteCollection(t, x);

        assertThat(counts.get("quarantine_chunks")).isZero();
        assertThat(audit(t, DELETE_OP)).isEmpty();
        assertThat(audit(t, SIBLING_DELETE_OP)).isEmpty();
    }

    @Test
    void theAuditChashListIsTruncatedAtTheCapAndTheCountIsExact() throws Exception {
        String t = newTenant();
        String x = col("a6x");
        register(t, x);
        String sibling = reaperSibling(x);
        int n = dev.nexus.service.db.CatalogRepository.GC_AUDIT_MAX_CHASHES + 1;
        var hexes = new ArrayList<String>(n);
        var texts = new ArrayList<String>(n);
        var vecs = new ArrayList<float[]>(n);
        var metas = new ArrayList<Map<String, Object>>(n);
        for (int i = 0; i < n; i++) {
            hexes.add(Chash.ofText(sibling + "/bulk/" + i).toHex());
            texts.add("bulk " + i);
            vecs.add(new float[384]);
            metas.add(Map.of("origin_collection", x, "quarantined_at", "2026-09-01T00:00:00Z"));
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, t, sibling);
            PgContainerHelper.insertChunks(ctx, t, sibling, hexes, texts, vecs, metas);
        }

        Map<String, Integer> counts = repo.deleteCollection(t, x);

        assertThat(counts.get("quarantine_chunks")).isEqualTo(n);
        var row = only(audit(t, DELETE_OP), sibling);
        assertThat(row.get("chash_count")).as("the count is the full count").isEqualTo(n);
        assertThat(chashes(row)).hasSize(dev.nexus.service.db.CatalogRepository.GC_AUDIT_MAX_CHASHES);
        assertThat(details(row)).containsEntry("chashes_truncated", true).containsEntry("count", n);
    }

    // ── B: delete of a quarantine collection is allowed and audited ──────────

    @Test
    void deleteOfAQuarantineCollectionIsAllowed_removesItsRowsAndRegistration_andIsAudited() throws Exception {
        String t = newTenant();
        String sibling = reaperSibling(col("b1x"));
        String a = qrow(t, sibling, "one", col("b1o"));
        String b = qrow(t, sibling, "two", null);

        Map<String, Integer> counts = repo.deleteCollection(t, sibling);

        assertThat(counts.get("chunks")).isEqualTo(2);
        assertThat(count(t, sibling)).isZero();
        assertThat(registered(t, sibling)).isFalse();
        var rows = audit(t, SIBLING_DELETE_OP);
        assertThat(rows).hasSize(1);
        var row = rows.get(0);
        assertThat(row.get("collection")).isEqualTo(sibling);
        assertThat(row.get("chash_count")).isEqualTo(2);
        assertThat(chashes(row)).containsExactlyInAnyOrder(a, b);
        assertThat(details(row)).containsEntry("count", 2);
        assertThat(audit(t, DELETE_OP)).as("a quarantine delete is not an origin delete").isEmpty();
    }

    @Test
    void deleteOfAQuarantineCollectionTruncatesItsChashListAtTheCap() throws Exception {
        String t = newTenant();
        String sibling = reaperSibling(col("b2x"));
        int n = dev.nexus.service.db.CatalogRepository.GC_AUDIT_MAX_CHASHES + 1;
        var hexes = new ArrayList<String>(n);
        var texts = new ArrayList<String>(n);
        var vecs = new ArrayList<float[]>(n);
        var metas = new ArrayList<Map<String, Object>>(n);
        for (int i = 0; i < n; i++) {
            hexes.add(Chash.ofText(sibling + "/bulk/" + i).toHex());
            texts.add("bulk " + i);
            vecs.add(new float[384]);
            metas.add(Map.of("quarantined_at", "2026-09-01T00:00:00Z"));
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, t, sibling);
            PgContainerHelper.insertChunks(ctx, t, sibling, hexes, texts, vecs, metas);
        }

        repo.deleteCollection(t, sibling);

        var row = audit(t, SIBLING_DELETE_OP).get(0);
        assertThat(row.get("chash_count")).isEqualTo(n);
        assertThat(chashes(row)).hasSize(dev.nexus.service.db.CatalogRepository.GC_AUDIT_MAX_CHASHES);
        assertThat(details(row)).containsEntry("chashes_truncated", true);
    }

    @Test
    void deleteOfAnAbsentQuarantineNameWritesNoAuditRow() throws Exception {
        String t = newTenant();
        repo.deleteCollection(t, reaperSibling(col("b3x")));
        assertThat(audit(t, SIBLING_DELETE_OP)).isEmpty();
    }

    // ── C: rename retags the quarantine rows ─────────────────────────────────

    @Test
    void renameOfAnOriginRetagsItsQuarantineRows_oneAuditRowPerSibling_siblingNamesUnchanged() throws Exception {
        String t = newTenant();
        String x = col("c1x");
        String y = col("c1y");
        String other = col("c1other");
        register(t, x);
        setOwner(t, x, "curator-7");
        String reaper = reaperSibling(x);
        String derived = rowDerivedSibling(t, x);
        assertThat(derived).isNotEqualTo(reaper);
        String tagged = qrow(t, reaper, "tagged", x);
        String untagged = qrow(t, reaper, "untagged", null);
        String othersRow = qrow(t, reaper, "others", other);
        String taggedDerived = qrow(t, derived, "tagged-derived", x);
        String untaggedDerived = qrow(t, derived, "untagged-derived", null);
        String unrelated = reaperSibling(col("c1z"));
        String strangers = qrow(t, unrelated, "stranger-untagged", null);

        repo.renameCollection(t, x, y);

        assertThat(tag(t, reaper, tagged)).isEqualTo(y);
        assertThat(quarantinedAt(t, reaper, tagged)).as("a retag merges: the other keys survive")
            .isEqualTo("2026-09-01T00:00:00Z");
        assertThat(tag(t, reaper, untagged)).as("an untagged row in the origin's own sibling is tagged for Y").isEqualTo(y);
        assertThat(tag(t, reaper, othersRow)).as("another origin's row is left alone").isEqualTo(other);
        assertThat(tag(t, derived, taggedDerived)).isEqualTo(y);
        assertThat(tag(t, derived, untaggedDerived)).isEqualTo(y);
        assertThat(tag(t, unrelated, strangers)).as("an unrelated sibling's untagged row is not Y's").isNull();
        assertThat(count(t, reaper)).as("sibling names are not renamed").isEqualTo(3);
        assertThat(count(t, derived)).isEqualTo(2);

        var rows = audit(t, RETAG_OP);
        assertThat(rows).hasSize(2);
        var inReaper = only(rows, reaper);
        assertThat(details(inReaper)).containsEntry("from", x).containsEntry("to", y).containsEntry("count", 2);
        assertThat(inReaper.get("chash_count")).isEqualTo(2);
        assertThat(chashes(inReaper)).containsExactlyInAnyOrder(tagged, untagged);
        assertThat(details(only(rows, derived))).containsEntry("from", x).containsEntry("to", y).containsEntry("count", 2);
    }

    @Test
    void afterARenameTheEngineFindsTheSiblingsForTheNewName_andARestoreIntoItWorks() throws Exception {
        String t = newTenant();
        String x = col("c2x");
        String y = col("c2y");
        register(t, x);
        String hex = qrow(t, reaperSibling(x), "restorable", x);

        repo.renameCollection(t, x, y);

        assertThat(vectors.resolveQuarantineSiblings(t, y)).containsExactly(reaperSibling(x));
        assertThat(tag(t, reaperSibling(x), hex)).as("the row is tagged for the new name, not the retired one").isEqualTo(y);
        var out = vectors.quarantineRestore(t, y, null, List.of(hex), "test-operator", false);
        assertThat(out.restored()).containsExactly(hex);
        assertThat(count(t, y)).isEqualTo(1);
    }

    @Test
    void theCrossModelCopyBranchRetagsToo() throws Exception {
        String t = newTenant();
        String x = col("c3x");
        String y = col("c3y");
        register(t, x);
        register(t, y);
        String hex = qrow(t, reaperSibling(x), "copy-branch", x);

        repo.renameCollection(t, x, y);

        assertThat(tag(t, reaperSibling(x), hex)).isEqualTo(y);
        assertThat(only(audit(t, RETAG_OP), reaperSibling(x)).get("chash_count")).isEqualTo(1);
    }

    @Test
    void theChashRenameRetagsToo() throws Exception {
        String t = newTenant();
        String x = col("c4x");
        String y = col("c4y");
        register(t, x);
        register(t, y);
        String tagged = qrow(t, reaperSibling(x), "chash-tagged", x);
        String untagged = qrow(t, reaperSibling(x), "chash-untagged", null);

        chashRepo.renameCollection(t, x, y);

        assertThat(tag(t, reaperSibling(x), tagged)).isEqualTo(y);
        assertThat(tag(t, reaperSibling(x), untagged)).isEqualTo(y);
        assertThat(only(audit(t, RETAG_OP), reaperSibling(x)).get("chash_count")).isEqualTo(2);
    }

    @Test
    void aRenameWithNoQuarantineRowsWritesNoRetagAudit() throws Exception {
        String t = newTenant();
        String x = col("c5x");
        register(t, x);

        repo.renameCollection(t, x, col("c5y"));

        assertThat(audit(t, RETAG_OP)).isEmpty();
    }

    @Test
    void aRenameIsTenantScoped() throws Exception {
        String t = newTenant();
        String u = newTenant();
        String x = col("c6x");
        String y = col("c6y");
        register(t, x);
        register(u, x);
        String sibling = reaperSibling(x);
        qrow(t, sibling, "t-row", x);
        String uRow = qrow(u, sibling, "u-row", x);

        repo.renameCollection(t, x, y);

        assertThat(tag(u, sibling, uRow)).as("the other tenant's tag is not this rename's").isEqualTo(x);
        assertThat(audit(u, RETAG_OP)).isEmpty();
    }

    // ── D: a quarantine name is refused where the verbs would move or delete its rows ──────────────────────

    private static void assertNamesTheSanctionedVerbs(Throwable e, String name) {
        assertThat(e).isInstanceOf(IllegalArgumentException.class);
        assertThat(e.getMessage()).contains(name).contains("nx t3 quarantine restore").contains("nx t3 gc");
    }

    @Test
    void storeDeleteRefusesAQuarantineCollection_andTouchesNothing() throws Exception {
        String t = newTenant();
        String sibling = reaperSibling(col("d1x"));
        String hex = qrow(t, sibling, "keep", col("d1o"));

        assertThatThrownBy(() -> vectors.delete(t, sibling, List.of(hex)))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, sibling));
        assertThat(count(t, sibling)).isEqualTo(1);
    }

    @Test
    void renameRefusesAQuarantineSource_andAQuarantineTarget() throws Exception {
        String t = newTenant();
        String x = col("d2x");
        register(t, x);
        String sibling = reaperSibling(col("d2s"));
        qrow(t, sibling, "keep", x);

        assertThatThrownBy(() -> repo.renameCollection(t, sibling, col("d2y")))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, sibling));
        assertThatThrownBy(() -> repo.renameCollection(t, x, sibling))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, sibling));
        assertThat(count(t, sibling)).isEqualTo(1);
        assertThat(registered(t, sibling)).isTrue();
    }

    @Test
    void theChashRenameRefusesAQuarantineName() throws Exception {
        String t = newTenant();
        String x = col("d3x");
        register(t, x);
        String sibling = reaperSibling(col("d3s"));
        qrow(t, sibling, "keep", x);

        assertThatThrownBy(() -> chashRepo.renameCollection(t, sibling, x))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, sibling));
        assertThatThrownBy(() -> chashRepo.renameCollection(t, x, sibling))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, sibling));
        assertThat(count(t, sibling)).isEqualTo(1);
    }

    @Test
    void rehomeRefusesAQuarantineSource_andAQuarantineTarget() throws Exception {
        String t = newTenant();
        String x = col("d4x");
        register(t, x);
        String sibling = reaperSibling(col("d4s"));
        qrow(t, sibling, "keep", x);

        assertThatThrownBy(() -> repo.rehomeCollection(t, sibling, x))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, sibling));
        assertThatThrownBy(() -> repo.rehomeCollection(t, x, sibling))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, sibling));
        assertThat(count(t, sibling)).isEqualTo(1);
    }
}
