package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-0ys55 — pure, fast (no DB) parity pin between {@link
 * TenantScope#VACUUM_ALLOWED_TABLES} and {@link CatalogRepository#PURGE_VACUUM_TABLES}.
 *
 * <p>Three call sites must agree on the SAME five schema-qualified table names: this
 * allowlist (what {@link TenantScope#vacuumAnalyze} will accept), {@code
 * CatalogRepository}'s sweep-target list (what {@code purgeTrash} actually asks to be
 * vacuumed), and the {@code grants-003-purge-vacuum-maintain} changeset in {@code
 * grants-nexus-svc.xml} (what {@code nexus_svc} is actually granted {@code MAINTAIN}
 * on). The changelog XML is not something a JUnit test can parse cheaply without a
 * live Liquibase run — {@link CatalogPurgeTrashVacuumTest} covers that side end-to-end
 * against a real Postgres instance — but the two Java-side lists cost nothing to keep
 * in lockstep here, and a drift between them is exactly the kind of silent divergence
 * this test exists to catch before it reaches a real deploy.
 */
class TenantScopeVacuumMaintainGrantParityTest {

    @Test
    void tenantScopeAllowlistAndCatalogRepositorySweepListNameTheSameFiveTables() {
        assertThat(TenantScope.VACUUM_ALLOWED_TABLES)
            .as("TenantScope.VACUUM_ALLOWED_TABLES and CatalogRepository.PURGE_VACUUM_TABLES "
                + "must name the identical table set -- a drift here means either purge-trash's "
                + "VACUUM step will reject a table it means to sweep, or the allowlist silently "
                + "permits a table nothing sweeps")
            .isEqualTo(Set.copyOf(CatalogRepository.PURGE_VACUUM_TABLES));
    }

    @Test
    void catalogRepositorySweepListHasNoDuplicates() {
        // PURGE_VACUUM_TABLES is a List (order matters for vacuumAnalyze's log/report
        // ordering); this pins that it is still duplicate-free, so the size-based
        // Set-equality assertion above cannot be fooled by a repeated entry masking a
        // missing one.
        assertThat(CatalogRepository.PURGE_VACUUM_TABLES)
            .hasSize(Set.copyOf(CatalogRepository.PURGE_VACUUM_TABLES).size());
    }
}
