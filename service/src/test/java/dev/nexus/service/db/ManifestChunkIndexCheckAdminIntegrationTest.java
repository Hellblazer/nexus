// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

import org.jooq.DSLContext;
import org.jooq.Field;

import java.util.List;
import java.util.Map;

/**
 * nexus-6pfgb.42: the {@code chunk_index = position} CHECK's census and steps on a real PostgreSQL (the steps are
 * {@link ManifestCheckAdminIntegrationBase}'s). A violator is a row whose {@code chunk_index} differs from its
 * {@code position}.
 */
class ManifestChunkIndexCheckAdminIntegrationTest extends ManifestCheckAdminIntegrationBase {

    private static final String OWNER = "mci_owner_adm";

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
        return ManifestCheck.CHUNK_INDEX;
    }

    @Override
    String expectedExpression() {
        return "\"chunk_index\" = \"position\"";
    }

    @Override
    List<String> definitionWords() {
        return List.of("chunk_index", "position");
    }

    @Override
    Map<Field<?>, Object> cleanColumns(int position) {
        return Map.of(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position);
    }

    @Override
    Map<Field<?>, Object> violatingColumns(int position) {
        return Map.of(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position + 5);
    }

    @Override
    void breakRow(DSLContext ctx, String doc) {
        ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, 9)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc)).execute();
    }

    @Override
    void repairRow(DSLContext ctx, String doc) {
        ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.POSITION)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc)).execute();
    }
}
