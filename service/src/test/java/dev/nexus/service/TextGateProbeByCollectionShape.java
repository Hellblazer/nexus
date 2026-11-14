// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;

import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-bbtc2: what {@code nexus.text_gate_probe_by_collection_<dim>} (vectors-060) is in {@code pg_proc}, asserted for
 * either posture: its {@code proconfig} is exactly the intended SET clauses (and never the worker count, which the engine
 * sets per statement), the three dimensions have one body, and its security mode, owner and ACL are those of
 * {@code text_gate_probe_<dim>}, the function whose posture it reads.
 */
public final class TextGateProbeByCollectionShape {

    private TextGateProbeByCollectionShape() {
    }

    /** The SET clauses every posture carries. */
    static final List<String> COMMON_SETS = List.of(
        "plan_cache_mode=force_custom_plan", "parallel_setup_cost=0", "parallel_tuple_cost=0",
        "min_parallel_table_scan_size=0", "cpu_operator_cost=0.25", "jit=off");

    /** @param definer whether the probes (and so the new functions) are SECURITY DEFINER in this database */
    public static void assertShape(DSLContext ctx, boolean definer) {
        var defs = PgCatalogProbes.functionDefinitionsIn(ctx, "nexus");
        String body384 = null;
        for (int dim : new int[] {384, 768, 1024}) {
            var fn = only(defs, "text_gate_probe_by_collection_" + dim + "(");
            var probe = only(defs, "text_gate_probe_" + dim + "(");
            List<String> expected = new java.util.ArrayList<>(COMMON_SETS);
            if (definer) {
                expected.add("search_path=pg_catalog, pg_temp");
            }
            assertThat(configEntries(fn.config())).as("dim %d: pg_proc.proconfig of the by-collection function", dim)
                .containsExactlyInAnyOrderElementsOf(expected);
            assertThat(fn.config()).as("dim %d: the engine, not the function, sets the workers", dim)
                .doesNotContain("max_parallel_workers_per_gather");
            assertThat(fn.body()).as("dim %d body names no worker setting", dim)
                .doesNotContain("max_parallel_workers_per_gather");
            assertThat(fn.securityDefiner()).as("dim %d security mode follows the probe", dim)
                .isEqualTo(probe.securityDefiner()).isEqualTo(definer);
            assertThat(fn.owner()).as("dim %d owner follows the probe", dim).isEqualTo(probe.owner());
            assertThat(fn.acl()).as("dim %d ACL follows the probe (PUBLIC revoked for a definer, EXECUTE to nexus_svc)", dim)
                .isEqualTo(probe.acl());
            assertThat(fn.language()).isEqualTo("plpgsql");
            assertThat(fn.volatility()).as("STABLE: one snapshot across both statements").isEqualTo("s");
            if (body384 == null) {
                body384 = fn.body();
            } else {
                assertThat(fn.body()).as("dim %d body equals the 384 body (nothing in it names a dimension)", dim)
                    .isEqualTo(body384);
            }
        }
    }

    private static PgCatalogProbes.FunctionDefinition only(Map<String, PgCatalogProbes.FunctionDefinition> defs,
                                                           String identityPrefix) {
        var hits = defs.values().stream().filter(f -> f.identity().startsWith(identityPrefix)).toList();
        assertThat(hits).as("exactly one function %s...", identityPrefix).hasSize(1);
        return hits.get(0);
    }

    /** {@code {a=1,b=2}} as the catalog's array text prints it, split on the commas outside the quoted search_path. */
    private static List<String> configEntries(String config) {
        assertThat(config).as("proconfig is set").isNotNull();
        String inner = config.substring(1, config.length() - 1);
        TreeSet<String> out = new TreeSet<>();
        for (String e : inner.split(",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)")) {
            out.add(e.replace("\"", "").strip());
        }
        return Arrays.asList(out.toArray(String[]::new));
    }
}
