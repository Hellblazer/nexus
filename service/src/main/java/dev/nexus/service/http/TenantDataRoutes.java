// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.IdentityRepository;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.PipelineRepository;
import dev.nexus.service.db.PlanRepository;
import dev.nexus.service.db.RemapRepository;
import dev.nexus.service.db.ScratchRepository;
import dev.nexus.service.db.TaxonomyCoverage;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;

import java.util.List;

/**
 * The route registrations of the per-tenant stores that need nothing but their repository and the auth filter
 * (memory, plans, telemetry, T1 scratch, taxonomy, aspects, chash, remap, ladder, pipeline, identity), moved out of
 * {@code NexusService}'s constructor (nexus-hczdo.1.1) so that file, which sits at its size ceiling, has room
 * for the routes added after it. The order and the contexts are unchanged: each is one {@code createContext}
 * with the auth filter on it, exactly as the constructor had them.
 */
public final class TenantDataRoutes {

    private TenantDataRoutes() {
    }

    /** Registers the eleven contexts, each behind {@code authFilter}. */
    public static void register(HttpServer server, List<Filter> authFilter,
                                MemoryRepository memoryRepo, PlanRepository planRepo,
                                TelemetryRepository telemetryRepo, ScratchRepository scratchRepo,
                                TaxonomyRepository taxonomyRepo, TaxonomyCentroidRepository taxonomyCentroidRepo,
                                TaxonomyCoverage taxonomyCoverage, AspectRepository aspectRepo,
                                ChashRepository chashRepo, RemapRepository remapRepo,
                                LadderRepository ladderRepo, PipelineRepository pipelineRepo,
                                IdentityRepository identityRepo) {
        // /v1/memory/* — memory endpoints
        server.createContext("/v1/memory", new MemoryHandler(memoryRepo)).getFilters().addAll(authFilter);

        // /v1/plans/* — plan library endpoints (bead nexus-gmiaf.11)
        server.createContext("/v1/plans", new PlanHandler(planRepo)).getFilters().addAll(authFilter);

        // /v1/telemetry/* — telemetry endpoints (bead nexus-gmiaf.12)
        server.createContext("/v1/telemetry", new TelemetryHandler(telemetryRepo)).getFilters().addAll(authFilter);

        // /v1/t1/* — T1 scratch endpoints (bead nexus-gmiaf.13)
        server.createContext("/v1/t1", new ScratchHandler(scratchRepo)).getFilters().addAll(authFilter);

        // /v1/taxonomy/* — taxonomy endpoints (bead nexus-gmiaf.14)
        server.createContext("/v1/taxonomy", new TaxonomyHandler(taxonomyRepo, taxonomyCentroidRepo, taxonomyCoverage))
                .getFilters().addAll(authFilter);

        // /v1/aspects/* — aspects / highlights / queue / promotion-log (bead nexus-gmiaf.15)
        server.createContext("/v1/aspects", new AspectHandler(aspectRepo)).getFilters().addAll(authFilter);

        // /v1/chash/* — chash_index endpoints (bead nexus-gmiaf.16)
        server.createContext("/v1/chash", new ChashHandler(chashRepo)).getFilters().addAll(authFilter);

        // /v1/remap/* — chash_remap endpoints (RDR-186 nexus-146xx.4: wire re-id map write-through + per-leg clear
        // + live membership counts). The RDR-180 per-tenant full-digest rekey (nexus-jxizy.6/nexus-b878d) was
        // RETIRED with nexus.chash_alias (nexus-lgdel.l1) — see RemapHandler's class javadoc.
        server.createContext("/v1/remap", new RemapHandler(remapRepo)).getFilters().addAll(authFilter);

        // /v1/ladder/* — upgrade-ladder completion bookkeeping (RDR-186 nexus-146xx.12: the ladder.db
        // retirement's PG write/read surface)
        server.createContext("/v1/ladder", new LadderHandler(ladderRepo)).getFilters().addAll(authFilter);

        // /v1/pipeline/* — engine-hosted streaming-PDF buffer (RDR-186 nexus-146xx.16: pipeline.db's PG twin; state
        // hosts here, the extraction compute stays client-side)
        server.createContext("/v1/pipeline", new PipelineHandler(pipelineRepo)).getFilters().addAll(authFilter);

        // /v1/identity/* — session identity: seats, aliases, the binding log and the session index (RDR-253 Step 1,
        // bead nexus-hczdo.1.1). The handler admits each route for the credential that needs it.
        server.createContext("/v1/identity", new IdentityHandler(identityRepo)).getFilters().addAll(authFilter);
    }
}
