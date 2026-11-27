// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionSupersededException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 R7 (bead nexus-p2m1x.25, the paired cut; folio's critique of 8398b73d2c): the supersede, rename and write refusals that
 * print what a retired collection was "superseded by". A tombstone whose successor was deleted carries the text of a label nothing
 * bears; printing it as the successor sends the operator to a name that is not there. Each site finds the successor through
 * {@code CollectionSuccessor} and, with none, says there is none. A live successor is still named, by its current label.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RetiredNoSuccessorMessagesTest {

    private static final String BGE = "bge-base-en-v15-768";
    private static final String SUPERSEDE = "/v1/catalog/collections/supersede";
    private static final String RENAME = "/v1/catalog/collections/rename";

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture(true);
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private static String name(String owner, int version) {
        return "knowledge__" + owner + "__" + BGE + "__v" + version;
    }

    private String owner() {
        return "r7msg" + (n++);
    }

    private String live(String label) {
        fx.register(label);
        return label;
    }

    /** A tombstone of {@code label}: successor id of the row bearing {@code supersededBy}, else its own id (no successor), with that text. */
    private String retired(String label, String supersededBy) {
        fx.register(label);
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID,
                DSL.coalesce(CollectionIds.idOf(fx.tenant, supersededBy), CATALOG_COLLECTIONS.COLLECTION_ID))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label)).execute());
        return label;
    }

    private static String dead(String owner) {
        return name(owner, 9);
    }

    private static void assertSaysNone(Resp r, String dead) {
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.body()).as("says it has none").contains("no live successor");
        assertThat(r.body()).as("never prints the dead label as a successor").doesNotContain(dead);
    }

    // ---- supersede ----------------------------------------------------------------------------------------------------------

    @Test
    void supersedeOntoATombstoneWithNoSuccessor_saysItHasNone() {
        String o = owner();
        String src = live(name(o, 1));
        String target = retired(name(o, 2), dead(o));
        assertSaysNone(fx.post(SUPERSEDE, "{\"name\":" + json(src) + ",\"superseded_by\":" + json(target) + "}"), dead(o));
    }

    @Test
    void supersedeOntoATombstoneWithALiveSuccessor_stillNamesTheSuccessor() {
        String o = owner();
        String src = live(name(o, 1));
        String successor = live(name(o, 3));
        String target = retired(name(o, 2), successor);
        Resp r = fx.post(SUPERSEDE, "{\"name\":" + json(src) + ",\"superseded_by\":" + json(target) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.json().path("error").asText()).contains("is itself superseded by " + successor);
    }

    @Test
    void supersedeOfATombstoneWithNoSuccessorToAnotherCollection_saysItHasNone() {
        String o = owner();
        String src = retired(name(o, 1), dead(o));
        String other = live(name(o, 2));
        assertSaysNone(fx.post(SUPERSEDE, "{\"name\":" + json(src) + ",\"superseded_by\":" + json(other) + "}"), dead(o));
    }

    @Test
    void supersedeOfATombstoneWithALiveSuccessorToAnotherCollection_stillNamesTheSuccessor() {
        String o = owner();
        String successor = live(name(o, 3));
        String src = retired(name(o, 1), successor);
        String other = live(name(o, 2));
        Resp r = fx.post(SUPERSEDE, "{\"name\":" + json(src) + ",\"superseded_by\":" + json(other) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.json().path("error").asText()).contains("is already superseded by " + successor);
    }

    // ---- rename -------------------------------------------------------------------------------------------------------------

    @Test
    void renameOfATombstoneWithNoSuccessor_saysItHasNone() {
        String o = owner();
        String src = retired(name(o, 1), dead(o));
        assertSaysNone(fx.post(RENAME, "{\"old_name\":" + json(src) + ",\"new_name\":" + json(name(o, 2)) + "}"), dead(o));
    }

    @Test
    void renameOfATombstoneWithALiveSuccessor_stillNamesTheSuccessor() {
        String o = owner();
        String successor = live(name(o, 3));
        String src = retired(name(o, 1), successor);
        Resp r = fx.post(RENAME, "{\"old_name\":" + json(src) + ",\"new_name\":" + json(name(o, 2)) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.json().path("error").asText()).contains("(superseded by " + successor + ")");
    }

    @Test
    void renameOntoATombstoneWithNoSuccessor_saysItHasNone() {
        String o = owner();
        String src = live(name(o, 1));
        String target = retired(name(o, 2), dead(o));
        assertSaysNone(fx.post(RENAME, "{\"old_name\":" + json(src) + ",\"new_name\":" + json(target) + "}"), dead(o));
    }

    @Test
    void renameOntoATombstoneWithALiveSuccessor_stillNamesTheSuccessor() {
        String o = owner();
        String src = live(name(o, 1));
        String successor = live(name(o, 3));
        String target = retired(name(o, 2), successor);
        Resp r = fx.post(RENAME, "{\"old_name\":" + json(src) + ",\"new_name\":" + json(target) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.json().path("error").asText()).contains("(superseded by " + successor + ")")
            .contains("it was retired in favour of " + successor);
    }

    // ---- a chunk write to a retired name -------------------------------------------------------------------------------------

    @Test
    void aWriteToATombstoneWithNoSuccessor_isRefusedSayingItHasNone() {
        String o = owner();
        String tomb = retired(name(o, 1), dead(o));
        assertThatThrownBy(() -> fx.asSuperuser(ctx -> CollectionRegistry.requireWritable(ctx, fx.tenant, tomb)))
            .isInstanceOf(CollectionSupersededException.class)
            .satisfies(t -> {
                var e = (CollectionSupersededException) t;
                assertThat(e.hasSuccessor()).isFalse();
                assertThat(e.getMessage()).contains("no live successor").doesNotContain(dead(o)).doesNotContain("write to '");
            });
    }

    @Test
    void aWriteToATombstoneWithALiveSuccessor_stillNamesTheSuccessor() {
        String o = owner();
        String successor = live(name(o, 3));
        String tomb = retired(name(o, 1), successor);
        assertThatThrownBy(() -> fx.asSuperuser(ctx -> CollectionRegistry.requireWritable(ctx, fx.tenant, tomb)))
            .isInstanceOf(CollectionSupersededException.class)
            .satisfies(t -> {
                var e = (CollectionSupersededException) t;
                assertThat(e.hasSuccessor()).isTrue();
                assertThat(e.supersededBy()).isEqualTo(successor);
                assertThat(e.getMessage()).contains("write to '" + successor + "' instead");
            });
    }
}
