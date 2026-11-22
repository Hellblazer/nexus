// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * RDR-229 P3.3a (nexus-p2m1x.19): {@code vectors-072-collection-id-fk-twins.xml} read as text, both ways against the one list
 * {@code db/rdr229/p33a-fk-twins.tsv}: a changeset per twin that adds the id foreign key NOT VALID beside the name key and a changeset per
 * twin that registers its validation move. The integration test ({@link Rdr229FkTwinsIntegrationTest}) runs them; this one pins what they
 * say, so an edit that drops a twin, changes an ON DELETE action, a P3.2 value or the template is red before a database is involved.
 */
class Rdr229FkTwinsChangesetShapeTest {

    /** What a registering INSERT lists, in order: the quoted strings and the numbers of its VALUES row. */
    private static final Pattern TOKEN = Pattern.compile("'((?:[^']|'')*)'|(\\d+)");
    private static final Pattern STRING = Pattern.compile("'(?:[^']|'')*'");
    private static final Pattern ROW_DML = Pattern.compile("(?is)\\b(?:update|delete\\s+from|insert\\s+into|merge\\s+into)\\b");

    private static String lower(String s) {
        return s.toLowerCase(Locale.ROOT).replaceAll("\\s+", " ");
    }

    /** The ten values of the VALUES row of the first registering INSERT of {@code sql}: label, template, schema, table, chunk, sleep, timeout, verification, on_complete, changeset. */
    private static List<String> values(String sql) {
        String values = "values (";
        int at = sql.toLowerCase(Locale.ROOT).indexOf(values);
        assertThat(at).as("a registering INSERT").isPositive();
        Matcher m = TOKEN.matcher(sql.substring(at + values.length()));
        List<String> out = new ArrayList<>();
        while (out.size() < 10 && m.find()) {
            out.add(m.group(1) != null ? m.group(1).replace("''", "'") : m.group(2));
        }
        assertThat(out).as("ten values").hasSize(10);
        return out;
    }

    @Test
    void theChangelogHoldsExactlyOneTwinChangesetAndOneMoveChangesetPerRowOfTheList() throws Exception {
        var rows = FkTwinSpecs.twins();
        assertThat(rows).as("non-vacuity: seven twins").hasSize(7);
        List<String> expected = new ArrayList<>();
        for (int i = 0; i < rows.size(); i++) expected.add(FkTwinSpecs.ddlChangeset(i));
        for (int i = 0; i < rows.size(); i++) expected.add(FkTwinSpecs.moveChangeset(i));
        assertThat(FkTwinSpecs.changesets()).extracting(FkTwinSpecs.Changeset::id)
            .as("every changeset of the file is one of the list's, twins before moves").containsExactlyElementsOf(expected);
        assertThat(rows).extracting(FkTwins.Row::label).doesNotHaveDuplicates().allSatisfy(l -> assertThat(l).doesNotContain("/"));
    }

    @Test
    void eachTwinChangesetAddsItsForeignKeyNotValid_copiesTheNameKeysDeleteAction_andCarriesItsDataEffectAndRollback() throws Exception {
        for (int i = 0; i < FkTwinSpecs.twins().size(); i++) {
            FkTwins.Row r = FkTwinSpecs.twins().get(i);
            var cs = FkTwinSpecs.changeset(FkTwinSpecs.ddlChangeset(i));
            String sql = lower(cs.sql());
            String where = r.table();
            assertThat(sql).as(where).contains("add constraint " + r.twin())
                .contains("foreign key (" + r.fkColumns().replace(",", ", ") + ")")
                .contains("references nexus.catalog_collections (" + r.referencedColumns().replace(",", ", ") + ")")
                .contains("on delete " + r.onDelete().toLowerCase(Locale.ROOT))
                .contains("not valid")
                .contains("set local lock_timeout = '5s'")
                .doesNotContain("on update")   // ids never change: the default, NO ACTION
                .doesNotContain("validate constraint");   // the move validates, never a boot
            assertThat(ROW_DML.matcher(STRING.matcher(cs.sql()).replaceAll("''")).find()).as("%s: no row DML", where).isFalse();
            assertThat(r.twin()).as(where).isEqualTo(r.table() + "_" + r.fkColumns().split(",")[1] + "_fk");
            assertThat(r.nameKey()).as(where).isEqualTo(r.table() + "_collection_fk");
            assertThat(cs.comment()).as("%s: its comment states the data effect", where).contains("DATA EFFECT").contains("NOT VALID");
            assertThat(lower(cs.rollback())).as("%s: the rollback drops the twin", where).contains("drop constraint if exists " + r.twin());
            assertThat(sql).as("%s: the name key is not touched", where).doesNotContain(r.nameKey());
        }
    }

