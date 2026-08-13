package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.URISyntaxException;
import java.net.URL;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-0ys55 — pure, fast (no DB) THREE-WAY parity pin between {@link
 * TenantScope#VACUUM_ALLOWED_TABLES}, {@link CatalogRepository#PURGE_VACUUM_TABLES},
 * and the {@code grants-005-chunks-unify-maintain} changeset in {@code
 * grants-nexus-svc.xml} (nexus-o8dil.41 comment 2 — RDR-191 Phase 4, repoint-batch
 * lane D5: widened from the original two-Java-list pin to also read the changelog
 * text directly, closing the gap the original docstring flagged as "not something a
 * JUnit test can parse cheaply" — cheap enough via a scoped regex over the ONE
 * changeset's {@code <sql>} body, no live Liquibase run needed).
 *
 * <p>Three call sites must agree on the SAME schema-qualified table names — {@code
 * nexus.chunks}, {@code nexus.catalog_document_chunks}, {@code
 * nexus.catalog_documents} post-unification (was five, including
 * {@code chunks_384/768/1024}, under {@code grants-003-purge-vacuum-maintain} before
 * {@code grants-005} superseded it — see {@code vectors-004-unify-chunks.xml}'s header
 * for the boot-brick analysis that motivated the split): this allowlist (what {@link
 * TenantScope#vacuumAnalyze} will accept), {@code CatalogRepository}'s sweep-target
 * list (what {@code purgeTrash} actually asks to be vacuumed), and the changeset (what
 * {@code nexus_svc} is actually granted {@code MAINTAIN} on). {@link
 * CatalogPurgeTrashVacuumTest} covers the live-Postgres end-to-end side; this test is
 * the cheap, fast, no-DB static cross-check that a drift is caught before it reaches a
 * real deploy.
 */
class TenantScopeVacuumMaintainGrantParityTest {

    @Test
    void tenantScopeAllowlistAndCatalogRepositorySweepListNameTheSameTables() {
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

    /**
     * The THIRD leg: {@code grants-005-chunks-unify-maintain}'s {@code GRANT MAINTAIN
     * ON <table> TO nexus_svc} statements must name the identical table set as the two
     * Java-side lists above. Scoped to just that one changeset's body (between its own
     * {@code <changeSet id="grants-005-chunks-unify-maintain"...>} tag and the next
     * {@code </changeSet>}) so a MAINTAIN grant belonging to a DIFFERENT changeset
     * (e.g. the retired {@code grants-003}, or the unrelated {@code chash_alias}
     * MAINTAIN grant) is never accidentally counted.
     */
    @Test
    void grantsChangesetMaintainListMatchesBothJavaSideLists() throws IOException, URISyntaxException {
        String xml = Files.readString(grantsChangelogPath());

        int changesetStart = xml.indexOf("id=\"grants-005-chunks-unify-maintain\"");
        assertThat(changesetStart)
            .as("grants-005-chunks-unify-maintain changeset must exist in grants-nexus-svc.xml")
            .isGreaterThan(-1);
        int bodyStart = xml.indexOf('>', changesetStart) + 1;
        int bodyEnd = xml.indexOf("</changeSet>", bodyStart);
        assertThat(bodyEnd).as("changeSet must close").isGreaterThan(bodyStart);
        String body = xml.substring(bodyStart, bodyEnd);

        Pattern grantMaintain = Pattern.compile(
            "GRANT MAINTAIN ON (nexus\\.[a-z_]+) TO nexus_svc");
        Matcher m = grantMaintain.matcher(body);
        Set<String> granted = new LinkedHashSet<>();
        while (m.find()) {
            granted.add(m.group(1));
        }

        assertThat(granted)
            .as("grants-005-chunks-unify-maintain's GRANT MAINTAIN table set")
            .isNotEmpty()
            .isEqualTo(TenantScope.VACUUM_ALLOWED_TABLES)
            .isEqualTo(Set.copyOf(CatalogRepository.PURGE_VACUUM_TABLES));
    }

    private static Path grantsChangelogPath() throws URISyntaxException {
        URL url = TenantScopeVacuumMaintainGrantParityTest.class.getResource(
            "/db/changelog/grants-nexus-svc.xml");
        assertThat(url).as("grants-nexus-svc.xml must be on the test classpath").isNotNull();
        return Paths.get(url.toURI());
    }
}
