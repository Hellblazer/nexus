// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_SENSITIVITY_CHANGES;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Typed superuser reads and fixture writes for {@code CatalogDocumentMoveTest} and {@code CatalogOwnerMoveTest}: the
 * rows a move touches, read past row-level security so a test sees the tenant's whole state. Typed jOOQ throughout
 * (the raw-SQL ratchet of {@code RawSqlGateTest} admits no new raw call site).
 */
final class MoveTestDb {

    private final PostgreSQLContainer<?> pg;

    MoveTestDb(PostgreSQLContainer<?> pg) {
        this.pg = pg;
    }

    private <T> T with(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    void registerCollection(String t, String collection) throws Exception {
        with(dsl -> { PgContainerHelper.insertCollection(dsl, t, collection); return null; });
    }

    /** source_uri, file_path, alias_of, sensitive, deleted_at of one document row (live or trashed). */
    Map<String, Object> doc(String t, String tumbler) throws Exception {
        var r = with(dsl -> dsl.select(CATALOG_DOCUMENTS.SOURCE_URI, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.ALIAS_OF,
                CATALOG_DOCUMENTS.SENSITIVE, CATALOG_DOCUMENTS.DELETED_AT)
            .from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
            .fetchOne());
        assertThat(r).as("document row %s", tumbler).isNotNull();
        return r.intoMap();
    }

    /** name, owner_type, repo_hash, repo_root, deactivated_at of one owner row. */
    Map<String, Object> owner(String t, String prefix) throws Exception {
        var r = with(dsl -> dsl.select(CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH,
                CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.DEACTIVATED_AT)
            .from(CATALOG_OWNERS)
            .where(CATALOG_OWNERS.TENANT_ID.eq(t).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(prefix)))
            .fetchOne());
        assertThat(r).as("owner row %s", prefix).isNotNull();
        return r.intoMap();
    }

    /** The prefixes of the owners (live or deactivated) holding a repo hash. */
    List<String> ownersHolding(String t, String hash) throws Exception {
        return with(dsl -> dsl.select(CATALOG_OWNERS.TUMBLER_PREFIX).from(CATALOG_OWNERS)
            .where(CATALOG_OWNERS.TENANT_ID.eq(t).and(CATALOG_OWNERS.REPO_HASH.eq(hash)))
            .fetch(CATALOG_OWNERS.TUMBLER_PREFIX));
    }

    List<Map<String, Object>> aspects(String t) throws Exception {
        return with(dsl -> dsl.select(DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH,
                DOCUMENT_ASPECTS.SOURCE_URI, DOCUMENT_ASPECTS.PROBLEM_FORMULATION)
            .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.TENANT_ID.eq(t))
            .orderBy(DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH)
            .fetch().intoMaps());
    }

    List<Map<String, Object>> highlights(String t) throws Exception {
        return with(dsl -> dsl.select(DOCUMENT_HIGHLIGHTS.DOC_ID, DOCUMENT_HIGHLIGHTS.COLLECTION,
                DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD)
            .from(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(t))
            .orderBy(DOCUMENT_HIGHLIGHTS.DOC_ID)
            .fetch().intoMaps());
    }

    List<Map<String, Object>> links(String t) throws Exception {
        return with(dsl -> dsl.select(CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE)
            .from(CATALOG_LINKS).where(CATALOG_LINKS.TENANT_ID.eq(t))
            .orderBy(CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE)
            .fetch().intoMaps());
    }

    /** The audit rows of one document's sensitivity changes. */
    int sensitivityAudits(String t, String tumbler) throws Exception {
        return with(dsl -> dsl.fetchCount(CATALOG_SENSITIVITY_CHANGES,
            CATALOG_SENSITIVITY_CHANGES.TENANT_ID.eq(t).and(CATALOG_SENSITIVITY_CHANGES.TUMBLER.eq(tumbler))));
    }

    int manifestRows(String t, String docId) throws Exception {
        return with(dsl -> dsl.fetchCount(CATALOG_DOCUMENT_CHUNKS,
            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))));
    }

    void setAlias(String t, String tumbler, String aliasOf) throws Exception {
        with(dsl -> dsl.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, aliasOf)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute());
    }

    void blankSourceUri(String t, String tumbler) throws Exception {
        with(dsl -> dsl.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "")
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute());
    }

    void setUriAndPath(String t, String tumbler, String sourceUri, String filePath) throws Exception {
        with(dsl -> dsl.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, sourceUri)
            .set(CATALOG_DOCUMENTS.FILE_PATH, filePath)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute());
    }

    /** Insert an aspect row naming {@code docId}, past the repository: for a doc_id that names no document. */
    void insertAspectRow(String t, String collection, String docId) throws Exception {
        registerCollection(t, collection);
        with(dsl -> dsl.insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, t).set(DOCUMENT_ASPECTS.COLLECTION, collection)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, "p").set(DOCUMENT_ASPECTS.EXTRACTED_AT, OffsetDateTime.now())
            .set(DOCUMENT_ASPECTS.MODEL_VERSION, "v").set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "e")
            .set(DOCUMENT_ASPECTS.SOURCE_URI, "file:///p").set(DOCUMENT_ASPECTS.DOC_ID, docId)
            .execute());
    }

    /** Every row a move can touch, as text: equal before and after means nothing was written. */
    String snapshot(String t) throws Exception {
        return with(dsl -> {
            StringBuilder sb = new StringBuilder();
            sb.append(dsl.selectFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(t))
                .orderBy(CATALOG_DOCUMENTS.TUMBLER).fetch().formatCSV()).append("\n--\n");
            sb.append(dsl.selectFrom(CATALOG_OWNERS).where(CATALOG_OWNERS.TENANT_ID.eq(t))
                .orderBy(CATALOG_OWNERS.TUMBLER_PREFIX).fetch().formatCSV()).append("\n--\n");
            sb.append(dsl.selectFrom(CATALOG_LINKS).where(CATALOG_LINKS.TENANT_ID.eq(t))
                .orderBy(CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE)
                .fetch().formatCSV()).append("\n--\n");
            sb.append(dsl.selectFrom(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.TENANT_ID.eq(t))
                .orderBy(DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH)
                .fetch().formatCSV()).append("\n--\n");
            sb.append(dsl.selectFrom(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(t))
                .orderBy(DOCUMENT_HIGHLIGHTS.DOC_ID).fetch().formatCSV()).append("\n--\n");
            sb.append(dsl.selectFrom(CATALOG_SENSITIVITY_CHANGES).where(CATALOG_SENSITIVITY_CHANGES.TENANT_ID.eq(t))
                .orderBy(CATALOG_SENSITIVITY_CHANGES.TUMBLER, CATALOG_SENSITIVITY_CHANGES.REASON)
                .fetch().formatCSV()).append("\n--\n");
            sb.append(dsl.selectFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t))
                .orderBy(CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION)
                .fetch().formatCSV());
            return sb.toString();
        });
    }
}
