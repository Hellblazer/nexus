// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.CollectionIds;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 R7, the re-embed readers of a retired collection that names no live successor (bead nexus-p2m1x.25, linda's ruling
 * 2026-11-23): a source or a target row whose successor was deleted carries its own dead label in {@code superseded_by}, and a refusal
 * must not name that label as a successor. By label the handler refuses in plain words ({@code TargetSelection#retiredWithNoSuccessor});
 * by id the route boundary answers today's 409 {@code collection_superseded} with no successor field. A retired row WITH a live successor
 * keeps naming it, by its CURRENT label (found by {@code superseded_by_id}, not by the text the row stored). The plan and the start
 * refuse identically, so every case runs on both.
 *
 * <p>The fixture row of a tombstone whose successor has no row is {@code CatalogDualWriteIdTest}'s (an upsert naming a successor that was
 * never registered stamps the row's own id).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReembedRetiredNoSuccessorTest {

    private static final String PLAN = "/v1/vectors/reembed/plan";
    private static final String JOBS = "/v1/vectors/reembed/jobs";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINI = "minilm-l6-v2-384";

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture(true);
        fx.asSuperuser(ctx -> ctx.insertInto(EMBEDDING_PROFILE, EMBEDDING_PROFILE.TENANT_ID, EMBEDDING_PROFILE.CONTENT_TYPE,
                EMBEDDING_PROFILE.EMBEDDING_MODEL, EMBEDDING_PROFILE.DIMENSION)
            .values(fx.tenant, "knowledge", MINI, 384).onConflictDoNothing().execute());
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private String owner() {
        return "r7own" + (n++);
    }

    private static String name(String owner, String model, int version) {
        return "knowledge__" + owner + "__" + model + "__v" + version;
    }

    /** A live registry row of the conformant name. */
    private String live(String label) {
        fx.register(label);
        return label;
    }

    /**
     * A tombstone of {@code label} that names {@code supersededBy}, written as a direct row write (no cache ever held it as retired). A
     * successor with a row is stamped with its id; one with none is R7's "retired, no successor": the row's own id, and the text a
     * label nothing bears (the fixture of {@code CatalogDualWriteIdTest} and {@code CollectionRenameTupleTest}).
     */
    private String retired(String label, String owner, String supersededBy) {
        fx.register(label);
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID,
                DSL.coalesce(CollectionIds.idOf(fx.tenant, supersededBy), CATALOG_COLLECTIONS.COLLECTION_ID))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label)).execute());
        assertThat(fx.supersededBy(label)).as("setup: %s is a tombstone", label).isNotEmpty();
        return label;
    }

    private static String deadLabel(String owner) {
        return name(owner, "gone-model-1", 1);
    }

    private Resp start(String route, String body) {
        return fx.post(route, body);
    }

    private static void assertRetiredWithNoSuccessor(Resp r, String retired, String deadLabel) {
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        String error = r.json().path("error").asText();
        assertThat(error).as(r.body()).contains("collection " + retired + " was retired and has no successor");
        assertThat(r.body()).as("its own dead label is no successor").doesNotContain(deadLabel);
        assertThat(r.json().has("superseded_by")).as("no successor field: %s", r.body()).isFalse();
        assertThat(r.json().path("collection").asText()).isEqualTo(retired);
    }

    private static void assertSupersededBodyWithNoSuccessor(Resp r, String retired, long retiredId) {
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection").asText()).isEqualTo(retired);
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(retiredId);
        assertThat(r.json().has("superseded_by")).as("no successor fields: %s", r.body()).isFalse();
        assertThat(r.json().has("successor_collection_id")).isFalse();
    }

    /**
     * The cloud leg's jobs probe names a source nothing registers (tests/e2e/lib/cloud_leg_route_snippets.py, REEMBED_PROBES). On a local engine the
     * handler answers 404 with exactly this body, from reads alone: no job row is written for it (ReembedHandler#resolve, after keptTombstonedJobOf).
     */
    @ParameterizedTest
    @ValueSource(strings = {PLAN, JOBS})
    void anUnregisteredSource_isRefused404WithAnExactBody_andNoJobRowIsWritten(String route) {
        String name = name(owner(), BGE, 1);
        long jobsBefore = fx.asSuperuser(ctx -> (long) ctx.fetchCount(REEMBED_JOBS, REEMBED_JOBS.TENANT_ID.eq(fx.tenant)));

        Resp r = start(route, "{\"source_collection\":" + json(name) + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.json()).hasSize(1);
        assertThat(r.json().path("error").asText()).isEqualTo("source collection " + name + " is not registered");
        long jobsAfter = fx.asSuperuser(ctx -> (long) ctx.fetchCount(REEMBED_JOBS, REEMBED_JOBS.TENANT_ID.eq(fx.tenant)));
        assertThat(jobsAfter).as("no job row was written").isEqualTo(jobsBefore);
    }

    // ---- the source ---------------------------------------------------------------------------------------------------------------

    @ParameterizedTest
    @ValueSource(strings = {PLAN, JOBS})
    void aRetiredSourceWithNoSuccessor_byLabel_isRefusedAsRetired_naming_noSuccessor(String route) {
        String owner = owner();
        String dead = deadLabel(owner);
        String source = retired(name(owner, BGE, 1), owner, dead);

        assertRetiredWithNoSuccessor(start(route, "{\"source_collection\":" + json(source) + "}"), source, dead);
    }

    @ParameterizedTest
    @ValueSource(strings = {PLAN, JOBS})
    void aRetiredSourceWithNoSuccessor_byId_isToday409CollectionSuperseded(String route) {
        String owner = owner();
        String source = retired(name(owner, BGE, 1), owner, deadLabel(owner));
        long id = fx.idOf(source);

        assertSupersededBodyWithNoSuccessor(start(route, "{\"source_collection_id\":" + id + "}"), source, id);
    }

    /** The control: a retired source that HAS a live successor still names it, by the successor's CURRENT label. */
    @ParameterizedTest
    @ValueSource(strings = {PLAN, JOBS})
    void aRetiredSourceWithALiveSuccessor_keepsNamingIt_byItsCurrentLabel(String route) {
        String owner = owner();
        String successor = live(name(owner, BGE, 2));
        String source = retired(name(owner, BGE, 1), owner, successor);
        long successorId = fx.idOf(successor);

        Resp byLabel = start(route, "{\"source_collection\":" + json(source) + "}");
        assertThat(byLabel.status()).as(byLabel.body()).isEqualTo(409);
        assertThat(byLabel.json().path("error").asText()).contains("is already superseded by " + successor);
        assertThat(byLabel.json().path("superseded_by").asText()).isEqualTo(successor);

        Resp byId = start(route, "{\"source_collection_id\":" + fx.idOf(source) + "}");
        assertThat(byId.status()).as(byId.body()).isEqualTo(409);
        assertThat(byId.reason()).isEqualTo("collection_superseded");
        assertThat(byId.json().path("superseded_by").asText()).isEqualTo(successor);
        assertThat(byId.json().path("successor_collection_id").asLong(-1)).isEqualTo(successorId);
    }

    /** The successor is found by id, so its label is the one it bears now, not the one the tombstone stored. */
    @Test
    void aRetiredSourceWhoseSuccessorWasRenamed_namesTheLiveEndByItsCurrentLabel() {
        String owner = owner();
        String successor = live(name(owner, BGE, 2));
        String source = retired(name(owner, BGE, 1), owner, successor);
        String renamed = name(owner, BGE, 3);
        assertThat(fx.post("/v1/catalog/collections/rename",
            "{\"old_name\":" + json(successor) + ",\"new_name\":" + json(renamed) + "}").status()).as("setup: the rename").isEqualTo(200);

        Resp r = start(JOBS, "{\"source_collection\":" + json(source) + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.json().path("superseded_by").asText()).as("the live end, not the stored text %s: %s", successor, r.body())
            .isEqualTo(renamed);
        assertThat(r.json().path("error").asText()).contains("is already superseded by " + renamed);
    }

    // ---- the target, through TargetSelection ---------------------------------------------------------------------------------------

    @ParameterizedTest
    @ValueSource(strings = {PLAN, JOBS})
    void aRetiredTargetWithNoSuccessor_byLabel_isRefusedAsRetired_neverNamingItsDeadLabel(String route) {
        String owner = owner();
        String dead = deadLabel(owner);
        String source = live(name(owner, BGE, 1));
        String target = retired(name(owner, MINI, 1), owner, dead);

        Resp r = start(route, "{\"source_collection\":" + json(source) + "}");

        assertRetiredWithNoSuccessor(r, target, dead);
        assertThat(r.json().path("error").asText()).contains("cannot be this job's target");
    }

    @ParameterizedTest
    @ValueSource(strings = {PLAN, JOBS})
    void aRetiredTargetWithNoSuccessor_byId_isToday409CollectionSuperseded(String route) {
        String owner = owner();
        String source = live(name(owner, BGE, 1));
        String target = retired(name(owner, MINI, 1), owner, deadLabel(owner));
        long id = fx.idOf(target);

        assertSupersededBodyWithNoSuccessor(
            start(route, "{\"source_collection\":" + json(source) + ",\"target_collection_id\":" + id + "}"), target, id);
    }

    /** The control: a retired target whose live successor is another collection is refused naming that collection, by its current label. */
    @ParameterizedTest
    @ValueSource(strings = {PLAN, JOBS})
    void aRetiredTargetWithALiveSuccessorThatIsNotTheSource_isRefusedNamingIt(String route) {
        String owner = owner();
        String source = live(name(owner, BGE, 1));
        String elsewhere = live(name(owner + "x", MINI, 2));   // another owner: not on the source's tuple
        String target = retired(name(owner, MINI, 1), owner, elsewhere);

        Resp r = start(route, "{\"source_collection\":" + json(source) + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.json().path("error").asText()).contains("target " + target + " is superseded by " + elsewhere)
            .contains("not by this job's source " + source);
        assertThat(r.json().path("collection").asText()).isEqualTo(target);
    }

    /** The control: a retired target whose live successor IS the source is revived, as before. */
    @Test
    void aRetiredTargetWhoseLiveSuccessorIsTheSource_isStillRevived() {
        String owner = owner();
        String source = live(name(owner, BGE, 1));
        String target = retired(name(owner, MINI, 1), owner, source);

        Resp r = start(PLAN, "{\"source_collection\":" + json(source) + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("target_collection").asText()).isEqualTo(target);
        assertThat(r.json().path("target_action").asText()).isEqualTo("revive");
    }

    /** The control: a live source with no target row plans a registration, by label and by id. */
    @Test
    void aLiveSource_plansItsTarget_byLabelAndById() {
        String owner = owner();
        String source = live(name(owner, BGE, 1));
        String target = name(owner, MINI, 1);

        Resp byLabel = start(PLAN, "{\"source_collection\":" + json(source) + ",\"target_collection\":" + json(target) + "}");
        Resp byId = start(PLAN, "{\"source_collection_id\":" + fx.idOf(source) + ",\"target_collection\":" + json(target) + "}");

        for (Resp r : new Resp[] {byLabel, byId}) {
            assertThat(r.status()).as(r.body()).isEqualTo(200);
            assertThat(r.json().path("target_action").asText()).isEqualTo("register");
        }
    }
}
