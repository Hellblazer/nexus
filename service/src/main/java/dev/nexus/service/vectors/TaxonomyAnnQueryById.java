// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.binding.VectorBinding;
import dev.nexus.service.jooq.nexus.Nexus;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.TableField;
import org.jooq.TableOptions;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultDataType;
import org.jooq.impl.SQLDataType;
import org.jooq.impl.TableImpl;

/**
 * The call of {@code nexus.taxonomy_ann_query_<dim>(p_embedding, p_collection, p_cross_collection, p_n, p_embedding_model,
 * p_tenant, p_collection_id bigint)}, the overload vectors-084 adds (RDR-229 P3.5b, nexus-p2m1x.24/.25). It is written by hand and
 * not generated because the overload is CONTINUE-gated: a fresh walk, which is what the build's jOOQ codegen sees, skips
 * vectors-084, so the generated {@code TAXONOMY_ANN_QUERY_<dim>} has only the text signature. The shape is the generated class's:
 * a function table (jOOQ renders {@code "nexus"."taxonomy_ann_query_<dim>"(?, ?, ...)}) whose columns are {@code topic_id} and
 * {@code distance}.
 */
public final class TaxonomyAnnQueryById extends TableImpl<Record> {

    private static final long serialVersionUID = 1L;

    final TableField<Record, Long> topicId = createField(DSL.name("topic_id"), SQLDataType.BIGINT, this, "");
    final TableField<Record, Double> distance = createField(DSL.name("distance"), SQLDataType.DOUBLE, this, "");

    private TaxonomyAnnQueryById(int dim, Field<?>[] parameters) {
        super(DSL.name("taxonomy_ann_query_" + dim), Nexus.NEXUS, null, parameters, DSL.comment(""), TableOptions.function());
    }

    /** The call for {@code dim} (384, 768 or 1024); {@code collectionId} is the registry row's id for {@code collection}. */
    public static TaxonomyAnnQueryById call(int dim, Vector embedding, String collection, boolean crossCollection, int n,
                                     String embeddingModel, String tenant, long collectionId) {
        return new TaxonomyAnnQueryById(dim, new Field<?>[] {
            DSL.val(embedding, DefaultDataType.getDefaultDataType("\"nexus\".\"vector\"").asConvertedDataType(new VectorBinding())),
            DSL.val(collection, SQLDataType.CLOB),
            DSL.val(crossCollection, SQLDataType.BOOLEAN),
            DSL.val(n, SQLDataType.INTEGER),
            DSL.val(embeddingModel, SQLDataType.CLOB),
            DSL.val(tenant, SQLDataType.CLOB),
            DSL.val(collectionId, SQLDataType.BIGINT)
        });
    }
}
