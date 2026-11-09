// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.vectors.ChunkKeyAllowlistActivity;

/**
 * The {@code /v1/status} fields of the chunk-metadata allow-list's log mode (RDR-236 Phase 4 Step 2a, nexus-6pfgb.66),
 * all [additive]. One request is counted once per label it carries, so a view's counts need not add up to the total.
 *
 * <ul>
 *   <li>{@code chunk_metadata_key_would_refuse_total}: number, offending requests since boot.</li>
 *   <li>{@code chunk_metadata_key_would_refuse_by_route_class}: array of {@code {route, key_class, count}},
 *       one per route and key class seen (a fixed, small set); {@code key_class} is document, position, legacy or
 *       other.</li>
 *   <li>{@code chunk_metadata_key_would_refuse_by_key}: {@code {top: [{key, key_class, count}], overflow: number}},
 *       the {@link ChunkKeyAllowlistActivity#TOP_KEYS} most frequent keys; {@code overflow} is the occurrences of
 *       every other key.</li>
 *   <li>{@code chunk_metadata_key_would_refuse_by_client}: {@code {clients: [{user_agent, client_version, count}],
 *       overflow: number}}, at most 64 clients; {@code overflow} is the requests of the rest. A client that sent no
 *       header reads {@code absent}.</li>
 * </ul>
 *
 * <p>Key and client text is client-supplied, cleaned before it is counted and escaped here.
 */
final class ChunkKeyStatusJson {

    private ChunkKeyStatusJson() {
    }

    /** The five fields, each led by a comma, ready to append inside the status object. */
    static String fields() {
        StringBuilder b = new StringBuilder(512);
        b.append(",\"chunk_metadata_key_would_refuse_total\":").append(ChunkKeyAllowlistActivity.wouldRefuseTotal());
        b.append(",\"chunk_metadata_key_would_refuse_by_route_class\":[");
        boolean first = true;
        for (var e : ChunkKeyAllowlistActivity.byRouteClass()) {
            b.append(first ? "" : ",").append("{\"route\":").append(HttpUtil.jsonString(e.route()))
                .append(",\"key_class\":").append(HttpUtil.jsonString(e.keyClass()))
                .append(",\"count\":").append(e.count()).append('}');
            first = false;
        }
        b.append("],\"chunk_metadata_key_would_refuse_by_key\":{\"top\":[");
        first = true;
        for (var e : ChunkKeyAllowlistActivity.topKeys()) {
            b.append(first ? "" : ",").append("{\"key\":").append(HttpUtil.jsonString(e.key()))
                .append(",\"key_class\":").append(HttpUtil.jsonString(e.keyClass()))
                .append(",\"count\":").append(e.count()).append('}');
            first = false;
        }
        b.append("],\"overflow\":").append(ChunkKeyAllowlistActivity.keyOverflow())
            .append("},\"chunk_metadata_key_would_refuse_by_client\":{\"clients\":[");
        first = true;
        for (var e : ChunkKeyAllowlistActivity.clients()) {
            b.append(first ? "" : ",").append("{\"user_agent\":").append(HttpUtil.jsonString(e.userAgent()))
                .append(",\"client_version\":").append(HttpUtil.jsonString(e.clientVersion()))
                .append(",\"count\":").append(e.count()).append('}');
            first = false;
        }
        b.append("],\"overflow\":").append(ChunkKeyAllowlistActivity.clientOverflow()).append('}');
        return b.toString();
    }
}
