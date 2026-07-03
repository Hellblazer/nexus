// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.http.HttpUtil;
import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Wave review (Java-tree audit High-1): systemic admission control in
 * {@link TenantScope}. The virtual-thread-per-request HTTP layer has no inherent
 * bound on concurrent DB work; the semaphore bounds it at 2x pool size and maps
 * rejection to the SAME typed retryable signal as HikariCP connectionTimeout
 * ({@code SQLTransientConnectionException} in the cause chain), so
 * {@link HttpUtil#isPoolExhausted} — and therefore every handler's typed-503
 * ladder — covers it with no new mapping surface.
 *
 * <p>Pure unit test: a stub {@link DataSource} hands out no-op proxy Connections,
 * so admission semantics are exercised without Postgres.
 */
class TenantScopeAdmissionTest {

    /** No-op JDBC connection: absorbs setAutoCommit/commit/close; prepareStatement -> no-op stmt. */
    private static Connection noopConnection() {
        InvocationHandler stmtHandler = (proxy, method, args) -> switch (method.getName()) {
            case "execute" -> false;
            case "close" -> null;
            default -> defaultValue(method.getReturnType());
        };
        Object stmt = Proxy.newProxyInstance(
            TenantScopeAdmissionTest.class.getClassLoader(),
            new Class<?>[]{java.sql.PreparedStatement.class}, stmtHandler);
        InvocationHandler connHandler = (proxy, method, args) -> switch (method.getName()) {
            case "prepareStatement" -> stmt;
            case "isValid" -> true;
            default -> defaultValue(method.getReturnType());
        };
        return (Connection) Proxy.newProxyInstance(
            TenantScopeAdmissionTest.class.getClassLoader(),
            new Class<?>[]{Connection.class}, connHandler);
    }

    private static Object defaultValue(Class<?> type) {
        if (!type.isPrimitive() || type == void.class) return null;
        if (type == boolean.class) return false;
        if (type == float.class) return 0f;
        if (type == double.class) return 0d;
        if (type == long.class) return 0L;
        return 0;
    }

    private static DataSource stubDataSource() {
        InvocationHandler h = (proxy, method, args) -> switch (method.getName()) {
            case "getConnection" -> noopConnection();
            default -> defaultValue(method.getReturnType());
        };
        return (DataSource) Proxy.newProxyInstance(
            TenantScopeAdmissionTest.class.getClassLoader(),
            new Class<?>[]{DataSource.class}, h);
    }

    @Test
    void surplusCallerGetsTypedRetryableRejection_notUnboundedQueueing() throws Exception {
        // 2 permits, 200ms admission timeout. Two workers occupy both permits
        // (parked inside the work lambda); the third caller must be rejected with
        // the typed retryable signal within ~timeout, not queue indefinitely.
        DataSource ds = stubDataSource();
        TenantScope scope = new TenantScope(ds, 2, 200);

        CountDownLatch bothInside = new CountDownLatch(2);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            for (int i = 0; i < 2; i++) {
                pool.submit(() -> scope.withTenant("t1", ctx -> {
                    bothInside.countDown();
                    try {
                        release.await(10, TimeUnit.SECONDS);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                    return null;
                }));
            }
            assertThat(bothInside.await(5, TimeUnit.SECONDS)).isTrue();

            AtomicReference<Throwable> rejected = new AtomicReference<>();
            long start = System.nanoTime();
            try {
                scope.withTenant("t1", ctx -> null);
            } catch (RuntimeException e) {
                rejected.set(e);
            }
            long elapsedMs = (System.nanoTime() - start) / 1_000_000;

            assertThat(rejected.get()).as("third caller rejected").isNotNull();
            assertThat(HttpUtil.isPoolExhausted(rejected.get()))
                .as("rejection carries the typed retryable pool-exhaustion signal").isTrue();
            assertThat(elapsedMs)
                .as("rejection is bounded by the admission timeout, not open-ended")
                .isLessThan(5_000);
        } finally {
            release.countDown();
            pool.shutdownNow();
            assertThat(pool.awaitTermination(5, TimeUnit.SECONDS)).isTrue();
        }
    }

    @Test
    void admissionReleasesPermits_afterWorkCompletes() {
        DataSource ds = stubDataSource();
        TenantScope scope = new TenantScope(ds, 1, 200);
        // Sequential calls far exceeding the permit count all succeed — permits
        // are released on completion (success AND failure paths).
        for (int i = 0; i < 5; i++) {
            String out = scope.withTenant("t1", ctx -> "ok");
            assertThat(out).isEqualTo("ok");
        }
        for (int i = 0; i < 3; i++) {
            try {
                scope.withTenant("t1", ctx -> { throw new IllegalStateException("boom"); });
            } catch (IllegalStateException expected) {
                // work exception propagates unchanged
            }
        }
        String out = scope.withTenant("t1", ctx -> "still-ok");
        assertThat(out).isEqualTo("still-ok");
    }

    @Test
    void sameDataSource_sharesOneAdmissionBound_acrossScopeInstances() throws Exception {
        // Production constructs multiple TenantScopes over ONE DataSource
        // (NexusService + Main's PgVectorRepository) — the bound must be shared,
        // not multiplied per instance.
        DataSource ds = stubDataSource();
        TenantScope a = new TenantScope(ds, 1, 200);
        TenantScope b = new TenantScope(ds, 99, 200);  // ignored: ds already registered

        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(1);
        try {
            pool.submit(() -> a.withTenant("t1", ctx -> {
                inside.countDown();
                try {
                    release.await(10, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return null;
            }));
            assertThat(inside.await(5, TimeUnit.SECONDS)).isTrue();

            Throwable rejected = null;
            try {
                b.withTenant("t1", ctx -> null);  // scope B, same ds -> same semaphore
            } catch (RuntimeException e) {
                rejected = e;
            }
            assertThat(rejected).as("scope B rejected while scope A holds the only permit").isNotNull();
            assertThat(HttpUtil.isPoolExhausted(rejected)).isTrue();
        } finally {
            release.countDown();
            pool.shutdownNow();
            assertThat(pool.awaitTermination(5, TimeUnit.SECONDS)).isTrue();
        }
    }
}
