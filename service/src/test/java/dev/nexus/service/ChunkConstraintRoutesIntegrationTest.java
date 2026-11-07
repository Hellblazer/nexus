// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.ChunkReaper.RunResult;
import dev.nexus.service.ChunkReaper.Settings;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.vectors.ChunkConstraintAdmin;
import dev.nexus.service.vectors.ChunkMetadataKeys;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * nexus-6pfgb.43 slice B: the table-wide routes {@code add-constraints} and {@code validate-constraints}, through the
 * real service and a real PostgreSQL, in the order an operator runs them. One container, so the steps are ordered: the
 * constraints, once added, are on {@code nexus.chunks} for every test after them.
 *
 * <p>Five tenants. {@code cc-a} holds retired keys (to be swept), a quarantine row that carries them and a chunk the
 * real reaper moved to quarantine with its retired document keys; {@code cc-b} is clean; {@code cc-c} is clean except
 * one row whose {@code chunk_text_hash} is not its chash; {@code cc-ghost} holds chunks with retired keys and NO
 * {@code service_tokens} row (the tenant a token-list walk never sees); {@code cc-blocked} holds one retired-key row in a
 * collection the sweep must leave alone (its census reads legacy-unmanifested), the row an add freezes.
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ChunkConstraintRoutesIntegrationTest extends ChunkKeysTestBase {

    private static final String A = "cc-a";
    private static final String B = "cc-b";
    private static final String C = "cc-c";
    private static final String GHOST = "cc-ghost";
    private static final String D = "cc-blocked";
    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-10-01T12:00:00Z"), ZoneOffset.UTC);
    private static final List<String> FIVE = List.of("chunks_metadata_keys_check", "chunks_chunk_text_hash_check",
        "chunks_embedding_model_check", "chunks_quarantine_keys_check", "chunks_chash_octet_check");

    private String tokenA;
    private String liveA;
    private String quarantineA;
    private String liveC;
    private String mismatchedHash;
    private String liveGhost;
    private String liveD;
    private String blockedRow;
    private String reaperOrigin;
    private String reaperChash;
    private String restoreOrigin;
    private String restoreChash;

    private ChunkConstraintAdmin admin() {
        return new ChunkConstraintAdmin(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()));
    }

    @org.junit.jupiter.api.BeforeAll
    void seedTenants() throws Exception {
        tokenA = tenantToken(A);
        tenantToken(B);
        tenantToken(C);
        tenantToken(D);
        liveA = conformant("cc-a");
        quarantineA = "quarantine-" + liveA;
        Map<String, Map<String, Object>> a = new LinkedHashMap<>();
        for (int i = 1; i <= 3; i++) {
            Map<String, Object> m = keyed(A, liveA, "a" + i, "title", "t" + i, "tags", "x,y", "doc_id", "gone");
            a.put("a" + i, m);
        }
        seed(A, liveA, a);
        seed(A, quarantineA, Map.of("qa1", keyed(A, quarantineA, "qa1", "title", "t", "quarantined_at",
            "2026-01-02T00:00:00Z", "origin_collection", liveA)));
        seed(B, conformant("cc-b"), Map.of("b1", keyed(B, conformant("cc-b"), "b1"),
            "b2", keyed(B, conformant("cc-b"), "b2")));
        liveC = conformant("cc-c");
        Map<String, Object> badHash = new LinkedHashMap<>(keyed(C, liveC, "c1"));
        badHash.put("chunk_text_hash", "0".repeat(64));
        seed(C, liveC, Map.of("c1", badHash, "c2", keyed(C, liveC, "c2")));
        mismatchedHash = hexOf(C, liveC, "c1");

        // A tenant with chunk rows and no service_tokens row: seed() writes chunks and registers the collection, and
        // issues no token.
        liveGhost = conformant("cc-ghost");
        seed(GHOST, liveGhost, Map.of("g1", keyed(GHOST, liveGhost, "g1", "title", "g", "doc_id", "gone"),
            "g2", keyed(GHOST, liveGhost, "g2", "tags", "x")));

        // A collection the sweep leaves alone: a live document found through the chunk's own forward key has no
        // manifest row (the census's legacy-unmanifested bucket).
        liveD = conformant("cc-blocked");
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), D, "census.d");
        }
        seed(D, liveD, Map.of("d1", keyed(D, liveD, "d1", "catalog_doc_id", "census.d", "title", "held")));
        blockedRow = hexOf(D, liveD, "d1");

        reaperOrigin = "code" + "_".repeat(2) + "cc-r" + "_".repeat(2) + MODEL + "_".repeat(2) + "v1";
        reaperChash = reaperQuarantined("cc-reaper", reaperOrigin, "gc-restore", true);
        restoreOrigin = "code" + "_".repeat(2) + "cc-q" + "_".repeat(2) + MODEL + "_".repeat(2) + "v1";
        restoreChash = reaperQuarantined("cc-reaper", restoreOrigin, "quarantine-restore", false);
    }

    /**
     * A chunk holding retired document keys, moved to its quarantine sibling by the real reaper. With {@code
     * reReference} a live document of the origin names it again (the foreign key bypassed, the one way a manifest row
     * can name a chunk that lives only in quarantine), which is what {@code gc_restore_rereferenced} needs.
     */
    private String reaperQuarantined(String tenant, String origin, String slug, boolean reReference) throws Exception {
        tenantToken(tenant);
        String doc = tenant + "." + slug;
        String chash = Chash.ofText(origin + "/" + slug).toHex();
        Map<String, Object> legacy = new LinkedHashMap<>();
        legacy.put("title", slug);
        legacy.put("catalog_doc_id", doc);
        legacy.put("doc_id", doc);
        legacy.put("chunk_index", 0);
        legacy.put("content_hash", "h-" + slug);
        legacy.put("tags", "a,b");
        legacy.put("chunk_text_hash", chash);
        legacy.put("embedding_model", MODEL);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            PgContainerHelper.insertChunks(ctx, tenant, origin, List.of(chash), List.of(slug + " text"),
                embedder.embed(List.of(slug + " text")), List.of(legacy));
        }
        var ladder = new LadderRepository(tenantScope);
        ladder.record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
        RunResult run = new ChunkReaper(new ReaperRepository(tenantScope), vectors, repo,
            new Rdr192BackfillGate(ladder), () -> List.of(tenant), Settings.defaults(), CLOCK).runOnce(Duration.ZERO);
        assertThat(run.tenant(tenant).collection(origin).moved()).as("fixture: the reaper moved it").isEqualTo(1);
        assertThat(metadataOf(tenant, "quarantine-" + origin, chash)).as("fixture: it carries its retired keys")
            .containsKeys("title", "doc_id", "catalog_doc_id", "quarantined_by");
        if (reReference) {
            repo.upsertDocument(tenant, Map.of("tumbler", doc, "title", slug, "content_type", "code", "corpus", "code",
                "physical_collection", origin, "chunk_count", 1));
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                PgContainerHelper.dropCatalogChunksFk(su);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                        CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                    .values(tenant, doc, 0, Chash.fromHex(chash).toBytes(), origin,
                        PgContainerHelper.collectionModel(ctx, tenant, origin)).execute();
                PgContainerHelper.readdCatalogChunksFkNotValid(su);
            }
        }
        return chash;
    }

    private static List<String> tenantNames(JsonNode body) {
        List<String> out = new ArrayList<>();
        body.path("tenants").forEach(t -> out.add(t.path("tenant").asText()));
        return out;
    }

    private static List<String> checkedNames(JsonNode body) {
        List<String> out = new ArrayList<>();
        body.path("tenants_checked").forEach(t -> out.add(t.asText()));
        return out;
    }

    private HttpResponse<String> add(String bearer, Object... extra) throws Exception {
        return post("/v1/admin/chunk-keys/add-constraints", bearer, plan(extra));
    }

    private HttpResponse<String> validate(String bearer) throws Exception {
        return post("/v1/admin/chunk-keys/validate-constraints", bearer, plan());
    }

    private JsonNode tenantRow(JsonNode body, String tenant) {
        for (JsonNode t : body.path("tenants")) {
            if (tenant.equals(t.path("tenant").asText())) {
                return t;
            }
        }
        throw new AssertionError("tenant " + tenant + " is not named in " + body);
    }

    private List<String> presentConstraints() {
        return admin().states().stream().filter(ChunkConstraintAdmin.State::present)
            .map(ChunkConstraintAdmin.State::name).toList();
    }

    // ---- 0. before the sweep: a legacy-keyed row is still written --------------------------------------------

    @Test
    @Order(0)
    void aLegacyKeyedRowIsStillUpsertedBeforeTheSweep_noCheckExistsYet() throws Exception {
        String a1 = hexOf(A, liveA, "a1");

        vectors.upsertChunks(A, liveA, List.of(a1), List.of("a1 text"),
            List.of(Map.<String, Object>of("source_path", "/legacy/writer", "title", "t1")));

        assertThat(metadataOf(A, liveA, a1)).as("the write landed, merged onto the row").containsEntry("source_path",
            "/legacy/writer");
        assertThat(presentConstraints()).containsExactly("chunks_chash_octet_check");
    }

    // ---- 1. refusals before anything is swept -------------------------------------------------------------------

    @Test
    @Order(10)
    void addIsRefusedWhileATenantIsNotReady_namingEach_andAddsNothing() throws Exception {
        HttpResponse<String> r = add(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(body.path("reason").asText()).isEqualTo("tenant_unswept");
        assertThat(tenantNames(body)).as("the unswept tenants and the one whose hash differs; not the clean one nor "
            + "the one whose only retired row is in a blocked collection").containsExactlyInAnyOrder(A, C, GHOST);
        assertThat(tenantRow(body, A).path("unswept_rows").asLong()).isEqualTo(3);
        assertThat(tenantRow(body, C).path("chunk_text_hash_mismatch").asLong()).as("B3: an equality violation").isEqualTo(1);
        assertThat(tenantNames(body)).doesNotContain(B);
        assertThat(presentConstraints()).containsExactly("chunks_chash_octet_check");
    }

    @Test
    @Order(11)
    void aTenantWithChunksAndNoServiceTokensRowIsCheckedAndNamed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(SERVICE_TOKENS, SERVICE_TOKENS.TENANT_ID.eq(GHOST)))
                .as("fixture: the tenant has no token row, so a token-list walk cannot see it").isZero();
        }

        HttpResponse<String> r = add(ROOT);

        assertThat(r.statusCode()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(tenantNames(body)).contains(GHOST);
        assertThat(tenantRow(body, GHOST).path("unswept_rows").asLong()).isEqualTo(2);
        assertThat(checkedNames(body)).as("the checked set is named: the default tenant, the known ones and this one")
            .contains(TenantConstants.DEFAULT_TENANT, A, B, C, D, GHOST);
        assertThat(body.path("table_rows_examined").asLong()).as("the probe examined rows").isGreaterThanOrEqualTo(8);
        assertThat(presentConstraints()).containsExactly("chunks_chash_octet_check");
        // validate sees it too, and the blocked collection's row: VALIDATE cannot pass over it.
        JsonNode v = json(validate(ROOT));
        assertThat(tenantNames(v)).contains(GHOST, D);
    }

    // ---- 1a. the admin session must see every row (round 2, F1) -----------------------------------------------

    private static final String BLIND = "blind_admin";
    private static final String BLIND_PASS = "blindadminpw";

    private ChunkConstraintAdmin blindAdmin() {
        return new ChunkConstraintAdmin(new AdminConnection(pg.getJdbcUrl(), BLIND, BLIND_PASS));
    }

    /** A login role with SELECT on the table, NOSUPERUSER NOBYPASSRLS, and no policy naming it: it reads nothing. */
    private void createBlindRole() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.createLoginRole(su, BLIND, BLIND_PASS, false);
            PgContainerHelper.grantSchemaUsage(su, "nexus", BLIND);
            DSL.using(su, SQLDialect.POSTGRES).grant(DSL.privilege("SELECT")).on(CHUNKS).to(DSL.role(BLIND)).execute();
        }
    }

    @Test
    @Order(12)
    void anAdminRoleThatCannotSeeEveryRowIsRefusedBeforeAnythingIsDone_notTrustedToReadZero() throws Exception {
        createBlindRole();
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(CHUNKS)).as("fixture: the table has rows")
                .isGreaterThan(0);
        }
        try (Connection blind = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), BLIND, BLIND_PASS)) {
            assertThat(DSL.using(blind, SQLDialect.POSTGRES).fetchCount(CHUNKS))
                .as("fixture: under FORCE RLS this role reads ZERO rows, which is what the old probe took for clean")
                .isZero();
        }
        service.chunkConstraintAdmin(blindAdmin());
        try {
            for (HttpResponse<String> r : List.of(add(ROOT), validate(ROOT))) {
                assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
                assertThat(json(r).path("reason").asText()).isEqualTo("admin_cannot_see_all_tenants");
                assertThat(json(r).path("error").asText()).contains(BLIND);
            }
            assertThat(presentConstraints()).as("nothing was added").containsExactly("chunks_chash_octet_check");
        } finally {
            service.chunkConstraintAdmin(admin());
        }
    }

    @Test
    @Order(13)
    void anAdminRoleWithAnUnrestrictedSelectPolicyIsTrusted_aRestrictiveOrNarrowerPolicyDoesNotCount() throws Exception {
        service.chunkConstraintAdmin(blindAdmin());
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // A RESTRICTIVE policy with a true qual grants nothing: restrictive policies only narrow.
            PgContainerHelper.createPolicyUsingTrue(su, CHUNKS, "blind_restrictive", PgContainerHelper.PolicyKind.RESTRICTIVE,
                "SELECT", BLIND);
            try {
                assertThat(add(ROOT).statusCode()).as("a restrictive policy is not a grant").isEqualTo(503);
            } finally {
                PgContainerHelper.dropPolicy(su, CHUNKS, "blind_restrictive");
            }
            PgContainerHelper.createPolicyUsingTrue(su, CHUNKS, "blind_read_all", PgContainerHelper.PolicyKind.PERMISSIVE,
                "SELECT", BLIND);
            try {
                HttpResponse<String> r = add(ROOT);
                assertThat(r.statusCode()).as("the policy lets it see every row, so it reaches the tenant check: "
                    + r.body()).isEqualTo(409);
                assertThat(json(r).path("reason").asText()).isEqualTo("tenant_unswept");
                assertThat(json(r).path("table_rows_examined").asLong()).as("and it read the rows").isGreaterThan(0);
            } finally {
                PgContainerHelper.dropPolicy(su, CHUNKS, "blind_read_all");
            }
        } finally {
            service.chunkConstraintAdmin(admin());
        }
    }

    @Test
    @Order(13)
    void aRestrictivePolicyThatIsNotTrueOverridesAnUnrestrictedSelectPolicy() throws Exception {
        // nexus-gy3vq: Postgres ANDs an applicable restrictive policy onto the permissive ones, so the
        // permissive true policy no longer means the role reads every row.
        service.chunkConstraintAdmin(blindAdmin());
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.createPolicyUsingTrue(su, CHUNKS, "blind_read_all2", PgContainerHelper.PolicyKind.PERMISSIVE,
                "SELECT", BLIND);
            try {
                PgContainerHelper.createPolicy(su, CHUNKS, "blind_narrowed", PgContainerHelper.PolicyKind.RESTRICTIVE,
                    "SELECT", BLIND, false);
                try {
                    HttpResponse<String> r = add(ROOT);
                    assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
                    assertThat(json(r).path("reason").asText()).isEqualTo("admin_cannot_see_all_tenants");
                    assertThat(r.body()).contains("blind_narrowed");
                } finally {
                    PgContainerHelper.dropPolicy(su, CHUNKS, "blind_narrowed");
                }
            } finally {
                PgContainerHelper.dropPolicy(su, CHUNKS, "blind_read_all2");
            }
        } finally {
            service.chunkConstraintAdmin(admin());
        }
    }

    /** A restrictive policy that narrows nothing the role reads; each must leave the trusted role trusted. */
    private enum Harmless { RESTRICTIVE_TRUE_FOR_THE_ROLE, RESTRICTIVE_FALSE_FOR_ANOTHER_ROLE, RESTRICTIVE_FALSE_FOR_UPDATE_ONLY }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.EnumSource(Harmless.class)
    @Order(13)
    void aRestrictivePolicyThatDoesNotNarrowTheRolesSelectDoesNotRefuseIt(Harmless kind) throws Exception {
        // nexus-gy3vq, the other direction: only a restrictive policy that applies to the role's SELECT and is
        // not true refuses it.
        String other = "gy3vq_other_role";
        service.chunkConstraintAdmin(blindAdmin());
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            if (kind == Harmless.RESTRICTIVE_FALSE_FOR_ANOTHER_ROLE) {
                PgContainerHelper.createLoginRole(su, other, "gy3vqotherpw", false);
            }
            PgContainerHelper.createPolicyUsingTrue(su, CHUNKS, "blind_read_all3", PgContainerHelper.PolicyKind.PERMISSIVE,
                "SELECT", BLIND);
            try {
                switch (kind) {
                    case RESTRICTIVE_TRUE_FOR_THE_ROLE -> PgContainerHelper.createPolicy(su, CHUNKS, "blind_harmless",
                        PgContainerHelper.PolicyKind.RESTRICTIVE, "SELECT", BLIND, true);
                    case RESTRICTIVE_FALSE_FOR_ANOTHER_ROLE -> PgContainerHelper.createPolicy(su, CHUNKS, "blind_harmless",
                        PgContainerHelper.PolicyKind.RESTRICTIVE, "SELECT", other, false);
                    case RESTRICTIVE_FALSE_FOR_UPDATE_ONLY -> PgContainerHelper.createPolicy(su, CHUNKS, "blind_harmless",
                        PgContainerHelper.PolicyKind.RESTRICTIVE, "UPDATE", BLIND, false);
                }
                try {
                    HttpResponse<String> r = add(ROOT);
                    assertThat(r.statusCode()).as("trusted, so it reaches the tenant check: " + r.body()).isEqualTo(409);
                    assertThat(json(r).path("reason").asText()).isEqualTo("tenant_unswept");
                } finally {
                    PgContainerHelper.dropPolicy(su, CHUNKS, "blind_harmless");
                }
            } finally {
                PgContainerHelper.dropPolicy(su, CHUNKS, "blind_read_all3");
                if (kind == Harmless.RESTRICTIVE_FALSE_FOR_ANOTHER_ROLE) {
                    PgContainerHelper.dropRoleRevokingGrants(su, other);
                }
            }
        } finally {
            service.chunkConstraintAdmin(admin());
        }
    }

    @Test
    @Order(14)
    void aProbeThatReadsFewerRowsThanATenantScopedCountIsRefused_evenWhenTheRoleLooksUnrestricted() throws Exception {
        var conn = new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
        var shortOne = new ChunkConstraintAdmin(conn) {
            @Override
            public List<TenantProbe> probe(dev.nexus.service.vectors.ChunkKeyPlan p) {
                return super.probe(p).stream().map(t -> A.equals(t.tenant())
                    ? new TenantProbe(t.tenant(), t.rows() - 1, t.retiredRows(), t.hashMismatch(), t.modelMismatch(),
                        t.wrongWidth(), t.quarantineBreach()) : t).toList();
            }
        };
        var missingOne = new ChunkConstraintAdmin(conn) {
            @Override
            public List<TenantProbe> probe(dev.nexus.service.vectors.ChunkKeyPlan p) {
                return super.probe(p).stream().filter(t -> !B.equals(t.tenant())).toList();
            }
        };
        for (var lying : List.of(shortOne, missingOne)) {
            service.chunkConstraintAdmin(lying);
            try {
                for (HttpResponse<String> r : List.of(add(ROOT), validate(ROOT))) {
                    assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
                    assertThat(json(r).path("reason").asText()).isEqualTo("admin_probe_undercount");
                    assertThat(json(r).path("error").asText()).contains(lying == shortOne ? A : B);
                }
            } finally {
                service.chunkConstraintAdmin(admin());
            }
        }
        assertThat(presentConstraints()).as("nothing was added").containsExactly("chunks_chash_octet_check");
    }

    @Test
    @Order(20)
    void validateIsRefusedWhileATenantStillBreaksAConstraint_namingEach() throws Exception {
        HttpResponse<String> r = validate(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(body.path("reason").asText()).isEqualTo("tenant_violates_constraint");
        assertThat(tenantNames(body)).containsExactlyInAnyOrder(A, C, GHOST, D);
        assertThat(tenantRow(body, A).path("retired_key_rows").asLong()).isEqualTo(3);
        assertThat(tenantRow(body, D).path("retired_key_rows").asLong()).as("a blocked collection's row counts here")
            .isEqualTo(1);
        assertThat(tenantRow(body, C).path("chunk_text_hash_mismatch").asLong()).isEqualTo(1);
    }

    @Test
    @Order(30)
    void theTableWideRoutesNeedTheRootToken() throws Exception {
        assertThat(add(tokenA).statusCode()).isEqualTo(403);
        assertThat(validate(tokenA).statusCode()).isEqualTo(403);
        assertThat(add(null).statusCode()).isEqualTo(401);
        assertThat(post("/v1/admin/chunk-keys/add-constraints", token(A, TokenStore.SCOPE_DATA), plan())
            .statusCode()).isEqualTo(403);
    }

    // ---- 1b. the engine pins the plan (B2) ----------------------------------------------------------------------

    @Test
    @Order(35)
    void aPlanThatDiffersFromTheEnginesPinsIsRefusedBeforeAnythingIsDone() throws Exception {
        List<String> allowed = new ArrayList<>(ChunkMetadataKeys.CHUNK);
        allowed.addAll(ROW_KEYS);
        Map<String, Map<String, Object>> variants = new LinkedHashMap<>();
        variants.put("taxonomy omitted", plan("bypass_prefixes", List.of("quarantine-")));
        variants.put("an extra prefix", plan("bypass_prefixes", List.of(TAXONOMY_PREFIX, "quarantine-",
            "knowledge" + "_".repeat(2))));
        List<String> unknown = new ArrayList<>(allowed);
        unknown.add("zz_extra");
        variants.put("an unknown key", plan("allowed_keys", unknown));
        List<String> dropped = new ArrayList<>(allowed);
        dropped.remove("origin_collection");
        variants.put("a dropped ROW key", plan("allowed_keys", dropped));
        Map<String, Object> noImplication = plan();
        noImplication.remove("quarantine_implies");
        variants.put("no quarantine implication", noImplication);
        variants.put("the implication reversed", plan("quarantine_implies", List.of("quarantined_at", "quarantined_by")));

        for (var e : variants.entrySet()) {
            for (String route : List.of("add-constraints", "validate-constraints")) {
                HttpResponse<String> r = post("/v1/admin/chunk-keys/" + route, ROOT, e.getValue());
                assertThat(r.statusCode()).as(e.getKey() + " on " + route + ": " + r.body()).isEqualTo(400);
            }
            assertThat(sweep(tokenA, e.getValue()).statusCode()).as(e.getKey() + " on sweep").isEqualTo(400);
        }
        assertThat(presentConstraints()).as("nothing was added").containsExactly("chunks_chash_octet_check");
        assertThat(retiredRowCount(A, liveA)).as("and nothing was swept").isEqualTo(3);
    }

    // ---- 2. sweep, add ------------------------------------------------------------------------------------------

    @Test
    @Order(40)
    void theSweepLeavesTheQuarantineRowsRetiredKeysAlone() throws Exception {
        HttpResponse<String> r = sweep(tokenA, plan("dry_run", false));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(json(r).path("rows_swept").asLong()).isEqualTo(3);
        assertThat(metadataOf(A, quarantineA, hexOf(A, quarantineA, "qa1"))).containsKeys("title", "quarantined_at");
        assertThat(metadataOf("cc-reaper", "quarantine-" + reaperOrigin, reaperChash))
            .as("a chunk the reaper moved still holds its retired keys: only the restore strips them")
            .containsKeys("title", "doc_id", "catalog_doc_id", "quarantined_by");
    }

    @Test
    @Order(41)
    void theGhostTenantIsSweptOnceItHoldsACredential() throws Exception {
        HttpResponse<String> r = sweep(tenantToken(GHOST), plan("dry_run", false));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(json(r).path("rows_swept").asLong()).isEqualTo(2);
    }

    @Test
    @Order(42)
    void theBlockedCollectionIsLeftAloneByTheSweep() throws Exception {
        HttpResponse<String> r = sweep(tenantToken(D), plan("dry_run", false, "accept", accept("homeless_documents", 1,
            "generated_nulls", 1)));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("rows_swept").asLong()).isZero();
        assertThat(body.path("blocked_collections").get(0).path("collection").asText()).isEqualTo(liveD);
        assertThat(metadataOf(D, liveD, blockedRow)).containsKey("title");
    }

    @Test
    @Order(43)
    void theHashThatDiffersIsFixedByTheOperatorBeforeTheAdd() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS)
                .set(CHUNKS.METADATA, JSONB.jsonb("{\"chunk_text_hash\":\"" + mismatchedHash
                    + "\",\"embedding_model\":\"" + MODEL + "\"}"))
                .where(CHUNKS.TENANT_ID.eq(C)).and(CHUNKS.CHASH.eq(Chash.fromHex(mismatchedHash).toBytes())).execute();
        }
        assertThat(metadataOf(C, liveC, mismatchedHash).get("chunk_text_hash")).isEqualTo(mismatchedHash);
    }

    @Test
    @Order(50)
    void addAddsTheFourConstraintsNotValid_reportsWhatItLeavesBehind_andARerunLeavesThemBe() throws Exception {
        HttpResponse<String> r = add(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(checkedNames(body)).contains(TenantConstants.DEFAULT_TENANT, A, B, C, D, GHOST);
        Map<String, String> actions = new LinkedHashMap<>();
        body.path("constraints").forEach(c -> actions.put(c.path("name").asText(), c.path("action").asText()));
        assertThat(actions).containsOnlyKeys("chunks_metadata_keys_check", "chunks_chunk_text_hash_check",
            "chunks_embedding_model_check", "chunks_quarantine_keys_check")
            .containsValue("added").doesNotContainValue("present");
        for (JsonNode c : body.path("constraints")) {
            assertThat(c.path("present").asBoolean()).isTrue();
            assertThat(c.path("validated").asBoolean()).as(c.path("name").asText() + " is NOT VALID").isFalse();
        }
        String allowList = body.path("constraints").get(0).path("definition").asText();
        assertThat(allowList).contains("quarantine-").contains("chunk_text_hash").contains("embedding_model")
            .contains("quarantined_by");
        assertThat(allowList).as("the bypass prefixes are exempt").contains("starts_with");

        // B3: what the add leaves behind, per tenant, by name.
        JsonNode left = body.path("left_behind");
        assertThat(left).hasSize(1);
        assertThat(left.get(0).path("tenant").asText()).isEqualTo(D);
        assertThat(left.get(0).path("blocked_collections").get(0).path("collection").asText()).isEqualTo(liveD);
        assertThat(left.get(0).path("blocked_collections").get(0).path("blocked").asText())
            .isEqualTo("legacy_unmanifested");
        assertThat(left.get(0).path("blocked_collections").get(0).path("retired_key_rows").asLong()).isEqualTo(1);

        // A rerun leaves them be, and the stored definitions are compared with what the plan would add: equal, so
        // "present", never a silent present for a stale definition.
        JsonNode again = json(add(ROOT));
        again.path("constraints").forEach(c -> assertThat(c.path("action").asText()).isEqualTo("present"));
    }

    @Test
    @Order(51)
    void afterTheAdd_anUpdateOfARowTheSweepLeftBehindIsRefused_theAcceptedFreeze() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Even an UPDATE that touches no metadata fails: a NOT VALID CHECK is evaluated on every new row version.
            assertThatThrownBy(() -> ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, STAMP.plusDays(1))
                .where(CHUNKS.TENANT_ID.eq(D)).and(CHUNKS.CHASH.eq(Chash.fromHex(blockedRow).toBytes())).execute())
                .as("the row in the blocked collection is frozen until fixed")
                .hasMessageContaining("chunks_metadata_keys_check");
            // A swept row of another tenant is not frozen.
            assertThat(ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, STAMP.plusDays(1))
                .where(CHUNKS.TENANT_ID.eq(B)).execute()).isGreaterThan(0);
        }
    }

    @Test
    @Order(52)
    void aStoredDefinitionThatIsNotThePlansIsReported_notCalledPresent() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropConstraint(su, CHUNKS, "chunks_embedding_model_check");
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CHUNKS)
                .add(DSL.constraint("chunks_embedding_model_check").check(DSL.trueCondition())).execute();
        }

        JsonNode r = json(add(ROOT));

        Map<String, JsonNode> byName = new LinkedHashMap<>();
        r.path("constraints").forEach(c -> byName.put(c.path("name").asText(), c));
        assertThat(byName.get("chunks_embedding_model_check").path("action").asText()).isEqualTo("present_differs");
        assertThat(byName.get("chunks_embedding_model_check").path("expected_definition").asText())
            .contains("embedding_model");
        assertThat(byName.get("chunks_metadata_keys_check").path("action").asText()).isEqualTo("present");
        assertThat(byName.get("chunks_chunk_text_hash_check").path("action").asText()).isEqualTo("present");
    }

    @Test
    @Order(60)
    void replaceDropsAndReAddsInOneStatement_forAChangedKeyTable() throws Exception {
        HttpResponse<String> r = add(ROOT, "replace", true);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        json(r).path("constraints").forEach(c -> {
            assertThat(c.path("action").asText()).isEqualTo("replaced");
            assertThat(c.path("validated").asBoolean()).isFalse();
            // Only the stale one the previous step put in (enforced, so VALID) loses a validation by being replaced.
            assertThat(c.has("warning")).as(c.path("name").asText())
                .isEqualTo("chunks_embedding_model_check".equals(c.path("name").asText()));
        });
        JsonNode again = json(add(ROOT));
        again.path("constraints").forEach(c -> assertThat(c.path("action").asText())
            .as("the replaced stale definition is now the plan's").isEqualTo("present"));
    }

    @Test
    @Order(70)
    void afterTheAdd_aLegacyKeyedWriteToALiveCollectionIsRefusedByTheDatabase_theBypassCollectionsAreNot()
            throws Exception {
        String hex = Chash.ofText("after-add legacy").toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThatThrownBy(() -> PgContainerHelper.insertChunks(ctx, B, conformant("cc-b"), List.of(hex),
                List.of("t"), List.of(new float[384]), List.of(Map.<String, Object>of("title", "legacy"))))
                .as("a document key on a live collection's row").hasMessageContaining("chunks_metadata_keys_check");
            PgContainerHelper.insertChunks(ctx, B, conformant("cc-b"), List.of(hex), List.of("t"),
                List.of(new float[384]), List.of(Map.<String, Object>of("chunk_text_hash", hex, "embedding_model",
                    MODEL)));
            String q = "quarantine-" + conformant("cc-b");
            PgContainerHelper.insertCollection(ctx, B, q, MODEL);
            PgContainerHelper.insertChunks(ctx, B, q, List.of(hex), List.of("t"), List.of(new float[384]),
                List.of(Map.<String, Object>of("title", "legacy is fine in quarantine")));
            String a1 = hexOf(A, liveA, "a1");
            assertThatThrownBy(() -> vectors.upsertChunks(A, liveA, List.of(a1), List.of("a1 text"),
                List.of(Map.<String, Object>of("title", "a late legacy writer"))))
                .as("the engine's own upsert path meets the same CHECK").hasMessageContaining("chunks_metadata_keys_check");
            vectors.upsertChunks(A, liveA, List.of(a1), List.of("a1 text"),
                List.of(Map.<String, Object>of("chunk_text_hash", a1, "embedding_model", MODEL)));
            assertThat(metadataOf(A, liveA, a1).keySet()).as("a swept row still takes a CHUNK-key write")
                .containsExactlyInAnyOrder("chunk_text_hash", "embedding_model");
            assertThatThrownBy(() -> PgContainerHelper.insertChunks(ctx, B, conformant("cc-b"),
                List.of(Chash.ofText("wrong hash").toHex()), List.of("t"), List.of(new float[384]),
                List.of(Map.<String, Object>of("chunk_text_hash", "0".repeat(64)))))
                .hasMessageContaining("chunks_chunk_text_hash_check");
            assertThatThrownBy(() -> PgContainerHelper.insertChunks(ctx, B, conformant("cc-b"),
                List.of(Chash.ofText("half stamped").toHex()), List.of("t"), List.of(new float[384]),
                List.of(Map.<String, Object>of("quarantined_by", "engine"))))
                .hasMessageContaining("chunks_quarantine_keys_check");
        }
    }

    @Test
    @Order(75)
    void afterTheAdd_theRetentionStampOutsideTheAllowListRidesAQuarantineRowButNotALiveOne() throws Exception {
        // nexus-alsq3: quarantine_expires_at is in neither the CHUNK nor the ROW keys, so the allow-list does not
        // name it. It is safe on a quarantine-* row only because that collection prefix is exempt by name.
        String stamp = "2026-12-01T00:00:00Z";
        assertThat((List<String>) plan().get("allowed_keys")).as("the plan's allow-list does not name the stamp")
            .doesNotContain("quarantine_expires_at");
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String live = conformant("cc-b");
            String liveHex = Chash.ofText("stamp on a live row").toHex();
            assertThatThrownBy(() -> PgContainerHelper.insertChunks(ctx, B, live, List.of(liveHex), List.of("t"),
                List.of(new float[384]), List.of(Map.<String, Object>of("chunk_text_hash", liveHex,
                    "embedding_model", MODEL, "quarantine_expires_at", stamp))))
                .as("the stamp on a live row is outside the allow-list").hasMessageContaining("chunks_metadata_keys_check");
            String q = "quarantine-" + live;
            String qHex = Chash.ofText("stamp on a quarantine row").toHex();
            PgContainerHelper.insertCollection(ctx, B, q, MODEL);
            PgContainerHelper.insertChunks(ctx, B, q, List.of(qHex), List.of("t"), List.of(new float[384]),
                List.of(Map.<String, Object>of("chunk_text_hash", qHex, "embedding_model", MODEL,
                    "quarantined_at", "2026-11-01T00:00:00Z", "quarantine_expires_at", stamp)));
            assertThat(metadataOf(B, q, qHex)).as("the CHECK admits the stamp on a quarantine-* row")
                .containsEntry("quarantine_expires_at", stamp);
        }
    }

    // ---- 3. validate --------------------------------------------------------------------------------------------

    @Test
    @Order(80)
    void validateIsStillRefusedForTheTenantWhoseBlockedRowWasLeftBehind_andOnlyThatOne() throws Exception {
        HttpResponse<String> r = validate(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(tenantNames(body)).containsExactly(D);
        assertThat(body.path("tenants").get(0).path("retired_key_rows").asLong()).isEqualTo(1);
        assertThat(admin().states()).filteredOn(s -> "chunks_metadata_keys_check".equals(s.name()))
            .allSatisfy(s -> assertThat(s.validated()).isFalse());
    }

    @Test
    @Order(90)
    void validateValidatesAllFive_readingConvalidatedFirst_andARerunIsANoOp() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // The operator fixes the frozen row the only way left: it cannot be UPDATEd, so it is deleted.
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(D)).and(CHUNKS.CHASH.eq(Chash.fromHex(blockedRow).toBytes())).execute();
        }

        HttpResponse<String> r = validate(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        Map<String, String> actions = new LinkedHashMap<>();
        json(r).path("constraints").forEach(c -> actions.put(c.path("name").asText(), c.path("action").asText()));
        assertThat(actions.keySet()).containsExactlyElementsOf(FIVE);
        assertThat(actions.values()).containsOnly("validated");
        assertThat(admin().states()).hasSize(5).allSatisfy(s -> {
            assertThat(s.present()).as(s.name()).isTrue();
            assertThat(s.validated()).as(s.name() + " is validated").isTrue();
        });

        JsonNode again = json(validate(ROOT));
        again.path("constraints").forEach(c -> assertThat(c.path("action").asText()).isEqualTo("already_valid"));
    }

    @Test
    @Order(95)
    void replaceOnAValidConstraintWarnsItIsNotValidNow_andValidateRestoresIt() throws Exception {
        JsonNode r = json(add(ROOT, "replace", true));

        r.path("constraints").forEach(c -> {
            assertThat(c.path("action").asText()).isEqualTo("replaced");
            assertThat(c.path("validated").asBoolean()).isFalse();
            assertThat(c.path("warning").asText()).contains("NOT VALID");
        });
        JsonNode v = json(validate(ROOT));
        Map<String, String> actions = new LinkedHashMap<>();
        v.path("constraints").forEach(c -> actions.put(c.path("name").asText(), c.path("action").asText()));
        assertThat(actions.get("chunks_metadata_keys_check")).isEqualTo("validated");
        assertThat(actions.get("chunks_chash_octet_check")).isEqualTo("already_valid");
    }

    // ---- 4. the restores pass the validated CHECK ---------------------------------------------------------------

    @Test
    @Order(100)
    void gcRestoreRereferencedBringsALegacyKeyedQuarantineRowBackAsChunkKeysOnly_throughTheValidatedCheck()
            throws Exception {
        assertThat(vectors.restoreRereferenced("cc-reaper", "quarantine-" + reaperOrigin, reaperOrigin))
            .isEqualTo(1L);

        Map<String, Object> meta = metadataOf("cc-reaper", reaperOrigin, reaperChash);
        assertThat(meta.keySet()).as("only the CHUNK keys come back").isEqualTo(ChunkMetadataKeys.CHUNK);
        assertThat(meta.get("chunk_text_hash")).isEqualTo(reaperChash);
    }

    @Test
    @Order(110)
    void quarantineRestoreChunksBringsALegacyKeyedQuarantineRowBackAsChunkKeysOnly_throughTheValidatedCheck()
            throws Exception {
        var out = vectors.quarantineRestore("cc-reaper", restoreOrigin, "quarantine-" + restoreOrigin,
            List.of(restoreChash), "test", false, false);

        assertThat(out.restored()).containsExactly(restoreChash);
        assertThat(metadataOf("cc-reaper", restoreOrigin, restoreChash).keySet()).isEqualTo(ChunkMetadataKeys.CHUNK);
    }
}
