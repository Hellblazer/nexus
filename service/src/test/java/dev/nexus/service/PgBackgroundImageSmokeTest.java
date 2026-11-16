// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;
import java.time.Duration;
import java.util.List;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

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
    void theServer_isPostgres17_andPgvector_isAtLeast08() throws Exception {
        try (Connection c = pg.createConnection(""); Statement st = c.createStatement()) {
            int num = Integer.parseInt(scalar(st, "SHOW server_version_num"));
            st.execute("CREATE EXTENSION IF NOT EXISTS vector");
            String vector = scalar(st, "SELECT extversion FROM pg_extension WHERE extname = 'vector'");
            System.out.println("PG_BACKGROUND_IMAGE server " + scalar(st, "SHOW server_version") + " (minor " + (num % 100)
                + "), pgvector " + vector + ", image " + PgBackgroundImage.imageName());
            assertThat(num / 10000).as("server major version (server_version_num %s)", num).isEqualTo(17);
            Matcher m = Pattern.compile("^(\\d+)\\.(\\d+)").matcher(vector);
            assertThat(m.find()).as("pgvector extversion %s", vector).isTrue();
            int major = Integer.parseInt(m.group(1));
            int minor = Integer.parseInt(m.group(2));
            assertThat(major > 0 || minor >= 8).as("pgvector %s is at least 0.8", vector).isTrue();
        }
    }

    @Test
    void createExtension_pgBackground_atThePinnedVersion() throws Exception {
        try (Connection c = pg.createConnection(""); Statement st = c.createStatement()) {
            st.execute("CREATE EXTENSION IF NOT EXISTS pg_background");
            assertThat(scalar(st, "SELECT extversion FROM pg_extension WHERE extname = 'pg_background'"))
                .isEqualTo(PgBackgroundImage.PG_BACKGROUND_EXTVERSION);
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

    /** startDataMove(List) appends postmaster settings after the recipe's own: the slot-exhaustion tests set max_worker_processes. */
    @Test
    void startDataMove_withServerArgs_startsTheClusterWithThem() throws Exception {
        PostgreSQLContainer<?> c = PgContainerHelper.startDataMove(List.of("-c", "max_worker_processes=4"));
        try (Connection conn = c.createConnection(""); Statement st = conn.createStatement()) {
            assertThat(scalar(st, "SHOW max_worker_processes")).isEqualTo("4");
            // The recipe's own flags are still there, in front of the appended ones.
            assertThat(scalar(st, "SHOW fsync")).isEqualTo("off");
        } finally {
            c.stop();
        }
        // Non-vacuity: the plain container does not run with 4, so the assertion above reads the argument.
        try (Connection conn = pg.createConnection(""); Statement st = conn.createStatement()) {
            assertThat(scalar(st, "SHOW max_worker_processes")).isNotEqualTo("4");
        }
    }

    // ── the build lock ──────────────────────────────────────────────────────────────────────────────────────

    @Test
    void theLockFile_isPerUser_andABusyLockFailsLoudAfterTheDeadline() throws Exception {
        assertThat(PgBackgroundImage.LOCK_FILE.getFileName().toString())
            .contains(System.getProperty("user.name").replaceAll("[^A-Za-z0-9_.-]", "_"))
            .startsWith("nexus-pg-background-image-").endsWith(".lock");
        Path f = Files.createTempFile("pgbg-lock", ".lock");
        try (FileChannel holder = FileChannel.open(f, StandardOpenOption.WRITE);
             FileLock held = holder.lock();
             FileChannel waiter = FileChannel.open(f, StandardOpenOption.WRITE)) {
            assertThat(held.isValid()).isTrue();
            long t0 = System.nanoTime();
            assertThatThrownBy(() -> PgBackgroundImage.lock(waiter, Duration.ofSeconds(1)))
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("has held");
            assertThat(Duration.ofNanos(System.nanoTime() - t0)).as("it waited the deadline, not less").isGreaterThanOrEqualTo(Duration.ofSeconds(1));
        } finally {
            Files.deleteIfExists(f);
        }
        // A free lock is taken at once.
        Path g = Files.createTempFile("pgbg-lock", ".lock");
        try (FileChannel ch = FileChannel.open(g, StandardOpenOption.WRITE);
             FileLock l = PgBackgroundImage.lock(ch, Duration.ofSeconds(1))) {
            assertThat(l.isValid()).isTrue();
        } finally {
            Files.deleteIfExists(g);
        }
    }

    // ── the pin: the image builds the commit the bundle builds ──────────────────────────────────────────────

    private static final Pattern SH_DEFAULT_VERSION = Pattern.compile(
        "(?m)^PG_BACKGROUND_VERSION=\"\\$\\{PG_BACKGROUND_VERSION:-(v[0-9][^}\"]*)\\}\"");
    private static final Pattern SH_CASE_ENTRY = Pattern.compile("(?m)^\\s*(v[0-9][^)\\s]*)\\)\\s*echo\\s+\"([0-9a-f]{40})\"");
    private static final Pattern PY_DEFAULT_VERSION = Pattern.compile(
        "(?m)^DEFAULT_PG_BACKGROUND_VERSION\\s*=\\s*\"(v[0-9][^\"]*)\"");
    private static final Pattern PY_DICT = Pattern.compile(
        "(?ms)^PINNED_PG_BACKGROUND_COMMITS\\b[^=\\n]*=\\s*\\{(.*?)^\\}");
    private static final Pattern PY_ENTRY = Pattern.compile("\"(v[0-9][^\"]*)\"\\s*:\\s*\"([0-9a-f]{40})\"");

    /** The default version and the version-to-commit entries of one bundle script. */
    record Pin(String defaultVersion, Map<String, String> commits) {
        String commitOfDefault() {
            return commits.get(defaultVersion);
        }
    }

    static Pin shellPin(String text) {
        Matcher d = SH_DEFAULT_VERSION.matcher(text);
        assertThat(d.find()).as("PG_BACKGROUND_VERSION default in build_pg_bundle.sh").isTrue();
        int from = text.indexOf("pg_background_pinned_commit()");
        assertThat(from).as("pg_background_pinned_commit() in build_pg_bundle.sh").isGreaterThanOrEqualTo(0);
        Map<String, String> commits = new LinkedHashMap<>();
        Matcher e = SH_CASE_ENTRY.matcher(text.substring(from));
        while (e.find()) {
            commits.put(e.group(1), e.group(2));
        }
        return new Pin(d.group(1), commits);
    }

    static Pin pythonPin(String text) {
        Matcher d = PY_DEFAULT_VERSION.matcher(text);
        assertThat(d.find()).as("DEFAULT_PG_BACKGROUND_VERSION in build_pg_bundle_windows.py").isTrue();
        Matcher block = PY_DICT.matcher(text);
        assertThat(block.find()).as("PINNED_PG_BACKGROUND_COMMITS in build_pg_bundle_windows.py").isTrue();
        Map<String, String> commits = new LinkedHashMap<>();
        Matcher e = PY_ENTRY.matcher(block.group(1));
        while (e.find()) {
            commits.put(e.group(1), e.group(2));
        }
        return new Pin(d.group(1), commits);
    }

    /**
     * The recipe's pin is the pin of both bundle scripts. Each script's DEFAULT version is read, and the commit that
     * version maps to, so a bump that adds the new entry and leaves the old one behind fails (the default moved), and so
     * does one that moves the default and forgets the entry (no commit).
     */
    @Test
    void theImagePin_isTheBundlesDefaultVersionAndItsCommit() throws IOException {
        Map<String, Pin> pins = new LinkedHashMap<>();
        pins.put("build_pg_bundle.sh", shellPin(Files.readString(REPO_SCRIPTS.resolve("build_pg_bundle.sh"), StandardCharsets.UTF_8)));
        pins.put("build_pg_bundle_windows.py",
            pythonPin(Files.readString(REPO_SCRIPTS.resolve("build_pg_bundle_windows.py"), StandardCharsets.UTF_8)));
        pins.forEach((script, pin) -> {
            assertThat(pin.commits()).as("%s: version-to-commit entries examined", script).isNotEmpty();
            assertThat(pin.defaultVersion()).as("%s default version", script).isEqualTo(PgBackgroundImage.PG_BACKGROUND_VERSION);
            assertThat(pin.commitOfDefault()).as("%s commit of its default version", script)
                .isEqualTo(PgBackgroundImage.PG_BACKGROUND_COMMIT);
        });
        assertThat(PgBackgroundImage.PG_BACKGROUND_COMMIT).hasSize(40);
        assertThat(PgBackgroundImage.imageTag())
            .startsWith(PgBackgroundImage.PG_BACKGROUND_VERSION.substring(1) + "-" + PgBackgroundImage.PG_BACKGROUND_COMMIT.substring(0, 8) + "-");
    }

    @Test
    void thePinParsers_followTheDefault_notAnEntryLeftBehind() {
        String sh = """
            PG_BACKGROUND_VERSION="${PG_BACKGROUND_VERSION:-v2.0.5}"
            pg_background_pinned_commit() {
                case "$1" in
                    v2.0.4) echo "c1db2540408ce4626ea21bc97572ff43b1a19ff3" ;;
                    v2.0.5) echo "0123456789abcdef0123456789abcdef01234567" ;;
                    *) return 1 ;;
                esac
            }
            """;
        Pin s = shellPin(sh);
        assertThat(s.defaultVersion()).isEqualTo("v2.0.5");
        assertThat(s.commits()).hasSize(2);
        assertThat(s.commitOfDefault()).isEqualTo("0123456789abcdef0123456789abcdef01234567");
        String py = """
            DEFAULT_PG_BACKGROUND_VERSION = "v2.0.5"
            PINNED_PG_BACKGROUND_COMMITS: dict[str, str] = {
                "v2.0.4": "c1db2540408ce4626ea21bc97572ff43b1a19ff3",
                "v2.0.5": "0123456789abcdef0123456789abcdef01234567",
            }
            """;
        Pin p = pythonPin(py);
        assertThat(p.defaultVersion()).isEqualTo("v2.0.5");
        assertThat(p.commits()).hasSize(2);
        assertThat(p.commitOfDefault()).isEqualTo("0123456789abcdef0123456789abcdef01234567");
    }
}
