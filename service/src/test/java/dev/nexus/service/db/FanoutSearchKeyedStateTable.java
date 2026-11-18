// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;

/**
 * The per-collection fan-out search's rows of {@link TenantKeyedStateGateTest}'s not-keyed table, in their own file
 * because the gate test is at its size ceiling. Everything here is built by one {@code searchPerCollection} request
 * and dropped with it: the merge heap, the lexical leg's rows and stats (nexus-xd2rc), and the leg's shared text-gate
 * probe answer (nexus-bbtc2, {@code SharedLexicalProbe}, a local of that request).
 */
final class FanoutSearchKeyedStateTable {

    private FanoutSearchKeyedStateTable() {
    }

    /** Adds the fan-out search's entries to {@code notKeyed} (field to reason). */
    static void fill(Map<String, String> notKeyed) {
        notKeyed.put("PgVectorRepository.FanoutMerger.worstOnTop", "the merge heap of one fan-out search; "
            + "dies with the request");
        notKeyed.put("PgVectorRepository.LexicalLeg.rows", "the lexical arms' rows of one fan-out search "
            + "(nexus-xd2rc); dies with the request");
        notKeyed.put("PgVectorRepository.LexicalLeg.Union.extras", "the lexical rows one fan-out search "
            + "returns beside its vector rows; dies with the request");
        notKeyed.put("PgVectorRepository.LexicalLeg.Union.stats", "per-collection lexical stats of one "
            + "fan-out search; dies with the request");
        notKeyed.put("SharedLexicalProbe.kind", "how one fan-out search's shared text-gate probe answered; "
            + "dies with the request");
        notKeyed.put("SharedLexicalProbe.byCollection", "per-collection chashes of one fan-out search's shared "
            + "probe; dies with the request");
        notKeyed.put("SharedLexicalProbe.dense", "the collections one fan-out search's shared probe found dense; "
            + "dies with the request");
        // RDR-134 Step 3 (nexus-7fku2.1.3): the topic route of one search-per-collection request
        notKeyed.put("TopicRoute.collections", "the collections one request's topic_route names; a record component "
            + "of that request's parsed route, dies with the request");
        notKeyed.put("TopicRouteArm.tenantScope", "a TenantScope handle: it holds the DataSource wrapper only, the "
            + "same handle every repository is built with; the arm itself is built per request");
        notKeyed.put("TopicRouteArm.Attempt.rows", "the candidate rows of one collection's routed arm; handed to the "
            + "merge and dropped with the request");
    }
}
