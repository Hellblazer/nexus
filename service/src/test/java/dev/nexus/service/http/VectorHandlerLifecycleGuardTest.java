// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;

/**
 * RDR-229 P2.5 (bead nexus-p2m1x.10) (b): the manifest-less census and the reapable listing refuse a collection
 * whose catalog ROW is {@code lifecycle_state = 'quarantine'}, whatever its name; a prefixed name whose row is
 * {@code live} is admitted; a name with no row is judged by its prefix as before
 * (pinned here too: the row-less cases that used to be database-free tests of the one-argument form).
 */
class VectorHandlerLifecycleGuardTest extends AtomicWriteTestBase {

    private PgVectorRepository vectors;

    @BeforeAll
    void wire() {
        vectors = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    private String newTenant() {
        return "vhl" + seq.incrementAndGet();
    }

    private String col(String tag) {
        return "knowledge__" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private void row(String tenant, String name, String state) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, name);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, state)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(name))).execute();
        }
    }

    @Test
    void aCollectionWhoseRowIsQuarantineIsRefused_whateverItsName() throws Exception {
        String t = newTenant();
        String q = col("q");
        row(t, q, "quarantine");

        assertThatThrownBy(() -> VectorHandler.requireNotQuarantineCollection(vectors, t, q))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining(q).hasMessageContaining("is a quarantine collection");
    }

    @Test
    void aPrefixedNameWhoseRowIsLiveIsAdmitted() throws Exception {
        String t = newTenant();
        String c = "quarantine-" + col("l");
        row(t, c, "live");

        assertThatCode(() -> VectorHandler.requireNotQuarantineCollection(vectors, t, c)).doesNotThrowAnyException();
    }

    @Test
    void aRowLessNameIsJudgedByItsPrefixAsBefore() {
        String t = newTenant();

        assertThatThrownBy(() -> VectorHandler.requireNotQuarantineCollection(vectors, t, "quarantine-" + col("r")))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatCode(() -> VectorHandler.requireNotQuarantineCollection(vectors, t, col("r")))
            .doesNotThrowAnyException();
    }

    @Test
    void aRowLessNameOfThePrefixIsRefusedWithTheQuarantineMessage_andOrdinaryAndBareQuarantineAreAccepted() {
        String t = newTenant();

        assertThatThrownBy(() -> VectorHandler.requireNotQuarantineCollection(vectors, t,
                "quarantine-code__x__voyage-code-3__v1"))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("quarantine");
        assertThatCode(() -> VectorHandler.requireNotQuarantineCollection(vectors, t,
                "knowledge__x__minilm-l6-v2-384__v1")).doesNotThrowAnyException();
        // "quarantine" alone (no trailing hyphen) is not the quarantine-collection grammar (docs/collections.md):
        // a future edit must not widen the prefix test to a bare substring match.
        assertThatCode(() -> VectorHandler.requireNotQuarantineCollection(vectors, t, "quarantine"))
            .doesNotThrowAnyException();
        assertThatThrownBy(() -> VectorHandler.requireNotQuarantineCollection(vectors, t, "quarantine-docs__x__m__v1"))
            .as("the reapable listing refuses it like the census").isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("quarantine");
    }

    @Test
    void theAnswerIsTheSameWhenTheRowIsAlreadyInTheRegistryCache() throws Exception {
        // dimForCollection reads the row through CollectionRegistry and caches it; the guard then answers from there.
        String t = newTenant();
        String live = col("cl");
        String quarantine = col("cq");
        row(t, live, "live");
        row(t, quarantine, "quarantine");
        assertThat(vectors.dimForCollection(t, live)).isEqualTo(384);
        assertThat(vectors.dimForCollection(t, quarantine)).isEqualTo(384);

        assertThatCode(() -> VectorHandler.requireNotQuarantineCollection(vectors, t, live)).doesNotThrowAnyException();
        assertThatThrownBy(() -> VectorHandler.requireNotQuarantineCollection(vectors, t, quarantine))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
