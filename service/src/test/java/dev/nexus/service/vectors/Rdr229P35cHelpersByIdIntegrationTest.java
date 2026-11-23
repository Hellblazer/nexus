// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.VecPciRegistryNames;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.CollectionRegistry;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the package-private readers of the vectors package, with a registry row renamed in place and the
 * latch held: {@code ManifestLabelStage} (the manifest owners of a collection's chunks), {@code ChunkSensitivity} (the
 * exclusion predicate) and {@code TopicRouteArm#attachMetadata} (the routed rows' metadata). Each finds the renamed collection by its
 * id and answers nothing on the name path, which looks for a chunk and manifest name the rename never wrote.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cHelpersByIdIntegrationTest extends AtomicWriteTestBase {

    private static final Gate3 LATCHED = () -> true;

    private record Renamed(String oldName, String newName, List<String> chashes, String docId) {}

    private Renamed renamed(String tag) throws Exception {
        String oldName = "code__p35chelp" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(oldName);
        String docId = freshDoc("p35chelp", oldName);
        List<String> chashes = List.of(ch("p35c helpers " + tag + " one"), ch("p35c helpers " + tag + " two"));
        svc.writeManyCombined(TENANT, oldName,
            List.of(Map.of("chash", chashes.get(0), "text", "alpha one", "metadata", Map.of("p35c-m", "one")),
                    Map.of("chash", chashes.get(1), "text", "alpha two", "metadata", Map.of("p35c-m", "two"))),
            List.of(doc(docId, List.of(row(0, chashes.get(0)), row(1, chashes.get(1))))), null, false, true);
        String newName = oldName.replace("__v1", "__v2");
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            VecPciRegistryNames.dropRegistryNameKeys(ctx);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, newName)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(oldName)).execute();
        }
        CollectionRegistry.evict(TENANT, oldName);
        CollectionRegistry.evict(TENANT, newName);
        return new Renamed(oldName, newName, chashes, docId);
    }

    @Test
    void theManifestOwnersOfARenamedCollectionAreFoundByIdFromGate3() throws Exception {
        Renamed c = renamed("owners");
        Map<String, Set<String>> asked = Map.of(c.newName(), new LinkedHashSet<>(c.chashes()));

        var latched = new ManifestLabelStage(tenantScope, LATCHED).firstOwners(TENANT, asked, false);
        var never = new ManifestLabelStage(tenantScope, Gate3.never()).firstOwners(TENANT, asked, false);

        assertThat(latched.keySet()).containsExactlyInAnyOrderElementsOf(
            c.chashes().stream().map(h -> ManifestLabelStage.ownerKey(c.newName(), h)).toList());
        assertThat(latched.values()).extracting(ManifestLabelStage.Occurrence::docId).containsOnly(c.docId());
        assertThat(never).as("the name path finds no manifest row named the new label").isEmpty();
        assertThat(new ManifestLabelStage(tenantScope, LATCHED).firstOwners(TENANT,
            Map.of(c.oldName(), new LinkedHashSet<>(c.chashes())), false)).as("the old label holds nothing now").isEmpty();
    }

    @Test
    void theSensitiveExclusionFindsTheManifestOwnerByIdFromGate3() throws Exception {
        Renamed c = renamed("sens");
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            long id = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(c.newName()))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            var mine = CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION_ID.eq(id));
            var notSensitive = ChunkSensitivity.notSensitive(LATCHED, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID,
                CHUNKS.CHASH);

            assertThat(ctx.fetchCount(CHUNKS, mine.and(notSensitive))).as("no sensitive owner: every chunk passes").isEqualTo(2);
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, true)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq(c.docId())).execute();
            assertThat(ctx.fetchCount(CHUNKS, mine.and(notSensitive))).as("a sensitive live owner: none passes").isZero();
            assertThat(ctx.fetchCount(CHUNKS, mine.and(ChunkSensitivity.servableUnderExclude(LATCHED, CHUNKS.TENANT_ID,
                CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID, CHUNKS.CHASH)))).isZero();
            assertThat(ChunkSensitivity.sensitiveChashes(ctx, TENANT, c.newName(), c.chashes()))
                .as("the batched lookup, handed the label").containsExactlyInAnyOrderElementsOf(c.chashes());
        }
    }

    @Test
    void theRoutedRowsMetadataIsReadByTheResolvedIdAndKeyedByIt() throws Exception {
        Renamed c = renamed("attach");
        List<Map<String, Object>> rows = new ArrayList<>();
        for (String h : c.chashes()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("id", h);
            row.put("collection", c.newName());
            rows.add(row);
        }

        new TopicRouteArm(tenantScope, LATCHED).attachMetadata(TENANT, "minilm-l6-v2-384", 384, rows, Set.of(c.newName()), 5_000,
            RequestDeadlineProbe.NONE);

        assertThat(rows).extracting(r -> r.get("p35c-m")).containsExactly("one", "two");
        List<Map<String, Object>> byName = new ArrayList<>();
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("id", c.chashes().get(0));
        row.put("collection", c.newName());
        byName.add(row);
        new TopicRouteArm(tenantScope, Gate3.never()).attachMetadata(TENANT, "minilm-l6-v2-384", 384, byName,
            Set.of(c.newName()), 5_000, RequestDeadlineProbe.NONE);
        assertThat(byName.get(0)).as("the name path finds no chunk named the new label").doesNotContainKey("p35c-m");
    }
}
