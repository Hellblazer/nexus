// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P2.5 (bead nexus-p2m1x.10), ruling of the RDR author 2026-11-20: an ETL import can land a collection with a
 * PLAIN name (no {@code quarantine-} prefix), content_type {@code quarantine-knowledge} and no lifecycle_state, as
 * lifecycle_state {@code quarantine}. The {@code chunks_allow_list} CHECK exempts a collection by NAME prefix
 * ({@code quarantine-}, {@code taxonomy__}) and reads no lifecycle column, so a chunk in that row is NOT exempt: it
 * must satisfy the allow-list, and a retired document key on it is refused. This test pins that behaviour, and the
 * control that the same chunk in a {@code quarantine-} named collection is admitted. It is acceptable because no
 * engine path moves chunks into a plain-named quarantine row (the reaper mints only {@code quarantine-} siblings), and
 * RDR-236's sweep strips retired document keys from quarantine rows too, since a restore copies them into a live
 * collection where the CHECK applies; so a plain-named quarantine row loses nothing a prefixed one keeps after the
 * sweep.
 */
class ImportedPlainNamedQuarantineAllowListTest extends ChunkKeysTestBase {

    private static final String TENANT = "pq-import";
    private static final String CHECK = "chunks_metadata_keys_check";

    private String plainQuarantine;
    private String prefixedQuarantine;

    @BeforeAll
    void importAndAddConstraints() throws Exception {
        plainQuarantine = conformant("pq-plain");
        prefixedQuarantine = "quarantine-" + conformant("pq-prefixed");

        // An ETL import row: plain name, quarantine content type, no lifecycle_state (as LifecycleFromRowTest.reg).
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("name", plainQuarantine);
        row.put("content_type", "quarantine-knowledge");
        row.put("owner_id", "o");
        row.put("embedding_model", MODEL);
        row.put("model_version", "v1");
        repo.importCollection(TENANT, row);

        // The control collection: a quarantine- NAME, registered the way the sibling tests register one.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, prefixedQuarantine, MODEL);
        }

        // The allow-list CHECK in place (NOT VALID: it judges every row written from here on).
        HttpResponse<String> r = post("/v1/admin/chunk-keys/add-constraints", ROOT, plan());
        assertThat(r.statusCode()).as("fixture: the constraints were added: " + r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("constraints").toString()).as("fixture: the allow-list is among them")
            .contains(CHECK);
    }

    private void insert(String collection, String seed, Map<String, Object> metadata) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertChunks(ctx, TENANT, collection, List.of(hexOf(TENANT, collection, seed)),
                List.of(seed + " text"), List.of(new float[384]), List.of(metadata));
        }
    }

    /** CHUNK keys plus the quarantine ROW keys the reaper writes; {@code extra} adds more. */
    private Map<String, Object> quarantineKeyed(String collection, String seed, Object... extra) {
        Map<String, Object> m = new LinkedHashMap<>(keyed(TENANT, collection, seed,
            "origin_collection", conformant("pq-origin"),
            "quarantined_at", "2026-11-01T00:00:00Z",
            "quarantined_by", "engine"));
        for (int i = 0; i < extra.length; i += 2) {
            m.put((String) extra[i], extra[i + 1]);
        }
        return m;
    }

    /** The SQLSTATE of the first {@link SQLException} in the cause chain that carries one. */
    private static String sqlState(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof SQLException se && se.getSQLState() != null) {
                return se.getSQLState();
            }
        }
        return null;
    }

    @Test
    void theImportedRowIsAPlainNamedQuarantineRow_guard() throws Exception {
        assertThat(plainQuarantine).as("fixture: no quarantine- prefix").doesNotStartWith("quarantine-");
        try (Connection su = pg.createConnection("")) {
            var rec = DSL.using(su, SQLDialect.POSTGRES)
                .select(CATALOG_COLLECTIONS.LIFECYCLE_STATE, CATALOG_COLLECTIONS.CONTENT_TYPE)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(plainQuarantine))
                .fetchOne();
            assertThat(rec).as("the import registered the row").isNotNull();
            assertThat(rec.value1()).as("the import landed it as quarantine").isEqualTo("quarantine");
            assertThat(rec.value2()).isEqualTo("quarantine-knowledge");
        }
    }

    @Test
    void aChunkWithOnlyAllowedKeysIsAcceptedInThePlainNamedQuarantineRow() throws Exception {
        String seed = "allowed";
        insert(plainQuarantine, seed, quarantineKeyed(plainQuarantine, seed));

        assertThat(metadataOf(TENANT, plainQuarantine, hexOf(TENANT, plainQuarantine, seed)))
            .containsKeys("chunk_text_hash", "embedding_model", "quarantined_at", "quarantined_by",
                "origin_collection");
    }

    @Test
    void aChunkWithARetiredDocumentKeyIsRefusedInThePlainNamedQuarantineRow_theCheckReadsTheNameNotTheState()
            throws Exception {
        for (String retired : List.of("title", "doc_id")) {
            String seed = "retired-" + retired;
            assertThatThrownBy(() -> insert(plainQuarantine, seed,
                quarantineKeyed(plainQuarantine, seed, retired, "gone")))
                .as("retired key " + retired + " on a plain-named quarantine row")
                .hasMessageContaining(CHECK)
                .satisfies(e -> assertThat(sqlState(e)).as("check_violation").isEqualTo("23514"));
        }
    }

    @Test
    void theSameRetiredKeyChunkIsAdmittedInAQuarantinePrefixedCollection_control() throws Exception {
        String seed = "control";
        insert(prefixedQuarantine, seed, quarantineKeyed(prefixedQuarantine, seed, "title", "legacy", "doc_id", "gone"));

        assertThat(metadataOf(TENANT, prefixedQuarantine, hexOf(TENANT, prefixedQuarantine, seed)))
            .as("the bypass is by NAME prefix: a quarantine- collection keeps its retired keys")
            .containsKeys("title", "doc_id");
    }
}
