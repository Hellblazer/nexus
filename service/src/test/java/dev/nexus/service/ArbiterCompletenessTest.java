// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogIdentityConflictException;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.SqlConstraints;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.UniqueRaceRetry;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * Every unique key on {@code catalog_owners} and {@code catalog_documents} is driven to
 * conflict, and the write must resolve deliberately instead of raising an unhandled
 * {@code 23505} (nexus-0ehwe; sightings nexus-pbawi, nexus-jq53b, nexus-z3ssg).
 *
 * <p><strong>The class under test is one rule violation, not three bugs.</strong> An
 * {@code ON CONFLICT} arbiter names ONE unique key; every OTHER caller-determined unique
 * key on the table is then an unhandled failure path. PostgreSQL offers no way to widen
 * the arbiter — naming two conflict targets in one statement is a SYNTAX error, verified
 * against PG 17 — so the keys the arbiter omits must be resolved BEFORE the write.
 *
 * <p><strong>Why some of these assert a refusal rather than a convergence.</strong> Both
 * tables separate an ADDRESS key from IDENTITY keys. Where the caller names the identity
 * and lets the server allocate the address, converging on the existing row is correct and
 * is asserted as such. Where the caller names the ADDRESS and the identity already lives
 * at a different address, there is no convergent answer: re-targeting would misroute the
 * write and updating in place would merge two distinct entities (the silent-merge class
 * of nexus-v6za0, in this same file). Those refuse BY NAME — which still closes the
 * defect, whose essence was an undiagnosable crash from a key the author did not know
 * existed.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ArbiterCompletenessTest {

    private static final String TENANT = "arbiter-tenant";
    private static final String SVC_ROLE = "svc_arbiter";
    private static final String SVC_PASS = "svc_arbiter_pass";

    PostgreSQLContainer<?> pg;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su)))
                .update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(8);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private void owner(String prefix, String name, String type) {
        repo.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", prefix, "name", name, "owner_type", type));
    }

    /** Run raw SQL as superuser and return the SQLException, or null if it succeeded. */
    private SQLException rawFailure(String sql) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(sql);
            return null;
        } catch (SQLException e) {
            return e;
        }
    }

    private String ownerNameAt(String prefix) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery(
                "SELECT name FROM nexus.catalog_owners WHERE tenant_id = '" + TENANT
                + "' AND tumbler_prefix = '" + prefix + "'");
            return rs.next() ? rs.getString(1) : null;
        }
    }

    private String repoHashAt(String prefix) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery(
                "SELECT repo_hash FROM nexus.catalog_owners WHERE tenant_id = '" + TENANT
                + "' AND tumbler_prefix = '" + prefix + "'");
            return rs.next() ? rs.getString(1) : null;
        }
    }

    private String sourceUriAt(String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery(
                "SELECT source_uri FROM nexus.catalog_documents WHERE tenant_id = '" + TENANT
                + "' AND tumbler = '" + tumbler + "'");
            return rs.next() ? rs.getString(1) : null;
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // 0. The names the guards branch on are the names PostgreSQL actually reports
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * The whole fix branches on constraint NAMES. If a name here drifts from the DDL, every
     * guard below silently stops firing and every convergence test still passes — the
     * "green because it was verified by the wrong assertion" trap. So pin the names by
     * provoking each violation for real and reading what the server says.
     */
    @Test
    void constraintNamesAreExactlyWhatPostgresReports() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_hash, repo_root) "
                + "VALUES ('" + TENANT + "', '9000', 'names-a', 'repo', 'NAMEHASH', '')");
        }

        var pkDup = rawFailure(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_root) "
            + "VALUES ('" + TENANT + "', '9000', 'names-other', 'repo', '')");
        assertThat(SqlConstraints.violated(pkDup))
            .as("address key of catalog_owners")
            .isEqualTo("catalog_owners_pk");

        var nameTypeDup = rawFailure(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_root) "
            + "VALUES ('" + TENANT + "', '9001', 'names-a', 'repo', '')");
        assertThat(SqlConstraints.violated(nameTypeDup))
            .as("identity key of catalog_owners — nexus-jq53b")
            .isEqualTo("catalog_owners_unique_name_type");

        var repoHashDup = rawFailure(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_hash, repo_root) "
            + "VALUES ('" + TENANT + "', '9002', 'names-b', 'repo', 'NAMEHASH', '')");
        assertThat(SqlConstraints.violated(repoHashDup))
            .as("alias key of catalog_owners — nexus-z3ssg")
            .isEqualTo("idx_catalog_owners_repo_hash");

        repo.registerDocument(TENANT, "9000", Map.of(
            "title", "n", "source_uri", "file:///names/a.md"));
        var docPkDup = rawFailure(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
            + "VALUES ('" + TENANT + "', '9000.1', 'dup')");
        assertThat(SqlConstraints.violated(docPkDup))
            .as("address key of catalog_documents — the key nexus-pbawi's arbiter omitted")
            .isEqualTo("catalog_documents_pk");

        var docUriDup = rawFailure(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, source_uri) "
            + "VALUES ('" + TENANT + "', '9000.999', 'dup', 'file:///names/a.md')");
        assertThat(SqlConstraints.violated(docUriDup))
            .as("identity key of catalog_documents")
            .isEqualTo("ux_catalog_documents_live_source_uri");
    }

    /**
     * The load-bearing PostgreSQL fact behind the whole design: an arbiter cannot be
     * widened to cover a second key. If this ever stops being true, the resolve-first
     * machinery could be replaced by a two-target arbiter and should be.
     */
    @Test
    void postgresRefusesTwoConflictTargetsInOneStatement() throws Exception {
        var e = rawFailure(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_root) "
            + "VALUES ('" + TENANT + "', '9100', 'two-target', 'repo', '') "
            + "ON CONFLICT (tenant_id, tumbler_prefix) DO NOTHING "
            + "ON CONFLICT (tenant_id, name, owner_type) DO NOTHING");
        assertThat((Throwable) e).as("two conflict targets must remain a hard error").isNotNull();
        assertThat(e.getSQLState())
            .as("syntax error — not a runtime one; the statement cannot even be parsed")
            .isEqualTo("42601");
    }

    /**
     * A partial unique index cannot be inferred from its columns alone, and cannot be
     * named via ON CONSTRAINT at all (it is an index, not a constraint) — it needs its
     * predicate restated. This is why {@code idx_catalog_owners_repo_hash} could not
     * simply have been made the arbiter at the owner sites.
     */
    @Test
    void partialUniqueIndexNeedsItsPredicateRestatedToBeArbitrable() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_hash, repo_root) "
                + "VALUES ('" + TENANT + "', '9200', 'partial-a', 'repo', 'PARTIALHASH', '')");
        }
        var bare = rawFailure(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_hash, repo_root) "
            + "VALUES ('" + TENANT + "', '9201', 'partial-b', 'repo', 'PARTIALHASH', '') "
            + "ON CONFLICT (tenant_id, repo_hash) DO NOTHING");
        assertThat((Throwable) bare).as("column list alone must not infer a PARTIAL index").isNotNull();
        assertThat(bare.getSQLState()).isEqualTo("42P10");

        var onConstraint = rawFailure(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_hash, repo_root) "
            + "VALUES ('" + TENANT + "', '9202', 'partial-c', 'repo', 'PARTIALHASH', '') "
            + "ON CONFLICT ON CONSTRAINT idx_catalog_owners_repo_hash DO NOTHING");
        assertThat((Throwable) onConstraint)
            .as("ON CONSTRAINT addresses CONSTRAINTS; this is a bare INDEX")
            .isNotNull();

        var withPredicate = rawFailure(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_hash, repo_root) "
            + "VALUES ('" + TENANT + "', '9203', 'partial-d', 'repo', 'PARTIALHASH', '') "
            + "ON CONFLICT (tenant_id, repo_hash) WHERE repo_hash IS NOT NULL AND repo_hash != '' "
            + "DO NOTHING");
        assertThat((Throwable) withPredicate)
            .as("with the predicate restated the partial index IS arbitrable")
            .isNull();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // 1. catalog_owners — address key (catalog_owners_pk)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void ownersAddressKey_repeatUpsertAtSameAddressConverges() {
        // NON-VACUITY: the guards must not perturb the ordinary idempotent upsert.
        owner("1000", "addr-key", "repo");
        assertThatCode(() -> owner("1000", "addr-key", "repo"))
            .as("re-upserting the same owner at the same address is the common case")
            .doesNotThrowAnyException();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // 2. catalog_owners — identity key (catalog_owners_unique_name_type) — jq53b
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * nexus-jq53b's SEQUENTIAL repro (nexus-aqbrk): registering the same name+type twice
     * allocated a SECOND prefix, so the tumbler_prefix arbiter missed and the identity key
     * fired as a raw 23505. Hal's decision (a): name+type IS an identity key and
     * register_owner is idempotent by it.
     */
    @Test
    void ownersIdentityKey_repeatRegisterWithoutAddressConverges() throws Exception {
        repo.upsertOwner(TENANT, Map.of("name", "seq-repro", "owner_type", "curator"));
        var first = repo.ownersByName(TENANT, "seq-repro");
        assertThat(first).hasSize(1);
        String prefix = (String) first.get(0).get("tumbler_prefix");

        // Pre-fix: this allocated a fresh prefix and raised a bare 409.
        assertThatCode(() -> repo.upsertOwner(TENANT, Map.of("name", "seq-repro", "owner_type", "curator")))
            .as("same name+type must return the SAME owner, not allocate a second")
            .doesNotThrowAnyException();

        assertThat(repo.ownersByName(TENANT, "seq-repro"))
            .as("idempotent by identity — exactly one owner, at the original address")
            .hasSize(1)
            .allSatisfy(o -> assertThat(o.get("tumbler_prefix")).isEqualTo(prefix));
    }

    /**
     * The other half of Hal's decision: identity is idempotent, but it must NOT be
     * silently transferable. An explicit address asking for an identity that lives
     * elsewhere is a rename/merge, and rename must never route through the ensure path
     * (jq53b constraint 1).
     */
    @Test
    void ownersIdentityKey_acrossAddressesRefusesByNameAndChangesNothing() throws Exception {
        owner("1100", "ident-holder", "repo");

        assertThatThrownBy(() -> owner("1101", "ident-holder", "repo"))
            .isInstanceOf(CatalogIdentityConflictException.class)
            .hasMessageContaining("1100")
            .hasMessageContaining("1101")
            .asInstanceOf(org.assertj.core.api.InstanceOfAssertFactories.type(
                CatalogIdentityConflictException.class))
            .satisfies(e -> assertThat(e.constraint()).isEqualTo("catalog_owners_unique_name_type"));

        assertThat(ownerNameAt("1100"))
            .as("the incumbent must be untouched — no silent merge (nexus-v6za0 class)")
            .isEqualTo("ident-holder");
        assertThat(ownerNameAt("1101"))
            .as("and the refused address must not have been created")
            .isNull();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // 3. catalog_owners — alias key (idx_catalog_owners_repo_hash) — z3ssg
    // ══════════════════════════════════════════════════════════════════════════

    /** nexus-z3ssg path (a): two owners, different prefixes, same repo_hash — INSERT arm. */
    @Test
    void ownersAliasKey_insertArmRefusesByName() throws Exception {
        repo.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", "1200", "name", "alias-a", "owner_type", "repo",
            "repo_hash", "ALIASHASH"));

        assertThatThrownBy(() -> repo.upsertOwner(TENANT, Map.of(
                "tumbler_prefix", "1201", "name", "alias-b", "owner_type", "repo",
                "repo_hash", "ALIASHASH")))
            .isInstanceOf(CatalogIdentityConflictException.class)
            .asInstanceOf(org.assertj.core.api.InstanceOfAssertFactories.type(
                CatalogIdentityConflictException.class))
            .satisfies(e -> assertThat(e.constraint()).isEqualTo("idx_catalog_owners_repo_hash"));

        assertThat(ownerNameAt("1201")).as("refused write must not land").isNull();
    }

    /**
     * nexus-z3ssg path (b), the nastier arm: the tumbler_prefix conflict is cleanly
     * HANDLED, and the DO UPDATE arm — which re-sets repo_hash from the excluded row —
     * then violates the repo_hash index in the SAME statement.
     */
    @Test
    void ownersAliasKey_doUpdateArmRefusesByName() throws Exception {
        repo.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", "1300", "name", "arm-holder", "owner_type", "repo",
            "repo_hash", "ARMHASH"));
        repo.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", "1301", "name", "arm-target", "owner_type", "repo"));

        // 1301 EXISTS, so the PK arbiter matches and DO UPDATE runs — and that arm
        // would set repo_hash=ARMHASH, which 1300 already holds.
        assertThatThrownBy(() -> repo.upsertOwner(TENANT, Map.of(
                "tumbler_prefix", "1301", "name", "arm-target", "owner_type", "repo",
                "repo_hash", "ARMHASH")))
            .as("a HANDLED conflict on one key can still break another on the update arm")
            .isInstanceOf(CatalogIdentityConflictException.class)
            .asInstanceOf(org.assertj.core.api.InstanceOfAssertFactories.type(
                CatalogIdentityConflictException.class))
            .satisfies(e -> assertThat(e.constraint()).isEqualTo("idx_catalog_owners_repo_hash"));

        assertThat(repoHashAt("1300")).isEqualTo("ARMHASH");
        assertThat(repoHashAt("1301")).as("the update arm must not have run").isNull();
    }

    /** NON-VACUITY: a blank repo_hash is outside the partial index and must not alias. */
    @Test
    void ownersAliasKey_blankRepoHashDoesNotAliasEveryOwner() {
        assertThatCode(() -> {
            repo.upsertOwner(TENANT, Map.of(
                "tumbler_prefix", "1400", "name", "blank-a", "owner_type", "repo", "repo_hash", ""));
            repo.upsertOwner(TENANT, Map.of(
                "tumbler_prefix", "1401", "name", "blank-b", "owner_type", "repo", "repo_hash", ""));
        }).as("'' is not an identity — the index's own predicate excludes it")
          .doesNotThrowAnyException();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // 4. catalog_owners — the register (ensure) sites
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void registerDocumentEnsure_identityAtAnotherAddressRefusesByName() {
        owner("1500", "ensure-holder", "curator");

        assertThatThrownBy(() -> repo.registerDocument(TENANT, "1501", Map.of(
                "title", "t", "owner_name", "ensure-holder", "owner_type", "curator",
                "source_uri", "file:///ensure/a.md")))
            .as("the ensure insert named only the address key and 23505'd on the identity")
            .isInstanceOf(CatalogIdentityConflictException.class);
    }

    @Test
    void registerDocumentManyEnsure_identityAtAnotherAddressRefusesByName() {
        owner("1600", "ensure-batch-holder", "curator");

        assertThatThrownBy(() -> repo.registerDocumentMany(TENANT, "1601", List.of(
                Map.of("title", "t", "owner_name", "ensure-batch-holder", "owner_type", "curator",
                       "source_uri", "file:///ensurebatch/a.md"))))
            .as("the batch ensure site carries the identical defect")
            .isInstanceOf(CatalogIdentityConflictException.class);
    }

    /** NON-VACUITY: the ordinary register path must be completely unaffected. */
    @Test
    void registerDocument_ordinaryPathStillWorks() {
        owner("1700", "ordinary", "repo");
        assertThat(repo.registerDocument(TENANT, "1700", Map.of(
            "title", "a", "source_uri", "file:///ordinary/a.md"))).isEqualTo("1700.1");
        assertThat(repo.registerDocument(TENANT, "1700", Map.of(
            "title", "b", "source_uri", "file:///ordinary/b.md"))).isEqualTo("1700.2");
        // re-register of an existing source_uri returns the SAME tumbler, no new seq
        assertThat(repo.registerDocument(TENANT, "1700", Map.of(
            "title", "a again", "source_uri", "file:///ordinary/a.md"))).isEqualTo("1700.1");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // 5. catalog_documents — identity key (ux_catalog_documents_live_source_uri)
    // ══════════════════════════════════════════════════════════════════════════

    /** INSERT arm: a fresh tumbler carrying an already-live source_uri. */
    @Test
    void documentsIdentityKey_upsertInsertArmRefusesByName() throws Exception {
        owner("2000", "doc-insert-arm", "repo");
        repo.registerDocument(TENANT, "2000", Map.of(
            "title", "held", "source_uri", "file:///docs/held.md"));

        assertThatThrownBy(() -> repo.upsertDocument(TENANT, Map.of(
                "tumbler", "2000.500", "title", "thief", "source_uri", "file:///docs/held.md")))
            .isInstanceOf(CatalogIdentityConflictException.class)
            .asInstanceOf(org.assertj.core.api.InstanceOfAssertFactories.type(
                CatalogIdentityConflictException.class))
            .satisfies(e -> assertThat(e.constraint())
                .isEqualTo("ux_catalog_documents_live_source_uri"));

        assertThat(sourceUriAt("2000.500")).as("refused write must not land").isNull();
        assertThat(sourceUriAt("2000.1")).isEqualTo("file:///docs/held.md");
    }

    /**
     * DO UPDATE arm: the (tenant_id, tumbler) conflict IS handled, and then the update
     * arm — which sets source_uri from the excluded row — moves a live source_uri onto a
     * second row. The half that is easy to miss.
     */
    @Test
    void documentsIdentityKey_upsertDoUpdateArmRefusesByName() throws Exception {
        owner("2100", "doc-update-arm", "repo");
        repo.registerDocument(TENANT, "2100", Map.of(
            "title", "a", "source_uri", "file:///docs2/a.md"));
        repo.registerDocument(TENANT, "2100", Map.of(
            "title", "b", "source_uri", "file:///docs2/b.md"));

        // 2100.2 EXISTS → PK arbiter matches → DO UPDATE would set source_uri to a.md,
        // which 2100.1 already holds live.
        assertThatThrownBy(() -> repo.upsertDocument(TENANT, Map.of(
                "tumbler", "2100.2", "title", "b", "source_uri", "file:///docs2/a.md")))
            .isInstanceOf(CatalogIdentityConflictException.class);

        assertThat(sourceUriAt("2100.2"))
            .as("the update arm must not have run")
            .isEqualTo("file:///docs2/b.md");
    }

    /** NON-VACUITY: a TOMBSTONED row does not hold its source_uri — re-use must be legal. */
    @Test
    void documentsIdentityKey_tombstonedSourceUriIsReusable() throws Exception {
        owner("2200", "doc-tombstone", "repo");
        String t = repo.registerDocument(TENANT, "2200", Map.of(
            "title", "gone", "source_uri", "file:///docs3/gone.md"));
        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);

        assertThatCode(() -> repo.upsertDocument(TENANT, Map.of(
                "tumbler", "2200.700", "title", "reborn", "source_uri", "file:///docs3/gone.md")))
            .as("the partial index excludes tombstones, so the guard must too")
            .doesNotThrowAnyException();
        assertThat(sourceUriAt("2200.700")).isEqualTo("file:///docs3/gone.md");
    }

    /** NON-VACUITY: an EMPTY source_uri is outside the index and must never collide. */
    @Test
    void documentsIdentityKey_emptySourceUriNeverCollides() {
        owner("2300", "doc-empty-uri", "repo");
        assertThatCode(() -> {
            repo.upsertDocument(TENANT, Map.of("tumbler", "2300.1", "title", "x"));
            repo.upsertDocument(TENANT, Map.of("tumbler", "2300.2", "title", "y"));
        }).doesNotThrowAnyException();
    }

    /** NON-VACUITY: re-upserting the SAME document at its OWN address must still work. */
    @Test
    void documentsIdentityKey_upsertAtOwnAddressConverges() throws Exception {
        owner("2400", "doc-self", "repo");
        repo.registerDocument(TENANT, "2400", Map.of(
            "title", "self", "source_uri", "file:///docs4/self.md"));
        assertThatCode(() -> repo.upsertDocument(TENANT, Map.of(
                "tumbler", "2400.1", "title", "self v2", "source_uri", "file:///docs4/self.md")))
            .as("a document keeping its own source_uri is not a conflict")
            .doesNotThrowAnyException();
    }

    @Test
    void documentsIdentityKey_importBatchRefusesByName() {
        owner("2500", "doc-import", "repo");
        repo.registerDocument(TENANT, "2500", Map.of(
            "title", "held", "source_uri", "file:///docs5/held.md"));

        assertThatThrownBy(() -> repo.importDocumentsBatch(TENANT, List.of(
                Map.of("tumbler", "2500.900", "title", "thief",
                       "source_uri", "file:///docs5/held.md"))))
            .as("the batch import site carries the identical defect")
            .isInstanceOf(CatalogIdentityConflictException.class);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // 6. catalog_owners — the import sites
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void ownersImport_identityAtAnotherAddressRefusesByName() {
        owner("2600", "import-holder", "repo");
        assertThatThrownBy(() -> repo.importOwner(TENANT, Map.of(
                "tumbler_prefix", "2601", "name", "import-holder", "owner_type", "repo")))
            .isInstanceOf(CatalogIdentityConflictException.class);
    }

    @Test
    void ownersImportBatch_identityAtAnotherAddressRefusesByName() {
        owner("2700", "import-batch-holder", "repo");
        assertThatThrownBy(() -> repo.importOwnersBatch(TENANT, List.of(
                Map.of("tumbler_prefix", "2701", "name", "import-batch-holder", "owner_type", "repo"))))
            .isInstanceOf(CatalogIdentityConflictException.class);
    }

    /** NON-VACUITY: the ETL's own re-import of the SAME snapshot must stay idempotent. */
    @Test
    void ownersImport_reimportingTheSameSnapshotIsIdempotent() {
        assertThatCode(() -> {
            repo.importOwner(TENANT, Map.of(
                "tumbler_prefix", "2800", "name", "reimport", "owner_type", "repo",
                "repo_hash", "REIMPORTHASH", "next_seq", 7L));
            repo.importOwner(TENANT, Map.of(
                "tumbler_prefix", "2800", "name", "reimport", "owner_type", "repo",
                "repo_hash", "REIMPORTHASH", "next_seq", 7L));
        }).doesNotThrowAnyException();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // 7. The residual race the resolve-first check cannot close
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Resolve-first is a TOCTOU under READ COMMITTED: two transactions can both resolve
     * "absent" and both proceed. {@code idx_catalog_owners_repo_hash}'s DDL calls itself a
     * TOCTOU guard for exactly this. The belt is a transaction-boundary retry, and this
     * pins that the retry actually re-runs the unit (a genuine driver exception, not a
     * hand-built stub).
     */
    @Test
    void uniqueRaceRetry_reRunsTheTransactionOnANonArbitratedViolation() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_root) "
                + "VALUES ('" + TENANT + "', '3000', 'race-seed', 'repo', '')");
        }
        SQLException real = rawFailure(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_root) "
            + "VALUES ('" + TENANT + "', '3001', 'race-seed', 'repo', '')");
        assertThat(SqlConstraints.violated(real)).isEqualTo("catalog_owners_unique_name_type");

        AtomicInteger attempts = new AtomicInteger();
        String out = UniqueRaceRetry.run("test",
            new String[]{"catalog_owners_unique_name_type"},
            () -> {
                if (attempts.incrementAndGet() == 1) throw new RuntimeException("wrapped", real);
                return "converged";
            });
        assertThat(attempts.get()).as("the losing attempt must be re-run once").isEqualTo(2);
        assertThat(out).isEqualTo("converged");
    }

    /** A violation of a key NOT in the list must propagate untouched — no blind retrying. */
    @Test
    void uniqueRaceRetry_doesNotRetryAnUnrelatedViolation() throws Exception {
        SQLException unrelated = rawFailure(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_root) "
            + "VALUES ('" + TENANT + "', '3100', 'unrelated-a', 'repo', ''), "
            + "('" + TENANT + "', '3100', 'unrelated-b', 'repo', '')");
        assertThat(SqlConstraints.violated(unrelated)).isEqualTo("catalog_owners_pk");

        AtomicInteger attempts = new AtomicInteger();
        assertThatThrownBy(() -> UniqueRaceRetry.run("test",
                new String[]{"catalog_owners_unique_name_type"},
                () -> {
                    attempts.incrementAndGet();
                    throw new RuntimeException("wrapped", unrelated);
                }))
            .isInstanceOf(RuntimeException.class);
        assertThat(attempts.get()).as("no retry for a key outside the declared set").isEqualTo(1);
    }

    /**
     * The concurrent shape of nexus-jq53b: N threads register the SAME owner identity with
     * no address supplied. All must converge on ONE owner and none may surface a 23505.
     */
    @Test
    void ownersIdentityKey_concurrentRegisterConvergesOnOneOwner() throws Exception {
        final int threads = 6;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        try {
            CountDownLatch start = new CountDownLatch(1);
            List<Future<?>> futures = new java.util.ArrayList<>();
            for (int i = 0; i < threads; i++) {
                futures.add(pool.submit(() -> {
                    start.await();
                    repo.upsertOwner(TENANT, Map.of(
                        "name", "concurrent-identity", "owner_type", "curator"));
                    return null;
                }));
            }
            start.countDown();
            for (Future<?> f : futures) {
                f.get(60, TimeUnit.SECONDS);   // must not throw
            }
            assertThat(repo.ownersByName(TENANT, "concurrent-identity"))
                .as("concurrent ensure of one identity must yield exactly one owner")
                .hasSize(1);
        } finally {
            pool.shutdownNow();
        }
    }
}
