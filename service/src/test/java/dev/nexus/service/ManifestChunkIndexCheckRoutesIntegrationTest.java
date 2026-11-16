// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

import dev.nexus.service.db.ManifestCheck;
import org.jooq.DSLContext;
import org.jooq.Field;

import java.util.Map;

/**
 * nexus-6pfgb.42: the {@code chunk_index = position} CHECK's routes under {@code /v1/admin/manifest-chunk-index-check}
 * (the steps are {@link ManifestCheckRoutesIntegrationBase}'s). A violator is a row whose {@code chunk_index} differs
 * from its {@code position}.
 */
class ManifestChunkIndexCheckRoutesIntegrationTest extends ManifestCheckRoutesIntegrationBase {

    private static final String BLIND = "mc_blind_admin";
    private static final String OWNER = "mc_owner_route";

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
        return ManifestCheck.CHUNK_INDEX;
    }

    @Override
    String definitionWord() {
        return "chunk_index";
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
    void fixEveryViolator(DSLContext ctx) {
        ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.POSITION)
            .where(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX.ne(CATALOG_DOCUMENT_CHUNKS.POSITION)).execute();
    }
}
