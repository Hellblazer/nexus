// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21): the swap's two foreign-key gates under row-level security, in the role and session the move finisher
 * runs {@code on_complete} in.
 *
 * <p><b>The session.</b> {@code VerifyingFinisher#complete} runs the swap's and each validation move's {@code on_complete} on
 * {@code context.admin()}: a session {@code AdminDdlSession.open} connects as {@code AdminConnection.user} (production: {@code nexus_admin},
 * the migration role, a NOSUPERUSER NOBYPASSRLS owner of every relation) with no {@code SET ROLE} and no {@code pg_background} (that is the
 * per-chunk CALL's, not the finisher's), and with {@code nexus.tenant} unset: the re-check sets it only {@code SET LOCAL} per tenant and
 * clears it, and {@code requireNoTenant} refuses a session that still carries one before {@code on_complete} runs. {@link #theFinisherSession}
 * reads the same facts from a session of the test's admin role (NOSUPERUSER, NOBYPASSRLS, the tables' owner, FORCE ROW LEVEL SECURITY on).
 *
 * <p><b>The claim under test.</b> A row planted under a tenant the session does not name is hidden from it by {@code tenant_isolation}, so if
 * PostgreSQL's foreign-key validation ran under row security it would see no orphan and pass. It does not: {@code ALTER TABLE ... ADD
 * FOREIGN KEY} and {@code VALIDATE CONSTRAINT} fall back from the set-based check to a row-by-row one when RLS would apply
 * ({@code RI_Initial_Check}), and the checks themselves run as the owner with row security off. The swap's composite from nexus.chunks to the
 * registry must refuse a chunk with no registry row, and each child validation move must refuse a row whose id key has no chunk.
 * Rows are planted as the superuser (which bypasses RLS) under tenants of their own.
 *
 * <p>Two classes run the cases: {@link Rdr229SwapMovesRowSecurityOwnerReadIntegrationTest} on the schema as the changelog builds it (the driving
 * tables carry a permissive owner-read SELECT policy for the migration role, RDR-246, so the session sees the planted rows), and
 * {@link Rdr229SwapMovesTenantIsolationOnlyIntegrationTest} with those policies dropped, so the rows are hidden and the move is blocked before it
 * reaches its foreign-key step.
 */
abstract class Rdr229SwapMovesRowSecurityBase extends Rdr229SwapMovesFixtures {

    /** An id no registry row carries. */
    static final long NO_SUCH_COLLECTION_ID = 987_654_321_012L;

    /** What the finisher's session is, read from a session of the same role: user, whether it escapes RLS, and the tenant it names. */
    record Session(String user, boolean superuser, boolean bypassRls, String tenant, boolean owner, boolean forced) { }

