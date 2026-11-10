// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.vectors.ChunkKeyAllowlistActivity;

/**
 * The {@code /v1/status} fields of the chunk-metadata allow-list's log mode (RDR-236 Phase 4 Step 2a, nexus-6pfgb.66),
 * all [additive]. One request is counted once per label it carries, so the view's counts need not add up to the total.
 *
 * <ul>
 *   <li>{@code chunk_metadata_key_would_refuse_total}: number, offending requests since boot.</li>
 *   <li>{@code chunk_metadata_key_would_refuse_by_route_class}: array of {@code {route, key_class, count}},
 *       one per route and key class seen; {@code key_class} is document, position, legacy or other.</li>
 * </ul>
 *
 * <p>Every label here is fixed by the engine (a route name, one of four classes), so the body carries no
 * client-supplied text. That is deliberate: {@code GET /v1/status} has no tenant scope, so any caller sees the whole
 * engine's status, and a key name, {@code User-Agent} or client version would show one tenant's strings to every
 * other tenant. The per-key and per-client detail is in the {@code chunk_metadata_key_would_refuse} log line, which
 * is rate limited per route, tenant, collection, key set and client and so names every distinct writer while the
 * limiter has room (past its 10,000 keys new writers share one overflow line: the log is a sample).
 */
final class ChunkKeyStatusJson {

    private ChunkKeyStatusJson() {
    }

    /** The two fields, each led by a comma, ready to append inside the status object. */
    static String fields() {
        StringBuilder b = new StringBuilder(256);
        b.append(",\"chunk_metadata_key_would_refuse_total\":").append(ChunkKeyAllowlistActivity.wouldRefuseTotal());
        b.append(",\"chunk_metadata_key_would_refuse_by_route_class\":[");
        boolean first = true;
        for (var e : ChunkKeyAllowlistActivity.byRouteClass()) {
            b.append(first ? "" : ",").append("{\"route\":").append(HttpUtil.jsonString(e.route()))
                .append(",\"key_class\":").append(HttpUtil.jsonString(e.keyClass()))
                .append(",\"count\":").append(e.count()).append('}');
            first = false;
        }
        b.append(']');
        return b.toString();
    }
}
