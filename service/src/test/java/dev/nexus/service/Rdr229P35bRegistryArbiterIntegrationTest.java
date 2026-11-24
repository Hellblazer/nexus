// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.jooq.nexus.Routines;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_RESTORE_REREFERENCED_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_QUARANTINE_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24, linda 2026-11-23, ruling 4): the registry upserts vectors-077 redefines, with the registry's
 * {@code (tenant_id, name)} key gone.
 *
 * <p>P4.1 drops the name key (the primary key and the name-and-model unique) and keeps P3.6's live-label partial UNIQUE
 * ({@code idx_catalog_collections_live_label}) as the one arbiter of a label. Until then the primary key is a second arbiter
 * that a name-only {@code ON CONFLICT (tenant_id, name)} infers whatever its WHERE says, so no test that runs on the primary
 * key proves the upserts work on the partial index alone. This class builds that state (every foreign key onto the registry's name
 * and both name keys dropped, as P3.7 and P4.1 leave it), asserts the partial index is the only unique index on
 * {@code (tenant_id, name)}, and runs every registry upsert function: {@code gc_quarantine_orphans}, {@code _bounded},
 * {@code reaper_quarantine_chunks}, {@code gc_restore_rereferenced} and {@code _bounded}. The moves run twice, once where the
 * quarantine sibling is not registered (the INSERT) and once where it is (the conflict); the restores run where the origin is
 * registered (the conflict path of {@code ON CONFLICT ... DO NOTHING}; an unregistered origin with a manifest row to restore into
 * cannot exist once the collection id CHECKs hold, and PostgreSQL resolves the arbiter before it looks at a row).
 *
 * <p>Two states of the one arbiter. {@code p36}: the index as P3.6 ships it ({@code WHERE superseded_by = ''}). {@code p41}: the
 * index as P4.1 re-creates it ({@code WHERE superseded_by_id IS NULL}). PostgreSQL infers a partial unique index only when the
 * statement's WHERE implies the index predicate, so a WHERE of {@code superseded_by_id IS NULL} alone finds no arbiter on the
 * {@code p36} index and one of {@code superseded_by = ''} alone finds none on the {@code p41} index; the functions say both, and
 * {@link #aWhereThatImpliesOnlyOneOfTheTwoPredicates_findsNoArbiterOnTheOtherIndex} pins why.
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35bRegistryArbiterIntegrationTest {

    static final String T = "p35barb";
    static final String TS = "2026-11-21T00:00:00Z";
    static final String LIVE_LABEL_INDEX = "idx_catalog_collections_live_label";

    PostgreSQLContainer<?> pg;
    Connection su;
    DSLContext ctx;

    enum Fn { GC_QUARANTINE_ORPHANS, GC_QUARANTINE_ORPHANS_BOUNDED, REAPER_QUARANTINE_CHUNKS, GC_RESTORE_REREFERENCED,
        GC_RESTORE_REREFERENCED_BOUNDED;

        boolean isMove() {
            return ordinal() <= REAPER_QUARANTINE_CHUNKS.ordinal();
        }
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.select(DSL.function("set_config", String.class, DSL.val("nexus.tenant"), DSL.val(T), DSL.val(false))).fetch();
        // P3.7's and P4.1's state: no foreign key onto the registry's name, no name key
        assertThat(ByIdRestoreSupport.dropRegistryNameKeys(ctx)).as("non-vacuity: name foreign keys existed").isNotEmpty();
        ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(DSL.name("catalog_collections_pk")).cascade().execute();
        ctx.alterTable(CATALOG_COLLECTIONS).dropConstraintIfExists(DSL.name("catalog_collections_tenant_name_model_uq")).cascade().execute();
        // the restores name a chunk by a manifest row of the origin while the chunk is in the quarantine sibling
        ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(DSL.name("fk_catalog_chunks_chunk")).execute();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    private List<String> uniqueIndexesOnTenantAndName() {
        Table<?> idx = DSL.table(DSL.name("pg_catalog", "pg_indexes"));
        var name = DSL.field(DSL.name("indexname"), String.class);
        var def = DSL.field(DSL.name("indexdef"), String.class);
        return ctx.select(name).from(idx)
            .where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("tablename"), String.class).eq("catalog_collections"))
            .and(def.like("CREATE UNIQUE INDEX%(tenant_id, name)%"))
            .fetch(name);
    }

    @Test
    @Order(1)
    void thePartialLiveLabelIndexIsTheOnlyUniqueKeyOnTheName() {
        assertThat(uniqueIndexesOnTenantAndName()).as("the one arbiter of a label").containsExactly(LIVE_LABEL_INDEX);
        PgContainerHelper.insertCollection(ctx, T, "docs__p35barb-dup__bge-base-en-v15-768__v1");
        assertThatThrownBy(() -> ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(T, "docs__p35barb-dup__bge-base-en-v15-768__v1", "docs", T, "bge-base-en-v15-768", "live").execute())
            .as("a second live row of one label is refused by the partial index").isInstanceOf(DataAccessException.class)
            .hasMessageContaining(LIVE_LABEL_INDEX);
    }

    // -- the fixture -------------------------------------------------------------------------------------

    private static String origin(String tag) {
        return "docs__p35barb-" + tag + "__bge-base-en-v15-768__v1";
    }

    private static String sibling(String origin) {
        return "quarantine-" + origin;
    }

    private byte[] chunk(String collection, String tag, String metadataJson, int ageDays) {
        return chunk(collection, Chash.ofText(collection + "/" + tag).toBytes(), tag, metadataJson, ageDays);
    }

    private byte[] chunk(String collection, byte[] hash, String tag, String metadataJson, int ageDays) {
        ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, T).set(CHUNKS.COLLECTION, collection)
            .set(CHUNKS.COLLECTION_ID, CollectionIds.require(ctx, T, collection)).set(CHUNKS.CHASH, hash)
            .set(CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, T, collection))
            .set(CHUNKS.CHUNK_TEXT, tag + " text").set(VectorSeeds.chunkEmbedding(768), VectorSeeds.vec(768, 0.1f))
            .set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
            .set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minusDays(ageDays))
            .execute();
        return hash;
    }

    private long registryRows(String name) {
        return ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(T).and(CATALOG_COLLECTIONS.NAME.eq(name)));
    }

    private void callMove(Fn fn, String origin, String sibling) {
        switch (fn) {
            case GC_QUARANTINE_ORPHANS -> ctx.selectFrom(GC_QUARANTINE_ORPHANS.call(768, T, origin, sibling, TS, 20)).fetch();
            case GC_QUARANTINE_ORPHANS_BOUNDED ->
                ctx.selectFrom(GC_QUARANTINE_ORPHANS_BOUNDED.call(768, T, origin, sibling, TS, 20, 100)).fetch();
            case REAPER_QUARANTINE_CHUNKS ->
                ctx.selectFrom(REAPER_QUARANTINE_CHUNKS.call(T, origin, sibling, TS, 100, null, 1.0, 0, false)).fetch();
            default -> throw new IllegalArgumentException(fn.name());
        }
    }

    // -- the moves: the registry upsert of the quarantine sibling ------------------------------------------

    @ParameterizedTest(name = "{0}: the sibling is not registered, the INSERT")
    @EnumSource(value = Fn.class, names = {"GC_QUARANTINE_ORPHANS", "GC_QUARANTINE_ORPHANS_BOUNDED", "REAPER_QUARANTINE_CHUNKS"})
    @Order(2)
    void aMove_registersTheSibling_withThePartialIndexAsTheOnlyArbiter(Fn fn) {
        registersTheSibling(fn, "p36");
    }

    private void registersTheSibling(Fn fn, String state) {
        String origin = origin("mi-" + state + fn.ordinal());
        String sibling = sibling(origin);
        PgContainerHelper.insertCollection(ctx, T, origin);
        byte[] hash = chunk(origin, "orphan", "{}", 90);
        assertThat(registryRows(sibling)).as("non-vacuity: the sibling is not registered").isZero();

        callMove(fn, origin, sibling);

        assertThat(registryRows(sibling)).as("%s registered the sibling once", fn).isEqualTo(1);
        var row = ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(sibling)).fetchOne();
        assertThat(row.value1()).as("the sibling is live").isNull();
        assertThat(row.value2()).isEqualTo("quarantine");
        assertThat(ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(T).and(CHUNKS.COLLECTION.eq(sibling)).and(CHUNKS.CHASH.eq(hash))
                .and(CHUNKS.COLLECTION_ID.eq(CollectionIds.require(ctx, T, sibling)))))
            .as("the chunk moved to the sibling under the sibling's id").isTrue();
    }

    @ParameterizedTest(name = "{0}: the sibling is registered, the conflict")
    @EnumSource(value = Fn.class, names = {"GC_QUARANTINE_ORPHANS", "GC_QUARANTINE_ORPHANS_BOUNDED", "REAPER_QUARANTINE_CHUNKS"})
    @Order(3)
    void aMove_updatesAnExistingSibling_withThePartialIndexAsTheOnlyArbiter(Fn fn) {
        updatesTheSibling(fn, "p36");
    }

    private void updatesTheSibling(Fn fn, String state) {
        String origin = origin("mc-" + state + fn.ordinal());
        String sibling = sibling(origin);
        PgContainerHelper.insertCollection(ctx, T, origin);
        PgContainerHelper.insertCollection(ctx, T, sibling);
        long siblingId = CollectionIds.require(ctx, T, sibling);
        byte[] hash = chunk(origin, "orphan", "{}", 90);
        // a stale copy already in the sibling, so the chunk upsert conflicts as well
        chunk(sibling, hash, "stale", "{\"origin_collection\": \"" + origin + "\"}", 90);

        callMove(fn, origin, sibling);

        assertThat(registryRows(sibling)).as("%s left one row for the sibling", fn).isEqualTo(1);
        assertThat(CollectionIds.require(ctx, T, sibling)).as("and its id").isEqualTo(siblingId);
        assertThat(ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(T).and(CHUNKS.COLLECTION.eq(sibling)).and(CHUNKS.CHASH.eq(hash))
                .and(CHUNKS.COLLECTION_ID.eq(siblingId))))
            .as("the chunk is in the sibling under its id").isTrue();
    }

    // -- the restores: the registry upsert of the origin ---------------------------------------------------

    @ParameterizedTest(name = "{0}: the origin is registered, ON CONFLICT DO NOTHING")
    @EnumSource(value = Fn.class, names = {"GC_RESTORE_REREFERENCED", "GC_RESTORE_REREFERENCED_BOUNDED"})
    @Order(4)
    void aRestore_registersTheOriginIfAbsent_withThePartialIndexAsTheOnlyArbiter(Fn fn) {
        restoresIntoTheOrigin(fn, "p36");
    }

    private void restoresIntoTheOrigin(Fn fn, String state) {
        String origin = origin("rs-" + state + fn.ordinal());
        String sibling = sibling(origin);
        PgContainerHelper.insertCollection(ctx, T, origin);
        PgContainerHelper.insertCollection(ctx, T, sibling);
        long originId = CollectionIds.require(ctx, T, origin);
        byte[] hash = chunk(sibling, "held", "{\"origin_collection\": \"" + origin + "\", \"quarantined_at\": \"" + TS + "\"}", 1);
        String doc = "rs" + state + fn.ordinal() + ".1";
        PgContainerHelper.insertCatalogDocument(ctx, T, doc);
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, T).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0).set(CATALOG_DOCUMENT_CHUNKS.CHASH, hash)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, origin).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, originId)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, T, origin))
            .execute();

        if (fn == Fn.GC_RESTORE_REREFERENCED) {
            Routines.gcRestoreRereferenced(ctx.configuration(), 768, T, sibling, origin);
        } else {
            ctx.selectFrom(GC_RESTORE_REREFERENCED_BOUNDED.call(768, T, sibling, origin, 100)).fetch();
        }

        assertThat(registryRows(origin)).as("%s left one row for the origin", fn).isEqualTo(1);
        assertThat(ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(T).and(CHUNKS.COLLECTION.eq(origin)).and(CHUNKS.CHASH.eq(hash))
                .and(CHUNKS.COLLECTION_ID.eq(originId))))
            .as("the chunk is back in the origin under its id").isTrue();
    }

    // -- the arbiter as P4.1 re-creates it -----------------------------------------------------------------

    private void insertSiblingRowNamed(String name, org.jooq.Condition inferenceWhere) {
        var insert = ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(T, name, "docs", "own-" + name, "bge-base-en-v15-768", "live");  // an owner of its own: no live tuple twin
        if (inferenceWhere == null) {
            insert.onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME).doNothing().execute();
        } else {
            insert.onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME).where(inferenceWhere).doNothing().execute();
        }
    }

    private static final org.jooq.Condition LABEL_LIVE = CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("");
    private static final org.jooq.Condition ID_LIVE = CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNull();

    @Test
    @Order(10)
    void aWhereThatImpliesOnlyOneOfTheTwoPredicates_findsNoArbiterOnTheOtherIndex() {
        String name = "docs__p35barb-ctl__bge-base-en-v15-768__v1";
        PgContainerHelper.insertCollection(ctx, T, name);
        // the p36 index: the label predicate proves it, the id predicate does not, and a name-only ON CONFLICT has no arbiter at all
        insertSiblingRowNamed(name + "x", LABEL_LIVE);
        assertThatThrownBy(() -> insertSiblingRowNamed(name, ID_LIVE)).as("superseded_by_id IS NULL does not imply superseded_by = ''")
            .isInstanceOf(DataAccessException.class).hasMessageContaining("no unique or exclusion constraint matching");
        assertThatThrownBy(() -> insertSiblingRowNamed(name, null)).as("no WHERE: a partial index is not inferred")
            .isInstanceOf(DataAccessException.class).hasMessageContaining("no unique or exclusion constraint matching");
        insertSiblingRowNamed(name, LABEL_LIVE.and(ID_LIVE));
    }

    @Test
    @Order(11)
    void p41Index_everyRegistryUpsertStillWorks() throws Exception {
        // P4.1 re-creates the live-label index over superseded_by_id IS NULL in the changeset that swaps the key
        PgContainerHelper.dropIndex(su, DSL.name("nexus", LIVE_LABEL_INDEX));
        ctx.createUniqueIndex(DSL.name(LIVE_LABEL_INDEX)).on(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
            .where(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNull()).execute();
        assertThat(uniqueIndexesOnTenantAndName()).as("the p41 index is the one arbiter").containsExactly(LIVE_LABEL_INDEX);

        String name = "docs__p35barb-ctl2__bge-base-en-v15-768__v1";
        PgContainerHelper.insertCollection(ctx, T, name);
        insertSiblingRowNamed(name + "x", ID_LIVE);
        assertThatThrownBy(() -> insertSiblingRowNamed(name, LABEL_LIVE)).as("superseded_by = '' does not imply superseded_by_id IS NULL")
            .isInstanceOf(DataAccessException.class).hasMessageContaining("no unique or exclusion constraint matching");
        insertSiblingRowNamed(name, LABEL_LIVE.and(ID_LIVE));

        for (Fn fn : Fn.values()) {
            if (fn.isMove()) {
                registersTheSibling(fn, "p41");
                updatesTheSibling(fn, "p41");
            } else {
                restoresIntoTheOrigin(fn, "p41");
            }
        }
    }
}
