// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;

import java.sql.Connection;
import java.sql.ResultSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-yhmav mutation-falsification: empirically proves TWO specific legs of the
 * shared-cluster reset contract -- it does NOT prove "isolation" wholesale (an
 * earlier draft of this javadoc overclaimed exactly that; substantive-critic
 * 2026-08-09). What is covered here, and only this:
 * <ol>
 *   <li><b>Table-row isolation</b> ({@code @Order(1)}/{@code (2)}): a row written in
 *       one per-class database is invisible in the next class's template clone.</li>
 *   <li><b>Cluster-wide role-GUC isolation</b> ({@code @Order(3)}/{@code (4)},
 *       nexus-tyiht): {@code ALTER ROLE ... SET} with no {@code IN DATABASE} clause
 *       writes a CLUSTER-scoped {@code pg_db_role_setting} row ({@code setdatabase=0})
 *       that template cloning neither copies nor resets -- the one state ~90 test
 *       classes' bootstraps write that the database boundary does NOT isolate. The
 *       fix under test is {@link SharedCluster#acquireDatabase()}'s reset-at-acquire
 *       (every cluster-wide role setting is RESET before each clone is handed out);
 *       these two methods poison exactly that state and prove the next acquire
 *       clears it.</li>
 * </ol>
 * Anything else (roles' existence, extensions, sequences, etc.) is argued from PG
 * semantics in {@link SharedCluster}'s javadoc, not falsified here.
 *
 * <p>Each {@code @Test} calls {@link PgContainerHelper#start()} independently --
 * exactly as DIFFERENT test classes would -- so each gets its own
 * {@code CREATE DATABASE ... TEMPLATE} clone on the SAME shared per-fork cluster
 * (surefire always keeps one class's methods in a single fork/JVM regardless of
 * {@code nexus.test.forks}, so this falsification is valid at any fork count,
 * including the CI-pinned {@code forks=1}).
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class SharedClusterMutationFalsifyTest {

    private static final String MARKER_TENANT = "yhmav-poison-tenant";
    private static final String MARKER_PREFIX = "0.yhmav-poison";

    @Test
    @Order(1)
    void poisonWritesAMarkerRowVisibleOnlyHere() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_owners "
                + "(tenant_id, tumbler_prefix, name, owner_type) VALUES (?, ?, ?, ?) "
                + "ON CONFLICT DO NOTHING")) {
                ps.setString(1, MARKER_TENANT);
                ps.setString(2, MARKER_PREFIX);
                ps.setString(3, "yhmav-poison-marker");
                ps.setString(4, "test");
                ps.executeUpdate();
            }
            // Sanity: the marker IS visible in THIS class's own database --
            // otherwise the negative assertion in the next test would be vacuous.
            assertThat(markerCount(su))
                .as("the poison write must actually be visible in its own database, "
                    + "or the isolation assertion in the paired test is vacuous")
                .isEqualTo(1);
        } finally {
            pg.stop();
        }
    }

    @Test
    @Order(2)
    void nextDatabaseOnTheSharedClusterStartsPristine() throws Exception {
        // A DIFFERENT PgContainerHelper.start() call -- on the shared cluster this
        // is a DIFFERENT CREATE DATABASE ... TEMPLATE clone, not the same database
        // reused. If nexus-yhmav's reset leaked instead of isolated, the marker row
        // written above would show up here too.
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            assertThat(markerCount(su))
                .as("a class-level write from an earlier database on this SAME "
                    + "shared cluster must be invisible in a freshly templated "
                    + "database (nexus-yhmav CREATE DATABASE ... TEMPLATE isolation)")
                .isEqualTo(0);
        } finally {
            pg.stop();
        }
    }

    @Test
    @Order(3)
    void poisonSetsAClusterWideRoleGucVisibleNow() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // The exact offender shape from the ~90 bootstrap sites, with a poison
            // value nothing else in the suite would ever set: a CLUSTER-wide
            // (no IN DATABASE clause) role-level GUC on the shared nexus_svc role.
            su.createStatement().execute(
                "ALTER ROLE " + PgContainerHelper.SVC_USERNAME
                + " SET search_path TO yhmav_poison_schema, public");
            // Non-vacuity: the cluster-wide pg_db_role_setting row must exist NOW --
            // otherwise Order(4)'s absence assertion proves nothing.
            assertThat(clusterWideRoleSettings(su))
                .as("the poison ALTER ROLE must actually land as a cluster-wide "
                    + "(setdatabase=0) pg_db_role_setting row, or the isolation "
                    + "assertion in the paired test is vacuous")
                .contains("yhmav_poison_schema");
        } finally {
            pg.stop();
        }
    }

    @Test
    @Order(4)
    void nextAcquireResetsClusterWideRoleGucs() throws Exception {
        // A fresh acquire on the SAME shared cluster. Cluster-wide role settings are
        // exactly what CREATE DATABASE ... TEMPLATE does NOT isolate (nexus-tyiht) --
        // only SharedCluster.acquireDatabase()'s reset-at-acquire clears them. If that
        // reset is ever removed or narrowed, the poison from Order(3) survives into
        // this clone's cluster and this assertion fails.
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            assertThat(clusterWideRoleSettings(su))
                .as("a cluster-wide ALTER ROLE ... SET issued during an earlier "
                    + "class's lifetime must be RESET by the next acquire "
                    + "(SharedCluster reset-at-acquire, nexus-tyiht)")
                .doesNotContain("yhmav_poison_schema");
        } finally {
            pg.stop();
        }
    }

    /** All cluster-wide (setdatabase=0) role-level settings in one string --
     *  empty string when none exist (the virgin-cluster baseline). */
    private static String clusterWideRoleSettings(Connection c) throws Exception {
        try (var st = c.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT coalesce(string_agg(p.rolname || '=' "
                 + "|| array_to_string(s.setconfig, ','), '; '), '') "
                 + "FROM pg_db_role_setting s JOIN pg_roles p ON p.oid = s.setrole "
                 + "WHERE s.setdatabase = 0")) {
            rs.next();
            return rs.getString(1);
        }
    }

    private static int markerCount(Connection c) throws Exception {
        try (var st = c.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT count(*) FROM nexus.catalog_owners WHERE tenant_id = '"
                 + MARKER_TENANT + "' AND tumbler_prefix = '" + MARKER_PREFIX + "'")) {
            rs.next();
            return rs.getInt(1);
        }
    }
}
