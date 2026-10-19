package dev.nexus.service;

import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Table;
import org.jooq.types.DayToSecond;
import org.jooq.types.YearToMonth;
import org.jooq.types.YearToSecond;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.SchemaMigrator.MigrationException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Routines;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.exception.LiquibaseException;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.*;

import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.SQLException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_1024;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_384;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_768;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_CONFORMANCE_REPORT;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.COLLECTION_VECTOR_STATS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_TEXT;
import static dev.nexus.service.jooq.nexus.Tables.GC_EXPIRE_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.LIVE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_VERIFY;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-191 Phase 4 repoint batch, Step A (bead nexus-o8dil.18) —
 * {@code vectors-005-repoint-functions-views.xml}. Chains THREE staged
 * changesets in sequence (mirrors {@link VectorsUnifyChunksIntegrationTest}
 * and {@link VectorsUnifyCentroidsIntegrationTest}'s own isolation idiom,
 * extended to a three-file chain): real unmodified master to head, then
 * {@code vectors-004-unify-chunks.xml}, then
 * {@code taxonomy-007-unify-centroids.xml}, then THIS changeset — all as
 * standalone {@link Liquibase} applications pointed directly at their
 * staged classpath paths. Liquibase tracks {@code DATABASECHANGELOG} rows by
 * {@code (id, author, filename)}, not by which changelog reached them, so
 * three sequential {@code update()} calls produce byte-identical end state
 * to what a real {@code <include>} chain would.
 *
 * <p><strong>R8 discipline (plan of record [22445]):</strong> a green
 * {@code CREATE OR REPLACE FUNCTION} proves nothing for a plpgsql body —
 * PostgreSQL does not validate references inside a plpgsql body at CREATE
 * time, only at first CALL. Every one of the 24 objects this changeset
 * touches (2 views + 9 typed combined-query facades + document_text +
 * remap_membership + manifest_verify + manifest_verify_all +
 * manifest_orphans + chash_conformance_report + gc_quarantine_orphans +
 * gc_restore_rereferenced + gc_expire_quarantine + purge_trash +
 * assign_from_chashes_384/768/1024) is therefore CALLED at least once
 * below, not merely proven to compile.
 *
 * <p><strong>RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15:</strong> {@code
 * manifest_verify_all} and {@code manifest_orphans} coverage below was
 * DELETED — both functions are DROPPED (catalog-030-retire-manifest-
 * verify.xml), which {@link SchemaMigrator#migrate} applies to HEAD before
 * {@link #applyFullBatch} re-chains vectors-004/taxonomy-007/vectors-005 as
 * a (now-no-op, already-recorded) standalone sequence — so by the time this
 * file's assertions run, both functions are already gone from the fixture
 * database. {@code manifest_verify} coverage is KEPT — that function is
 * NOT dropped (completeIndexRun depends on it).
 *
 * <p><strong>Statement-level coverage (2026-08-13 round 2, substantive-critic
 * finding, T2 nexus/rdr-191-repoint-batch-step-a-critique-2026-08-13
 * [22454]):</strong> a CALLED function is not the same as an EXECUTED
 * statement -- {@code gcWritersAndPurgeTrash_eachCallableAgainstSeededData}
 * above always seeds a manifest-referenced fixture, so every gc_* call there
 * trips its early-return guard before reaching its retargeted
 * INSERT/ON CONFLICT/DELETE. {@code gcWriters_actuallyExecuteRetargetedStatements_
 * postStateAsserted} seeds a genuinely orphaned chunk so
 * gc_quarantine_orphans, gc_restore_rereferenced, and gc_expire_quarantine's
 * retargeted statements ACTUALLY RUN, with post-state row assertions rather
 * than must-not-throw alone.
 *
 * <p><strong>Cross-dim collision guard (Critical-1 fix, same T2 entry):</strong>
 * {@code gcQuarantineOrphans_crossDimCollisionAtQuarantineTarget_
 * raisesAndPreservesBothRows} and {@code gcRestoreRereferenced_
 * crossDimCollisionAtOriginTarget_raisesAndPreservesBothRows} construct a
 * real (tenant, collection, chash) collision across two different populated
 * embedding_&lt;dim&gt; columns and assert the RAISE fires with both rows
 * surviving untouched -- proven to fail against the pre-fix body (no guard
 * existed; the ON CONFLICT ... EXCLUDED copy silently overwrote the
 * pre-existing row instead).
 *
 * <p><strong>Tombstone-relay guard (Critical-3 fix, T2 nexus/review-rdr191-
 * stepA-critical-graphhop-tombstone-regression-2026-08-13 [22455]):</strong>
 * {@code searchGraphHop_tombstonedRelay_blocksReachabilityBeyondIt} proves
 * the recursive traversal itself gates on deleted_at (not just the final
 * row) -- proven to fail against the pre-fix body, which silently dropped
 * catalog-019's fd/td JOINs during the retarget.
 */
class VectorsRepointFunctionsIntegrationTest {

    private static final String VECTORS_004 = "db/changelog/vectors-004-unify-chunks.xml";
    private static final String TAXONOMY_007 = "db/changelog/taxonomy-007-unify-centroids.xml";
    private static final String VECTORS_005 = "db/changelog/vectors-005-repoint-functions-views.xml";

    private static final String TENANT = "t1";
    private static final java.time.OffsetDateTime SEED_TS = java.time.OffsetDateTime.parse("2026-01-01T00:00:00Z");

    // ── Shared aged-box scaffold (mirrors the sibling unify tests) ──────────

    private record Rig(PostgreSQLContainer<?> pg, com.zaxxer.hikari.HikariDataSource adminDs) {
        void close() {
            adminDs.close();
            pg.stop();
        }
    }

    private static Rig newRig(String label) throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        String role = "nexus_admin_" + label;
        String pass = role + "_pass";
        // Phase A: the DBA-equivalent bootstrap (migrating role, nexus_svc, CREATE grants,
        // pg_monitor ADMIN OPTION, extensions, relocation helpers) runs as the db.changelog-test-
        // nonsuper-owner.xml test changelog, as the superuser, before the NOSUPERUSER walk.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, role, pass);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(role);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-" + label);
        return new Rig(pg, new com.zaxxer.hikari.HikariDataSource(cfg));
    }

    private static void applyChangelog(com.zaxxer.hikari.HikariDataSource ds, String path) {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(path, new ClassLoaderResourceAccessor(), database)) {
                liquibase.update(new Contexts(), new LabelExpression());
            }
        } catch (SQLException e) {
            throw new MigrationException("Failed to obtain DB connection for migration", e);
        } catch (LiquibaseException e) {
            throw new MigrationException("Liquibase migration failed: " + path, e);
        }
    }

    /** Applies the full three-file repoint-batch chain in registration order. */
    private static void applyFullBatch(com.zaxxer.hikari.HikariDataSource ds) {
        applyChangelog(ds, VECTORS_004);
        applyChangelog(ds, TAXONOMY_007);
        applyChangelog(ds, VECTORS_005);
    }

    private static byte[] sha256(String s) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
    }

    /** A {@code dim}-wide pgvector value with every component {@code fill}. */
    private static Vector vec(int dim, double fill) {
        float[] f = new float[dim];
        Arrays.fill(f, (float) fill);
        return Vector.of(f);
    }

    /**
     * Seeds one {@code nexus.chunks} row under {@code collection}'s registered model, in the
     * {@code embedding_<dim>} column the width picks. {@code metadataJson} is left unset (the column
     * default applies) when null.
     */
    private static void insertChunk(DSLContext ctx, String collection, byte[] chash, String text, int dim,
                                    double fill, String metadataJson) {
        Field<Vector> col = switch (dim) {
            case 384 -> CHUNKS.EMBEDDING_384;
            case 768 -> CHUNKS.EMBEDDING_768;
            default -> CHUNKS.EMBEDDING_1024;
        };
        var ins = ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, TENANT)
            .set(CHUNKS.COLLECTION, collection)
            .set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, TENANT, collection))
            .set(CHUNKS.CHUNK_TEXT, text)
            .set(col, vec(dim, fill));
        if (metadataJson != null) {
            ins = ins.set(CHUNKS.METADATA, JSONB.jsonb(metadataJson));
        }
        ins.execute();
    }

    /** One {@code nexus.catalog_documents} row for {@code collection}. */
    private static void insertDocument(DSLContext ctx, String tumbler, String title, String collection) {
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.CORPUS,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
            .values(TENANT, tumbler, title, "code", "code", collection)
            .execute();
    }

    /** One manifest row ({@code catalog_document_chunks}) tying {@code chash} to {@code doc} at {@code position}. */
    private static void insertManifestRow(DSLContext ctx, String doc, int position, byte[] chash,
                                          String collection) {
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
            .values(TENANT, doc, position, chash, collection,
                PgContainerHelper.collectionModel(ctx, TENANT, collection))
            .execute();
    }

    private static void insertCitesLink(DSLContext ctx, String from, String to) {
        ctx.insertInto(CATALOG_LINKS, CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER,
                CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY)
            .values(TENANT, from, to, "cites", "test")
            .execute();
    }

    // ── Fixture: one document + one chunk + one manifest row + one centroid
    //    + one topic/topic_assignment + one catalog_link, all dim=384,
    //    seeded via the SUPERUSER connection (bypasses FORCE RLS entirely,
    //    matching the sibling tests' seedChunk/seedCentroid pattern). ─────────

    private record Fixture(byte[] chash, String chashHex, String collection, String docTumbler) {}

    /** RDR-225: the read functions take the model and the tenant; the fixture collection carries this model. */
    private static final String MODEL_384 = "minilm-l6-v2-384";

    private static Fixture seedFixture(PostgreSQLContainer<?> pg) throws Exception {
        byte[] chash = sha256("vectors-005 repoint fixture chunk text");
        String chashHex = bytesToHex(chash);
        String collection = "code__demo__minilm-l6-v2-384__v1";
        String docTumbler = "1.1";

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            // nexus.topics carries an FK to catalog_collections (topics_collection_fk)
            // that neither nexus.chunks nor nexus.taxonomy_centroids requires
            // (vectors-004/taxonomy-007 headers: no collection FK on either unified
            // table). Register the collection first so the topics insert below succeeds.
            // RDR-204 nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection,
            // which derives the constraint-satisfying attributes hygiene-002-1 now
            // requires (the bare two-column raw INSERT this used to run 23502s on
            // lifecycle_state NOT NULL).
            PgContainerHelper.insertCollection(ctx, TENANT, collection);
            final String model = modelOf(su, collection);

            insertDocument(ctx, docTumbler, "Repoint fixture doc", collection);

            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires the
            // nexus.chunks row to exist before the manifest row referencing it is
            // inserted -- chunk insert MUST precede the manifest insert below.
            insertChunk(ctx, collection, chash, "vectors-005 repoint fixture chunk text", 384, 0.01, null);

            insertManifestRow(ctx, docTumbler, 0, chash, collection);

            ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                    TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL,
                    TAXONOMY_CENTROIDS.EMBEDDING_384, TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT)
                .values(TENANT, collection, 1L, model, vec(384, 0.02), "alpha", 1)
                .execute();

            ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                    TOPICS.CREATED_AT)
                .values(TENANT, "alpha", collection, 1, SEED_TS)
                .execute();

            // RDR-194 P3c (nexus-tk070.p3c): doc_id is bytea now. The generated field binds the digest's
            // raw bytes; the raw-SQL form of this seed had to decode('hex') explicitly because a bare
            // '<hex-string>' literal against a bytea column is NOT a type error (Postgres accepts it as
            // legacy "escape format" input and silently reinterprets the hex characters as their own
            // ASCII bytes), which would have made every search_topic_scoped_<dim> assertion below fail to
            // find the seeded row for the wrong reason.
            Long alphaTopicId = ctx.select(TOPICS.ID).from(TOPICS)
                .where(TOPICS.TENANT_ID.eq(TENANT)).and(TOPICS.LABEL.eq("alpha"))
                .fetchOne(TOPICS.ID);
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
                .values(TENANT, chash, alphaTopicId, "centroid", collection, model)
                .execute();

            insertCitesLink(ctx, docTumbler, docTumbler);
        }
        return new Fixture(chash, chashHex, collection, docTumbler);
    }

    /** RDR-225: the embedding model {@code collection} is registered under (a chunk and every table that
     *  references it carries it). */
    private static String modelOf(Connection c, String collection) {
        return PgContainerHelper.collectionModel(DSL.using(c, SQLDialect.POSTGRES), TENANT, collection);
    }

    private static String bytesToHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private static void setTenantGuc(Connection conn, String tenant) {
        PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
    }

    // ── Test 1: the chain applies cleanly end to end (structural smoke) ─────

    @Test
    void chainedBatch_appliesCleanlyOverFreshInstall() throws Exception {
        Rig rig = newRig("chain");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            assertThatCode(() -> applyFullBatch(rig.adminDs()))
                .as("vectors-004 -> taxonomy-007 -> vectors-005 must chain cleanly over a fresh install")
                .doesNotThrowAnyException();

            try (Connection conn = rig.pg().createConnection("")) {
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                for (String obj : new String[] {
                        "live_chunks", "collection_vector_stats"}) {
                    assertThat(PgCatalogProbes.viewExists(ctx, "nexus", obj))
                        .as("view nexus.%s must exist", obj).isTrue();
                }
                for (String fn : new String[] {
                        "search_metadata_scoped_384", "search_metadata_scoped_768", "search_metadata_scoped_1024",
                        "search_topic_scoped_384", "search_topic_scoped_768", "search_topic_scoped_1024",
                        "search_graph_hop_384", "search_graph_hop_768", "search_graph_hop_1024",
                        // manifest_verify_all/manifest_orphans REMOVED here (RDR-191
                        // Phase 6, nexus-o8dil.33) — both dropped by catalog-030,
                        // already applied to HEAD by SchemaMigrator.migrate above.
                        // remap_membership REMOVED here (nexus-lgdel.l2) — dropped by
                        // legacy-002-drop-remap-membership.xml, same reason.
                        "document_text", "manifest_verify",
                        "chash_conformance_report",
                        "gc_quarantine_orphans", "gc_restore_rereferenced", "gc_expire_quarantine",
                        "purge_trash",
                        "assign_from_chashes_384", "assign_from_chashes_768", "assign_from_chashes_1024"}) {
                    assertThat(PgCatalogProbes.routineExists(ctx, "nexus", fn))
                        .as("function nexus.%s must exist", fn).isTrue();
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 2: the nine typed combined-query facades, called for real ──────

    @Test
    void nineTypedFacades_eachCallableAgainstSeededData() throws Exception {
        Rig rig = newRig("facades");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                String[] colls = {fx.collection()};
                String[] seeds = {fx.docTumbler()};

                // search_metadata_scoped_384: must find the seeded chunk.
                var meta384 = SEARCH_METADATA_SCOPED_384.call(
                    vec(384, 0.01), colls, null, null, null, null, null, null, 10, MODEL_384, TENANT);
                var metaIds = ctx.select(meta384.ID).from(meta384).fetch(meta384.ID);
                assertThat(metaIds).as("search_metadata_scoped_384 must find the seeded chunk").isNotEmpty();
                assertThat(metaIds.get(0)).isEqualTo(fx.docTumbler());
                // 768/1024: not seeded under those dims, so 0 rows is the correct
                // (not-erroring) answer -- proves the FROM/embedding_<dim> rewrite
                // does not throw on an empty match, not just on a hit.
                for (int dim : new int[] {768, 1024}) {
                    Table<?> fn = metadataFn(dim, colls);
                    assertThatCode(() -> ctx.selectFrom(fn).fetch())
                        .as("search_metadata_scoped_%d must not throw on an empty-match query", dim)
                        .doesNotThrowAnyException();
                }

                // search_topic_scoped_384: must find the seeded chunk via topic_assignments.
                var topic384 = SEARCH_TOPIC_SCOPED_384.call(
                    vec(384, 0.01), "alpha", fx.collection(), 10, MODEL_384, TENANT);
                assertThat(ctx.select(topic384.ID).from(topic384).fetch(topic384.ID))
                    .as("search_topic_scoped_384 must find the seeded chunk").isNotEmpty();
                for (int dim : new int[] {768, 1024}) {
                    Table<?> fn = topicFn(dim, fx.collection());
                    assertThatCode(() -> ctx.selectFrom(fn).fetch())
                        .as("search_topic_scoped_%d must not throw", dim)
                        .doesNotThrowAnyException();
                }

                // search_graph_hop_384: seed is its own link target (self-loop
                // 'cites'), depth 1, direction both, from itself as the seed.
                var hop384 = SEARCH_GRAPH_HOP_384.call(
                    vec(384, 0.01), seeds, colls, null, 1, "both", null, 10, MODEL_384, TENANT);
                assertThat(ctx.select(hop384.ID).from(hop384).fetch(hop384.ID))
                    .as("search_graph_hop_384 must find the seeded chunk via the self-loop link").isNotEmpty();
                for (int dim : new int[] {768, 1024}) {
                    Table<?> fn = graphHopFn(dim, seeds, colls, null, 1, "both");
                    assertThatCode(() -> ctx.selectFrom(fn).fetch())
                        .as("search_graph_hop_%d must not throw", dim)
                        .doesNotThrowAnyException();
                }
            }
        } finally {
            rig.close();
        }
    }

    private static Table<?> metadataFn(int dim, String[] colls) {
        Vector q = vec(dim, 0.01);
        return switch (dim) {
            case 384 -> SEARCH_METADATA_SCOPED_384.call(q, colls, null, null, null, null, null, null, 10, MODEL_384, TENANT);
            case 768 -> SEARCH_METADATA_SCOPED_768.call(q, colls, null, null, null, null, null, null, 10, MODEL_384, TENANT);
            default -> SEARCH_METADATA_SCOPED_1024.call(q, colls, null, null, null, null, null, null, 10, MODEL_384, TENANT);
        };
    }

    private static Table<?> topicFn(int dim, String collection) {
        Vector q = vec(dim, 0.01);
        return switch (dim) {
            case 384 -> SEARCH_TOPIC_SCOPED_384.call(q, "alpha", collection, 10, MODEL_384, TENANT);
            case 768 -> SEARCH_TOPIC_SCOPED_768.call(q, "alpha", collection, 10, MODEL_384, TENANT);
            default -> SEARCH_TOPIC_SCOPED_1024.call(q, "alpha", collection, 10, MODEL_384, TENANT);
        };
    }

    private static Table<?> graphHopFn(int dim, String[] seeds, String[] colls, String linkType, int depth,
                                       String direction) {
        Vector q = vec(dim, 0.01);
        return switch (dim) {
            case 384 -> SEARCH_GRAPH_HOP_384.call(q, seeds, colls, linkType, depth, direction, null, 10, MODEL_384, TENANT);
            case 768 -> SEARCH_GRAPH_HOP_768.call(q, seeds, colls, linkType, depth, direction, null, 10, MODEL_384, TENANT);
            default -> SEARCH_GRAPH_HOP_1024.call(q, seeds, colls, linkType, depth, direction, null, 10, MODEL_384, TENANT);
        };
    }

    // ── Test 2b: the combined-query functions now number TWELVE ─────────────
    //
    // RDR-191 Phase 6 (nexus-o8dil.33) mechanical-exclusion pin, Decision
    // item 4 / F7 risk 4: "the 9 combined-query stored functions do not
    // disappear... write a TEST asserting they still number 9." Distinct
    // from nineTypedFacades_eachCallableAgainstSeededData above (which calls
    // each by NAME and would not itself notice a 10th appearing or one of
    // the nine vanishing) — this counts pg_proc rows matching the naming
    // pattern directly, on the SAME post-catalog-030 HEAD schema (Class B/
    // manifest_orphans/manifest_verify_all/manifest_backfill are dropped by
    // that changeset; these nine are explicitly NOT).
    //
    // RDR-156 Decision 5 (bead nexus-ubnwk) added a FOURTH combined-query
    // shape, search_aspect_scoped_<dim> (vectors-008-aspect-scoped.xml) — the
    // census grows from 9 to 12 (4 verbs x 3 dims), not a re-baseline: the
    // original nine are unchanged, three new facades join them.

    @Test
    void combinedQueryFunctions_stillNumberTwelve() throws Exception {
        Rig rig = newRig("twelvecount");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());

            try (Connection conn = rig.pg().createConnection("")) {
                {
                    var names = PgCatalogProbes.routineNamesLike(
                        DSL.using(conn, SQLDialect.POSTGRES), "nexus",
                        "search_metadata_scoped_%", "search_topic_scoped_%",
                        "search_graph_hop_%", "search_aspect_scoped_%");
                    assertThat(names)
                        .as("the 12 combined-query facades (4 verbs x 3 dims) must "
                            + "number 12 post-RDR-156-Decision-5 — the original 9 "
                            + "(F7 risk 4) plus the 3 new search_aspect_scoped_<dim> "
                            + "facades (nexus-ubnwk); only "
                            + "manifest_orphans/manifest_verify_all/"
                            + "manifest_backfill are dropped by catalog-030: %s",
                            names)
                        .hasSize(12)
                        .containsExactlyInAnyOrder(
                            "search_metadata_scoped_384", "search_metadata_scoped_768",
                            "search_metadata_scoped_1024",
                            "search_topic_scoped_384", "search_topic_scoped_768",
                            "search_topic_scoped_1024",
                            "search_graph_hop_384", "search_graph_hop_768",
                            "search_graph_hop_1024",
                            "search_aspect_scoped_384", "search_aspect_scoped_768",
                            "search_aspect_scoped_1024");
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 3: document_text / manifest_verify /
    //    chash_conformance_report -- the "dim collapses to one reference"
    //    and "dim stays branched for routing" buckets. (manifest_orphans
    //    coverage DELETED, RDR-191 Phase 6 nexus-o8dil.33 — function
    //    dropped. remap_membership coverage DELETED, nexus-lgdel.l2 — function
    //    dropped, legacy-002-drop-remap-membership.xml.) ─────────────────

    @Test
    void collapsedAndBranchedReaders_eachCallableAgainstSeededData() throws Exception {
        Rig rig = newRig("readers");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);

                // document_text: ordered manifest join reconstruction.
                var docText = DOCUMENT_TEXT.call(fx.docTumbler());
                var docTextRows = ctx.select(docText.CHUNK_TEXT).from(docText).fetch(docText.CHUNK_TEXT);
                assertThat(docTextRows).as("document_text must resolve the seeded manifest row").isNotEmpty();
                assertThat(docTextRows.get(0)).isEqualTo("vectors-005 repoint fixture chunk text");

                // manifest_verify: referenced=1, present=1, missing=0.
                var verify = MANIFEST_VERIFY.call(fx.docTumbler());
                var verifyRow = ctx.select(verify.REFERENCED, verify.PRESENT, verify.MISSING)
                    .from(verify).fetchOne();
                assertThat(verifyRow).isNotNull();
                assertThat(verifyRow.value1()).isEqualTo(1L);
                assertThat(verifyRow.value2()).isEqualTo(1L);
                assertThat(verifyRow.value3()).isEqualTo(0L);

                // manifest_verify_all coverage DELETED (RDR-191 Phase 6,
                // nexus-o8dil.33) — the function is dropped.

                // remap_membership coverage DELETED (nexus-lgdel.l2) — the
                // function is dropped (legacy-002-drop-remap-membership.xml);
                // the orphaned GET /v1/remap/membership read surface is gone
                // with it.

                // manifest_orphans coverage DELETED (RDR-191 Phase 6,
                // nexus-o8dil.33) — the function is dropped.

                // chash_conformance_report(384): the seeded chunk's chash is a
                // real 32-byte sha256 digest, so non_conformant must be 0 for the
                // nexus.chunks row and total >= 1.
                var report384 = CHASH_CONFORMANCE_REPORT.call(384);
                boolean sawChunksRow = false;
                for (var row : ctx.select(report384.TABLE_NAME, report384.TOTAL, report384.NON_CONFORMANT)
                        .from(report384).fetch()) {
                    if (row.value1().startsWith("nexus.chunks")) {
                        sawChunksRow = true;
                        assertThat(row.value2()).isGreaterThanOrEqualTo(1L);
                        assertThat(row.value3()).isEqualTo(0L);
                    }
                }
                assertThat(sawChunksRow).isTrue();
                for (int dim : new int[] {768, 1024}) {
                    var report = CHASH_CONFORMANCE_REPORT.call(dim);
                    assertThatCode(() -> ctx.selectFrom(report).fetch())
                        .as("chash_conformance_report(%d) must not throw", dim)
                        .doesNotThrowAnyException();
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 4: the GC writers + purge_trash -- dim-agnostic collapse,
    //    called for real against the unified table. ─────────────────────────

    @Test
    void gcWritersAndPurgeTrash_eachCallableAgainstSeededData() throws Exception {
        Rig rig = newRig("gc");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);

                // gc_quarantine_orphans: the seeded chunk IS referenced by a
                // manifest row, so nothing should move (moved=0) -- proves the
                // dim-agnostic NOT EXISTS rewrite against nexus.chunks runs
                // clean, not that it moves anything.
                var quarantine = GC_QUARANTINE_ORPHANS.call(384, TENANT, fx.collection(),
                    "quarantine__demo__minilm-l6-v2-384__v1", "2026-01-01T00:00:00Z", 20);
                assertThat(ctx.select(quarantine.MOVED).from(quarantine).fetchOne(quarantine.MOVED))
                    .isEqualTo(0L);
                for (int dim : new int[] {768, 1024}) {
                    var nonexistent = GC_QUARANTINE_ORPHANS.call(dim, TENANT, "nonexistent-collection-" + dim,
                        "quarantine-" + dim, "2026-01-01T00:00:00Z", 20);
                    assertThatCode(() -> ctx.selectFrom(nonexistent).fetch())
                        .as("gc_quarantine_orphans(%d) must not throw", dim)
                        .doesNotThrowAnyException();
                }

                // gc_restore_rereferenced: no quarantined rows exist yet -> 0.
                assertThat(Routines.gcRestoreRereferenced(ctx.configuration(), 384, TENANT,
                        "quarantine__demo__minilm-l6-v2-384__v1", fx.collection()))
                    .isEqualTo(0L);

                // gc_expire_quarantine: nothing quarantined -> expired=0, refused=0.
                var expire = GC_EXPIRE_QUARANTINE.call(384, TENANT, "quarantine__demo__minilm-l6-v2-384__v1",
                    fx.collection(), "2099-01-01T00:00:00Z", 0.5, 100, false);
                var expireRow = ctx.select(expire.EXPIRED, expire.REFUSED).from(expire).fetchOne();
                assertThat(expireRow).isNotNull();
                assertThat(expireRow.value1()).isEqualTo(0L);
                assertThat(expireRow.value2()).isEqualTo(0L);

                // purge_trash: no tombstoned documents -> 0 rows purged, and the
                // manifest-referenced seeded chunk must SURVIVE the sweep.
                assertThat(Routines.purgeTrash(ctx.configuration(),
                        new YearToSecond(new YearToMonth(0, 0), new DayToSecond(0))))
                    .isEqualTo(0L);
                assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT)))
                    .as("purge_trash must not sweep a chunk still protected by a live manifest row")
                    .isEqualTo(1);
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 4b: the three gc_* functions' RETARGETED INSERT/ON CONFLICT/
    //    DELETE statements, ACTUALLY EXECUTED (Critical-2 fix, T2 [22454]):
    //    test 4 above only ever seeds a manifest-referenced fixture, so
    //    every gc_* call there trips its early-return guard before reaching
    //    the retargeted statements -- R8 claimed but not delivered for the
    //    highest-risk lines. This test seeds a genuinely ORPHANED chunk so
    //    gc_quarantine_orphans's copy-INSERT + DELETE run for real, then
    //    re-references it so gc_restore_rereferenced's copy-INSERT + DELETE
    //    run for real, then seeds an independent already-quarantined,
    //    unreferenced, past-cutoff row so gc_expire_quarantine's DELETE
    //    runs for real -- with post-state assertions on the underlying
    //    nexus.chunks rows, not merely "did not throw". ────────────────────

    @Test
    void gcWriters_actuallyExecuteRetargetedStatements_postStateAsserted() throws Exception {
        Rig rig = newRig("gcreal");
        try {
            // nexus_test.* (drop_constraint / add_fk_not_valid_composite3) for the FK bypass below,
            // installed through the migrating role's own connection BEFORE the walk (the
            // PgContainerHelper#installTestObjects ownership contract: the role whose Liquibase run
            // creates databasechangelog first owns it).
            try (Connection adminConn = rig.adminDs().getConnection()) {
                PgContainerHelper.installTestObjects(adminConn);
            }
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            String origin = fx.collection();
            String quarantineFlow = "quarantine__demo__minilm-l6-v2-384__v2";
            byte[] orphanChash = sha256("gc real-execution orphan chunk text");

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                // A genuinely orphaned chunk: present in nexus.chunks, NOT
                // referenced by any catalog_document_chunks row.
                insertChunk(DSL.using(su, SQLDialect.POSTGRES), origin, orphanChash,
                    "gc real-execution orphan chunk text", 384, 0.10, null);
            }
            // RDR-192 Step 8: gc_quarantine_orphans honours the 30 day grace window.
            ReapableFixtures.agePastGrace(rig.pg(), TENANT, origin);

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);

                // ── gc_quarantine_orphans: the orphan MUST move, the
                //    manifest-referenced fixture row MUST NOT. ────────────
                var quarantine = GC_QUARANTINE_ORPHANS.call(384, TENANT, origin, quarantineFlow,
                    "2020-01-01T00:00:00Z", 20);
                assertThat(ctx.select(quarantine.MOVED).from(quarantine).fetchOne(quarantine.MOVED))
                    .as("only the genuinely orphaned chunk moves; the manifest-referenced fixture stays")
                    .isEqualTo(1L);
                assertRowExists(conn, quarantineFlow, orphanChash,
                    "the orphan must actually be present in the quarantine collection after the copy-INSERT");
                assertRowAbsent(conn, origin, orphanChash,
                    "the orphan must actually be GONE from the origin collection after the DELETE");
                assertRowExists(conn, origin, fx.chash(),
                    "the manifest-referenced fixture chunk must survive untouched");

                // ── gc_restore_rereferenced: re-reference the orphan at
                //    origin, then it MUST come back. ─────────────────────
                //
                // RDR-191 Phase 5 (nexus-o8dil.29) / nexus-zlv38: at this exact
                // instant the orphan's nexus.chunks row sits ONLY in the
                // quarantine collection (moved by gc_quarantine_orphans above)
                // -- NOT at origin. fk_catalog_chunks_chunk now rejects a
                // manifest write that re-references a chash before
                // gc_restore_rereferenced has copied it back to origin; that
                // production-behavior gap is tracked by nexus-zlv38, out of
                // scope for this changeset. Bypass the FK locally for this one
                // insert so the test can keep proving gc_restore_rereferenced's
                // own copy-back + delete-from-quarantine behavior.
                ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("fk_catalog_chunks_chunk").execute();
                insertManifestRow(ctx, fx.docTumbler(), 1, orphanChash, origin);
                // Re-added NOT VALID (the existing rows are not re-checked); jOOQ's DDL DSL has no NOT VALID,
                // so this goes through nexus_test.add_fk_not_valid_composite3, whose model-aware body appends
                // embedding_model to both column lists: the original four-column key.
                PgContainerHelper.addFkNotValidComposite3(conn, CATALOG_DOCUMENT_CHUNKS, "fk_catalog_chunks_chunk",
                    "collection", "chash", CHUNKS, "collection", "chash",
                    "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE");
                assertThat(Routines.gcRestoreRereferenced(ctx.configuration(), 384, TENANT, quarantineFlow, origin))
                    .as("the re-referenced chash must be restored").isEqualTo(1L);
                assertRowExists(conn, origin, orphanChash,
                    "the restored chunk must actually be back in the origin collection after the copy-INSERT");
                assertRowAbsent(conn, quarantineFlow, orphanChash,
                    "the restored chunk must actually be GONE from the quarantine collection after the DELETE");

                // ── gc_expire_quarantine: an independent, already-
                //    quarantined, unreferenced, past-cutoff row MUST be
                //    hard-deleted. ────────────────────────────────────────
                byte[] expireChash = sha256("gc real-execution expire chunk text");
                // quarantineFlow was registered by gc_quarantine_orphans under the origin's model.
                insertChunk(ctx, quarantineFlow, expireChash, "gc real-execution expire chunk text", 384, 0.11,
                    "{\"quarantined_at\":\"2020-01-01T00:00:00Z\",\"origin_collection\":\"" + origin + "\"}");
                var expire = GC_EXPIRE_QUARANTINE.call(384, TENANT, quarantineFlow, origin,
                    "2025-01-01T00:00:00Z", 0.5, 100, false);
                var expireRow = ctx.select(expire.EXPIRED, expire.REFUSED).from(expire).fetchOne();
                assertThat(expireRow).isNotNull();
                assertThat(expireRow.value1()).isEqualTo(1L);
                assertThat(expireRow.value2()).isEqualTo(0L);
                assertRowAbsent(conn, quarantineFlow, expireChash,
                    "the expired row must actually be hard-deleted from nexus.chunks");
            }
        } finally {
            rig.close();
        }
    }

    private static void assertRowExists(Connection conn, String collection, byte[] chash, String message) {
        assertThat(chunkCount(conn, collection, chash)).as(message).isEqualTo(1);
    }

    private static void assertRowAbsent(Connection conn, String collection, byte[] chash, String message) {
        assertThat(chunkCount(conn, collection, chash)).as(message).isEqualTo(0);
    }

    private static int chunkCount(Connection conn, String collection, byte[] chash) {
        return DSL.using(conn, SQLDialect.POSTGRES).fetchCount(CHUNKS,
            CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.eq(chash)));
    }

    // ── Test 4c: cross-dim (tenant, collection, chash) collision guard
    //    (Critical-1 fix, T2 [22454]) -- the unified PK's shared namespace
    //    means an orphan sharing its chash with an already-quarantined row
    //    under a DIFFERENT dim must RAISE, never silently overwrite via the
    //    blind ON CONFLICT ... EXCLUDED pattern. Verified to FAIL against
    //    the pre-fix body during this fix's own development (the guard did
    //    not exist; the copy-INSERT silently clobbered the pre-existing
    //    row instead of raising). ────────────────────────────────────────

    @Test
    void gcQuarantineOrphans_crossDimCollisionAtQuarantineTarget_raisesAndPreservesBothRows() throws Exception {
        Rig rig = newRig("gccollisionq");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            byte[] collisionChash = sha256("cross-dim collision fixture chunk text");
            String quarantineShared = "quarantine__demo__shared__v1";

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
                // chunks_collection_fk (tenant_id, collection) -> catalog_collections
                // (tenant_id, name) — register the quarantine target first. RDR-204
                // nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection.
                DSLContext suCtx = DSL.using(su, SQLDialect.POSTGRES);
                PgContainerHelper.insertCollection(suCtx, TENANT, quarantineShared);
                // Pre-existing quarantined row under dim 768, at the SHARED
                // quarantine target, same chash. RDR-225: the target's name is not conformant, so it is
                // registered under the 768-wide fallback model (bge-base-en-v15-768) and the 768-wide
                // row is that model's own dimension; the origin below is a 384-wide (minilm) collection,
                // so the collision is between two collections of different models, still a
                // different populated embedding column at the same (tenant, collection, chash).
                insertChunk(suCtx, quarantineShared, collisionChash, "existing 768-dim quarantined chunk", 768,
                    0.05, "{\"quarantined_at\":\"2026-01-01T00:00:00Z\",\"origin_collection\":\"some-768-collection\"}");
                // A DIFFERENT, orphaned 384-dim chunk in fx.collection() that
                // happens to share the same chash -- the shared-quarantine-
                // naming gap the critique flagged (VectorHandler derives
                // quarantine_collection by convention, not server-enforced
                // per-dim distinctness).
                insertChunk(suCtx, fx.collection(), collisionChash, "colliding 384-dim orphan chunk", 384, 0.06,
                    null);
            }
            // RDR-192 Step 8: only an orphan older than the grace window reaches the collision guard.
            ReapableFixtures.agePastGrace(rig.pg(), TENANT, fx.collection());

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                var quarantine = GC_QUARANTINE_ORPHANS.call(384, TENANT, fx.collection(), quarantineShared,
                    "2026-01-01T00:00:00Z", 20);
                assertThatThrownBy(() -> ctx.selectFrom(quarantine).fetch())
                    .as("a cross-dim (tenant,collection,chash) collision at the quarantine target must "
                        + "RAISE, never silently overwrite the pre-existing row via blind "
                        + "ON CONFLICT ... EXCLUDED (Critical-1, T2 22454)")
                    .hasMessageContaining("cross-dim");

                // Post-state: the pre-existing 768-dim quarantined row is UNTOUCHED.
                var survivor = ctx.select(CHUNKS.CHUNK_TEXT, DSL.field(CHUNKS.EMBEDDING_384.isNull()),
                        DSL.field(CHUNKS.EMBEDDING_768.isNotNull()))
                    .from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(quarantineShared))
                    .and(CHUNKS.CHASH.eq(collisionChash))
                    .fetchOne();
                assertThat(survivor).as("the pre-existing quarantined row must still exist").isNotNull();
                assertThat(survivor.value1()).isEqualTo("existing 768-dim quarantined chunk");
                assertThat(survivor.value2()).as("embedding_384 must NOT have been set by the failed copy").isTrue();
                assertThat(survivor.value3()).as("embedding_768 must remain populated, untouched").isTrue();
                // And the origin orphan row survives too -- the RAISE aborts
                // the whole statement/transaction before the DELETE runs.
                assertRowExists(conn, fx.collection(), collisionChash,
                    "the origin orphan row must survive -- the guard prevents the whole move, not just the copy");
            }
        } finally {
            rig.close();
        }
    }

    @Test
    void gcRestoreRereferenced_crossDimCollisionAtOriginTarget_raisesAndPreservesBothRows() throws Exception {
        Rig rig = newRig("gccollisionr");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            byte[] collisionChash = sha256("restore cross-dim collision fixture chunk text");
            String quarantineCollection = "quarantine__demo__restore-collision__v1";

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
                // chunks_collection_fk (tenant_id, collection) -> catalog_collections
                // (tenant_id, name) — register the quarantine target first. RDR-204
                // nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection.
                DSLContext suCtx = DSL.using(su, SQLDialect.POSTGRES);
                PgContainerHelper.insertCollection(suCtx, TENANT, quarantineCollection);
                // Pre-existing row ALREADY at the origin collection, under a
                // DIFFERENT dim than the row being restored. RDR-225: the origin is a 384-wide (minilm)
                // collection, so its row is 384-wide, and the row to restore is the 768-wide one
                // (the quarantine target's name is not conformant, so it is registered under the 768-wide
                // fallback model): the same two different-dim rows for one chash, built from rows each
                // collection's own model accepts (the old 1024-wide origin row is refused by the
                // minilm partition's dimension CHECK).
                insertChunk(suCtx, fx.collection(), collisionChash, "existing 384-dim origin row", 384, 0.07, null);
                // The quarantined row to be restored, dim 768.
                insertChunk(suCtx, quarantineCollection, collisionChash, "quarantined 768-dim chunk to restore",
                    768, 0.08,
                    "{\"quarantined_at\":\"2026-01-01T00:00:00Z\",\"origin_collection\":\"" + fx.collection() + "\"}");
                // Manifest row re-referencing it at the origin -- makes it a
                // genuine restore candidate.
                insertManifestRow(suCtx, fx.docTumbler(), 2, collisionChash, fx.collection());
            }

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                // The field form goes through an ordinary select, so the failure surfaces as jOOQ's
                // DataAccessException carrying the server's RAISE text (the old PSQLException message).
                assertThatThrownBy(() -> ctx.select(Routines.gcRestoreRereferenced(384, TENANT,
                        quarantineCollection, fx.collection())).fetch())
                    .as("a cross-dim (tenant,collection,chash) collision at the restore target must "
                        + "RAISE, never silently overwrite the pre-existing row (Critical-1, T2 22454)")
                    .hasMessageContaining("cross-dim");

                // Post-state: the pre-existing 384-dim origin row is UNTOUCHED.
                var survivor = ctx.select(CHUNKS.CHUNK_TEXT, DSL.field(CHUNKS.EMBEDDING_768.isNull()),
                        DSL.field(CHUNKS.EMBEDDING_384.isNotNull()))
                    .from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(fx.collection()))
                    .and(CHUNKS.CHASH.eq(collisionChash))
                    .fetchOne();
                assertThat(survivor).as("the pre-existing origin row must still exist").isNotNull();
                assertThat(survivor.value1()).isEqualTo("existing 384-dim origin row");
                assertThat(survivor.value2()).as("embedding_768 must NOT have been set by the failed copy").isTrue();
                assertThat(survivor.value3()).as("embedding_384 must remain populated, untouched").isTrue();
                // And the quarantined row survives too -- the RAISE aborts
                // before the DELETE runs.
                assertRowExists(conn, quarantineCollection, collisionChash,
                    "the quarantined row must survive -- the guard prevents the whole move, not just the copy");
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 4d: search_graph_hop's tombstone-relay guard, carried forward
    //    from catalog-019 (Critical-3 fix, T2 [22455]) -- the recursive
    //    traversal itself must gate on deleted_at, not just the final row,
    //    or a tombstoned document can still act as a live relay forwarding
    //    reachability to documents beyond it. Verified to FAIL against the
    //    pre-fix body during this fix's own development (the fd/td JOINs
    //    were silently dropped in the retarget). ─────────────────────────

    @Test
    void searchGraphHop_tombstonedRelay_blocksReachabilityBeyondIt() throws Exception {
        Rig rig = newRig("graphhoptomb");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());

            String collection = "code__relay__minilm-l6-v2-384__v1";
            byte[] chashA = sha256("relay-a chunk text");
            byte[] chashB = sha256("relay-b chunk text");
            byte[] chashC = sha256("relay-c chunk text");

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
                // chunks_collection_fk (tenant_id, collection) -> catalog_collections
                // (tenant_id, name) — register the collection first. RDR-204
                // nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection.
                DSLContext suCtx = DSL.using(su, SQLDialect.POSTGRES);
                PgContainerHelper.insertCollection(suCtx, TENANT, collection);
                for (var doc : new Object[][] {{"relay-a", chashA}, {"relay-b", chashB}, {"relay-c", chashC}}) {
                    String tumbler = (String) doc[0];
                    byte[] chash = (byte[]) doc[1];
                    insertDocument(suCtx, tumbler, "Relay doc " + tumbler, collection);
                    // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk
                    // requires the nexus.chunks row before the manifest row below.
                    insertChunk(suCtx, collection, chash, tumbler + " chunk text", 384, 0.09, null);
                    insertManifestRow(suCtx, tumbler, 0, chash, collection);
                }
                for (var edge : new String[][] {{"relay-a", "relay-b"}, {"relay-b", "relay-c"}}) {
                    insertCitesLink(suCtx, edge[0], edge[1]);
                }
            }

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                assertThat(callGraphHop384(conn, collection, "relay-a", 2))
                    .as("baseline: relay-c reachable at depth 2 via live relay-b")
                    .containsExactlyInAnyOrder("relay-a", "relay-b", "relay-c");
            }

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.DELETED_AT, DSL.currentOffsetDateTime())
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq("relay-b"))
                    .execute();
            }

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                assertThat(callGraphHop384(conn, collection, "relay-a", 2))
                    .as("relay-c is reachable ONLY via the tombstoned relay-b -- the traversal itself "
                        + "must gate on deleted_at, not just the final row (catalog-019 regression, T2 22455)")
                    .containsExactly("relay-a");
            }
        } finally {
            rig.close();
        }
    }

    private static java.util.List<String> callGraphHop384(Connection conn, String collection, String seed, int depth) {
        var hop = SEARCH_GRAPH_HOP_384.call(vec(384, 0.09), new String[] {seed}, new String[] {collection},
            "cites", depth, "out", null, 10, MODEL_384, TENANT);
        return DSL.using(conn, SQLDialect.POSTGRES).select(hop.ID).from(hop).fetch(hop.ID);
    }

    // ── Test 5: assign_from_chashes_384/768/1024, retargeted to BOTH unified
    //    tables (o8dil.47 "one era" ruling). ─────────────────────────────────

    @Test
    void assignFromChashes_ownPass_persistsAssignmentAgainstBothUnifiedTables() throws Exception {
        Rig rig = newRig("assign");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                String[] chashes = {fx.chashHex()};

                var assign384 = ASSIGN_FROM_CHASHES_384.call(fx.collection(), chashes, false, MODEL_384, TENANT);
                var assigned = ctx.select(assign384.CHASH, assign384.TOPIC_ID).from(assign384).fetch();
                assertThat(assigned)
                    .as("assign_from_chashes_384 must find the seeded chunk against the seeded centroid")
                    .isNotEmpty();
                assertThat(assigned.get(0).value1()).isEqualTo(fx.chashHex());
                assertThat(assigned.get(0).value2()).isEqualTo(1L);
                for (int dim : new int[] {768, 1024}) {
                    String missing = "nonexistent-collection-" + dim;
                    Table<?> fn = dim == 768
                        ? ASSIGN_FROM_CHASHES_768.call(missing, chashes, false, MODEL_384, TENANT)
                        : ASSIGN_FROM_CHASHES_1024.call(missing, chashes, false, MODEL_384, TENANT);
                    assertThatCode(() -> ctx.selectFrom(fn).fetch())
                        .as("assign_from_chashes_%d must not throw on an empty-match call", dim)
                        .doesNotThrowAnyException();
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 6: the two views, queried directly. ─────────────────────────────

    @Test
    void liveChunksAndCollectionVectorStats_queryableAgainstSeededData() throws Exception {
        Rig rig = newRig("views");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                var live = ctx.select(LIVE_CHUNKS.DIM).from(LIVE_CHUNKS)
                    .where(LIVE_CHUNKS.TENANT_ID.eq(TENANT)).fetch(LIVE_CHUNKS.DIM);
                assertThat(live).as("live_chunks must expose the seeded, manifest-referenced chunk").isNotEmpty();
                assertThat(live.get(0))
                    .as("live_chunks.dim must be DERIVED from which embedding_<dim> column is populated")
                    .isEqualTo(384);
                var stats = ctx.select(COLLECTION_VECTOR_STATS.DIM, COLLECTION_VECTOR_STATS.CHUNK_COUNT)
                    .from(COLLECTION_VECTOR_STATS)
                    .where(COLLECTION_VECTOR_STATS.TENANT_ID.eq(TENANT)).fetch();
                assertThat(stats).isNotEmpty();
                assertThat(stats.get(0).value1()).isEqualTo(384);
                assertThat(stats.get(0).value2()).isEqualTo(1L);
            }
        } finally {
            rig.close();
        }
    }
}
