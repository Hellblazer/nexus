// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.SupersedeSeeds;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a row 14 (nexus-p2m1x.23): the writers the first {@code StrictWriterCensus} record run found that read no registry row
 * (or read a label's row and wrote by another). Two kinds of test, as the fix brief names them.
 *
 * <p><b>Rename gap</b> (a collection renamed in place, its old label taken by a new holder, as the one-row rename of P3.7 leaves things:
 * the registry row keeps its id and takes a new label, and every row that stored the old label stays under it): the write must land
 * under the renamed row's id and its current label, and reach nothing of the new holder. {@code DepartedWriterSpans} (the span clear
 * of a manifest purge), {@code ReembedJobRepository.reenter} and the earlier-job mark of {@code startMarking}.
 *
 * <p><b>Verb race</b> (an in-flight writer, which holds its registry row {@code FOR KEY SHARE}, against a verb that takes the row
 * {@code FOR UPDATE}): {@code ChashRepository.renameCollection} must wait for the writer, which it did not.
 *
 * <p>The one-row rename is not on this base, so the superuser does what it does ({@code RenameGapSeededChildRowsTest}): every foreign key
 * that references a name-bearing registry key goes, on this class's own database, and the registry row takes its new label in place.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229Row14ReembedRenameGapTest {

    private static final String SVC_ROLE = "svc_rdr229_row14";
    private static final String SVC_PASS = "svc_rdr229_row14_pass";
    private static final String T = "row14-gap";
    private static final int[] P_SPAN = {500, 510, 5000, 5100};

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository catalog;
    private final AtomicInteger n = new AtomicInteger();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        su(ctx -> {
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (!key.getFields().contains(CATALOG_COLLECTIONS.NAME)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
            return null;
        });
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        catalog = new CatalogRepository(scope);
    }

    @AfterAll
    void stop() {
        CollectionTombstones.setRowLockTimeoutMsForTests(null);
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    private <R> R su(Function<DSLContext, R> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private String label(String stem) {
        return "docs__row14" + stem + n.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private String registered(String stem) throws Exception {
        String l = label(stem);
        su(ctx -> { PgContainerHelper.insertCollection(ctx, T, l); return null; });
        return l;
    }

    private long idOf(String label) throws Exception {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    /** The one-row rename: the registry row takes a new label in place and keeps its id; nothing that stored the old label changes. */
    private String oneRowRename(String from) throws Exception {
        long id = idOf(from);
        String to = label("renamed");
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        assertThat(idOf(to)).as("setup: the renamed row is the same row").isEqualTo(id);
        return to;
    }

    /** A new collection takes the label a rename freed; returns its id. */
    private long retake(String oldLabel) throws Exception {
        su(ctx -> { PgContainerHelper.insertCollection(ctx, T, oldLabel); return null; });
        return idOf(oldLabel);
    }

    private ReembedJobRepository.Job finishedJob(ReembedJobRepository repo, String src, String tgt, String disposition) {
        var job = repo.start(T, src, tgt, null, false, false);
        repo.applyTransition(T, job.jobId(), ReembedJobRepository.PLANNED, null, ReembedJobRepository.DONE,
            disposition == null ? ReembedJobRepository.Transition.NONE : ReembedJobRepository.Transition.disposition(disposition));
        return job;
    }

    private record Ends(String source, String target, Long sourceId, Long targetId, String state, String disposition) {}

    private Ends jobRow(UUID id) throws Exception {
        return su(ctx -> ctx.select(REEMBED_JOBS.SOURCE_COLLECTION, REEMBED_JOBS.TARGET_COLLECTION, REEMBED_JOBS.SOURCE_COLLECTION_ID,
                REEMBED_JOBS.TARGET_COLLECTION_ID, REEMBED_JOBS.STATE, REEMBED_JOBS.DISPOSITION)
            .from(REEMBED_JOBS).where(REEMBED_JOBS.TENANT_ID.eq(T)).and(REEMBED_JOBS.JOB_ID.eq(id))
            .fetchOne(r -> new Ends(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6())));
    }

    // ── DepartedWriterSpans.update (both census rows) ───────────────────────

    private static String hexOf(String text) {
        return Chash.ofText(text).toHex();
    }

    private void doc(String id, String collection) throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, T).set(CATALOG_DOCUMENTS.TUMBLER, id).set(CATALOG_DOCUMENTS.TITLE, id)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(T, collection))
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + id + ".md").execute());
    }

    private static Map<String, Object> row(int position, String hex, int[] span) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("position", position);
        r.put("chash", hex);
        r.put("section_type", "body");
        if (span != null) {
            r.put("line_start", span[0]);
            r.put("line_end", span[1]);
            r.put("char_start", span[2]);
            r.put("char_end", span[3]);
        }
        return r;
    }

    @Test
    void departedSpanClear_afterTheCollectionWasRenamedInPlaceAndItsLabelRetaken_keepsTheChunksOwnId_andStampsNothingForTheNewHolder() throws Exception {
        String x = registered("spans");
        long xId = idOf(x);
        String hex = hexOf("row14 departed span " + x);
        Map<String, Object> spans = new LinkedHashMap<>();
        spans.put("line_start", P_SPAN[0]);
        spans.put("line_end", P_SPAN[1]);
        spans.put("chunk_start_char", P_SPAN[2]);
        spans.put("chunk_end_char", P_SPAN[3]);
        su(ctx -> { PgContainerHelper.insertChunks(ctx, T, x, List.of(hex), List.of("row14 span chunk"), List.of(new float[384]), List.of(spans)); return null; });
        String docP = "row14-p-" + n.incrementAndGet();
        String docA = "row14-a-" + n.incrementAndGet();
        doc(docP, x);
        doc(docA, x);
        // P wrote the chunk with its span and holds it; A holds it with no span of its own: when P leaves, A is the sole holder and lacks it
        catalog.writeManifest(T, docP, x, List.of(row(0, hex, P_SPAN)));
        catalog.writeManifest(T, docA, x, List.of(row(0, hex, null)));
        String renamed = oneRowRename(x);
        long newHolder = retake(x);
        assertThat(newHolder).as("setup: another collection bears the label the chunk still stores").isNotEqualTo(xId);

        catalog.purgeManifest(T, docP);

        var chunk = su(ctx -> ctx.select(CHUNKS.COLLECTION_ID, CHUNKS.COLLECTION, CHUNKS.METADATA).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.CHASH.eq(java.util.HexFormat.of().parseHex(hex))).fetchOne());
        assertThat(chunk.value1()).as("the clear kept the chunk's own collection id: it did not re-stamp it from the label the new holder bears").isEqualTo(xId);
        JSONB meta = chunk.value3();
        assertThat(meta.data()).as("and it still did its work: the departed writer's span is gone from the renamed collection's chunk")
            .doesNotContain("line_start").doesNotContain("chunk_end_char");
        int underNewHolder = su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(T).and(CHUNKS.COLLECTION_ID.eq(newHolder))));
        assertThat(underNewHolder).as("nothing was filed under the new holder").isZero();
        assertThat(renamed).isNotEqualTo(x);
    }

    // ── ReembedJobRepository.reenter ────────────────────────────────────────

    @Test
    void reenter_afterTheTargetWasRenamedInPlaceAndItsLabelRetaken_filesTheJobUnderTheRowsCurrentLabelAndId_notTheNewHolders() throws Exception {
        var repo = new ReembedJobRepository(scope);
        String src = registered("rsrc");
        String tgt = registered("rtgt");
        long srcId = idOf(src);
        long tgtId = idOf(tgt);
        var job = finishedJob(repo, src, tgt, "kept_tombstoned");
        String renamed = oneRowRename(tgt);
        long newHolder = retake(tgt);

        var again = repo.reenter(T, job.jobId(), ReembedJobRepository.RUNNING);

        assertThat(again).as("re-entered").isPresent();
        Ends row = jobRow(job.jobId());
        assertThat(row.state()).isEqualTo(ReembedJobRepository.RUNNING);
        assertThat(row.targetId()).as("the job still names the collection it was filed for").isEqualTo(tgtId);
        assertThat(row.target()).as("under that row's current label").isEqualTo(renamed);
        assertThat(row.sourceId()).isEqualTo(srcId);
        int jobsOnNewHolder = su(ctx -> ctx.fetchCount(REEMBED_JOBS, REEMBED_JOBS.TENANT_ID.eq(T)
            .and(REEMBED_JOBS.TARGET_COLLECTION_ID.eq(newHolder).or(REEMBED_JOBS.TARGET_COLLECTION.eq(tgt)))));
        assertThat(jobsOnNewHolder).as("no job targets the new holder of the old label").isZero();
    }

    @Test
    void reenter_whenTheTargetsRowIsGoneAndItsLabelRetaken_refusesAndWritesNothing() throws Exception {
        var repo = new ReembedJobRepository(scope);
        String src = registered("gsrc");
        String tgt = registered("gtgt");
        var job = finishedJob(repo, src, tgt, "kept_tombstoned");
        long tgtId = idOf(tgt);
        su(ctx -> ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(T))
            .and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(tgtId)).execute());
        retake(tgt);

        assertThatThrownBy(() -> repo.reenter(T, job.jobId(), ReembedJobRepository.RUNNING))
            .isInstanceOf(ReembedJobRepository.EndRefused.class).hasMessageContaining(job.jobId().toString());

        Ends row = jobRow(job.jobId());
        assertThat(row.state()).as("the job is as it was").isEqualTo(ReembedJobRepository.DONE);
        assertThat(row.disposition()).isEqualTo("kept_tombstoned");
        assertThat(row.targetId()).isEqualTo(tgtId);
    }

    @Test
    void reenter_whenTheTargetIsRetired_refusesAndWritesNothing() throws Exception {
        var repo = new ReembedJobRepository(scope);
        String src = registered("tsrc");
        String tgt = registered("ttgt");
        var job = finishedJob(repo, src, tgt, "kept_tombstoned");
        assertThat(SupersedeSeeds.supersede(catalog, T, tgt, label("successor"), null)).as("setup: the target is retired").isEqualTo(1);

        assertThatThrownBy(() -> repo.reenter(T, job.jobId(), ReembedJobRepository.RUNNING))
            .isInstanceOf(ReembedJobRepository.EndRefused.class).hasMessageContaining("superseded");

        assertThat(jobRow(job.jobId()).state()).isEqualTo(ReembedJobRepository.DONE);
    }

    // ── ReembedJobRepository.markRevived, run inside start's transaction ────

    @Test
    void startMarking_afterTheEarlierJobsTargetWasRenamedInPlaceAndItsLabelRetaken_marksTheJobOfTheRowById_andNotOneOfTheNewHolder() throws Exception {
        var repo = new ReembedJobRepository(scope);
        String a = registered("revA");
        String b = registered("revB");
        long aId = idOf(a);
        var earlier = finishedJob(repo, a, b, null);          // A -> B, finished
        String aRenamed = oneRowRename(a);                    // A bears another label now; its job still stores the old one
        long aNewHolder = retake(a);                          // a new collection bears the old label...
        var decoy = finishedJob(repo, a, b, null);            // ...and has a finished job of its own, A' -> B
        assertThat(jobRow(decoy.jobId()).sourceId()).as("setup: the decoy is the new holder's").isEqualTo(aNewHolder);

        // the way back: B -> A (the revived one, by its current label)
        var started = repo.startMarking(T, b, aRenamed, null, false, false, true);

        assertThat(started.revived()).as("the earlier job of the row (matched by its id, its stored label being stale)").isPresent();
        assertThat(started.revived().orElseThrow().jobId()).isEqualTo(earlier.jobId());
        assertThat(jobRow(earlier.jobId()).disposition()).isEqualTo("revived");
        assertThat(jobRow(decoy.jobId()).disposition()).as("the new holder's job is not touched").isNull();
        assertThat(jobRow(started.job().jobId()).targetId()).as("the new job goes to the row, not to the label's new holder").isEqualTo(aId);
    }

    @Test
    void startMarking_withoutMarking_leavesTheEarlierJobAlone() throws Exception {
        var repo = new ReembedJobRepository(scope);
        String a = registered("nomarkA");
        String b = registered("nomarkB");
        var earlier = finishedJob(repo, a, b, null);

        var started = repo.startMarking(T, b, a, null, false, false, false);

        assertThat(started.revived()).isEmpty();
        assertThat(jobRow(earlier.jobId()).disposition()).isNull();
    }

    // ── ChashRepository.renameCollection against an in-flight writer ────────

    @Timeout(60)
    @Test
    void chashRename_waitsForAnInFlightWriterOfTheOldCollection_thenFollowsIt() throws Exception {
        String oldName = registered("chashold");
        String newName = registered("chashnew");
        String hex = hexOf("row14 chash rename " + oldName);
        su(ctx -> { PgContainerHelper.insertChunks(ctx, T, oldName, List.of(hex), List.of("row14 chash chunk"), List.of(new float[384]),
            List.of(Map.<String, Object>of())); return null; });
        var chash = new ChashRepository(scope);
        CollectionTombstones.setRowLockTimeoutMsForTests("700");
        try (Connection writer = pg.createConnection("")) {
            writer.setAutoCommit(false);
            // an in-flight writer: it holds the old collection's registry row FOR KEY SHARE, as every writer does for the whole of its transaction
            DSL.using(writer, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(oldName)).forKeyShare().fetch();
            try {
                assertThatThrownBy(() -> chash.renameCollection(T, oldName, newName))
                    .as("the rename took the row FOR UPDATE and waited for the writer, up to its bound; it did not run past it")
                    .isInstanceOf(RenameBusyException.class);
                assertThat(chunkCollection(hex)).as("and changed nothing").isEqualTo(oldName);
            } finally {
                writer.rollback();
            }
        } finally {
            CollectionTombstones.setRowLockTimeoutMsForTests(null);
        }

        assertThat(chash.renameCollection(T, oldName, newName)).as("the writer is done: the rename goes through").isEqualTo(1);
        assertThat(chunkCollection(hex)).isEqualTo(newName);
    }

    private String chunkCollection(String hex) throws Exception {
        return su(ctx -> ctx.select(CHUNKS.COLLECTION).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(T))
            .and(CHUNKS.CHASH.eq(java.util.HexFormat.of().parseHex(hex))).fetchOne(CHUNKS.COLLECTION));
    }
}
