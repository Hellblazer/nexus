// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static org.jooq.impl.DSL.count;
import static org.jooq.impl.DSL.max;
import static org.jooq.impl.DSL.min;
import static org.jooq.impl.DSL.sum;

import java.util.List;

/**
 * {@link AspectRepository#confidenceAggregate}'s body (RDR-089: AVG / MIN / MAX confidence across source URIs, one SQL aggregate per batch),
 * moved out of it unchanged so that class stays under its file-size ceiling (RDR-229 P3.5c assembly, nexus-p2m1x.25).
 */
final class AspectConfidence {

    private AspectConfidence() { /* static */ }

    static Double aggregate(TenantScope tenantScope, String tenant, List<String> sourceUris, String reducerKind) {
        if (sourceUris == null || sourceUris.isEmpty()) return null;
        List<String> uris = sourceUris.stream().map(NoteUri::canonical).distinct().toList();  // RDR-231

        String aggFunc = switch (reducerKind) {
            case "avg_confidence" -> "AVG";
            case "min_confidence" -> "MIN";
            case "max_confidence" -> "MAX";
            default -> null;
        };
        if (aggFunc == null) return null;

        // Accumulate across batches: AVG needs sum+count; MIN/MAX fold naturally.
        double sumAcc = 0.0;
        long   cntAcc = 0L;
        Double minAcc = null;
        Double maxAcc = null;

        for (int start = 0; start < uris.size(); start += 300) {
            List<String> batch = uris.subList(start, Math.min(start + 300, uris.size()));

            final double[] localSum = {0.0};
            final long[]   localCnt = {0L};
            final Double[] localVal = {null};

            tenantScope.withTenant(tenant, ctx -> {
                if ("AVG".equals(aggFunc)) {
                    // For AVG we need SUM+COUNT to fold across batches correctly.
                    var rows = ctx.select(
                            sum(DOCUMENT_ASPECTS.CONFIDENCE),
                            count(DOCUMENT_ASPECTS.CONFIDENCE))
                        .from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                            .and(DOCUMENT_ASPECTS.CONFIDENCE.isNotNull()))
                        .fetch();
                    if (!rows.isEmpty()) {
                        Object sumVal = rows.get(0).get(0);
                        Object cntVal = rows.get(0).get(1);
                        if (sumVal != null) localSum[0] = ((Number) sumVal).doubleValue();
                        if (cntVal != null) localCnt[0] = ((Number) cntVal).longValue();
                    }
                } else if ("MIN".equals(aggFunc)) {
                    var rows = ctx.select(min(DOCUMENT_ASPECTS.CONFIDENCE))
                        .from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                            .and(DOCUMENT_ASPECTS.CONFIDENCE.isNotNull()))
                        .fetch();
                    if (!rows.isEmpty()) {
                        Object val = rows.get(0).get(0);
                        if (val != null) localVal[0] = ((Number) val).doubleValue();
                    }
                } else {
                    var rows = ctx.select(max(DOCUMENT_ASPECTS.CONFIDENCE))
                        .from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                            .and(DOCUMENT_ASPECTS.CONFIDENCE.isNotNull()))
                        .fetch();
                    if (!rows.isEmpty()) {
                        Object val = rows.get(0).get(0);
                        if (val != null) localVal[0] = ((Number) val).doubleValue();
                    }
                }
                return null;
            });

            if ("AVG".equals(aggFunc)) {
                sumAcc += localSum[0];
                cntAcc += localCnt[0];
            } else if (localVal[0] != null) {
                if ("MIN".equals(aggFunc)) {
                    minAcc = (minAcc == null) ? localVal[0] : Math.min(minAcc, localVal[0]);
                } else {
                    maxAcc = (maxAcc == null) ? localVal[0] : Math.max(maxAcc, localVal[0]);
                }
            }
        }

        return switch (reducerKind) {
            case "avg_confidence" -> cntAcc == 0 ? null : sumAcc / cntAcc;
            case "min_confidence" -> minAcc;
            case "max_confidence" -> maxAcc;
            default -> null;
        };
    }
}
