// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;

/**
 * RDR-225 (nexus-3wh8d.13): the engine's refusal of a write for an embedding model that has no
 * partition, raised BEFORE any write statement runs and naming the model.
 *
 * <p>{@code nexus.chunks} and {@code nexus.taxonomy_centroids} are LIST-partitioned by
 * {@code embedding_model}, then by {@code tenant_id}. A row whose model has no partition fails tuple
 * routing in PostgreSQL with SQLSTATE 23514 ("no partition of relation ... found for row"), a message
 * that names neither the model nor a remedy and that the dimension CHECK shares a code with. Adding a
 * model is a changeset that inserts the {@code embedding_models} row and calls
 * {@code nexus.create_model_partition} once per parent; this class is the guard for the window where
 * the first half happened without the second, and for a model nobody registered at all.
 *
 * <p>The answer comes from the catalog ({@code pg_inherits} joined to {@code pg_class}, comparing the
 * partition bound text), not from a naming convention, so it agrees with
 * {@code nexus.create_model_partition}, which also finds a partition by its bound. A positive answer is
 * cached for the life of the process: a model partition is never dropped by anything in this release,
 * and a stale positive could only ever turn the refusal back into the PostgreSQL error it replaces.
 * A negative answer is never cached, so a model added by a changeset while the engine runs is picked up
 * on the next write.
 */
public final class ModelPartitions {

    /** The parent name of {@code nexus.chunks}. */
    public static final String CHUNKS = "chunks";

    /** The parent name of {@code nexus.taxonomy_centroids}. */
    public static final String CENTROIDS = "taxonomy_centroids";

    private static final Set<String> KNOWN = ConcurrentHashMap.newKeySet();

    private ModelPartitions() {}

    /**
     * Refusal of a write for a model with no partition. {@code registered} says whether the model has
     * an {@code embedding_models} row: false means nobody registered it, true means its changeset
     * inserted the row and never created the partition.
     */
    public static final class ModelPartitionMissingException extends RuntimeException {
        private final String model;
        private final String parent;
        private final boolean registered;

        ModelPartitionMissingException(String parent, String model, boolean registered) {
            super(registered
                ? "embedding model '" + model + "' is registered but nexus." + parent
                    + " has no partition for it; the changeset that added the model must also call"
                    + " nexus.create_model_partition('nexus." + parent + "', '" + model + "'). Nothing was written."
                : "embedding model '" + model + "' is not registered in nexus.embedding_models and"
                    + " nexus." + parent + " has no partition for it. Nothing was written.");
            this.model = model;
            this.parent = parent;
            this.registered = registered;
        }

        public String model() { return model; }
        public String parent() { return parent; }
        public boolean registered() { return registered; }
    }

    private static String key(String parent, String model) {
        // '|' is not legal in a model token (they are [a-z0-9-]) nor in a table name.
        return parent + '|' + model;
    }

    /**
     * Throw {@link ModelPartitionMissingException} unless {@code nexus.<parent>} has a model partition
     * for {@code model}. Answers from the process cache when it can, else asks the catalog through
     * {@code ctx}.
     *
     * @param parent {@link #CHUNKS} or {@link #CENTROIDS}
     */
    public static void require(DSLContext ctx, String parent, String model) {
        if (model == null || model.isBlank()) {
            throw new IllegalArgumentException("embedding model must not be null or blank");
        }
        if (KNOWN.contains(key(parent, model))) {
            return;
        }
        if (exists(ctx, parent, model)) {
            KNOWN.add(key(parent, model));
            return;
        }
        boolean registered = ctx.fetchExists(ctx.selectOne().from(EMBEDDING_MODELS)
            .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(model)));
        throw new ModelPartitionMissingException(parent, model, registered);
    }

    /**
     * {@link #require(DSLContext, String, String)} for a caller with no open transaction: answers
     * from the cache without touching the database, and opens a short read transaction only on a miss.
     */
    public static void require(TenantScope scope, String tenant, String parent, String model) {
        if (model != null && KNOWN.contains(key(parent, model))) {
            return;
        }
        scope.withTenant(tenant, ctx -> {
            require(ctx, parent, model);
            return null;
        });
    }

    /** True when {@code nexus.<parent>} has a partition whose bound is {@code FOR VALUES IN ('<model>')}. */
    public static boolean exists(DSLContext ctx, String parent, String model) {
        var inh = DSL.table(DSL.name("pg_catalog", "pg_inherits")).as("i");
        var child = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var par = DSL.table(DSL.name("pg_catalog", "pg_class")).as("p");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Field<Object> childOid = DSL.field(DSL.name("c", "oid"), Object.class);
        Field<Object> childBound = DSL.field(DSL.name("c", "relpartbound"), Object.class);
        Field<String> boundText = DSL.function(DSL.name("pg_catalog", "pg_get_expr"),
            SQLDataType.CLOB, childBound, childOid);
        String expected = "FOR VALUES IN ('" + model.replace("'", "''") + "')";
        return ctx.fetchExists(ctx.selectOne()
            .from(inh)
            .join(child).on(DSL.field(DSL.name("c", "oid"), Object.class)
                .eq(DSL.field(DSL.name("i", "inhrelid"), Object.class)))
            .join(par).on(DSL.field(DSL.name("p", "oid"), Object.class)
                .eq(DSL.field(DSL.name("i", "inhparent"), Object.class)))
            .join(ns).on(DSL.field(DSL.name("n", "oid"), Object.class)
                .eq(DSL.field(DSL.name("p", "relnamespace"), Object.class)))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("p", "relname"), String.class).eq(parent))
            .and(boundText.eq(expected)));
    }

    /** Test-only: forget every cached positive answer. */
    static void clearForTests() {
        KNOWN.clear();
    }
}
