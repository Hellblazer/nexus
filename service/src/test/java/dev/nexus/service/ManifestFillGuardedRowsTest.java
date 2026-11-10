// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd, route R2, fix round r1 (S1): every shared guard of the fillable predicate, exercised through the FILL
 * for both kinds, each on a row that is fillable but for that one guard, beside a control row that is fillable. The
 * fill writes the control and leaves each guarded row exactly as it was, whatever the guard: a co-owned chunk, a chunk
 * a tombstoned document also holds, an alias document, an orphan-minted document, a chunk titled for another file, and
 * a chunk stamped more than five minutes after its document.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillGuardedRowsTest extends ManifestFillTestBase {

    private record Fixture(String control, String[] guarded) { }

    /** Each guarded row's chunk carries a label and a line start, so it is fillable by BOTH kinds but for its guard. */
    private Fixture fixture() throws Exception {
        String control = doc("control");
        chunk(TENANT, h(0x30), both(OK_AT));
        manifest(control, h(0x30));

        // co-owned: two live documents hold the chash, so neither row is the chunk's one live owner
        String coA = doc("co-owner a"), coB = doc("co-owner b");
        chunk(TENANT, h(0x31), both(OK_AT));
        manifest(coA, h(0x31));
        manifest(coB, h(0x31));

        // a live document and a tombstoned one hold the chash: the tombstone's rows are not the live owner's to fill
        String live = doc("co-owner live"), gone = doc("co-owner gone");
        chunk(TENANT, h(0x32), both(OK_AT));
        manifest(live, h(0x32));
        manifest(gone, h(0x32));
        tombstone(gone);

        // an alias of another document
        String alias = doc("alias");
        chunk(TENANT, h(0x33), both(OK_AT));
        manifest(alias, h(0x33));
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, "1.1.1")
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(alias))).execute();
            assertThat(n).isEqualTo(1);
        }

        // minted by the orphan backfill
        String minted = doc(TOKEN, TENANT, "minted", "", DOC_AT, "{\"backfill_from\":\"t3_orphan\"}");
        chunk(TENANT, h(0x34), both(OK_AT));
        manifest(minted, h(0x34));

        // the chunk is titled for another file than the document's
        String mismatch = doc(TOKEN, TENANT, "mine", "docs/mine.md", DOC_AT, "{}");
        chunk(TENANT, h(0x35), both(OK_AT, "docs/theirs.md"));
        manifest(mismatch, h(0x35));

        // stamped one second past the five-minute skew after the document
        String skewed = doc("skewed");
        chunk(TENANT, h(0x36), both("2026-10-01T12:05:01+00:00"));
        manifest(skewed, h(0x36));

        return new Fixture(control, new String[] {coA, coB, live, gone, alias, minted, mismatch, skewed});
    }

    private void assertTheFillWritesOnlyTheControl(String kind, Row control) throws Exception {
        var f = fixture();
        assertThat(fillable(kind)).as(kind + ": the control is the only fillable row").isEqualTo(1);

        var status = runFill(kind, false, 3);

        assertThat(status).containsEntry("state", "done").containsEntry("filled", 1).containsEntry("done_rows", 1);
        assertThat(counts(status)).containsEntry("fillable", 1).containsEntry("skipped_changed", 0);
        assertThat(row(f.control(), 0)).as(kind + ": the control").isEqualTo(control);
        String[] names = {"co-owned (a)", "co-owned (b)", "the live holder of a tombstoned co-owner's chunk",
            "the tombstoned co-owner", "alias", "orphan-minted", "title mismatch", "skew violation"};
        for (int i = 0; i < f.guarded().length; i++) {
            assertThat(row(f.guarded()[i], 0)).as(kind + ": guarded row " + i + " (" + names[i] + ") is left unfilled")
                .isEqualTo(LEGACY);
        }
        assertThat(fillable(kind)).isZero();
    }

    @Test
    void labelsFillTheControlAndNoGuardedRow() throws Exception {
        assertTheFillWritesOnlyTheControl("labels", new Row(true, "Intro", "", null, null, null, null, null));
    }

    @Test
    void spansFillTheControlAndNoGuardedRow() throws Exception {
        assertTheFillWritesOnlyTheControl("spans", new Row(false, null, null, null, 5, null, null, null));
    }
}
