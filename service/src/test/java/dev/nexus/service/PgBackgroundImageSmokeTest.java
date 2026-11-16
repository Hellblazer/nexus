// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.build.PgContainerRecipe;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

/**
 * nexus-f3yqd.2.8 (RDR-246 Phase 1 Step 3c): the data-move test image carries pg_background at the commit the
 * PG bundle pins, a background worker really runs in it, and pgvector still creates. Every later data-move Java
 * test starts its container through {@link PgContainerHelper#startDataMove()}.
 */
class PgBackgroundImageSmokeTest {

    private static final Path REPO_SCRIPTS = Path.of("..", "scripts");

    static PostgreSQLContainer<?> pg;

    @BeforeAll
    static void start() {
        pg = PgContainerHelper.startDataMove();
    }

    @AfterAll
    static void stop() {
        if (pg != null) pg.stop();
    }

    private static String scalar(Statement st, String sql) throws Exception {
        try (ResultSet rs = st.executeQuery(sql)) {
            assertThat(rs.next()).as(sql).isTrue();
            return rs.getString(1);
        }
    }

    @Test
    void createExtension_pgBackground_atThePinnedVersion() throws Exception {
        try (Connection c = pg.createConnection(""); Statement st = c.createStatement()) {
            st.execute("CREATE EXTENSION pg_background");
            assertThat(scalar(st, "SELECT extversion FROM pg_extension WHERE extname = 'pg_background'"))
                .isEqualTo(PgContainerRecipe.PG_BACKGROUND_EXTVERSION);
        }
    }

    @Test
    void aBackgroundWorker_runsASelectAndReturnsItsResult_inAnotherBackend() throws Exception {
        try (Connection c = pg.createConnection(""); Statement st = c.createStatement()) {
            st.execute("CREATE EXTENSION IF NOT EXISTS pg_background");
            String mine = scalar(st, "SELECT pg_backend_pid()");
            assertThat(scalar(st,
                "SELECT r.v FROM pg_background_launch('SELECT 6 * 7') h, "
                    + "LATERAL pg_background_result(h.pid, h.cookie) AS r(v int)"))
                .isEqualTo("42");
            // The worker is a backend of its own: the pid it reports is not this session's.
            String worker = scalar(st,
                "SELECT r.v FROM pg_background_launch('SELECT pg_backend_pid()') h, "
                    + "LATERAL pg_background_result(h.pid, h.cookie) AS r(v int)");
            assertThat(worker).isNotEqualTo(mine);
        }
    }

    @Test
    void theVectorExtension_stillCreates() throws Exception {
        try (Connection c = pg.createConnection(""); Statement st = c.createStatement()) {
            st.execute("CREATE EXTENSION IF NOT EXISTS vector");
            assertThat(scalar(st, "SELECT extversion FROM pg_extension WHERE extname = 'vector'")).isNotBlank();
        }
    }

    /**
     * The recipe's pin is the bundle's pin. The Dockerfile takes the commit from the recipe, so the one place the
     * two could drift is the recipe against the two bundle scripts. Non-vacuity: both files are read and each must
     * hold the commit, so a moved script or a renamed pin fails here rather than passing on an empty match.
     */
    @Test
    void theRecipePin_isTheBundlesPin() throws IOException {
        for (String script : new String[] {"build_pg_bundle.sh", "build_pg_bundle_windows.py"}) {
            Path p = REPO_SCRIPTS.resolve(script);
            assertThat(p).as("the repo's %s, relative to service/", script).isRegularFile();
            assertThat(Files.readString(p, StandardCharsets.UTF_8))
                .as("%s pins the commit the data-move image builds", script)
                .contains(PgContainerRecipe.PG_BACKGROUND_COMMIT)
                .contains(PgContainerRecipe.PG_BACKGROUND_VERSION);
        }
        assertThat(PgContainerRecipe.PG_BACKGROUND_COMMIT).hasSize(40);
        assertThat(PgContainerRecipe.DATA_MOVE_IMAGE)
            .contains(PgContainerRecipe.PG_BACKGROUND_COMMIT.substring(0, 8))
            .contains(PgContainerRecipe.PG_BACKGROUND_VERSION.substring(1));
    }
}
