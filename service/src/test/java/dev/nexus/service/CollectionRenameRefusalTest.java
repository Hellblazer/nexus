// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

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
        fx.renameWindow.set(Missing.NONE);
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
        for (Missing window : Missing.values()) {
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
     * RDR line 2656: "reads by the id once the check sees gate 3 hold, with no restart". Item 6 (Gate3's javadoc): gate 3
     * "can drop back to false (an id index going INVALID makes gate 3 fail again)". One running service: refused while gate 3
     * holds, allowed when it does not, refused again when it holds again.
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

        fx.gate3.set(true);
        assertRefused(renameByLabel(a, a2), MISSING_MOVE, a, idA, a2);

        fx.gate3.set(false);
        Resp allowed = renameByLabel(b, b2);
        assertThat(allowed.status()).as("gate 3 dropped back: the name path again: %s", allowed.body()).isEqualTo(200);

        fx.gate3.set(true);
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

    // ---- the window closed ---------------------------------------------------------------------------------------------

    /**
     * RDR line 2656 (the P3.5 test), second clause: from gate 3 a rename is refused "until P3.7's one-row rename can run".
     * With gate 3 holding, the drop move complete and every P3.5b changeset applied, the refusal does not apply. The rename
     * that then runs is P3.7's one-row rename, which keeps the id; this test asserts only that the refusal is gone and the
     * rename succeeds, and it passes with P3.5a alone (the name path still works). The id-keeping half is P3.7's own test.
     * Residual R8 (nexus-p2m1x.47): the P3.5 test is closed with P3.7 on the same branch.
     */
    @Test
    void fromGate3_withTheMoveCompleteAndEveryChangesetApplied_theRefusalDoesNotApply() {
        fx.gate3.set(true);
        fx.renameWindow.set(Missing.NONE);
        String from = registered("open");
        String to = CollectionRouteFixture.label("rr-open-to-" + (n++));
        Resp r = renameByLabel(from, to);
        assertThat(r.reason()).as(r.body()).isNotEqualTo(REASON);
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(fx.exists(to)).isTrue();
    }
}