    @Test
    void theQueuesTwinAndMoveAreGuardedLikeVectors068_18_andNoOtherChangesetHasAPrecondition() throws Exception {
        for (int i = 0; i < FkTwinSpecs.twins().size(); i++) {
            boolean queue = FkTwinSpecs.twins().get(i).table().equals(FkTwinSpecs.QUEUE);
            for (String id : List.of(FkTwinSpecs.ddlChangeset(i), FkTwinSpecs.moveChangeset(i))) {
                var cs = FkTwinSpecs.changeset(id);
                if (!queue) {
                    assertThat(cs.preConditions()).as(id).isNull();
                    continue;
                }
                assertThat(cs.preConditions()).as(id + ": the queue's changesets wait, they do not fail").isNotNull();
                assertThat(cs.preConditions().getAttribute("onFail")).isEqualTo("CONTINUE");
                assertThat(cs.preConditions().getAttribute("onError")).isEqualTo("CONTINUE");
                String sql = lower(cs.sql());
                assertThat(sql).as(id + ": a DO guard for the absent table").contains("to_regclass('nexus.aspect_extraction_queue') is not null");
                assertThat(lower(cs.comment())).contains("data effect");
            }
        }
    }

    @Test
    void eachMoveIsRegisteredByOneInsert_withP32sValues_theStampsTemplateAndVerificationAndAGuardedValidateOfItsTwin() throws Exception {
        var stamps = FkTwinSpecs.stampChangesets();
        for (int i = 0; i < FkTwinSpecs.twins().size(); i++) {
            FkTwins.Row r = FkTwinSpecs.twins().get(i);
            var cs = FkTwinSpecs.changeset(FkTwinSpecs.moveChangeset(i));
            String sql = cs.sql();
            List<String> v = values(sql);
            String where = r.table();
            assertThat(v.get(0)).as(where).isEqualTo(r.label());
            assertThat(v.get(2)).isEqualTo("nexus");
            assertThat(v.get(3)).isEqualTo(r.table());
            assertThat(v.get(4)).as("%s: chunk_size", where).isEqualTo(Integer.toString(FkTwinSpecs.chunkSize(r.table())));
            assertThat(v.get(5)).as("%s: sleep_ms", where).isEqualTo(Integer.toString(FkTwinSpecs.sleepMs(r.table())));
            assertThat(v.get(6)).as("%s: statement_timeout_ms", where).isEqualTo(Integer.toString(FkTwinSpecs.STATEMENT_TIMEOUT_MS));
            assertThat(v.get(9)).as("%s: the changeset's own id", where).isEqualTo(cs.id());
            var stamp = values(stamps.get(r.table()));
            assertThat(v.get(1)).as("%s: the template is the stamp's, word for word", where).isEqualTo(stamp.get(1));
            assertThat(v.get(7)).as("%s: so is the verification", where).isEqualTo(stamp.get(7));
            assertThat(v.get(1)).contains("<driving_table>").contains("<chunking_clause>");
            String onComplete = lower(v.get(8));
            assertThat(onComplete).as("%s: the on_complete validates the twin, only when pg_constraint has it", where)
                .contains("validate constraint " + r.twin()).contains("pg_constraint").contains("conname = '" + r.twin() + "'");
            assertThat(onComplete).as("%s: and only its twin", where).doesNotContain(r.nameKey()).doesNotContain("_check");
            String outsideStrings = STRING.matcher(sql).replaceAll("''").replaceAll("(?i)insert\\s+into\\s+nexus\\.data_moves", "");
            assertThat(ROW_DML.matcher(outsideStrings).find()).as("%s: the changeset registers, it writes no row of a table", where).isFalse();
            assertThat(lower(cs.rollback())).as(where).contains("delete from nexus.data_moves where label = '" + r.label() + "'");
            assertThat(cs.comment()).as("%s: its comment states the data effect", where).contains("DATA EFFECT");
        }
    }

    @Test
    void theMovesAreRegisteredAfterTheTwins_andNoMoveSitsBeforeItsTwin() throws Exception {
        List<String> ids = FkTwinSpecs.changesets().stream().map(FkTwinSpecs.Changeset::id).toList();
        for (int i = 0; i < FkTwinSpecs.twins().size(); i++) {
            assertThat(ids.indexOf(FkTwinSpecs.moveChangeset(i))).isGreaterThan(ids.indexOf(FkTwinSpecs.ddlChangeset(i)));
        }
    }
}
