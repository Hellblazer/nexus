// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.GC_EXPIRE_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.GC_EXPIRE_QUARANTINE_BOUNDED;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;

/**
 * RDR-222 Amendment A1 (bead nexus-1579m): {@code nexus.gc_expire_quarantine_bounded} through the RLS-subject
 * service role against real Postgres, and {@link PgVectorRepository#expireQuarantineBounded} over it. The route and
 * ledger layers are pinned by {@code KeyedSweepRouteTest}.
 *
 * <p>Fixtures name their chashes by hand: a group's first byte is chosen, so a test controls the order the function
 * walks ({@code 00} protected rows sort before {@code ee} eligible ones). All SQL is typed jOOQ (the test tree's
 * raw-SQL ratchets, {@code RawSqlGateTest} and {@code SqlStringLiteralGateTest}, hold this file at none).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GcExpireQuarantineBoundedTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String SVC_ROLE = "svc_1579m_test";
    private static final String SVC_PASS = "svc_1579m_test_pass";
    private static final String TENANT = "t1579m";
    private static final String MODEL = "minilm-l6-v2-384";
    /** A quarantined_at before the cutoff, one after it. */
    private static final String OLD = "2026-07-01T00:00:00Z";
    private static final String NEW = "2026-09-01T00:00:00Z";
    private static final String CUTOFF = "2026-08-01T00:00:00Z";
    private static final float[] ZERO_384 = new float[384];

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalog;
    PgVectorRepository vecRepo;
    int seq;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        catalog = new CatalogRepository(tenantScope);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    record Fx(String origin, String sibling) {}

    record Page(long expired, Long refused, boolean more, boolean floorRefused) {}

    record AuditRow(int chashCount, List<String> chashes, JsonNode details) {}

    private void su(java.util.function.Consumer<DSLContext> work) {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private <T> T suGet(java.util.function.Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private Fx newFx() {
        int id = ++seq;
        String origin = "code__eqb" + id + "__" + MODEL + "__v1";
        Fx fx = new Fx(origin, "quarantine-" + origin);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, fx.origin());
            PgContainerHelper.insertCollection(ctx, TENANT, fx.sibling());
        });
        return fx;
    }

    private static String sha256Hex(String seed) {
        try {
            byte[] d = MessageDigest.getInstance("SHA-256").digest(seed.getBytes(StandardCharsets.UTF_8));
            return Chash.fromSha256Bytes(d).toHex();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** {@code n} distinct chashes whose first byte is {@code pp}, ascending. */
    private static List<String> chashes(String sibling, String pp, int n) {
        List<String> out = new ArrayList<>();
        for (int i = 0; i < n; i++) out.add(pp + sha256Hex(sibling + "-" + pp + "-" + i).substring(2));
        out.sort(null);
        return out;
    }

    private static Map<String, Object> meta(Fx fx, String quarantinedAt, boolean engineOwned) {
        var m = new java.util.LinkedHashMap<String, Object>();
        m.put("quarantined_at", quarantinedAt);
        m.put("origin_collection", fx.origin());
        if (engineOwned) {
            m.put("quarantined_by", "engine-reaper");
            m.put("reaper_quarantined_at", quarantinedAt);
        }
        return m;
    }

    private List<String> seedGroup(Fx fx, String pp, int n, String quarantinedAt, boolean engineOwned) {
        List<String> hashes = chashes(fx.sibling(), pp, n);
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            texts.add("text " + i);
            vecs.add(ZERO_384);
            metas.add(meta(fx, quarantinedAt, engineOwned));
        }
        su(ctx -> PgContainerHelper.insertChunks(ctx, TENANT, fx.sibling(), hashes, texts, vecs, metas));
        return hashes;
    }

    /** Eligible rows: past the cutoff, untagged, no manifest row. */
    private List<String> eligible(Fx fx, String pp, int n) {
        return seedGroup(fx, pp, n, OLD, false);
    }

    /** Past the cutoff but named by a manifest row in the origin: the function must keep them. */
    private List<String> protectedRows(Fx fx, String pp, int n) {
        List<String> hashes = seedGroup(fx, pp, n, OLD, false);
        String doc = "doc-" + fx.sibling() + "-" + pp;
        catalog.upsertDocument(TENANT, Map.of(
            "tumbler", doc, "title", doc, "content_type", "code", "corpus", "code",
            "physical_collection", fx.origin(), "chunk_count", 0));
        su(ctx -> {
            // The origin holds a stub per chash (the manifest foreign key needs a chunk row to point at).
            PgContainerHelper.insertChunks(ctx, TENANT, fx.origin(), hashes,
                hashes.stream().map(h -> "stub").toList(), hashes.stream().map(h -> ZERO_384).toList(),
                hashes.stream().map(h -> Map.<String, Object>of()).toList());
            String model = PgContainerHelper.collectionModel(ctx, TENANT, fx.origin());
            for (int i = 0; i < hashes.size(); i++) {
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                        CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                    .values(TENANT, doc, i, Chash.fromHex(hashes.get(i)).toBytes(), fx.origin(), model)
                    .execute();
            }
        });
        return hashes;
    }

    private List<String> engineOwned(Fx fx, String pp, int n) {
        return seedGroup(fx, pp, n, OLD, true);
    }

    private List<String> notYetPastCutoff(Fx fx, String pp, int n) {
        return seedGroup(fx, pp, n, NEW, false);
    }

    private Set<String> chashesIn(String collection) {
        return suGet(ctx -> {
            Set<String> out = new HashSet<>();
            for (byte[] b : ctx.selectDistinct(CHUNKS.CHASH).from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))).fetch(CHUNKS.CHASH)) {
                out.add(Chash.fromSha256Bytes(b).toHex());
            }
            return out;
        });
    }

    private long rowCount(String collection) {
        return suGet(ctx -> (long) ctx.fetchCount(CHUNKS,
            CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))));
    }

    private List<AuditRow> audits(Fx fx) {
        return suGet(ctx -> {
            List<AuditRow> out = new ArrayList<>();
            for (var r : ctx.select(GC_AUDIT.CHASH_COUNT, GC_AUDIT.CHASHES, GC_AUDIT.DETAILS).from(GC_AUDIT)
                    .where(GC_AUDIT.TENANT_ID.eq(TENANT)
                        .and(GC_AUDIT.OPERATION.eq("gc_expire_quarantine_bounded"))
                        .and(GC_AUDIT.COLLECTION.eq(fx.sibling())))
                    .orderBy(GC_AUDIT.ID).fetch()) {
                try {
                    List<String> hashes = new ArrayList<>();
                    MAPPER.readTree(r.get(GC_AUDIT.CHASHES).data()).forEach(n -> hashes.add(n.asText()));
                    out.add(new AuditRow(r.get(GC_AUDIT.CHASH_COUNT), hashes,
                        MAPPER.readTree(r.get(GC_AUDIT.DETAILS).data())));
                } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                    throw new IllegalStateException(e);
                }
            }
            return out;
        });
    }

    // ── the call ─────────────────────────────────────────────────────────────

    private Page callIn(DSLContext ctx, int dim, Fx fx, int rowLimit, boolean countRefused, double fraction,
                        int minChunks, boolean force) {
        var rec = ctx.selectFrom(GC_EXPIRE_QUARANTINE_BOUNDED.call(
                dim, TENANT, fx.sibling(), fx.origin(), CUTOFF, fraction, minChunks, force, rowLimit, countRefused))
            .fetchOne();
        return new Page(rec.get(GC_EXPIRE_QUARANTINE_BOUNDED.EXPIRED), rec.get(GC_EXPIRE_QUARANTINE_BOUNDED.REFUSED),
            rec.get(GC_EXPIRE_QUARANTINE_BOUNDED.MORE), rec.get(GC_EXPIRE_QUARANTINE_BOUNDED.FLOOR_REFUSED));
    }

    private Page call(Fx fx, int rowLimit, boolean countRefused, double fraction, int minChunks, boolean force) {
        return tenantScope.withTenant(TENANT,
            ctx -> callIn(ctx, 384, fx, rowLimit, countRefused, fraction, minChunks, force));
    }

    /** The default call: the inert floor the clients send (a fraction of 1.0 can never fire), refused counted. */
    private Page call(Fx fx, int rowLimit) {
        return call(fx, rowLimit, true, 1.0, 1, false);
    }

    private static SQLException sqlException(Throwable t) {
        for (int d = 0; t != null && d < 32; d++, t = t.getCause()) {
            if (t instanceof SQLException se) return se;
        }
        return null;
    }

    private Connection holdGate(String key) throws Exception {
        Connection holder = pg.createConnection("");
        holder.setAutoCommit(false);
        DSL.using(holder, SQLDialect.POSTGRES)
           .select(DSL.function("pg_advisory_xact_lock", Object.class,
                   DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + TENANT + "/" + key))))
           .fetch();
        return holder;
    }

    // ── drain without skip or repeat ─────────────────────────────────────────

    @Test
    void aDrainDeletesEveryEligibleChashOnce_inAscendingPages_andKeepsEveryOtherRow() {
        Fx fx = newFx();
        int limit = 40;
        List<String> want = new ArrayList<>(eligible(fx, "80", 60));
        want.addAll(eligible(fx, "c0", 40));
        List<String> prot = protectedRows(fx, "10", 15);
        List<String> owned = engineOwned(fx, "20", 15);
        List<String> later = notYetPastCutoff(fx, "30", 15);
        assertThat(want).as("guard: 2.5 pages of eligible chashes").hasSize(100);
        assertThat(rowCount(fx.sibling())).as("guard: plus 45 non-eligible rows").isEqualTo(145);

        List<Page> pages = new ArrayList<>();
        Page p;
        do {
            p = call(fx, limit);
            pages.add(p);
            assertThat(pages.size()).as("the loop terminates").isLessThan(10);
        } while (p.more());

        assertThat(pages).extracting(Page::expired).as("40, 40, 20").containsExactly(40L, 40L, 20L);
        assertThat(pages).extracting(Page::more).containsExactly(true, true, false);
        List<AuditRow> audits = audits(fx);
        assertThat(audits).as("one audit row per page that deleted").hasSize(3);
        Set<String> seen = new HashSet<>();
        String previousLast = "";
        for (AuditRow a : audits) {
            assertThat(a.chashes()).as("each page's chashes are ascending").isSorted();
            assertThat(a.chashes().get(0)).as("and above the previous page's").isGreaterThan(previousLast);
            previousLast = a.chashes().get(a.chashes().size() - 1);
            for (String h : a.chashes()) {
                assertThat(seen.add(h)).as("%s is in one page only", h).isTrue();
            }
            assertThat(a.chashCount()).isEqualTo(a.chashes().size());
        }
        assertThat(seen).as("the union of the pages is exactly the eligible set").containsExactlyInAnyOrderElementsOf(want);
        Set<String> left = chashesIn(fx.sibling());
        assertThat(left).as("every non-eligible row survives, and nothing else").containsExactlyInAnyOrderElementsOf(
            java.util.stream.Stream.of(prot, owned, later).flatMap(List::stream).toList());
        assertThat(audits.get(0).details().get("origin_collection").asText()).isEqualTo(fx.origin());
        assertThat(audits.get(0).details().get("cutoff").asText()).isEqualTo(CUTOFF);
        assertThat(audits.get(0).details().get("row_limit").asInt()).isEqualTo(limit);
        assertThat(audits.get(0).details().get("rows").asInt()).isEqualTo(40);
        assertThat(audits.get(0).details().get("more_after").asBoolean()).isTrue();
        assertThat(audits.get(2).details().get("more_after").asBoolean()).isFalse();
    }

    // ── no stall behind rows the predicate excludes ──────────────────────────

    @Test
    void protectedAndEngineOwnedRowsThatSortFirstDoNotStallTheLoop() {
        Fx fx = newFx();
        int limit = 25;
        protectedRows(fx, "00", 60);
        List<String> owned = engineOwned(fx, "01", 40);
        eligible(fx, "ee", 30);

        // The hazard, stated: a LIMIT applied BEFORE the exclusions takes only rows the predicate then rejects, so
        // a page built that way deletes nothing and a loop on it never ends.
        long victimsInFirstNRows = suGet(ctx -> {
            long n = 0;
            for (var r : ctx.select(CHUNKS.CHASH, CHUNKS.METADATA).from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(fx.sibling())))
                    .orderBy(CHUNKS.CHASH).limit(limit).fetch()) {
                JsonNode m;
                try {
                    m = MAPPER.readTree(r.get(CHUNKS.METADATA).data());
                } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                    throw new IllegalStateException(e);
                }
                boolean untagged = !m.has("quarantined_by");
                boolean past = m.get("quarantined_at").asText().compareTo(CUTOFF) <= 0;
                boolean referenced = ctx.fetchExists(CATALOG_DOCUMENT_CHUNKS,
                    CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(fx.origin()))
                        .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(r.get(CHUNKS.CHASH))));
                if (untagged && past && !referenced) n++;
            }
            return n;
        });
        assertThat(victimsInFirstNRows).as("guard: LIMIT-then-filter would find no victim on this fixture").isZero();

        Page first = call(fx, limit);
        assertThat(first.expired()).as("the real page deletes eligible rows").isEqualTo(25L);
        assertThat(first.more()).isTrue();
        Page second = call(fx, limit);
        assertThat(second.expired()).isEqualTo(5L);
        assertThat(second.more()).isFalse();
        assertThat(rowCount(fx.sibling())).as("60 protected and 40 engine-owned remain").isEqualTo(100);
        assertThat(chashesIn(fx.sibling())).containsAll(owned);
    }

    // ── more ─────────────────────────────────────────────────────────────────

    @Test
    void moreIsTrueOnlyWhenAnEligibleChashLiesPastThePage() {
        Fx exact = newFx();
        eligible(exact, "80", 10);
        Page one = call(exact, 10);
        assertThat(one.expired()).isEqualTo(10L);
        assertThat(one.more()).as("exactly row_limit eligible chashes drain in one page").isFalse();

        Fx plusOne = newFx();
        eligible(plusOne, "80", 11);
        Page a = call(plusOne, 10);
        assertThat(a.expired()).isEqualTo(10L);
        assertThat(a.more()).as("row_limit + 1 leaves one behind").isTrue();
        Page b = call(plusOne, 10);
        assertThat(b.expired()).isEqualTo(1L);
        assertThat(b.more()).isFalse();
        assertThat(call(plusOne, 10).expired()).as("an empty sibling answers zero").isZero();
    }

    // ── a row that changed while the page waited on its lock ─────────────────

    /**
     * The DELETE restates the eligibility predicates so a victim that changed and committed while the page waited on
     * its row lock is re-checked against its new version (READ COMMITTED, EvalPlanQual). Each test below changes the
     * locked victim in the one way a single predicate refuses, so each can fail on that clause alone: the cutoff,
     * the engine-ownership test and the origin tag. The manifest {@code NOT EXISTS} clause is NOT reachable this
     * way: EvalPlanQual re-evaluates the locked table's row against its new version but keeps the other relations'
     * rows as the statement saw them, so a manifest row committed during the wait is invisible to the re-check and
     * no lock-wait test can turn that clause red. It is held by the selection (the same predicate ahead of the
     * LIMIT, see {@code protectedAndEngineOwnedRowsThatSortFirstDoNotStallTheLoop}).
     *
     * <p>The holder rewrites the first victim's metadata to {@code changed} and holds it uncommitted; the page then
     * selects its victims from the committed snapshot (all ten), blocks on the row lock in its DELETE, and on commit
     * deletes the other nine.
     */
    private void aVictimChangedUnderItsRowLockSurvives(java.util.function.Function<Fx, Map<String, Object>> changed)
            throws Exception {
        Fx fx = newFx();
        List<String> victims = eligible(fx, "80", 10);
        String locked = victims.get(0);

        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            var holderCtx = DSL.using(holder, SQLDialect.POSTGRES);
            holderCtx.update(CHUNKS).set(CHUNKS.METADATA, JSONB.jsonb(MAPPER.writeValueAsString(changed.apply(fx))))
                .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(fx.sibling()))
                    .and(CHUNKS.CHASH.eq(Chash.fromHex(locked).toBytes())))
                .execute();

            CompletableFuture<Page> page = CompletableFuture.supplyAsync(() -> call(fx, 100));
            var activity = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));
            Field<String> waitType = DSL.field(DSL.name("wait_event_type"), String.class);
            Field<String> query = DSL.field(DSL.name("query"), String.class);
            Field<Integer> pid = DSL.field(DSL.name("pid"), Integer.class);
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            boolean waiting = false;
            while (System.nanoTime() < deadline && !waiting) {
                waiting = suGet(ctx -> ctx.fetchCount(activity, waitType.eq("Lock")
                    .and(query.like("%gc_expire_quarantine_bounded%"))
                    .and(pid.ne(DSL.function("pg_backend_pid", Integer.class))))) > 0;
                if (!waiting) Thread.sleep(20);
            }
            assertThat(waiting).as("the page is waiting on the victim's row lock (inside the 2 s lock_timeout)").isTrue();
            holder.commit();
            Page p = page.get(15, TimeUnit.SECONDS);
            assertThat(p.expired()).as("the other nine").isEqualTo(9L);
        }

        assertThat(chashesIn(fx.sibling())).as("the changed victim survived").containsExactly(locked);
        assertThat(audits(fx).get(0).chashes()).as("and is not in the audit row").doesNotContain(locked).hasSize(9);
    }

    @Test
    void aVictimRequarantinedWhileThePageWaitsOnItsRowLockSurvives() throws Exception {
        // Re-quarantined with a fresh stamp, as a quarantine run that moved the chash again would leave it: the
        // cutoff clause refuses it.
        aVictimChangedUnderItsRowLockSurvives(fx -> meta(fx, NEW, false));
    }

    @Test
    void aVictimTakenByTheReaperWhileThePageWaitsOnItsRowLockSurvives() throws Exception {
        // Engine-owned: quarantined_by = engine-reaper with reaper_quarantined_at equal to quarantined_at (here the
        // old stamp, so the cutoff still passes and only the ownership clause refuses it).
        aVictimChangedUnderItsRowLockSurvives(fx -> {
            Map<String, Object> m = meta(fx, OLD, true);
            assertThat(m.get("reaper_quarantined_at")).isEqualTo(m.get("quarantined_at"));
            return m;
        });
    }

    @Test
    void aVictimRewrittenToAnotherOriginWhileThePageWaitsOnItsRowLockSurvives() throws Exception {
        // The row now says it was quarantined out of a different origin; the stamp is still old and the row is not
        // engine-owned, so only the origin-tag clause refuses it.
        aVictimChangedUnderItsRowLockSurvives(fx -> {
            Map<String, Object> m = meta(fx, OLD, false);
            m.put("origin_collection", "code__elsewhere__" + MODEL + "__v1");
            return m;
        });
    }

    // ── bounds and arguments ─────────────────────────────────────────────────

    @Test
    void aNonPositiveRowLimitIsRefused_neverUnbounded() {
        Fx fx = newFx();
        eligible(fx, "80", 3);
        for (int bad : new int[] {0, -5}) {
            Throwable t = catchThrowable(() -> call(fx, bad));
            SQLException se = sqlException(t);
            assertThat((Object) se).as("row_limit=%d: %s", bad, t).isNotNull();
            assertThat(se.getSQLState()).isEqualTo("22023");
            assertThat(se.getMessage()).contains("p_row_limit must be positive");
        }
        assertThat(rowCount(fx.sibling())).as("nothing deleted").isEqualTo(3);
    }

    @Test
    void anOversizedRowLimitIsClampedToTheAuditCeiling_andTheAuditRowListsEveryDeletedChash() {
        Fx fx = newFx();
        eligible(fx, "80", 5_200);

        Page p = call(fx, 6_000);

        assertThat(p.expired()).as("6,000 asked, 5,000 taken").isEqualTo(5_000L);
        assertThat(p.more()).isTrue();
        AuditRow a = audits(fx).get(0);
        assertThat(a.chashes()).as("the audit row lists every chash it deleted").hasSize(5_000);
        assertThat(a.chashCount()).isEqualTo(5_000);
        assertThat(a.details().get("row_limit").asInt()).as("the clamped bound is what the row records").isEqualTo(5_000);
        assertThat(a.details().has("chashes_truncated")).isFalse();
        assertThat(rowCount(fx.sibling())).isEqualTo(200);
    }

    @Test
    void aChashStoredUnderTwoModelsCountsBothRows_andIsAuditedOnce() throws Exception {
        Fx fx = newFx();
        List<String> hashes = eligible(fx, "80", 2);
        String twin = hashes.get(0);
        // chunks_collection_fk allows one model per collection; the twin row is filed under another model with
        // the constraint out of the way, the shape a collection whose model was changed would leave.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropConstraint(su, CHUNKS, "chunks_collection_fk");
            DSL.using(su, SQLDialect.POSTGRES)
               .insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                           CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_768, CHUNKS.METADATA)
               .values(TENANT, fx.sibling(), Chash.fromHex(twin).toBytes(), "bge-base-en-v15-768", "twin",
                       Vector.of(new float[768]), JSONB.jsonb(MAPPER.writeValueAsString(meta(fx, OLD, false))))
               .execute();
        }
        assertThat(rowCount(fx.sibling())).as("guard: three rows, two chashes").isEqualTo(3);

        Page p = call(fx, 10);

        assertThat(p.expired()).as("one per model row").isEqualTo(3L);
        AuditRow a = audits(fx).get(0);
        assertThat(a.chashes()).as("the chash is listed once").hasSize(2);
        assertThat(a.chashCount()).isEqualTo(2);
        assertThat(a.details().get("rows").asInt()).isEqualTo(3);
    }

    // ── refused and the floor ────────────────────────────────────────────────

    @Test
    void countRefusedFalseAnswersNull_trueAnswersTheProtectedCount() {
        Fx fx = newFx();
        eligible(fx, "80", 4);
        protectedRows(fx, "10", 6);

        Page uncounted = call(fx, 2, false, 1.0, 1, false);
        assertThat(uncounted.refused()).as("not counted: NULL, not zero").isNull();
        assertThat(uncounted.expired()).isEqualTo(2L);

        Page counted = call(fx, 2, true, 1.0, 1, false);
        assertThat(counted.refused()).as("the past-cutoff rows the origin's manifest names").isEqualTo(6L);
        assertThat(audits(fx).get(0).details().has("refused")).as("an uncounted page records none").isFalse();
        assertThat(audits(fx).get(1).details().get("refused").asInt()).isEqualTo(6);
    }

    @Test
    void underTheRouteDefaultFloorRefusedIsANumberEvenWithCountRefusedFalse() {
        Fx fx = newFx();
        eligible(fx, "80", 4);
        protectedRows(fx, "10", 6);

        // The route's defaults (0.5 / 50): 4 eligible of 10 client rows is under both bounds, so the floor scan
        // runs and passes. That scan counts the protected rows as a by-product, so refused is NOT null although
        // count_refused is false: it is null only when neither the floor scan nor count_refused counted it.
        Page p = call(fx, 10, false, 0.5, 50, false);

        assertThat(p.floorRefused()).isFalse();
        assertThat(p.expired()).isEqualTo(4L);
        assertThat(p.refused()).as("numeric, the floor scan's protected count").isNotNull().isEqualTo(6L);
        assertThat(audits(fx).get(0).details().get("refused").asInt()).as("and recorded").isEqualTo(6);
    }

    @Test
    void anInertFloorOrForceGivesTheUnboundedFunctionsExpiredAndRefused() {
        for (boolean useForce : new boolean[] {false, true}) {
            Fx bounded = newFx();
            Fx unbounded = newFx();
            for (Fx fx : new Fx[] {bounded, unbounded}) {
                eligible(fx, "80", 30);
                protectedRows(fx, "10", 7);
                engineOwned(fx, "20", 5);
                notYetPastCutoff(fx, "30", 3);
            }
            double fraction = useForce ? 0.1 : 1.0;
            Page b = call(bounded, 100, true, fraction, 1, useForce);
            var u = tenantScope.withTenant(TENANT, ctx -> ctx.selectFrom(GC_EXPIRE_QUARANTINE.call(
                    384, TENANT, unbounded.sibling(), unbounded.origin(), CUTOFF, fraction, 1, useForce))
                .fetchOne());
            assertThat(b.expired()).as("force=%s", useForce).isEqualTo(u.get(GC_EXPIRE_QUARANTINE.EXPIRED)).isEqualTo(30L);
            assertThat(b.refused()).as("force=%s", useForce).isEqualTo(u.get(GC_EXPIRE_QUARANTINE.REFUSED)).isEqualTo(7L);
            assertThat(b.floorRefused()).isFalse();
            assertThat(b.more()).isFalse();
        }
    }

    @Test
    void aFirstPageOverTheFloorIsRefusedWhole_andForceLetsItPage() {
        Fx fx = newFx();
        eligible(fx, "80", 30);
        protectedRows(fx, "10", 5);

        // 30 of 35 client rows are eligible: 0.857 > 0.5 and 30 >= 10, so the floor fires.
        Page refused = call(fx, 10, true, 0.5, 10, false);
        assertThat(refused.floorRefused()).isTrue();
        assertThat(refused.expired()).isZero();
        assertThat(refused.refused()).as("eligible + protected, as the unbounded form reports a floor refusal").isEqualTo(35L);
        assertThat(rowCount(fx.sibling())).as("nothing deleted").isEqualTo(35);
        assertThat(audits(fx)).as("and no audit row").isEmpty();

        Page forced = call(fx, 10, true, 0.5, 10, true);
        assertThat(forced.floorRefused()).isFalse();
        assertThat(forced.expired()).isEqualTo(10L);
        assertThat(forced.more()).isTrue();
    }

    @Test
    void aLoopThatPassesTheFloorOnItsFirstPageIsNeverRefusedLater() {
        Fx fx = newFx();
        eligible(fx, "80", 60);
        protectedRows(fx, "10", 40);   // 60 of 100: 0.6, under a floor of 0.9

        List<Page> pages = new ArrayList<>();
        Page p;
        do {
            p = call(fx, 20, true, 0.9, 10, false);
            pages.add(p);
            assertThat(pages.size()).isLessThan(10);
        } while (p.more());

        assertThat(pages).extracting(Page::floorRefused).containsOnly(false);
        assertThat(pages).extracting(Page::expired).containsExactly(20L, 20L, 20L);
        assertThat(rowCount(fx.sibling())).isEqualTo(40);
    }

    // ── the sweep gate and the statement bound ───────────────────────────────

    @Test
    void theSiblingsGateBlocksAPage_theOriginsDoesNot() throws Exception {
        Fx fx = newFx();
        eligible(fx, "80", 5);

        Connection origin = holdGate(fx.origin());
        try {
            assertThat(call(fx, 10).expired()).as("holding the origin's key does not block expiry").isEqualTo(5L);
        } finally {
            origin.rollback();
            origin.close();
        }

        eligible(fx, "81", 5);
        Connection sibling = holdGate(fx.sibling());
        try {
            long started = System.nanoTime();
            Throwable t = catchThrowable(() -> call(fx, 10));
            long ms = (System.nanoTime() - started) / 1_000_000L;
            SQLException se = sqlException(t);
            assertThat((Object) se).as("a held sibling gate fails the page: %s", t).isNotNull();
            assertThat(se.getSQLState()).isEqualTo("55P03");
            assertThat(se.getMessage()).contains(PgSession.SWEEP_GATE_MESSAGE_MARKER);
            assertThat(ms).as("inside the body's 2 s lock bound plus slack").isBetween(1_500L, 6_000L);
        } finally {
            sibling.rollback();
            sibling.close();
        }
        assertThat(rowCount(fx.sibling())).as("the refused page deleted nothing").isEqualTo(5);
    }

    @Test
    void theStatementBoundIsRealWhenSetBeforeTheCall_andADeadPageDeletesNothing() throws Exception {
        Fx fx = newFx();
        eligible(fx, "80", 5);
        Connection holder = holdGate(fx.sibling());
        try {
            long started = System.nanoTime();
            Throwable t = catchThrowable(() -> tenantScope.withTenant(TENANT, ctx -> {
                PgSession.setStatementAndLockBounds(ctx, 300, 5_000);
                return callIn(ctx, 384, fx, 10, true, 1.0, 1, false);
            }));
            long ms = (System.nanoTime() - started) / 1_000_000L;
            SQLException se = sqlException(t);
            assertThat((Object) se).as("%s", t).isNotNull();
            assertThat(se.getSQLState()).as("the statement bound, not the body's 2 s lock bound").isEqualTo("57014");
            assertThat(ms).isLessThan(1_900L);
        } finally {
            holder.rollback();
            holder.close();
        }
        assertThat(rowCount(fx.sibling())).isEqualTo(5);
    }

    // ── an unsupported dim ───────────────────────────────────────────────────

    @Test
    void anUnsupportedDimIsRefused() {
        Fx fx = newFx();
        Throwable t = catchThrowable(() -> tenantScope.withTenant(TENANT,
            ctx -> callIn(ctx, 512, fx, 10, true, 1.0, 1, false)));
        assertThat((Object) sqlException(t)).isNotNull();
        assertThat(sqlException(t).getMessage()).contains("unsupported dim");
    }

    // ── the repository: PgVectorRepository.expireQuarantineBounded ────────────

    @Test
    void theRepositoryPagesAnExpiryAndResolvesTheDimFromTheOrigin() {
        Fx fx = newFx();
        eligible(fx, "80", 25);
        protectedRows(fx, "10", 4);

        var a = vecRepo.expireQuarantineBounded(TENANT, fx.sibling(), fx.origin(), CUTOFF, 1.0, 1, false, 10, true);
        var b = vecRepo.expireQuarantineBounded(TENANT, fx.sibling(), fx.origin(), CUTOFF, 1.0, 1, false, 10, true);
        var c = vecRepo.expireQuarantineBounded(TENANT, fx.sibling(), fx.origin(), CUTOFF, 1.0, 1, false, 10, true);

        assertThat(a.expired()).isEqualTo(10L);
        assertThat(a.more()).isTrue();
        assertThat(a.refused()).isEqualTo(4L);
        assertThat(a.floorRefused()).isFalse();
        assertThat(b.expired()).isEqualTo(10L);
        assertThat(c.expired()).isEqualTo(5L);
        assertThat(c.more()).isFalse();
        assertThat(rowCount(fx.sibling())).as("only the protected rows are left").isEqualTo(4);
    }

    @Test
    void anUncountedRefusedIsNullThroughTheOutcome() {
        Fx fx = newFx();
        eligible(fx, "80", 3);
        protectedRows(fx, "10", 2);

        var out = vecRepo.expireQuarantineBounded(TENANT, fx.sibling(), fx.origin(), CUTOFF, 1.0, 1, false, 10, false);

        assertThat(out.expired()).isEqualTo(3L);
        assertThat(out.refused()).as("NULL, not zero").isNull();
    }

    @Test
    void theMarkerRunsInTheSameTransactionAfterTheSql_andARejectionRollsTheDeleteAndItsAuditBack() {
        Fx fx = newFx();
        eligible(fx, "80", 6);
        var order = new ArrayList<String>();
        var seenRowsInTx = new long[1];
        var visibleOutside = new long[] {-1};
        vecRepo.setAfterSweepSqlHookForTests(() -> {
            order.add("hook");
            visibleOutside[0] = rowCount(fx.sibling());
        });
        try {
            var out = vecRepo.expireQuarantineBounded(TENANT, fx.sibling(), fx.origin(), CUTOFF, 1.0, 1, false, 4, true,
                384, (ctx, outcome) -> {
                    order.add("marker");
                    seenRowsInTx[0] = ctx.fetchCount(CHUNKS,
                        CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(fx.sibling())));
                    assertThat(outcome.expired()).isEqualTo(4L);
                });
            assertThat(out.expired()).isEqualTo(4L);
        } finally {
            vecRepo.setAfterSweepSqlHookForTests(null);
        }
        assertThat(order).as("the hook (a test seam after the SQL) precedes the marker").containsExactly("hook", "marker");
        assertThat(seenRowsInTx[0]).as("the marker sees the page's delete").isEqualTo(2);
        assertThat(visibleOutside[0]).as("and nothing is committed while the hook holds the transaction").isEqualTo(6);
        assertThat(rowCount(fx.sibling())).isEqualTo(2);

        Fx lost = newFx();
        eligible(lost, "80", 6);
        assertThat(catchThrowable(() -> vecRepo.expireQuarantineBounded(
                TENANT, lost.sibling(), lost.origin(), CUTOFF, 1.0, 1, false, 4, true, 384,
                (ctx, outcome) -> { throw new IllegalStateException("lease lost"); })))
            .hasMessageContaining("lease lost");
        assertThat(rowCount(lost.sibling())).as("the rejected page deleted nothing").isEqualTo(6);
        assertThat(audits(lost)).as("and wrote no audit row").isEmpty();
    }

    @Test
    void anExplicitStatementBoundShorterThanTheWalkKillsThePageAndDeletesNothing() throws Exception {
        Fx fx = newFx();
        eligible(fx, "80", 5);
        Connection holder = holdGate(fx.sibling());
        try {
            Throwable t = catchThrowable(() -> vecRepo.expireQuarantineBounded(
                TENANT, fx.sibling(), fx.origin(), CUTOFF, 1.0, 1, false, 10, true, 300, 5_000));
            SQLException se = sqlException(t);
            assertThat((Object) se).as("%s", t).isNotNull();
            assertThat(se.getSQLState()).as("the bound the Java caller set, not the body's 2 s lock bound").isEqualTo("57014");
        } finally {
            holder.rollback();
            holder.close();
        }
        assertThat(rowCount(fx.sibling())).isEqualTo(5);
    }

    @Test
    void theRepositoryRefusesANonPositiveRowLimit() {
        Fx fx = newFx();
        eligible(fx, "80", 2);
        Throwable t = catchThrowable(() -> vecRepo.expireQuarantineBounded(
            TENANT, fx.sibling(), fx.origin(), CUTOFF, 1.0, 1, false, 0, true));
        assertThat((Object) sqlException(t)).isNotNull();
        assertThat(sqlException(t).getSQLState()).isEqualTo("22023");
        assertThat(rowCount(fx.sibling())).isEqualTo(2);
    }
}
