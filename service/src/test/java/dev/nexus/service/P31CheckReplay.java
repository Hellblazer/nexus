// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;

import java.sql.SQLException;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): lifts and restores a vectors-068 NOT VALID CHECK on a test's connection, the way an
 * upgraded install has rows the stamp has not reached (name set, id NULL).
 *
 * <p>The statements are the changeset's own text, replayed through {@link PgContainerHelper#replayChangesetRollback}
 * and {@link PgContainerHelper#replayChangeset}, so a test names a changeset id and carries no DDL string. The rollback
 * drops the changeset's constraints; the forward text adds the column back if absent and the constraints again NOT VALID.
 * The changeset ids are in {@code db/changelog/vectors-068-collection-id-expand.xml} (table to id: {@code
 * service/src/main/resources/db/rdr229/p31-checks.tsv} lists the tables in the changesets' order).
 */
public final class P31CheckReplay {

    public static final String CHANGELOG = "db/changelog/vectors-068-collection-id-expand.xml";

    /** {@code catalog_collections.superseded_by_id}'s CHECK (its own changeset, which waits on the 068-2 data precondition; vectors-068-2 is the column). */
    public static final String COLLECTIONS_SUPERSEDED = "vectors-068-19";
    /** {@code catalog_document_chunks.collection_id}'s CHECK (the manifest). */
    public static final String MANIFEST = "vectors-068-5";
    /** {@code document_aspects.collection_id}'s CHECK. */
    public static final String DOCUMENT_ASPECTS = "vectors-068-8";
    /** {@code document_highlights.collection_id}'s CHECK. */
    public static final String DOCUMENT_HIGHLIGHTS = "vectors-068-11";
    /** {@code catalog_documents.physical_collection_id}'s CHECK. */
    public static final String CATALOG_DOCUMENTS = "vectors-068-13";

    private P31CheckReplay() {}

    /** Drops the changeset's CHECK constraints on {@code ctx}'s connection (the connection must be autocommit). */
    public static void lift(DSLContext ctx, String changesetId) {
        ctx.connection(c -> {
            try {
                PgContainerHelper.replayChangesetRollback(c, CHANGELOG, changesetId);
            } catch (SQLException | RuntimeException e) {
                throw e;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
    }

    /** Adds the changeset's CHECK constraints again NOT VALID, so rows written while lifted stay as they are. */
    public static void restore(DSLContext ctx, String changesetId) {
        ctx.connection(c -> {
            try {
                PgContainerHelper.replayChangeset(c, CHANGELOG, changesetId);
            } catch (SQLException | RuntimeException e) {
                throw e;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
    }
}
