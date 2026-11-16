// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.ManifestCheck;
import dev.nexus.service.db.ManifestCheckAdmin;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * nexus-6pfgb.42 (substantive-critic r1 finding 3): with the {@code labels_written} CHECK in place, every live writer of
 * the manifest still works, and the one request the CHECK exists to refuse is refused the way a client sees it. The
 * writers run through HTTP on the real engine and a real PostgreSQL: {@code manifest/write} (the PLAIN arm of
 * {@code insertManifestChunkRows}), {@code manifest/append} and {@code append_many} (UPSERT_APPEND), {@code import/chunk}
 * (UPSERT_IMPORT) and the label rung of the manifest fill (an UPDATE). Each is given labelled rows, legacy rows (no label
 * key, or the stored pair {@code labels_written false} with NULL labels, as a read-back sends it) and a rewrite of one by
 * the other. The constraint is added NOT VALID, which is enough: it is evaluated on every new row and every update.
 *
 * <p>The refusal is {@code labels_written: false} beside a label (the audit's one way a writer can break the CHECK: an
 * explicit boolean wins in {@code labelsWritten(row)}). A single-document route answers 409 with the SQLSTATE and the
 * constraint name; a batch route answers 200 with the document in {@code failed_doc_ids} and the check-constraint reason.
 * The reattach (a plpgsql path) names neither the labels nor the marker, so it has no case here.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestLabelsCheckWritersTest extends ManifestFillTestBase {

    private static final String CONSTRAINT = "catalog_document_chunks_labels_written_check";
    private static final String CHECK_VIOLATION = "23514";

    private ManifestCheckAdmin admin() {
        return new ManifestCheckAdmin(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()),
            ManifestCheck.LABELS);
    }

    @BeforeAll
    void putTheCheckOn() {
        var added = admin().add();
        assertThat(added.action()).isEqualTo("added");
        assertThat(added.after().present()).isTrue();
        assertThat(added.after().validated()).as("added NOT VALID").isFalse();
        assertThat(added.after().definition()).as("the stored definition, as PostgreSQL deparses it").isEqualTo(
            "CHECK ((labels_written OR ((section_title IS NULL) AND (section_type IS NULL) AND (page_number IS NULL)))) "
                + "NOT VALID");
    }

    @AfterAll
    void takeTheCheckOff() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENT_CHUNKS)
                .dropConstraintIfExists(DSL.name(CONSTRAINT)).execute();
        }
    }

    // ---- request builders -------------------------------------------------------------------------------------

    /** One manifest row of a request: the chash seed and the alternating key/value pairs after it. */
    private static Map<String, Object> req(int position, int chashSeed, Object... keyValues) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("position", position);
        row.put("chash", h(chashSeed));
        for (int i = 0; i < keyValues.length; i += 2) {
            row.put((String) keyValues[i], keyValues[i + 1]);
        }
        return row;
    }

    private static Map<String, Object> labelled(int position, int chashSeed, String title, String type, Integer page) {
        return req(position, chashSeed, "section_title", title, "section_type", type, "page_number", page);
    }

    /** The unstated pair: no label key at all, as a client that predates the labels sends it. */
    private static Map<String, Object> legacy(int position, int chashSeed) {
        return req(position, chashSeed);
    }

    private static Row written(String title, String type, Integer page) {
        return new Row(true, title, type, page, null, null, null, null);
    }

    /** A document with its chunk rows (seeds {@code 1..chunks}) behind it, no manifest yet. */
    private String docWithChunks(int chunks) throws Exception {
        String d = doc("labels writers doc " + seq);
        for (int i = 1; i <= chunks; i++) {
            chunk(TENANT, h(i), "{}");
        }
        return d;
    }

    private HttpResponse<String> send(String route, String docId, List<Map<String, Object>> rows) throws Exception {
        return post(TOKEN, TENANT, route, mapper.writeValueAsString(
            Map.of("doc_id", docId, "collection", COLL, "rows", rows)));
    }

    private HttpResponse<String> sendMany(String route, List<String> docIds, List<Map<String, Object>> rows)
            throws Exception {
        List<Map<String, Object>> docs = new ArrayList<>();
        for (String id : docIds) {
            docs.add(Map.of("doc_id", id, "rows", rows));
        }
        return post(TOKEN, TENANT, route, mapper.writeValueAsString(Map.of("collection", COLL, "docs", docs)));
    }

    private void ok(HttpResponse<String> r) {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
    }

    // ---- the writers, with the CHECK in place ------------------------------------------------------------------

    @Test
    void manifestWrite_plainArm_takesLabelledRowsAndLegacyRows() throws Exception {
        String d = docWithChunks(4);

        ok(send("/v1/catalog/manifest/write", d, List.of(
            labelled(0, 1, "Intro", "heading", 3),
            legacy(1, 2),
            req(2, 3, "section_title", ""),                           // written: "no heading here" is still an answer
            req(3, 4, "labels_written", false, "section_title", null, "section_type", null, "page_number", null))));

        assertThat(row(d, 0)).isEqualTo(written("Intro", "heading", 3));
        assertThat(row(d, 1)).as("no label key: legacy").isEqualTo(LEGACY);
        assertThat(row(d, 2)).isEqualTo(written("", null, null));
        assertThat(row(d, 3)).as("the stored legacy pair, stated").isEqualTo(LEGACY);
        assertThat(admin().state().present()).as("fixture: the CHECK is on while these writes run").isTrue();
    }

    @Test
    void aManifestReadBackAndRewrittenAsItWasRead_stillGoesThrough_legacyRowsStayLegacy() throws Exception {
        String d = docWithChunks(2);
        ok(send("/v1/catalog/manifest/write", d, List.of(legacy(0, 1), labelled(1, 2, "Body", "prose", 9))));
        var read = get(TOKEN, TENANT, "/v1/catalog/manifest/get?doc_id=" + d);
        assertThat(read.statusCode()).as(read.body()).isEqualTo(200);
        JsonNode rows = mapper.readTree(read.body()).path("rows");
        assertThat(rows).as("fixture: both rows read back").hasSize(2);
        assertThat(rows.get(0).path("labels_written").asBoolean()).isFalse();
        assertThat(rows.get(1).path("labels_written").asBoolean()).isTrue();

        // the read-back carries the three label keys on the legacy row too (null) and labels_written false
        ok(post(TOKEN, TENANT, "/v1/catalog/manifest/write", mapper.writeValueAsString(
            Map.of("doc_id", d, "collection", COLL, "rows", mapper.convertValue(rows, List.class)))));

        assertThat(row(d, 0)).as("a legacy row stays legacy, though the read carried the label keys").isEqualTo(LEGACY);
        assertThat(row(d, 1)).isEqualTo(written("Body", "prose", 9));
    }

    @Test
    void manifestAppend_upsertAppendArm_rewritesInBothDirections_andInsertsNewPositions() throws Exception {
        String d = docWithChunks(3);
        ok(send("/v1/catalog/manifest/write", d, List.of(labelled(0, 1, "A", "heading", 1), legacy(1, 2))));

        ok(send("/v1/catalog/manifest/append", d, List.of(
            legacy(0, 1),                                             // conflict: labels go back to NULL, marker false
            labelled(1, 2, "B", "prose", 2),                          // conflict: labels set, marker true
            labelled(2, 3, "C", "code", null))));                     // a new position

        assertThat(row(d, 0)).isEqualTo(LEGACY);
        assertThat(row(d, 1)).isEqualTo(written("B", "prose", 2));
        assertThat(row(d, 2)).isEqualTo(written("C", "code", null));
    }

    @Test
    void manifestAppendMany_takesLabelledAndLegacyRows_withNoFailure() throws Exception {
        String a = docWithChunks(2);
        String b = doc("labels writers second doc");

        var r = sendMany("/v1/catalog/manifest/append_many", List.of(a, b),
            List.of(labelled(0, 1, "Intro", "heading", 1), legacy(1, 2)));

        ok(r);
        JsonNode body = mapper.readTree(r.body());
        assertThat(body.path("failed_doc_ids")).as(r.body()).isEmpty();
        assertThat(body.path("docs").asInt()).isEqualTo(2);
        for (String d : List.of(a, b)) {
            assertThat(row(d, 0)).isEqualTo(written("Intro", "heading", 1));
            assertThat(row(d, 1)).isEqualTo(LEGACY);
        }
    }

    @Test
    void manifestWriteMany_takesLabelledAndLegacyRows_withNoFailure() throws Exception {
        String a = docWithChunks(2);

        var r = sendMany("/v1/catalog/manifest/write_many", List.of(a),
            List.of(labelled(0, 1, "Intro", "heading", 1), legacy(1, 2)));

        ok(r);
        assertThat(mapper.readTree(r.body()).path("failed_doc_ids")).as(r.body()).isEmpty();
        assertThat(row(a, 0)).isEqualTo(written("Intro", "heading", 1));
        assertThat(row(a, 1)).isEqualTo(LEGACY);
    }

    @Test
    void importChunk_upsertImportArm_rewritesInBothDirections() throws Exception {
        String d = docWithChunks(2);
        ok(send("/v1/catalog/import/chunk", d, List.of(labelled(0, 1, "A", "heading", 1), legacy(1, 2))));
        assertThat(row(d, 0)).isEqualTo(written("A", "heading", 1));
        assertThat(row(d, 1)).isEqualTo(LEGACY);

        ok(send("/v1/catalog/import/chunk", d, List.of(legacy(0, 1), labelled(1, 2, "B", "prose", 4))));

        assertThat(row(d, 0)).isEqualTo(LEGACY);
        assertThat(row(d, 1)).isEqualTo(written("B", "prose", 4));
    }

    @Test
    void theLabelRungOfTheManifestFill_writesLabelsAndTheMarkerTogether_underTheCheck() throws Exception {
        String d = oneRow(0x21, "{\"section_title\":\"Intro\",\"section_type\":\"heading\",\"page_number\":3,"
            + "\"indexed_at\":\"" + OK_AT + "\"}");
        assertThat(row(d, 0)).as("fixture: a legacy row").isEqualTo(LEGACY);

        var status = runFill("labels");

        assertThat(status).containsEntry("state", "done").containsEntry("filled", 1);
        assertThat(row(d, 0)).isEqualTo(written("Intro", "heading", 3));
        assertThat(admin().state().present()).as("fixture: the CHECK was on during the fill").isTrue();
    }

    // ---- the refusal, as a client sees it ----------------------------------------------------------------------

    /** Each label alone, beside {@code labels_written: false}: the three shapes the CHECK refuses. */
    private static List<Map<String, Object>> contradictions(int position, int chashSeed) {
        return List.of(
            req(position, chashSeed, "labels_written", false, "section_title", "x"),
            req(position, chashSeed, "labels_written", false, "section_type", "x"),
            req(position, chashSeed, "labels_written", false, "page_number", 5));
    }

    private void assertRefused409(HttpResponse<String> r) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = mapper.readTree(r.body());
        assertThat(body.path("error").asText()).isEqualTo("integrity constraint violation");
        assertThat(body.path("sqlstate").asText()).isEqualTo(CHECK_VIOLATION);
        assertThat(body.path("constraint").asText()).isEqualTo(CONSTRAINT);
        assertThat(body.path("remedy").asText()).as("the body says what to send instead")
            .contains("labels_written true").contains("omit labels_written");
    }

    @Test
    void aLabelBesideLabelsWrittenFalse_isRefusedOnEverySingleDocumentWriter_with409_andTheRowsAreLeftAlone()
            throws Exception {
        String d = docWithChunks(1);
        ok(send("/v1/catalog/manifest/write", d, List.of(labelled(0, 1, "Keep", "heading", 7))));

        for (String route : List.of("/v1/catalog/manifest/write", "/v1/catalog/manifest/append",
                "/v1/catalog/import/chunk")) {
            for (var contradiction : contradictions(0, 1)) {
                assertRefused409(send(route, d, List.of(contradiction)));
            }
            // the same request on a position with no row yet is an INSERT, refused the same way
            assertRefused409(send(route, d, List.of(req(5, 1, "labels_written", false, "section_title", "x"))));
        }

        assertThat(row(d, 0)).as("every refused write rolled back").isEqualTo(written("Keep", "heading", 7));
        assertThat(get(TOKEN, TENANT, "/v1/catalog/manifest/get?doc_id=" + d).body()).contains("\"count\":1");
    }

    @Test
    void aLabelBesideLabelsWrittenFalse_onABatchRoute_answers200_withTheDocumentInFailedDocIds_andTheOtherDocWritten()
            throws Exception {
        String bad = docWithChunks(1);
        String good = doc("labels writers good doc");
        for (String route : List.of("/v1/catalog/manifest/write_many", "/v1/catalog/manifest/append_many")) {
            for (var contradiction : contradictions(0, 1)) {
                // one request, two documents: the refused one fails alone
                List<Map<String, Object>> docs = List.of(
                    Map.of("doc_id", bad, "rows", List.of(contradiction)),
                    Map.of("doc_id", good, "rows", List.of(labelled(0, 1, "Ok", "prose", 1))));
                var r = post(TOKEN, TENANT, route, mapper.writeValueAsString(Map.of("collection", COLL, "docs", docs)));

                ok(r);
                JsonNode body = mapper.readTree(r.body());
                assertThat(body.path("failed_doc_ids")).as(route + " " + r.body()).hasSize(1);
                assertThat(body.path("failed_doc_ids").get(0).asText()).isEqualTo(bad);
                assertThat(body.path("failed").get(0).path("reason").asText())
                    .isEqualTo("check constraint violation [" + CONSTRAINT + "]");
                assertThat(body.path("failed").get(0).path("sqlstate").asText()).isEqualTo(CHECK_VIOLATION);
                assertThat(body.path("docs").asInt()).as("the other document is written").isEqualTo(1);
                assertThat(row(good, 0)).isEqualTo(written("Ok", "prose", 1));
            }
        }
        assertThat(get(TOKEN, TENANT, "/v1/catalog/manifest/get?doc_id=" + bad).body())
            .as("nothing of the refused document was written").contains("\"count\":0");
    }
}
