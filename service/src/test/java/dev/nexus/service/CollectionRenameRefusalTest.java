// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.CollectionRouteCensus.StoreRename;
import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;

import static dev.nexus.service.CollectionRouteFixture.json;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a write half (nexus-p2m1x.23): the rename refusal.
 *
 * <p>RDR line 2668 (P3.5a): "from gate 3 until P3.7's one-row rename can run (that move complete and every P3.5b changeset
 * applied) the engine refuses a rename, with a typed error that names the missing condition, the move or the restart." Line
 * 2709 (P3.7): "while the move has no {@code completed_at} or a P3.5b changeset has not applied, the engine refuses a rename
 * with a typed error that names the missing condition, the move or the restart that applies P3.5b's changesets". Line 2728:
 * "a rename after gate 3 and before the name-key drop move completes is refused with the move named; and a rename after that
 * move completes and before every P3.5b changeset has applied is refused with the restart named." Item 6 (line ~1455): "The
 * engine therefore refuses a rename in that window: gate 3 holds and not both of those."
 *
 * <p>The two inputs are the two seams of {@link CollectionRouteFixture}: gate 3 ({@code Gate3}, read per call) and the second
 * condition ({@code RenameReadiness}, which P3.7's move fills in). The RDR does not give the typed error's wire shape;
 * the tests fix one, once, in {@link #REASON} and {@link #MISSING_MOVE} / {@link #MISSING_RESTART}, so the implementation
 * adopts it or changes the three constants.
 *
 * <p>Half of the P3.5 shared test ("while gate 3 does not hold ... renames correctly") is {@link #beforeGate3_aRenameIsTheNamePathInEveryWindow};
 * the other half ("from gate 3 a rename is refused until P3.7's one-row rename can run") is the rest of this class, and the
 * last test is the one that passes only with P3.7 on the same branch (residual R8, nexus-p2m1x.47).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRenameRefusalTest {

    /** The typed refusal: HTTP 409, {@code reason} = this, {@code missing} = one of the two below. */
    static final String REASON = "rename_refused";
    static final String MISSING_MOVE = "name_key_drop_move";
    static final String MISSING_RESTART = "restart";

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    @BeforeEach
    void reset() {
        fx.gate3.set(false);
        fx.renameWindow.set(Missing.NAME_KEY_DROP_MOVE);   // the state before any latch: no row, the drop move missing
    }

    private String registered(String stem) {
        String label = CollectionRouteFixture.label("rr-" + stem + "-" + (n++));
        fx.register(label);
        return label;
    }

    private Resp renameByLabel(String from, String to) {
        return fx.post("/v1/catalog/collections/rename", "{\"old_name\":" + json(from) + ",\"new_name\":" + json(to) + "}");
    }

    private Resp renameById(long id, String to) {
        return fx.post("/v1/catalog/collections/rename", "{\"collection_id\":" + id + ",\"new_name\":" + json(to) + "}");
    }

    private void assertRefused(Resp r, String missing, String from, long id, String to) {
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        JsonNode j = r.json();
        assertThat(j.path("reason").asText()).as("typed: %s", r.body()).isEqualTo(REASON);
        assertThat(j.path("missing").asText()).as("names the missing condition: %s", r.body()).isEqualTo(missing);
        assertThat(j.path("error").asText()).as("and says it in words").isNotBlank();
        assertThat(fx.idOf(from)).as("nothing renamed: the row keeps its id").isEqualTo(id);
        assertThat(fx.supersededBy(from)).as("nothing renamed: the row is still live").isEqualTo("");
        assertThat(fx.exists(to)).as("nothing renamed: the new label has no row").isFalse();
    }

    // ---- before gate 3: the name path, unchanged ----------------------------------------------------------------------

    /**
     * RDR line 2656 (the P3.5 test): a store "still searches, lists and renames correctly after the P3.5 and P3.7 cut while
     * gate 3 does not hold"; item 6 (line ~1448): "Until gate 3 ... {@code renameCollectionTxn} keeps rewriting the fourteen
     * {@code COLLECTION_SCOPED_TABLES}." The refusal is gated on gate 3, so in each of the three states of the second
     * condition a rename with gate 3 not holding goes through.
     */
    @Test
    void beforeGate3_aRenameIsTheNamePathInEveryWindow() {
        for (Missing window : new Missing[] {Missing.NAME_KEY_DROP_MOVE, Missing.P35B_RESTART}) {
            fx.renameWindow.set(window);
            fx.gate3.set(false);
            String from = registered("pre-w" + window.ordinal());
            String to = CollectionRouteFixture.label("rr-pre-to-w" + window.ordinal() + "-" + (n++));
            Resp r = renameByLabel(from, to);
            assertThat(r.status()).as("gate 3 does not hold, window %s: %s", window, r.body()).isEqualTo(200);
            assertThat(fx.exists(to)).isTrue();
            assertThat(fx.supersededBy(from)).as("the old label is the tombstone the name path leaves").isEqualTo(to);
        }
    }

    /**
     * linda's Q-B: no latch row while the readiness says the move is complete is an inconsistency (as after a partial restore). Every
     * rename, the catalog's and the five per-store routes, is the typed 409 {@code rename_refused} whose {@code missing} is
     * {@code latch_row_missing}, and nothing is renamed: the name path would mint a new id.
     */
    @Test
    void noLatchRow_withTheReadinessComplete_isRefusedNamingTheMissingRow() {
        fx.gate3.set(false);
        fx.renameWindow.set(Missing.NONE);
        String from = registered("norow");
        long id = fx.idOf(from);
        String to = registered("norow-to");
        Resp r = renameByLabel(from, CollectionRouteFixture.label("rr-norow-new-" + (n++)));
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).as(r.body()).isEqualTo(REASON);
        assertThat(r.json().path("missing").asText()).as(r.body()).isEqualTo("latch_row_missing");
        assertThat(fx.idOf(from)).as("nothing renamed").isEqualTo(id);
        assertThat(fx.supersededBy(from)).isEqualTo("");
        for (StoreRename route : CollectionRouteCensus.STORE_RENAMES) {
            Resp s = storeRename(route, from, to);
            assertThat(s.status()).as(route.path() + ": " + s.body()).isEqualTo(409);
            assertThat(s.reason()).as(s.body()).isEqualTo(REASON);
            assertThat(s.json().path("missing").asText()).as(s.body()).isEqualTo("latch_row_missing");
        }
    }

    // ---- from gate 3, the move missing --------------------------------------------------------------------------------

    /** RDR line 2728: "a rename after gate 3 and before the name-key drop move completes is refused with the move named". */
    @Test
    void fromGate3_beforeTheNameKeyDropMoveCompletes_isRefusedNamingTheMove() {
        fx.gate3.set(true);
        fx.renameWindow.set(Missing.NAME_KEY_DROP_MOVE);
        String from = registered("move");
        long id = fx.idOf(from);
        String to = CollectionRouteFixture.label("rr-move-to-" + (n++));
        assertRefused(renameByLabel(from, to), MISSING_MOVE, from, id, to);
    }

    // ---- from gate 3, the move done, a changeset not ------------------------------------------------------------------

    /**
     * RDR line 2728: "a rename after that move completes and before every P3.5b changeset has applied is refused with the
     * restart named"; item 6 (line ~1458): "The typed error names the missing condition, the move while it has no
     * {@code completed_at}, else the restart that applies P3.5b's changesets."
     */
    @Test
    void fromGate3_afterTheMove_beforeEveryP35bChangesetHasApplied_isRefusedNamingTheRestart() {
        fx.gate3.set(true);
        fx.renameWindow.set(Missing.P35B_RESTART);
        String from = registered("restart");
        long id = fx.idOf(from);
        String to = CollectionRouteFixture.label("rr-restart-to-" + (n++));
        assertRefused(renameByLabel(from, to), MISSING_RESTART, from, id, to);
    }

    /** The collection is named by id, as P3.5a lets every route do (item 6): the refusal does not depend on the spelling. */
    @Test
    void fromGate3_aRenameByIdIsRefusedTheSameWay() {
        fx.gate3.set(true);
        for (var window : new Missing[] {Missing.NAME_KEY_DROP_MOVE, Missing.P35B_RESTART}) {
            fx.renameWindow.set(window);
            String from = registered("byid-w" + window.ordinal());
            long id = fx.idOf(from);
            String to = CollectionRouteFixture.label("rr-byid-to-w" + window.ordinal() + "-" + (n++));
            assertRefused(renameById(id, to),
                window == Missing.NAME_KEY_DROP_MOVE ? MISSING_MOVE : MISSING_RESTART, from, id, to);
        }
    }

    // ---- gate 3 is read on each request -------------------------------------------------------------------------------

    /**
     * RDR line 2656: "reads by the id once the check sees gate 3 hold, with no restart". The signal is the LATCHED gate 3 (linda
     * 2026-11-23, review SB1; Gate3's javadoc): after P3.7's first one-row rename the name columns are stale, so a rename must
     * never be let through by a gate that dropped back. One running service: allowed before the latch, refused once the check's
     * sample latches it, and still refused after a later sample in which the live gate 3 fails (an id index goes INVALID).
     */
    @Test
    void theRefusalFollowsGate3WithNoRestart() {
        fx.renameWindow.set(Missing.NAME_KEY_DROP_MOVE);
        String a = registered("flip-a");
        String b = registered("flip-b");
        String c = registered("flip-c");
        long idA = fx.idOf(a);
        long idC = fx.idOf(c);

        String a2 = CollectionRouteFixture.label("rr-flip-a2-" + (n++));
        String b2 = CollectionRouteFixture.label("rr-flip-b2-" + (n++));
        String c2 = CollectionRouteFixture.label("rr-flip-c2-" + (n++));

        Resp allowed = renameByLabel(b, b2);
        assertThat(allowed.status()).as("before the latch: the name path: %s", allowed.body()).isEqualTo(200);

        fx.gate3.sample(true);
        assertRefused(renameByLabel(a, a2), MISSING_MOVE, a, idA, a2);

        fx.gate3.sample(false);
        assertRefused(renameByLabel(c, c2), MISSING_MOVE, c, idC, c2);
    }

    // ---- the rename answer ---------------------------------------------------------------------------------------------

    /**
     * Residual S-7 acceptance (bead note on nexus-p2m1x.23, marlowe): "the rename response returns the renamed row: the new
     * label, its {@code collection_id} and {@code ids_stable} ... This client renames ... drops its entries for the old
     * label and the old id and rebinds from that row." RDR line 2662: "responses carry both." While the drop move has no
     * {@code completed_at} the id is not stable (a name-path rename gave the collection a new id), so {@code ids_stable} is
     * false. P3.7 owns the signal's true value; P3.5a carries the key.
     */
    @Test
    void theRenameResponseCarriesTheRenamedRow() {
        fx.renameWindow.set(Missing.NAME_KEY_DROP_MOVE);
        String from = registered("answer");
        long oldId = fx.idOf(from);
        String to = CollectionRouteFixture.label("rr-answer-to-" + (n++));
        Resp r = renameByLabel(from, to);
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        JsonNode j = r.json();
        assertThat(j.path("name").asText()).as("the new label: %s", r.body()).isEqualTo(to);
        assertThat(j.path("collection_id").asLong(-1)).as("its id: %s", r.body()).isEqualTo(fx.idOf(to)).isNotEqualTo(oldId);
        assertThat(j.path("ids_stable").isBoolean()).as("ids_stable is present: %s", r.body()).isTrue();
        assertThat(j.path("ids_stable").asBoolean()).as("the id is not stable before the drop move completes").isFalse();
        assertThat(j.has("renamed")).as("the existing per-table counts stay (additive): %s", r.body()).isTrue();
    }

    /**
     * linda's ruling 5 (2026-11-22): "every response carrying {@code ids_stable} also carries {@code catalog_epoch}", from
     * baton's {@code CatalogEpoch.of(ctx, tenant)} (the first 16 hex characters of a sha256 over the cluster's system
     * identifier, timeline, postmaster start time and the tenant's restore marker; empty unless read inside the rename's
     * {@code withTenant} scope). On a stock cluster the engine role can read the pg_control
     * functions, so the epoch is there, beside {@code ids_stable}, in the states a catalog rename answers 200 in: no latch row, with
     * the drop move missing or with the window closed (from the row with the window closed the catalog rename fails closed until
     * P3.7 supplies its one-row rename: linda's R-A ruling, {@link #fromTheRow_withTheWindowClosed_theCatalogRenameFailsClosed}). The value is a function of the cluster and
     * the tenant, so two renames with nothing changed between them answer the same one. The test reads the epoch only through
     * the response, so it needs no production symbol.
     */
    @Test
    void theRenameResponseCarriesCatalogEpochBesideIdsStable_whenTheEpochIsAvailable() {
        String first = null;
        for (boolean windowClosed : new boolean[] {false, true}) {
            fx.gate3.set(false);
            fx.renameWindow.set(windowClosed ? Missing.P35B_RESTART : Missing.NAME_KEY_DROP_MOVE);
            String from = registered("epoch-w" + windowClosed);
            String to = CollectionRouteFixture.label("rr-epoch-to-w" + windowClosed + "-" + (n++));
            Resp r = renameByLabel(from, to);
            assertThat(r.status()).as(r.body()).isEqualTo(200);
            JsonNode j = r.json();
            assertThat(j.path("ids_stable").isBoolean()).as("ids_stable is present: %s", r.body()).isTrue();
            assertThat(j.has("catalog_epoch")).as("catalog_epoch rides with ids_stable: %s", r.body()).isTrue();
            assertThat(j.path("catalog_epoch").isTextual()).as("a string, never null: %s", r.body()).isTrue();
            assertThat(j.path("catalog_epoch").asText()).as("the first 16 hex characters of a sha256")
                .matches("[0-9a-f]{16}");
            if (first == null) {
                first = j.path("catalog_epoch").asText();
            } else {
                assertThat(j.path("catalog_epoch").asText()).as("nothing changed between the two renames").isEqualTo(first);
            }
        }
    }

    /**
     * linda's ruling 5, and the P3.7 acceptance she added 2026-11-22: "if the engine role cannot read
     * {@code pg_control_system}/{@code pg_control_checkpoint}, no epoch is reported (reads unstable), tested with the privilege
     * revoked". With EXECUTE revoked on both from PUBLIC and from the engine's role the rename still answers 200, with
     * {@code ids_stable} and with the {@code catalog_epoch} KEY OMITTED: {@code has()} is false, which is not the same as a
     * present null. Non-vacuity: the same engine answers with an epoch before the revoke; the privilege is given back to PUBLIC
     * after, the state a stock cluster starts in.
     */
    @Test
    void theRenameResponseOmitsCatalogEpoch_neverNull_whenTheEngineRoleCannotReadTheCluster() {
        fx.gate3.set(false);
        fx.renameWindow.set(Missing.NAME_KEY_DROP_MOVE);
        Resp before = renameByLabel(registered("noepoch-a"), CollectionRouteFixture.label("rr-noepoch-a2-" + (n++)));
        assertThat(before.status()).as(before.body()).isEqualTo(200);
        assertThat(before.json().path("catalog_epoch").isTextual()).as("non-vacuity: there is an epoch before the revoke: %s",
            before.body()).isTrue();

        try {
            runPgControlChangelog("revoke");
            Boolean svcCanExecute = fx.asSuperuser(ctx -> ctx.select(DSL.function("has_function_privilege", Boolean.class,
                    DSL.inline(PgContainerHelper.SVC_USERNAME), DSL.inline("pg_catalog.pg_control_system()"), DSL.inline("EXECUTE")))
                .fetchOne(0, Boolean.class));
            assertThat(svcCanExecute).as("the revoke took: the engine role cannot execute pg_control_system()")
                .isFalse();

            Resp r = renameByLabel(registered("noepoch-b"), CollectionRouteFixture.label("rr-noepoch-b2-" + (n++)));
            assertThat(r.status()).as("the rename still succeeds: %s", r.body()).isEqualTo(200);
            JsonNode j = r.json();
            assertThat(j.path("ids_stable").isBoolean()).as("ids_stable stays: %s", r.body()).isTrue();
            assertThat(j.has("catalog_epoch")).as("the key is omitted, not null: %s", r.body()).isFalse();
        } finally {
            runPgControlChangelog("grant");
        }
    }

    /** Runs db/changelog-test/db.changelog-test-p35a-pgcontrol-{mode}.xml as the superuser (fixed names; RawSqlGateTest). */
    private void runPgControlChangelog(String mode) {
        fx.asSuperuser(ctx -> {
            ctx.connection(c -> PgContainerHelper.runSuperuserTestChangelog(c,
                "db/changelog-test/db.changelog-test-p35a-pgcontrol-" + mode + ".xml",
                "databasechangelog_test_p35a_pgcontrol_" + mode, java.util.Map.of()));
            return null;
        });
    }

    // ---- the per-store rename routes (linda's ruling 3) ----------------------------------------------------------------

    /** One state of the two seams in which a rename is not refused. */
    record State(boolean gate3, Missing window) { }

    static final List<State> OPEN_STATES = List.of(new State(false, Missing.NAME_KEY_DROP_MOVE), new State(false, Missing.P35B_RESTART));

    private Resp storeRename(StoreRename route, String from, String to) {
        return fx.post(route.path(), "{\"old\":" + json(from) + ",\"new\":" + json(to) + "}");
    }

    /**
     * linda's ruling 3 (2026-11-22): "the rename refusal lives in the shared rename primitive so every entry point, per-store
     * rename_collection routes included, gets it". Each of the five per-store routes ({@link
     * CollectionRouteCensus#STORE_RENAMES}) rewrites a collection name in one store; from gate 3, in either window, each is
     * refused with the same typed 409 as the catalog rename, naming the same missing condition, and rewrites nothing. The
     * routes need the new label registered (they re-point rows at its registry row), so both ends are registered here: the
     * refusal is not an unregistered-target answer. The taxonomy route is also checked on its data, a topic filed under the old
     * label stays there.
     */
    @Test
    void everyPerStoreRenameRoute_fromGate3_isRefusedLikeTheCatalogRename_andRewritesNothing() {
        assertThat(CollectionRouteCensus.STORE_RENAMES).as("non-vacuity: the five routes").hasSize(5);
        for (StoreRename route : CollectionRouteCensus.STORE_RENAMES) {
            for (Missing window : new Missing[] {Missing.NAME_KEY_DROP_MOVE, Missing.P35B_RESTART}) {
                fx.gate3.set(false);
                fx.renameWindow.set(window);
                String from = registered("store-from");
                String to = registered("store-to");
                long fromId = fx.idOf(from);
                long toId = fx.idOf(to);
                assertThat(fx.post("/v1/taxonomy/topics/insert",
                    "{\"collection\":" + json(from) + ",\"label\":\"p35a-store-topic\",\"doc_count\":1}").status())
                    .as("setup: a topic filed under the old label").isEqualTo(200);

                fx.gate3.set(true);
                Resp r = storeRename(route, from, to);

                String what = route.path() + " in window " + window + ": " + r.body();
                assertThat(r.status()).as(what).isEqualTo(409);
                JsonNode j = r.json();
                assertThat(j.path("reason").asText()).as("typed: %s", what).isEqualTo(REASON);
                assertThat(j.path("missing").asText()).as("names the missing condition: %s", what)
                    .isEqualTo(window == Missing.NAME_KEY_DROP_MOVE ? MISSING_MOVE : MISSING_RESTART);
                assertThat(j.path("error").asText()).as("and says it in words").isNotBlank();
                assertThat(fx.idOf(from)).as("nothing renamed: %s", route.path()).isEqualTo(fromId);
                assertThat(fx.idOf(to)).isEqualTo(toId);
                assertThat(fx.supersededBy(from)).isEqualTo("");
                fx.gate3.set(false);
                assertThat(fx.get("/v1/taxonomy/topics?collection=" + java.net.URLEncoder.encode(from,
                    java.nio.charset.StandardCharsets.UTF_8)).body()).as("the topic is still filed under the old label (%s)",
                    what).contains("p35a-store-topic");
                assertThat(fx.get("/v1/taxonomy/topics?collection=" + java.net.URLEncoder.encode(to,
                    java.nio.charset.StandardCharsets.UTF_8)).body()).as("and none under the new one (%s)", what)
                    .doesNotContain("p35a-store-topic");
            }
        }
    }

    /**
     * The refusal's own conditions, on the same five routes: with no latch row (any window) a per-store rename is the name path as
     * before and answers 200. Without this the test above could pass on a route that refuses everything. (In the one-row regime the
     * route is a no-op that checks the registry rename happened: {@link #everyPerStoreRenameRoute_inTheOneRowRegime_answers409WhenTheRegistryRenameDidNotHappen}.)
     */
    @Test
    void everyPerStoreRenameRoute_isNotRefusedBeforeGate3() {
        for (StoreRename route : CollectionRouteCensus.STORE_RENAMES) {
            for (State st : OPEN_STATES) {
                fx.gate3.set(st.gate3());
                fx.renameWindow.set(st.window());
                String from = registered("store-open-from");
                String to = registered("store-open-to");
                Resp r = storeRename(route, from, to);
                String what = route.path() + " in " + st + ": " + r.body();
                assertThat(r.reason()).as(what).isNotEqualTo(REASON);
                assertThat(r.status()).as(what).isEqualTo(200);
            }
        }
    }

    /**
     * linda's Q-A: in the one-row regime a per-store rename is a no-op only if the registry rename moved the label. Here it did not
     * (both labels are live rows), so each of the five routes is a typed 409 {@code rename_not_in_registry} and changes nothing;
     * {@code RenameNoOpOneRowTest} holds the 200 for a pair the registry did rename, with the rows compared by xmin.
     */
    @Test
    void everyPerStoreRenameRoute_inTheOneRowRegime_answers409WhenTheRegistryRenameDidNotHappen() {
        for (StoreRename route : CollectionRouteCensus.STORE_RENAMES) {
            fx.gate3.set(true);
            fx.renameWindow.set(Missing.NONE);
            String from = registered("onerow-from");
            String to = registered("onerow-to");
            Resp r = storeRename(route, from, to);
            String what = route.path() + ": " + r.body();
            assertThat(r.status()).as(what).isEqualTo(409);
            assertThat(r.reason()).as(what).isEqualTo("rename_not_in_registry");
            assertThat(r.json().path("error").asText()).as(what).contains("rename the collection in the registry first");
            assertThat(fx.supersededBy(from)).as("nothing changed").isEqualTo("");
        }
    }

    // ---- the window closed ---------------------------------------------------------------------------------------------

    /**
     * RDR line 2656 (the P3.5 test), second clause, as linda's R-A ruling (2026-11-23) leaves it on this base: from the latch row
     * with the drop move complete and every P3.5b changeset applied, the {@code rename_refused} refusal does not apply, and the
     * rename that runs is P3.7's one-row rename. P3.7 is not on this base, so the catalog rename's one-row body is the fail-closed
     * default: 409 {@code rename_path_mismatch}, nothing renamed (running the name path would rewrite name columns the one-row
     * regime's keys depend on). This replaces the earlier assertion that this state answered 200 on the name path (a spec change from
     * the ruling); P3.7's acceptance test covers the real one-row rename.
     */
    @Test
    void fromTheRow_withTheWindowClosed_theCatalogRenameFailsClosed() {
        fx.gate3.set(true);
        fx.renameWindow.set(Missing.NONE);
        String from = registered("open");
        long id = fx.idOf(from);
        String to = CollectionRouteFixture.label("rr-open-to-" + (n++));
        Resp r = renameByLabel(from, to);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).as("not the window refusal: %s", r.body()).isEqualTo("rename_path_mismatch");
        assertThat(r.json().path("error").asText()).isNotBlank();
        assertThat(fx.idOf(from)).as("nothing renamed").isEqualTo(id);
        assertThat(fx.supersededBy(from)).isEqualTo("");
        assertThat(fx.exists(to)).isFalse();
    }
}
