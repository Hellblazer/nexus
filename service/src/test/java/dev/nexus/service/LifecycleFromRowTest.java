// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.ChunkReaper.RunResult;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.QuarantineRestoreOutcome;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P2.5 (bead nexus-p2m1x.10), parts (a) and (b): a collection's lifecycle is the
 * {@code catalog_collections.lifecycle_state} column, set explicitly when the collection is registered, and the
 * engine's guards on an EXISTING collection read that column instead of testing the {@code quarantine-} name
 * prefix. A name with no row is still judged by its prefix, as before, so the row-less outcomes are pinned here
 * too. The TARGET side of the rename and rehome guards (no row exists yet) stays a name test and is pinned as such.
 *
 * <p>Fixtures set {@code lifecycle_state} through substrate SQL, never through the code under test, so a guard test
 * is red for the guard and not for the registration. Real PostgreSQL throughout; each test works in its own tenant.
 */
class LifecycleFromRowTest extends AtomicWriteTestBase {

    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-10-01T12:00:00Z"), ZoneOffset.UTC);
    private static final String ACTOR = "test-operator";
    private static final String SIBLING_DELETE_OP = "quarantine_collection_delete";

    private PgVectorRepository vectors;
    private ChashRepository chashRepo;
    private ReaperRepository store;
    private LadderRepository ladder;
    private Rdr192BackfillGate gate;

