// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * RDR-229 P2.5 (bead nexus-p2m1x.10) (b): {@link QuarantineOrigin}'s source-side predicates read the catalog row,
 * and judge a row-less name by its prefix (the rule they had). The two package-private row finders
 * ({@code holdsRowsOf}, {@code countRowsOf}) are pinned directly because their callers (the re-embed plan and the
 * ghost sweep) reach them only deep inside a job.
 */
class QuarantineOriginLifecycleTest extends AtomicWriteTestBase {

    private String newTenant() {
        return "qol" + seq.incrementAndGet();
    }

    private String col(String tag) {
        return "knowledge__" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private void row(String tenant, String name, String state) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, name);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, state)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(name))).execute();
        }
    }

    private void taggedRow(String tenant, String sibling, String seed, String origin) throws Exception {
        String hex = Chash.ofText(sibling + "/" + seed).toHex();
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("origin_collection", origin);
        try (Connection su = pg.createConnection("")) {
            // the origin tag names its collection by id as well when the origin is registered (what the SQL move and a retag write); from gate 3 a
            // label-only tag is no sibling (linda ruling b)
            Long originId = DSL.using(su, SQLDialect.POSTGRES).select(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.COLLECTION_ID)
                .from(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
                .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(origin))
                .fetchOne(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.COLLECTION_ID);
            if (originId != null) meta.put(QuarantineOrigin.ORIGIN_ID_KEY, originId);
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), tenant, sibling, List.of(hex),
                List.of(seed), List.of(new float[384]), List.of(meta));
        }
    }

    /** {@link #taggedRow} with the origin's id written as a JSON string: the same text for every reader of the tag, a different JSON value. */
    private void taggedRowWithTextualId(String tenant, String sibling, String seed, String origin) throws Exception {
        String hex = Chash.ofText(sibling + "/" + seed).toHex();
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("origin_collection", origin);
        try (Connection su = pg.createConnection("")) {
            Long originId = DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(origin))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            meta.put(QuarantineOrigin.ORIGIN_ID_KEY, String.valueOf(originId));
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), tenant, sibling, List.of(hex),
                List.of(seed), List.of(new float[384]), List.of(meta));
        }
    }

    @Test
    void theRowDecides_andARowLessNameFallsBackToItsPrefix() throws Exception {
        String t = newTenant();
        String q = col("a");
        String liveButPrefixed = "quarantine-" + col("b");
        row(t, q, "quarantine");
        row(t, liveButPrefixed, "live");

        tenantScope.withTenant(t, ctx -> {
            assertThat(QuarantineOrigin.isQuarantine(ctx, t, q)).isTrue();
            assertThat(QuarantineOrigin.isQuarantine(ctx, t, liveButPrefixed)).isFalse();
            assertThat(QuarantineOrigin.isQuarantine(ctx, t, "quarantine-" + col("c"))).as("row-less, prefixed").isTrue();
            assertThat(QuarantineOrigin.isQuarantine(ctx, t, col("d"))).as("row-less, plain").isFalse();
            return null;
        });
        assertThat(QuarantineOrigin.isQuarantine(Optional.of("dormant"), "quarantine-x")).isFalse();
        assertThat(QuarantineOrigin.isQuarantine(Optional.empty(), "quarantine-x")).isTrue();
        assertThat(QuarantineOrigin.isQuarantine(Optional.empty(), null)).isFalse();
    }

    @Test
    void anOriginWhoseRowIsQuarantineHasNoQuarantineRowsOfItsOwn_andAPrefixedLiveOriginHasThem() throws Exception {
        String t = newTenant();
        String sibling = col("s");
        row(t, sibling, "quarantine");
        String quarantineRowOrigin = col("q");
        row(t, quarantineRowOrigin, "quarantine");
        String prefixedLiveOrigin = "quarantine-" + col("p");
        row(t, prefixedLiveOrigin, "live");
        taggedRow(t, sibling, "one", quarantineRowOrigin);
        taggedRow(t, sibling, "two", prefixedLiveOrigin);

        tenantScope.withTenant(t, ctx -> {
            assertThat(QuarantineOrigin.countRowsOf(ctx, t, quarantineRowOrigin, Gate3.never())).isZero();
            assertThat(QuarantineOrigin.holdsRowsOf(ctx, t, quarantineRowOrigin, Gate3.never())).isFalse();
            assertThat(QuarantineOrigin.countRowsOf(ctx, t, prefixedLiveOrigin, Gate3.never())).isEqualTo(1);
            assertThat(QuarantineOrigin.holdsRowsOf(ctx, t, prefixedLiveOrigin, Gate3.never())).isTrue();
            return null;
        });
    }

    /**
     * RDR-229 P3.5c: from gate 3 the same questions are asked of the collection ids. The quarantine sibling's registry row is renamed
     * (a one-row rename), so its chunk rows still carry the old name: a name match finds nothing, the id match finds both.
     */
    @Test
    void fromGate3TheQuarantineRowsOfAnOriginAreFoundByCollectionId_afterTheSiblingIsRenamed() throws Exception {
        String t = newTenant();
        String sibling = col("s");
        row(t, sibling, "quarantine");
        String origin = col("o");
        row(t, origin, "live");
        taggedRow(t, sibling, "one", origin);
        taggedRow(t, sibling, "two", origin);
        String renamed = col("renamed");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext d = DSL.using(su, SQLDialect.POSTGRES);
            dev.nexus.service.RegistryRenameSupport.dropForeignKeysOntoTheRegistry(d);
            dev.nexus.service.RegistryRenameSupport.rename(d, t, sibling, renamed);
        }
        Gate3 latched = () -> true;

        tenantScope.withTenant(t, ctx -> {
            assertThat(QuarantineOrigin.countRowsOf(ctx, t, origin, Gate3.never()))
                .as("the name arm: the chunk rows name a collection that is no longer a quarantine label").isZero();
            assertThat(QuarantineOrigin.countRowsOf(ctx, t, origin, latched)).as("the id arm").isEqualTo(2);
            assertThat(QuarantineOrigin.holdsRowsOf(ctx, t, origin, latched)).isTrue();
            assertThat(QuarantineOrigin.deleteRowsOf(ctx, t, origin, latched)).as("the sibling that lost rows, by its current label")
                .containsExactly(Map.entry(renamed, 2));
            assertThat(QuarantineOrigin.countRowsOf(ctx, t, origin, latched)).isZero();
            return null;
        });
    }

    /**
     * RDR-229 P3.5c (bead nexus-p2m1x.25, test-strength follow-up 4 of the census evidence pass, comment of 2026-11-23 21:09): the latched arm of
     * {@code retagRowsOf}, which only a one-row rename reaches (it is called with the id of the row it renamed, {@code CollectionOneRowRename}).
     * Both the quarantine sibling and the origin are renamed in place, so the sibling's chunk rows keep its old name and the origin's registry row
     * no longer bears the label the tag names. The retag finds the rows by the sibling's id (by its current label it finds none: the control, the
     * name arm over the same rows), and writes the new label and the origin's id into the tag.
     *
     * <p>Guards {@code retagRowsOf}'s {@code CHUNKS.COLLECTION_ID.eq(sib.id())} (against {@code CHUNKS.COLLECTION.eq(sibling)}) and {@code rowsOf}'s
     * id arms ({@code inQuarantine}, the tag read by the origin's id).
     */
    @Test
    void fromGate3RetagFindsTheSiblingRowsByIdAndTagsTheNewLabelAndTheOriginsId() throws Exception {
        String t = newTenant();
        String sibling = col("s");
        row(t, sibling, "quarantine");
        String origin = col("o");
        row(t, origin, "live");
        // The tag's id is a rename's invariant (the origin keeps its id), so a retag cannot be seen to write it by its value. Both rows are
        // seeded with the id as a JSON STRING, the one pre-existing shape a retag changes: it matches by the same text, and a retag that writes
        // the key leaves a JSON number behind, one that does not leaves the string.
        taggedRowWithTextualId(t, sibling, "one", origin);
        taggedRowWithTextualId(t, sibling, "two", origin);
        String renamedSibling = col("rs");
        String renamedOrigin = col("ro");
        long originId;
        long siblingId;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext d = DSL.using(su, SQLDialect.POSTGRES);
            dev.nexus.service.RegistryRenameSupport.dropForeignKeysOntoTheRegistry(d);
            dev.nexus.service.RegistryRenameSupport.rename(d, t, sibling, renamedSibling);
            dev.nexus.service.RegistryRenameSupport.rename(d, t, origin, renamedOrigin);
            originId = d.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t).and(CATALOG_COLLECTIONS.NAME.eq(renamedOrigin)))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            siblingId = d.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t).and(CATALOG_COLLECTIONS.NAME.eq(renamedSibling)))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
        Gate3 latched = () -> true;

        tenantScope.withTenant(t, ctx -> {
            assertThat(QuarantineOrigin.retagRowsOf(ctx, t, origin, renamedOrigin, Gate3.never(), originId))
                .as("control, the name arm: the chunk rows name a collection that is no longer a registered quarantine label").isZero();
            assertThat(QuarantineOrigin.retagRowsOf(ctx, t, origin, renamedOrigin, latched, originId))
                .as("latched: the sibling's rows are found by its id, under its current label").isEqualTo(2);
            assertThat(QuarantineOrigin.retagRowsOf(ctx, t, origin, renamedOrigin, latched, originId))
                .as("a second retag changes nothing").isZero();
            return null;
        });
        try (Connection su = pg.createConnection("")) {
            var tags = DSL.using(su, SQLDialect.POSTGRES).select(dev.nexus.service.jooq.nexus.Tables.CHUNKS.METADATA)
                .from(dev.nexus.service.jooq.nexus.Tables.CHUNKS)
                .where(dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(t))
                .and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID.eq(siblingId))
                .fetch(dev.nexus.service.jooq.nexus.Tables.CHUNKS.METADATA);
            assertThat(tags).hasSize(2);
            var json = new com.fasterxml.jackson.databind.ObjectMapper();
            for (var m : tags) {
                var node = json.readTree(m.data());
                assertThat(node.path("origin_collection").asText()).as("the tag names the new label").isEqualTo(renamedOrigin);
                assertThat(node.path(QuarantineOrigin.ORIGIN_ID_KEY).isNumber()).as("the retag wrote the id key (a JSON number; it was a string): " + node).isTrue();
                assertThat(node.path(QuarantineOrigin.ORIGIN_ID_KEY).asLong(-1)).as("and the origin's id, which a rename keeps")
                    .isEqualTo(originId);
            }
        }
    }
}
