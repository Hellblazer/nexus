// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * nexus-6pfgb.42: the {@code labels_written OR (section_title, section_type, page_number all NULL)} CHECK's census and
 * steps on a real PostgreSQL (the steps are {@link ManifestCheckAdminIntegrationBase}'s). A violator is a row that holds
 * one of the three labels while {@code labels_written} is false; the fixtures vary which label it holds, and a clean
 * row is either a legacy row (no label, false) or a written one (labels, true; an empty label is still a label).
 */
class ManifestLabelsCheckAdminIntegrationTest extends ManifestCheckAdminIntegrationBase {

    private static final String OWNER = "mlw_owner_adm";

    @Override
    String ownerRole() {
        return OWNER;
    }

    @Override
    void createOwnerRole(java.sql.Connection su) throws Exception {
        dev.nexus.service.PgContainerHelper.createLoginRole(su, OWNER, OWNER_PASS, false);
    }

    @Override
    ManifestCheck check() {
        return ManifestCheck.LABELS;
    }

    @Override
    String expectedExpression() {
        return "(\"labels_written\" or (\"section_title\" is null and \"section_type\" is null and \"page_number\" is null))";
    }

    @Override
    List<String> definitionWords() {
        return List.of("labels_written", "section_title", "section_type", "page_number");
    }

    @Override
    Map<Field<?>, Object> cleanColumns(int position) {
        Map<Field<?>, Object> columns = new HashMap<>();
        if (position % 2 == 0) {
            columns.put(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, false);   // legacy: NULL labels
        } else {
            columns.put(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true);
            columns.put(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, "");        // written, "none here"
            columns.put(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, "prose");
            columns.put(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, 3);
        }
        return columns;
    }

    @Override
    Map<Field<?>, Object> violatingColumns(int position) {
        Map<Field<?>, Object> columns = new HashMap<>();
        columns.put(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, false);
        switch (position % 3) {   // each of the three labels is the one that breaks it, in turn
            case 0 -> columns.put(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, "A heading");
            case 1 -> columns.put(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, "code");
            default -> columns.put(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, 7);
        }
        return columns;
    }

    @Override
    void breakRow(DSLContext ctx, String doc) {
        ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, false)
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, "x")
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc)).execute();
    }

    @Override
    void repairRow(DSLContext ctx, String doc) {
        ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc)).execute();
    }

    /** The two shapes the CHECK must let through, and the three it must refuse, one by one on a live constraint. */
    @Test
    @Order(80)
    void theCheckAcceptsLegacyAndWrittenRows_andRefusesEachLabelAloneWithTheMarkerFalse() throws Exception {
        // NOT VALID is enough: it is evaluated on every new row
        deleteAll();
        dropConstraintIfPresent();
        try {
            assertThat(admin().add().action()).isEqualTo("added");
            insert("lab-ok", 0, Map.of());                                                       // defaults: NULL, false
            insert("lab-ok", 1, Map.of(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true));            // written, no label
            insert("lab-ok", 2, Map.of(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true,
                CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, "t", CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, 1)); // written, labels
            for (var label : List.<Field<?>>of(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE,
                    CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER)) {
                Object value = label == CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER ? (Object) 4 : "v";
                Map<Field<?>, Object> columns = Map.of(label, value);
                assertCheckViolation(() -> insert("lab-bad", 9, columns));
            }
            assertThat(admin().census().stream().mapToLong(ManifestCheckAdmin.TenantCensus::violating).sum()).isZero();
        } finally {
            deleteAll();
            dropConstraintIfPresent();
        }
    }
}
