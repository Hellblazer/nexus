// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

import dev.nexus.service.db.ManifestCheck;
import org.jooq.DSLContext;
import org.jooq.Field;

import java.util.HashMap;
import java.util.Map;

/**
 * nexus-6pfgb.42: the {@code labels_written OR (the three section labels are NULL)} CHECK's routes under
 * {@code /v1/admin/manifest-labels-check} (the steps are {@link ManifestCheckRoutesIntegrationBase}'s). A violator is a
 * row that holds one of the labels while {@code labels_written} is false; the operator's fix marks it written.
 */
class ManifestLabelsCheckRoutesIntegrationTest extends ManifestCheckRoutesIntegrationBase {

    private static final String BLIND = "mlw_blind_admin";
    private static final String OWNER = "mlw_owner_route";

    @Override
    String blindRole() {
        return BLIND;
    }

    @Override
    void createBlindLoginRole(java.sql.Connection su) throws Exception {
        PgContainerHelper.createLoginRole(su, BLIND, BLIND_PASS, false);
    }

    @Override
    String ownerRole() {
        return OWNER;
    }

    @Override
    void createOwnerLoginRole(java.sql.Connection su) throws Exception {
        PgContainerHelper.createLoginRole(su, OWNER, OWNER_PASS, false);
    }

    @Override
    ManifestCheck check() {
        return ManifestCheck.LABELS;
    }

    @Override
    String definitionWord() {
        return "labels_written";
    }

    @Override
    Map<Field<?>, Object> cleanColumns(int position) {
        Map<Field<?>, Object> columns = new HashMap<>();
        if (position % 2 == 0) {
            columns.put(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, false);   // legacy: NULL labels
        } else {
            columns.put(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true);
            columns.put(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, "A heading");
            columns.put(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, 2);
        }
        return columns;
    }

    @Override
    Map<Field<?>, Object> violatingColumns(int position) {
        Map<Field<?>, Object> columns = new HashMap<>();
        columns.put(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, false);
        switch (position % 3) {
            case 0 -> columns.put(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, "A heading");
            case 1 -> columns.put(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, "code");
            default -> columns.put(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, 7);
        }
        return columns;
    }

    @Override
    void fixEveryViolator(DSLContext ctx) {
        ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true)
            .where(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN.isFalse()
                .and(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE.isNotNull()
                    .or(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE.isNotNull())
                    .or(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER.isNotNull()))).execute();
    }
}
