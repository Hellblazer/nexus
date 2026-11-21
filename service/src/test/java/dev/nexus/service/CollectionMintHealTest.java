// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.MintedLabel;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28): the mint when the {@code collection_id} sequence is behind the data. A restore that
 * writes explicit ids and runs no {@code setval} leaves the sequence below the rows it wrote; the next mint draws an id
 * that is already a row. The mint heals ONCE (through {@code nexus.collection_id_seq_heal}, the definer function of
 * {@code collections-002}: the engine role holds no UPDATE on the shared sequence, and the function only raises it), logs a WARN naming the old and new values, and draws again;
 * a second taken id is a {@link MintedLabel.SequenceBehindDataException} (lead ruling T2
 * {@code nexus_rdr/229-lead-ruling-collection-id-by-default-2026-11-21}). Its own database: it moves the sequence.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CollectionMintHealTest {

    private static final String TENANT = "mint-heal-tenant";
    private static final String SVC_ROLE = "svc_mint_heal";
    private static final String SVC_PASS = "svc_mint_heal_pass";
    private static final String MODEL = "bge-base-en-v15-768";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Map<String, Object> attrs(String owner) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("content_type", "knowledge");
        m.put("owner_id", owner);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        return m;
    }

    private static long id(Map<String, Object> row) {
        return ((Number) row.get("collection_id")).longValue();
    }

    private long nextSequenceValue() {
        return su(CollectionMintSupport::nextSequenceValue);
    }

    /** A live row with an explicit id, as a restore writes it (no sequence involved). */
    private void seedWithExplicitId(String name, long collectionId) {
        su(ctx -> {
            CollectionMintSupport.insertLiveRow(ctx, TENANT, name, "restored-" + name, MODEL, collectionId);
            return null;
        });
    }

    private <T> T svc(Function<DSLContext, T> work) {
        try (Connection c = svcDs.getConnection()) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    @Test @Order(0)
    void theEngineRoleHealsThroughADefinerFunctionAndHoldsNoUpdateOnTheSequence() {
        // nexus_svc is the production role; svc_mint_heal is this class's own login role, which the test changelog grants
        // exactly what nexus_svc holds. Neither may setval the shared sequence directly (a tenant must not be able to move
        // the identity every tenant draws from); both may call the one function that moves it forward.
        boolean prodExecute = su(ctx -> CollectionMintSupport.holdsExecuteOnHeal(ctx, "nexus_svc"));
        boolean prodUpdate = su(ctx -> CollectionMintSupport.holdsUpdateOnSequence(ctx, "nexus_svc"));
        boolean classExecute = su(ctx -> CollectionMintSupport.holdsExecuteOnHeal(ctx, SVC_ROLE));
        boolean classUpdate = su(ctx -> CollectionMintSupport.holdsUpdateOnSequence(ctx, SVC_ROLE));
        assertThat(prodExecute).as("nexus_svc may EXECUTE " + CollectionMintSupport.HEAL_FUNCTION).isTrue();
        assertThat(prodUpdate).as("nexus_svc holds no UPDATE on the collection_id sequence").isFalse();
        assertThat(classExecute).as("the test's service role may EXECUTE the heal function").isTrue();
        assertThat(classUpdate).as("the test's service role holds no UPDATE on the sequence").isFalse();
        String definition = su(CollectionMintSupport::healFunctionDefinition);
        assertThat(definition).as("a definer function with its search_path pinned")
            .contains("SECURITY DEFINER").containsIgnoringCase("search_path");
    }

    @Test @Order(1)
    void anIdAtOrAboveTheSequenceIsHealedOnceAndTheMintDrawsPastIt() {
        long next = nextSequenceValue();
        seedWithExplicitId("restored-at-next", next);
        seedWithExplicitId("restored-far-above", next + 5);
        var logs = new ListAppender<ILoggingEvent>();
        logs.start();
        var root = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        root.addAppender(logs);
        Map<String, Object> row;
        try {
            row = repo.mintCollection(TENANT, attrs("subject-heal-1"), true);
        } finally {
            root.detachAppender(logs);
        }

        assertThat(id(row)).as("the sequence was set to the largest id, so the next draw is past every restored row")
            .isEqualTo(next + 6);
        assertThat(row.get("name")).isEqualTo("c" + (next + 6));
        assertThat(logs.list.stream().map(ILoggingEvent::getFormattedMessage))
            .anyMatch(m -> m.contains("event=collection_id_sequence_behind_data"));
        assertThat(nextSequenceValue()).as("the heal left the sequence after the minted id").isEqualTo(next + 7);
    }

    @Test @Order(3)
    void theHealFunctionOnlyMovesTheSequenceForward() {
        long next = nextSequenceValue();

        Long raised = healAs(next + 100);
        assertThat(raised).as("a floor above the sequence raises it").isEqualTo(next + 100);
        assertThat(nextSequenceValue()).isEqualTo(next + 101);

        Long lower = healAs(1L);
        Long middle = healAs(next + 50);
        Long none = healAs(null);
        assertThat(lower).as("a lower floor leaves it").isEqualTo(next + 100);
        assertThat(middle).isEqualTo(next + 100);
        assertThat(none).as("no floor leaves it").isEqualTo(next + 100);
        assertThat(nextSequenceValue()).as("nothing moved it back").isEqualTo(next + 101);
    }

    private Long healAs(Long floor) {
        return svc(ctx -> CollectionMintSupport.callHeal(ctx, floor));
    }

    @Test @Order(4)
    void aHealInOneTenantNeverMovesTheSharedSequenceBackAndTheOtherTenantsNextMintDoesNotCollide() {
        // The identity sequence is one for every tenant; a tenant sees only its own rows (FORCE RLS), so a heal knows its
        // own tenant's largest id and nothing about the others'. Tenant A holds an id at the sequence's next draw, so
        // A's mint collides and heals; tenant B holds ids just above, so B's next mint collides and heals in its turn.
        String other = "mint-heal-tenant-b";
        long n = nextSequenceValue();
        seedWithExplicitId("two-a-held", n);
        su(ctx -> {
            for (long k = 1; k <= 3; k++) {
                CollectionMintSupport.insertLiveRow(ctx, other, "two-b-held-" + k, "restored-two-b-" + k, MODEL, n + k);
            }
            return null;
        });
        long before = n - 1;

        var a = repo.mintCollection(TENANT, attrs("subject-two-a"), true);

        assertThat(id(a)).as("A drew past its own taken id").isEqualTo(n + 1);
        assertThat(nextSequenceValue() - 1).as("A's heal left the sequence at or above where it was").isGreaterThanOrEqualTo(before);

        var b = repo.mintCollection(other, attrs("subject-two-b"), true);

        assertThat(id(b)).as("B's mint healed to B's own largest id and drew past it").isEqualTo(n + 4);
        assertThat(b.get("name")).isEqualTo("c" + (n + 4));
        assertThat(nextSequenceValue() - 1).as("the sequence only moved forward").isGreaterThanOrEqualTo(n + 4);
    }

    @Test @Order(9)
    void aSecondTakenIdAfterTheHealIsRefusedAsSequenceBehindData() {
        // The sequence cycles over [1, max]: after the heal (setval to max) the next draw wraps to 1, which is a row too.
        su(ctx -> {
            // the cycle spans the largest id of every tenant, and a heal only knows its own tenant's: the other tenant of the
            // test above goes, so this tenant's largest id is the sequence's largest
            ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.ne(TENANT)).execute();
            CollectionMintSupport.cycleOverTheRows(ctx, TENANT, "cycle-anchor", MODEL);
            return null;
        });

        assertThatThrownBy(() -> repo.mintCollection(TENANT, attrs("subject-heal-2"), true))
            .isInstanceOf(MintedLabel.SequenceBehindDataException.class);
        long written = su(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("subject-heal-2"))));
        assertThat(written).as("nothing was written").isZero();
    }
}