    Session theFinisherSession() throws Exception {
        try (Connection c = adminPool.getConnection()) {
            var d = DSL.using(c, SQLDialect.POSTGRES);
            Field<String> rolname = DSL.field(DSL.name("rolname"), String.class);
            var role = d.select(rolname, DSL.field(DSL.name("rolsuper"), Boolean.class), DSL.field(DSL.name("rolbypassrls"), Boolean.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_roles"))).where(rolname.eq(DSL.currentUser())).fetchOne();
            String tenant = d.select(DSL.function("current_setting", String.class, DSL.inline("nexus.tenant"), DSL.inline(true))).fetchOne(0, String.class);
            var rel = d.select(DSL.field(DSL.name("r", "rolname"), String.class), DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .join(DSL.table(DSL.name("pg_catalog", "pg_roles")).as("r")).on(DSL.field(DSL.name("r", "oid")).eq(DSL.field(DSL.name("c", "relowner"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus")).and(DSL.field(DSL.name("c", "relname"), String.class).eq("chunks"))
                .fetchOne();
            return new Session(role.value1(), role.value2(), role.value3(), tenant, rel.value1().equals(role.value1()), rel.value2());
        }
    }

    void assertTheSessionIsTheProductionShape() throws Exception {
        Session s = theFinisherSession();
        assertThat(s.superuser()).as("the finisher's role is not a superuser").isFalse();
        assertThat(s.bypassRls()).as("and does not bypass row-level security (nexus_admin is NOBYPASSRLS)").isFalse();
        assertThat(s.owner()).as("it owns the tables").isTrue();
        assertThat(s.forced()).as("which are FORCE ROW LEVEL SECURITY, so ownership does not exempt it").isTrue();
        assertThat(s.tenant()).as("and it names no tenant").isIn(null, "");
        assertThat(s.user()).isEqualTo(ADMIN);
    }

    /** How many rows of {@code table} of {@code tenant} the finisher's session can see. */
    long visibleToTheFinisher(String table, String tenant) throws Exception {
        try (Connection c = adminPool.getConnection()) {
            return DSL.using(c, SQLDialect.POSTGRES).fetchCount(table("nexus", table), TENANT_F.eq(tenant));
        }
    }

    boolean validated(String child) {
        return idTwins(child).get(0).validated();
    }


    /** A chunk whose (tenant_id, collection_id, embedding_model) has no registry row, under {@code tenant}; the id is another registry's nothing. */
    void plantOrphanChunk(String tenant) {
        String collection = StampMoveSeeds.collection(tenant, 0);
        PgContainerHelper.insertCollection(ctx(), tenant, collection, BGE);
        String hash = "cd" + "0".repeat(62);
        PgContainerHelper.insertChunks(ctx(), tenant, collection, List.of(hash), List.of("orphan"), List.of(new float[768]), List.of(Map.of()));
        ctx().update(CHUNKS).set(CHUNKS.COLLECTION_ID, NO_SUCH_COLLECTION_ID).where(CHUNKS.TENANT_ID.eq(tenant)).execute();
        assertThat(ctx().fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.eq(NO_SUCH_COLLECTION_ID))))
            .as("non-vacuity: the orphan chunk is there").isEqualTo(1);
    }

    /** Cycles the production worker until each child's validation move is completed or failed, or eight cycles have run. */
    void runValidationMoves() {
        var w = productionWorker(new VerifyingFinisher());
        for (int i = 0; i < 8 && !CHILD_ID_COLUMN.keySet().stream().allMatch(c -> swapSettled(Rdr229SwapMove.validationLabel(c))); i++) {
            w.runCycle();
        }
    }

    /** One orphan row of {@code child}: a second chunk under the collection, and a row of the child naming that chunk's key under another collection's id. */
    void plantChildOrphan(String child, String tenant) throws Exception {
        String name = StampMoveSeeds.collection(tenant, 0);
        String otherName = name.replace("__v1", "r__v1");
        PgContainerHelper.insertCollection(ctx(), tenant, otherName, BGE);
        long otherId = StampMoveSeeds.id(ctx(), tenant, otherName);
        String hash = "ab" + "0".repeat(62);
        byte[] chash = HexFormat.of().parseHex(hash);
        PgContainerHelper.insertChunks(ctx(), tenant, name, List.of(hash), List.of("child orphan"), List.of(new float[768]), List.of(Map.of()));
        switch (child) {
            case "catalog_document_chunks" -> ctx().insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, "1.9.0")
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 77).set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
                .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, 77).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, name)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, otherId).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, BGE).execute();
            case "chunk_orphaned_at" -> ctx().insertInto(table("nexus", "chunk_orphaned_at"))
                .set(TENANT_F, tenant).set(DSL.field(DSL.name("collection"), String.class), name)
                .set(DSL.field(DSL.name("collection_id"), Long.class), otherId).set(DSL.field(DSL.name("chash"), byte[].class), chash)
                .set(DSL.field(DSL.name("orphaned_at"), OffsetDateTime.class), OffsetDateTime.now())
                .set(DSL.field(DSL.name("embedding_model"), String.class), BGE).execute();
            case "topic_assignments" -> {
                Long topic = ctx().select(DSL.field(DSL.name("id"), Long.class)).from(table("nexus", "topics")).where(TENANT_F.eq(tenant)).limit(1)
                    .fetchOne(0, Long.class);
                ctx().insertInto(table("nexus", "topic_assignments"))
                    .set(TENANT_F, tenant).set(DSL.field(DSL.name("doc_id"), byte[].class), chash)
                    .set(DSL.field(DSL.name("topic_id"), Long.class), topic)
                    .set(DSL.field(DSL.name("source_collection"), String.class), name)
                    .set(DSL.field(DSL.name("source_collection_id"), Long.class), otherId)
                    .set(DSL.field(DSL.name("embedding_model"), String.class), BGE).execute();
            }
            default -> throw new IllegalArgumentException(child);
        }
        assertThat(ctx().fetchCount(table("nexus", child), TENANT_F.eq(tenant))).as("non-vacuity: " + child + " holds its rows (superuser)").isGreaterThanOrEqualTo(2);
    }
}