    @BeforeAll
    void wire() {
        vectors = new PgVectorRepository(tenantScope, embedder, embedder);
        chashRepo = new ChashRepository(tenantScope);
        store = new ReaperRepository(tenantScope);
        ladder = new LadderRepository(tenantScope);
        gate = new Rdr192BackfillGate(ladder, ChunkReaper.emptyTenantProbe(store));
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    private String newTenant() {
        return "lfr" + seq.incrementAndGet();
    }

    private void openGate(String tenant) {
        ladder.record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
    }

    /** A conformant, NON-prefixed name. */
    private String col(String tag) {
        return "knowledge__" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private static String prefixed(String name) {
        return "quarantine-" + name;
    }

    /** A registration payload as the upsert route takes it; {@code state} null leaves the key out. */
    private static Map<String, Object> reg(String name, String state) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("content_type", "knowledge");
        m.put("owner_id", "o");
        m.put("embedding_model", "minilm-l6-v2-384");
        m.put("model_version", "v1");
        if (state != null) m.put("lifecycle_state", state);
        return m;
    }

    /** Registers {@code name} through substrate SQL with exactly {@code state}, whatever the name says. */
    private void row(String tenant, String name, String state) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, name);
            int n = ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, state)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(name))).execute();
            assertThat(n).as("fixture row %s", name).isEqualTo(1);
        }
    }

    /** One chunk in an already registered collection, tagged for {@code originTag} when non-null. */
    private String chunk(String tenant, String collection, String seed, String originTag) throws Exception {
        String hex = Chash.ofText(collection + "/" + seed).toHex();
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("title", seed);
        meta.put("quarantined_at", "2026-09-01T00:00:00Z");
        if (originTag != null) meta.put("origin_collection", originTag);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), tenant, collection, List.of(hex),
                List.of(seed + " text"), List.of(new float[384]), List.of(meta));
        }
        return hex;
    }

    private String state(String tenant, String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(name)))
                .fetchOne(CATALOG_COLLECTIONS.LIFECYCLE_STATE);
        }
    }

    private int count(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(CHUNKS,
                CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)));
        }
    }

    private ChunkReaper reaper(String tenant) {
        return new ChunkReaper(store, vectors, repo, gate, () -> List.of(tenant), ChunkReaper.Settings.defaults(),
            CLOCK);
    }

    private static void assertNamesTheSanctionedVerbs(Throwable e, String name) {
        assertThat(e).isInstanceOf(IllegalArgumentException.class);
        assertThat(e.getMessage()).contains(name).contains("nx t3 quarantine restore").contains("nx t3 gc");
    }

    // ── (a) registration takes an explicit lifecycle_state ───────────────────

    @Test
    void registrationHonoursAnExplicitQuarantineState_forAQuarantinePrefixedName() throws Exception {
        String t = newTenant();
        String name = prefixed(col("a1"));

        repo.upsertCollection(t, reg(name, "quarantine"));

        assertThat(state(t, name)).isEqualTo("quarantine");
    }

    @Test
    void aNewRowWhoseNameAndEffectiveStateDisagreeIsRefused_inEveryForm_namingBoth() throws Exception {
        // Review N1: 'prefix iff quarantine'. A quarantine- name takes only 'quarantine', a plain name never does.
        String t = newTenant();
        String prefixedNoState = prefixed(col("a1a"));
        String prefixedLive = prefixed(col("a1b"));
        String plainQuarantine = col("a1d");

        for (var c : List.of(reg(prefixedNoState, null), reg(prefixedLive, "live"),
                             reg(plainQuarantine, "quarantine"))) {
            String name = (String) c.get("name");
            assertThatThrownBy(() -> repo.upsertCollection(t, c))
                .as("engine form " + name).isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(name).hasMessageContaining("lifecycle_state").hasMessageContaining("quarantine-")
                .hasMessageContaining("'" + (c.get("lifecycle_state") == null ? "live" : c.get("lifecycle_state")) + "'");
            assertThat(state(t, name)).as("nothing written for " + name).isNull();
        }
    }

    @Test
    void aRegistrationThatNamesNoStateIsLive_evenWhenItsContentTypeCarriesThePrefix() throws Exception {
        String t = newTenant();
        String plain = col("a2p");
        String byContentType = col("a2c");
        Map<String, Object> ct = reg(byContentType, null);
        ct.put("content_type", "quarantine-knowledge");

        repo.upsertCollection(t, reg(plain, null));
        repo.upsertCollection(t, ct);

        assertThat(state(t, plain)).isEqualTo("live");
        assertThat(state(t, byContentType)).as("a content_type prefix is not read at registration").isEqualTo("live");
    }

    @Test
    void aStateOutsideTheVocabularyIsRefusedAsABadRequest() {
        String t = newTenant();
        assertThatThrownBy(() -> repo.upsertCollection(t, reg(col("a3"), "frozen")))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("frozen");
    }

    @Test
    void theClientFormRefusesAnyStateButLiveAndANewQuarantineName_whileTheEngineFormStillMintsBoth()
            throws Exception {
        // Rules A and B (linda 2026-11-20): the client route registers live only and never a new quarantine- name;
        // the engine-internal two-argument form (the Java mint) is unchanged.
        String t = newTenant();
        String plainQ = col("a4c");
        String prefixedNew = prefixed(col("a4d"));
        String prefixedExisting = prefixed(col("a4e"));
        row(t, prefixedExisting, "quarantine");

        assertThatThrownBy(() -> repo.upsertCollection(t, reg(plainQ, "quarantine"), true))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("lifecycle_state");
        assertThatThrownBy(() -> repo.upsertCollection(t, reg(plainQ, "dormant"), true))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("lifecycle_state");
        assertThatThrownBy(() -> repo.upsertCollection(t, reg(prefixedNew, null), true))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(prefixedNew);
        assertThat(state(t, plainQ)).isNull();
        assertThat(state(t, prefixedNew)).isNull();

        repo.upsertCollection(t, reg(prefixedExisting, null), true);
        assertThat(state(t, prefixedExisting)).as("an existing row is not a NEW registration").isEqualTo("quarantine");

        repo.upsertCollection(t, reg(prefixedNew, "quarantine"));
        assertThat(state(t, prefixedNew)).as("the engine form mints a quarantine- row").isEqualTo("quarantine");
    }

    @Test
    void anExistingRowIsNotJudgedForDisagreement_whenReRegistered() throws Exception {
        // The agreement rule is for a NEW row; an existing one (an engine-minted sibling, a legacy row) is untouched.
        String t = newTenant();
        String legacyPrefixedLive = prefixed(col("a4f"));
        String plainQuarantine = col("a4g");
        row(t, legacyPrefixedLive, "live");
        row(t, plainQuarantine, "quarantine");

        repo.upsertCollection(t, reg(legacyPrefixedLive, null));
        repo.upsertCollection(t, reg(plainQuarantine, "live"));

        assertThat(state(t, legacyPrefixedLive)).isEqualTo("live");
        assertThat(state(t, plainQuarantine)).isEqualTo("quarantine");
    }

    @Test
    void anExistingRowKeepsItsStateWhenReRegistered() throws Exception {
        String t = newTenant();
        String name = col("a4");
        repo.upsertCollection(t, reg(name, "live"));

        repo.upsertCollection(t, reg(name, "quarantine"));

        assertThat(state(t, name)).as("an existing row is never re-pointed").isEqualTo("live");
    }

    @Test
    void theEngineQuarantineMintRegistersTheSiblingAsQuarantine() throws Exception {
        // The reaper's move (nexus.reaper_quarantine_chunks) inserts the sibling's registry row with
        // lifecycle_state 'quarantine' literally; nothing here derives it from the name.
        String t = newTenant();
        openGate(t);
        String origin = col("a5");
        row(t, origin, "live");
        chunk(t, origin, "orphan", null);

        RunResult run = reaper(t).runOnce(Duration.ZERO);

        assertThat(run.tenant(t).collection(origin).moved()).isEqualTo(1);
        assertThat(state(t, prefixed(origin))).isEqualTo("quarantine");
    }

    @Test
    void theOtherSqlMoverRegistersItsSiblingAsQuarantine_soTheLiveDefaultOfAnUpsertCannotHideAMoverRegression()
            throws Exception {
        // nexus.gc_quarantine_orphans (the client's move) inserts the sibling row itself; with the upsert's
        // prefix inference gone, this is what keeps a mover's sibling 'quarantine'.
        String t = newTenant();
        String origin = col("a5b");
        row(t, origin, "live");
        String orphan = chunk(t, origin, "orphan", null);
        ReapableFixtures.agePastGrace(pg, t, origin);   // gc selects orphans with the 30 day grace
        String sibling = prefixed(origin);

        var outcome = vectors.quarantineOrphans(t, origin, sibling, "2026-08-10T00:00:00Z", 20);

        assertThat(outcome.moved()).isEqualTo(1L);
        assertThat(count(t, sibling)).isEqualTo(1);
        assertThat(state(t, sibling)).isEqualTo("quarantine");
        assertThat(orphan).isNotNull();
    }

    @Test
    void anImportAcceptsAStateThatAgreesWithTheName_andAnAbsentStateFallsBackToThePrefix() throws Exception {
        String t = newTenant();
        String prefixedQuarantine = prefixed(col("a6q"));
        String plainLive = col("a6l");
        String plainDormant = col("a6d");
        String prefixedNoState = prefixed(col("a6p"));
        String plainNoState = col("a6n");
        String batchQuarantine = prefixed(col("a7q"));
        String batchNoState = prefixed(col("a7p"));

        repo.importCollection(t, reg(prefixedQuarantine, "quarantine"));
        repo.importCollection(t, reg(plainLive, "live"));
        repo.importCollection(t, reg(plainDormant, "dormant"));
        repo.importCollection(t, reg(prefixedNoState, null));
        repo.importCollection(t, reg(plainNoState, null));
        repo.importCollectionsBatch(t, List.of(reg(batchQuarantine, "quarantine"), reg(batchNoState, null)));

        assertThat(state(t, prefixedQuarantine)).as("an imported quarantine row is accepted").isEqualTo("quarantine");
        assertThat(state(t, plainLive)).isEqualTo("live");
        assertThat(state(t, plainDormant)).isEqualTo("dormant");
        assertThat(state(t, prefixedNoState)).as("the ETL fallback: the prefix").isEqualTo("quarantine");
        assertThat(state(t, plainNoState)).isEqualTo("live");
        assertThat(state(t, batchQuarantine)).isEqualTo("quarantine");
        assertThat(state(t, batchNoState)).isEqualTo("quarantine");
    }

    @Test
    void anImportedPlainNameWithAQuarantineContentTypeAndNoState_landsAsQuarantine() throws Exception {
        // linda 2026-11-20, reversing her round-1 ruling for the import path: a legacy row in an ETL import is a
        // migration source (RDR-229 item 9), and P2.1's CatalogEtlImportNonStrandingTest pins that this shape lands as
        // quarantine. On the import path a quarantine- content_type is prefix evidence, as the name is.
        String t = newTenant();
        String plain = col("a8c");
        String plainBatch = col("a8c2");
        Map<String, Object> legacy = reg(plain, null);
        legacy.put("content_type", "quarantine-knowledge");
        Map<String, Object> legacyBatch = reg(plainBatch, null);
        legacyBatch.put("content_type", "quarantine-knowledge");

        repo.importCollection(t, legacy);
        repo.importCollectionsBatch(t, List.of(legacyBatch));

        assertThat(state(t, plain)).isEqualTo("quarantine");
        assertThat(state(t, plainBatch)).isEqualTo("quarantine");
    }

    @Test
    void anImportedPlainNameWithAPlainContentTypeAndAnExplicitQuarantineState_isRefused() throws Exception {
        // The refusal linda kept: an explicit 'quarantine' on a plain name with a plain content_type has no legacy
        // evidence behind it, so it is a contradiction, not a migration source.
        String t = newTenant();
        String plain = col("a8h");
        Map<String, Object> bad = reg(plain, "quarantine");
        assertThat(bad.get("content_type").toString()).as("guard: the content_type is plain")
            .doesNotStartWith("quarantine-");

        assertThatThrownBy(() -> repo.importCollection(t, bad))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(plain)
            .hasMessageContaining("lifecycle_state=quarantine");
        assertThatThrownBy(() -> repo.importCollectionsBatch(t, List.of(bad)))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(plain).hasMessageContaining("1 row");
        assertThat(state(t, plain)).isNull();
    }

    @Test
    void anImportedPlainNameWithAQuarantineContentTypeAndAnExplicitLiveState_importsAsQuarantine_andIsNamed()
            throws Exception {
        // linda 2026-11-20: prefix evidence (here the content_type) plus 'live' imports as 'quarantine', named in the
        // result. No writer is known to produce this shape (CollectionRegistration#stateCorrectedOnImport's source
        // analysis); it is handled as the revived-sibling shape is.
        String t = newTenant();
        String plain = col("a8i");
        Map<String, Object> row = reg(plain, "live");
        row.put("content_type", "quarantine-knowledge");

        var result = repo.importCollectionsBatchReport(t, List.of(row));
        assertThat(result.stateCorrected()).containsExactly(plain);
        assertThat(state(t, plain)).isEqualTo("quarantine");

        String plainSingle = col("a8j");
        Map<String, Object> single = reg(plainSingle, "live");
        single.put("content_type", "quarantine-knowledge");
        repo.importCollection(t, single);
        assertThat(state(t, plainSingle)).as("the single-row import applies the same rule").isEqualTo("quarantine");
    }

    @Test
    void everyCorrectedRowGetsOneWarnLine_andOnlyOnceItIsStored() throws Exception {
        // linda 2026-11-20: the correction is visible as one WARN per row. The single-row path logs after its insert,
        // so a row the insert refuses is never reported as corrected.
        String t = newTenant();
        String revivedBatch = prefixed(col("a8w"));
        String revivedSingle = prefixed(col("a8x"));
        String clean = col("a8y");
        Map<String, Object> refused = reg(prefixed(col("a8z")), "live");
        refused.remove("model_version");
        refused.put("name", "quarantine-not-conformant-a8z");

        var logger = (ch.qos.logback.classic.Logger)
            org.slf4j.LoggerFactory.getLogger(dev.nexus.service.db.CatalogRepository.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        try {
            repo.importCollectionsBatch(t, List.of(reg(revivedBatch, "live"), reg(clean, "live")));
            repo.importCollection(t, reg(revivedSingle, "live"));
            assertThatThrownBy(() -> repo.importCollection(t, refused))
                .as("guard: the insert refuses a row with no version and a name that carries none")
                .isInstanceOf(IllegalArgumentException.class);
        } finally {
            logger.detachAppender(logs);
            logs.stop();
        }
        var warned = logs.list.stream()
            .filter(e -> e.getLevel() == ch.qos.logback.classic.Level.WARN)
            .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
            .filter(m -> m.contains("event=collection_import_state_corrected"))
            .toList();
        assertThat(warned).hasSize(2);
        assertThat(warned).anySatisfy(m -> assertThat(m).contains("name=" + revivedBatch));
        assertThat(warned).anySatisfy(m -> assertThat(m).contains("name=" + revivedSingle));
        assertThat(warned).noneSatisfy(m -> assertThat(m).contains(clean));
        assertThat(warned).noneSatisfy(m -> assertThat(m).contains("a8z"));
    }

    @Test
    void forTupleEmitsTheRowsOwnState_notAConstant() throws Exception {
        // linda 2026-11-20: the for_tuple row's lifecycle_state is the column, so a dormant row says dormant (and
        // resolves; the client lets dormant and disputed through). A hard-coded "live" fails here.
        String t = newTenant();
        String dormant = col("a8d2");
        repo.upsertCollection(t, reg(dormant, "dormant"));

        var row = repo.collectionForTuple(t, "knowledge", "o", "minilm-l6-v2-384");
        assertThat(row).as("guard: the dormant row resolves the tuple").isNotNull();
        assertThat(row.get("name")).isEqualTo(dormant);
        assertThat(row.get("lifecycle_state")).isEqualTo("dormant");
    }

    @Test
    void dormantAndDisputedAreAdminStatesOrthogonalToTheName_importAndEngineRegistrationAcceptThem() throws Exception {
        // Review: only 'quarantine-name with live' and 'plain name with quarantine' contradict. hygiene-002 marks rows
        // 'disputed', and an export holding one must still import.
        String t = newTenant();
        String prefixedDisputed = prefixed(col("a8d"));
        String plainDormant = col("a8e");
        String prefixedDormant = prefixed(col("a8f"));
        String plainDisputed = col("a8g2");

        repo.importCollection(t, reg(prefixedDisputed, "disputed"));
        repo.importCollectionsBatch(t, List.of(reg(plainDormant, "dormant")));
        repo.upsertCollection(t, reg(prefixedDormant, "dormant"));
        repo.upsertCollection(t, reg(plainDisputed, "disputed"));

        assertThat(state(t, prefixedDisputed)).isEqualTo("disputed");
        assertThat(state(t, plainDormant)).isEqualTo("dormant");
        assertThat(state(t, prefixedDormant)).isEqualTo("dormant");
        assertThat(state(t, plainDisputed)).isEqualTo("disputed");
    }

    @Test
    void aRevivedQuarantineSibling_quarantineNameWithStateLive_importsAsQuarantine_andIsNamed() throws Exception {
        // The shape the v0.1.109-111 ghost-sweep gap could leave on a local install (linda 2026-11-20): demoted to
        // dormant, revived to live, and missed by hygiene-005-3's dormant-only promotion.
        String t = newTenant();
        String revived = prefixed(col("a8"));
        String revivedSingle = prefixed(col("a8s"));
        String good = col("a8g");

        var result = repo.importCollectionsBatchReport(t, List.of(reg(good, "live"), reg(revived, "live")));
        assertThat(result.imported()).isEqualTo(2);
        assertThat(result.stateCorrected()).as("the corrected row is named, the clean one is not").containsExactly(revived);
        assertThat(state(t, revived)).isEqualTo("quarantine");
        assertThat(state(t, good)).isEqualTo("live");

        repo.importCollection(t, reg(revivedSingle, "live"));
        assertThat(state(t, revivedSingle)).as("the single-row import applies the same rule").isEqualTo("quarantine");

        var again = repo.importCollectionsBatchReport(t, List.of(reg(revived, "live")));
        assertThat(again.stateCorrected()).as("an existing row is skipped, so nothing is corrected twice").isEmpty();
    }

    @Test
    void aNewImportedRowWhoseNameIsPlainAndStateSaysQuarantineIsRefused_namingTheRow_andNothingIsStored() throws Exception {
        String t = newTenant();
        String bad = col("a9");
        String bad2 = col("a9b");

        assertThatThrownBy(() -> repo.importCollection(t, reg(bad, "quarantine")))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(bad).hasMessageContaining("lifecycle_state");
        assertThatThrownBy(() -> repo.importCollectionsBatch(t, List.of(reg(bad, "quarantine"), reg(bad2, "quarantine"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(bad).hasMessageContaining(bad2)
            .hasMessageContaining("2 row");
        assertThat(state(t, bad)).isNull();
    }

    @Test
    void aDisagreeingRowThatIsAlreadyRegisteredIsSkippedNotRefused() throws Exception {
        String t = newTenant();
        String name = prefixed(col("a10"));
        row(t, name, "quarantine");

        repo.importCollection(t, reg(name, "live"));
        repo.importCollectionsBatch(t, List.of(reg(name, "live")));

        assertThat(state(t, name)).as("an import never overwrites an existing row").isEqualTo("quarantine");
    }

    // ── (b) guards on an existing collection read the row ────────────────────

    @Test
    void storeDeleteRefusesACollectionWhoseRowIsQuarantine_whateverItsName() throws Exception {
        String t = newTenant();
        String q = col("b1");
        row(t, q, "quarantine");
        String hex = chunk(t, q, "keep", null);

        assertThatThrownBy(() -> vectors.delete(t, q, List.of(hex)))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, q));
        assertThat(count(t, q)).isEqualTo(1);
    }

    @Test
    void storeDeleteAdmitsAPrefixedNameWhoseRowIsLive() throws Exception {
        String t = newTenant();
        String c = prefixed(col("b2"));
        row(t, c, "live");
        String hex = chunk(t, c, "gone", null);

        int deleted = vectors.delete(t, c, List.of(hex));

        assertThat(deleted).isEqualTo(1);
        assertThat(count(t, c)).isZero();
    }

    @Test
    void storeDeleteOfARowLessNameIsJudgedByItsPrefixAsBefore() {
        String t = newTenant();
        String rowLessPrefixed = prefixed(col("b3"));

        assertThatThrownBy(() -> vectors.delete(t, rowLessPrefixed, List.of(Chash.ofText("x").toHex())))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, rowLessPrefixed));
        assertThatThrownBy(() -> vectors.delete(t, col("b3p"), List.of(Chash.ofText("x").toHex())))
            .as("a row-less plain name is not a quarantine name: it fails as an unregistered collection")
            .isNotInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void renameRefusesASourceWhoseRowIsQuarantine_butAdmitsAPrefixedSourceWhoseRowIsLive() throws Exception {
        String t = newTenant();
        String q = col("b4q");
        row(t, q, "quarantine");
        String l = prefixed(col("b4l"));
        row(t, l, "live");
        String target = col("b4t");

        assertThatThrownBy(() -> repo.renameCollection(t, q, col("b4n")))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, q));
        Map<String, Integer> counts = repo.renameCollection(t, l, target);

        assertThat(counts).containsKey("catalog_collections_superseded");
        assertThat(state(t, target)).isEqualTo("live");
    }

    @Test
    void theRenameTargetIsStillJudgedByItsName_noRowExistsYet() throws Exception {
        String t = newTenant();
        String x = col("b5x");
        row(t, x, "live");
        String rowLessTarget = prefixed(col("b5t"));

        assertThatThrownBy(() -> repo.renameCollection(t, x, rowLessTarget))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, rowLessTarget));
        assertThatThrownBy(() -> chashRepo.renameCollection(t, x, rowLessTarget))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, rowLessTarget));
        assertThatThrownBy(() -> repo.rehomeCollection(t, x, rowLessTarget))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, rowLessTarget));
    }

    @Test
    void aTargetWhoseRowIsQuarantineIsRefusedWhateverItsName_andAPrefixedTargetWhoseRowIsLiveIsAdmitted()
            throws Exception {
        // Review S1: the TARGET guards read the row too (a row-less target keeps the prefix rule).
        String t = newTenant();
        String x = col("b5a");
        row(t, x, "live");
        chunk(t, x, "one", null);
        String plainQuarantine = col("b5q");
        row(t, plainQuarantine, "quarantine");

        assertThatThrownBy(() -> repo.rehomeCollection(t, x, plainQuarantine))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, plainQuarantine));
        assertThatThrownBy(() -> repo.renameCollection(t, x, plainQuarantine))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, plainQuarantine));
        assertThatThrownBy(() -> chashRepo.renameCollection(t, x, plainQuarantine))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, plainQuarantine));
        assertThat(count(t, x)).as("nothing moved").isEqualTo(1);

        String prefixedLive = prefixed(col("b5l"));
        row(t, prefixedLive, "live");
        var moved = repo.rehomeCollection(t, x, prefixedLive);

        assertThat(moved.movedChunks()).isEqualTo(1);
        assertThat(count(t, prefixedLive)).isEqualTo(1);
    }

    @Test
    void theChashRenameRefusesASourceWhoseRowIsQuarantine() throws Exception {
        String t = newTenant();
        String q = col("b6q");
        row(t, q, "quarantine");
        chunk(t, q, "keep", null);

        assertThatThrownBy(() -> chashRepo.renameCollection(t, q, col("b6n")))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, q));
        assertThat(count(t, q)).isEqualTo(1);
    }

    @Test
    void rehomeRefusesASourceWhoseRowIsQuarantine() throws Exception {
        String t = newTenant();
        String q = col("b7q");
        row(t, q, "quarantine");
        String x = col("b7x");
        row(t, x, "live");
        chunk(t, q, "keep", null);

        assertThatThrownBy(() -> repo.rehomeCollection(t, q, x))
            .satisfies(e -> assertNamesTheSanctionedVerbs(e, q));
        assertThat(count(t, q)).isEqualTo(1);
    }

    @Test
    void deleteOfACollectionWhoseRowIsQuarantineIsTheAuditedQuarantineDelete_whateverItsName() throws Exception {
        String t = newTenant();
        String q = col("b8");
        row(t, q, "quarantine");
        String a = chunk(t, q, "one", null);
        String b = chunk(t, q, "two", null);

        Map<String, Integer> counts = repo.deleteCollection(t, q);

        assertThat(counts.get("quarantine_chunks")).isEqualTo(2);
        assertThat(count(t, q)).isZero();
        var rows = repo.listGcAudit(t, null, SIBLING_DELETE_OP, 500, 0);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).get("collection")).isEqualTo(q);
        assertThat(rows.get(0).get("chashes").toString()).contains(a).contains(b);
    }

    @Test
    void deleteOfAPrefixedNameWhoseRowIsLiveIsAnOriginDelete_notAQuarantineDelete() throws Exception {
        String t = newTenant();
        String c = prefixed(col("b9"));
        row(t, c, "live");
        chunk(t, c, "one", null);

        Map<String, Integer> counts = repo.deleteCollection(t, c);

        assertThat(counts.get("chunks")).isEqualTo(1);
        assertThat(counts.get("quarantine_chunks")).as("no quarantine rows were touched").isZero();
        assertThat(repo.listGcAudit(t, null, SIBLING_DELETE_OP, 500, 0)).isEmpty();
    }

    @Test
    void aRestoreNamingASiblingWhoseRowIsQuarantineAcceptsItWithoutThePrefix() throws Exception {
        String t = newTenant();
        String origin = col("b10o");
        row(t, origin, "live");
        String sibling = col("b10s");
        row(t, sibling, "quarantine");
        String hex = chunk(t, sibling, "back", origin);

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, origin, sibling, List.of(hex), ACTOR, false);

        assertThat(out.restored()).containsExactly(hex);
        assertThat(count(t, origin)).isEqualTo(1);
    }

    @Test
    void aRestoreNamingALiveSiblingIsRefusedWhateverItsName_andARowLessPlainNameAsNotAQuarantineName()
            throws Exception {
        String t = newTenant();
        String origin = col("b11o");
        row(t, origin, "live");
        String liveSibling = prefixed(col("b11s"));
        row(t, liveSibling, "live");
        String hex = chunk(t, liveSibling, "x", origin);

        assertThatThrownBy(() -> vectors.quarantineRestore(t, origin, liveSibling, List.of(hex), ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("is not a quarantine collection");
        assertThatThrownBy(() -> vectors.quarantineRestore(t, origin, col("b11p"), List.of(hex), ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("must be a quarantine- sibling");
    }

    @Test
    void aRestoreIntoAnOriginWhoseRowIsQuarantineIsRefused_whateverItsName() throws Exception {
        String t = newTenant();
        String q = col("b12");
        row(t, q, "quarantine");
        String hex = chunk(t, q, "x", null);

        assertThatThrownBy(() -> vectors.quarantineRestore(t, q, q, List.of(hex), ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("the origin collection must be a non-quarantine collection");
    }

    // ── (b) the reaper splits its collections by the row, not by the prefix ──

    @Test
    void theReaperTreatsACollectionWhoseRowIsQuarantineAsASibling_whateverItsName() throws Exception {
        String t = newTenant();
        openGate(t);
        String q = col("c1");
        row(t, q, "quarantine");
        String hex = chunk(t, q, "aged", null);

        RunResult run = reaper(t).runOnce(Duration.ZERO);

        assertThat(run.tenant(t).collection(q)).as("a quarantine collection is never reaped").isNull();
        assertThat(run.tenant(t).expiry(q)).as("its expiry ran").isNotNull();
        assertThat(count(t, q)).isEqualTo(1);
        assertThat(hex).isNotNull();
    }

    @Test
    void theReaperVisitsAPrefixedNameWhoseRowIsLiveAsALiveCollection() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = prefixed(col("c2"));
        row(t, c, "live");
        chunk(t, c, "orphan", null);

        RunResult run = reaper(t).runOnce(Duration.ZERO);

        assertThat(run.tenant(t).collection(c)).isNotNull();
        assertThat(run.tenant(t).collection(c).refusal()).isNull();
        assertThat(run.tenant(t).expiry(c)).isNull();
    }
}
