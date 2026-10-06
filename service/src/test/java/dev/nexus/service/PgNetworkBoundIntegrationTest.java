/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;

import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.SweepBounds;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.FanoutSettings;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import javax.sql.DataSource;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-u9zkn -- the per-path read bound. A path that the engine already bounds with a
 * {@code statement_timeout} also gets a network (socket read) timeout of that bound plus a margin, so a
 * read on a server that has gone silent fails instead of waiting on TCP; a path with no statement bound
 * gets none, because a connection waiting on a busy server is as silent as one waiting on a dead one.
 *
 * <p>The dead peer is the 2026-10-05 failover shape: the peer accepts, then says nothing (no RST, no
 * FIN). It is an in-test loopback proxy in front of the real PostgreSQL container that can be frozen, which
 * keeps both sockets open and discards every byte in both directions.
 *
 * <p>Non-vacuity, each measured by deleting the thing and watching the test fail: the search test fails
 * when {@code PgSession#setLocal} stops binding the network timeout (the read then hangs past the test's own
 * wait, and the test turns that hang into a failure rather than sitting in it); the recording tests fail
 * when a bounded path stops being bound, or when an unbounded one starts to be.
 *
 * <p>The first statement of every borrow is the tenant GUC stamp in {@code TenantScope#stampAndRun}, which
 * runs BEFORE the path sets its statement bound (a first version of the search test froze the peer there and
 * hung). The stamp therefore carries its own network bound of max(margin, 5 s), reset to none before the
 * work runs; the stamp test pins it, and the search test freezes the peer at the search statement itself.
 *
 * <p>Runs as {@code nexus_svc}, the production role.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 240, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
class PgNetworkBoundIntegrationTest {

    static final String QUERY = "u9zkn network bound query";
    static final String TENANT = "u9zkn-net";
    static final String COLLECTION = "knowledge__u9zkn-net__minilm-l6-v2-384__v1";

    /** Test margin, large enough to tell from the bound, small enough to keep the suite quick. */
    static final int MARGIN_MS = 1_500;
    /** The tenant stamp's bound for that margin: {@code max(margin, 5 s)}, not the margin alone. */
    static final int STAMP_MS = Math.max(MARGIN_MS, 5_000);
    /** The statement bound the frozen-peer search runs under. */
    static final int SEARCH_BOUND_MS = 1_000;

    PostgreSQLContainer<?> pg;
    HikariDataSource directDs;
    TenantScope directScope;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    PgVectorRepository directRepo;

    final List<FreezableProxy> proxies = new CopyOnWriteArrayList<>();
    final ExecutorService exec = Executors.newCachedThreadPool(r -> {
        var t = new Thread(r, "network-bound-test");
        t.setDaemon(true);
        return t;
    });

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        directDs = svcPool(pg.getJdbcUrl(), 6);
        directScope = new TenantScope(directDs);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1f, 0f);
        directRepo = new PgVectorRepository(directScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < 3; i++) {
            String text = TENANT + "|" + i;
            double theta = i * 0.05;
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of("kind", "odd"));
        }
        directRepo.upsertChunks(TENANT, COLLECTION, ids, texts, metas);
        // A chunk no catalog document owns is invisible to search; own them, as the production writer does.
        directScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, COLLECTION, ids.toArray(new String[0]));
            return null;
        });
    }

    @AfterEach
    void resetMargin() {
        PgSession.setNetworkBoundMarginMsForTests(-1);
    }

    @AfterAll
    void stopAll() {
        exec.shutdownNow();
        proxies.forEach(FreezableProxy::close);
        if (directDs != null) {
            directDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    private static HikariDataSource svcPool(String url, int size) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(url);
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(size);
        cfg.setMinimumIdle(1);
        cfg.setConnectionTimeout(10_000);
        cfg.setAutoCommit(true);
        // The same helper Main uses, so the pool carries the production tcpKeepAlive and, with it, NO
        // pool-wide socketTimeout: every network timeout this test sees is the per-path one.
        dev.nexus.service.db.PoolKeepAlive.apply(cfg, url);
        return new HikariDataSource(cfg);
    }

    private static String chash(String text) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    // ── (a) a bounded search read on a silent peer fails in bound + margin ─────────────────────

    @Test
    void aBoundedSearchOnASilentPeerFailsWithinTheBoundPlusMargin() throws Exception {
        PgSession.setNetworkBoundMarginMsForTests(MARGIN_MS);
        var m = java.util.regex.Pattern.compile("jdbc:postgresql://([^:/]+):(\\d+)/([^?]*)").matcher(pg.getJdbcUrl());
        assertThat(m.find()).as(pg.getJdbcUrl()).isTrue();
        var proxy = new FreezableProxy(m.group(1), Integer.parseInt(m.group(2)));
        proxies.add(proxy);
        String proxiedUrl = "jdbc:postgresql://127.0.0.1:" + proxy.port() + "/" + m.group(3) + "?sslmode=disable";

        try (HikariDataSource ds = svcPool(proxiedUrl, 4)) {
            var repo = new PgVectorRepository(new TenantScope(ds), embedder, embedder);
            // Large fan-out budget: it is the STATEMENT bound (the search bound) that sets the arm's
            // bound, and nothing else may end this call before the network timeout does.
            var settings = new FanoutSettings(1, 120_000L, SEARCH_BOUND_MS);

            // Healthy path through the proxy; also warms the registry caches so the frozen call borrows
            // only for its arm.
            var warm = repo.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 3, 10, null, null, false, settings);
            assertThat(warm.rows()).as("the healthy search works through the proxy").isNotEmpty();

            // The peer goes silent, no RST, at the moment the client sends the search statement itself:
            // the tenant stamp and the statement_timeout round trips before it were answered, so what is
            // in flight is exactly a bounded statement's read. (A peer silent from the stamp on is a
            // different, earlier read that no statement bound reaches; see the class doc.)
            proxy.freezeWhenClientSends("plain_search");
            long boundPlusMarginMs = SEARCH_BOUND_MS + MARGIN_MS;
            long start = System.nanoTime();
            Future<?> search = exec.submit(
                () -> repo.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 3, 10, null, null, false, settings));
            Throwable failure = null;
            try {
                search.get(boundPlusMarginMs * 8, TimeUnit.MILLISECONDS);
            } catch (TimeoutException hung) {
                failure = hung;
            } catch (ExecutionException e) {
                failure = e.getCause();
            } finally {
                proxy.close();                                       // RST so any blocked thread ends
            }
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

            assertThat(failure)
                .as("the search must FAIL, not hang past %d ms (no network timeout bound to the search?)",
                    boundPlusMarginMs * 8)
                .isNotInstanceOf(TimeoutException.class);
            assertThat(causeChain(failure))
                .as("the failure is a socket read timeout")
                .anyMatch(t -> t instanceof SocketTimeoutException);
            assertThat(elapsedMs).as("fails at about statement bound + margin (%d ms)", boundPlusMarginMs)
                .isBetween(boundPlusMarginMs - 300, boundPlusMarginMs + 3_000);
        }
    }

    // ── (a2) a peer silent from the tenant stamp on fails in the stamp bound ────────────────────────

    @Test
    void aPeerSilentAtTheTenantStampFailsWithinTheStampBound() throws Exception {
        PgSession.setNetworkBoundMarginMsForTests(MARGIN_MS);
        var m = java.util.regex.Pattern.compile("jdbc:postgresql://([^:/]+):(\\d+)/([^?]*)").matcher(pg.getJdbcUrl());
        assertThat(m.find()).as(pg.getJdbcUrl()).isTrue();
        var proxy = new FreezableProxy(m.group(1), Integer.parseInt(m.group(2)));
        proxies.add(proxy);
        String proxiedUrl = "jdbc:postgresql://127.0.0.1:" + proxy.port() + "/" + m.group(3) + "?sslmode=disable";

        try (HikariDataSource ds = svcPool(proxiedUrl, 4)) {
            var scope = new TenantScope(ds);
            scope.withTenant(TENANT, ctx -> ctx.selectOne().fetch());   // healthy through the proxy

            // The first set_config the client sends from here on is the next borrow's tenant stamp.
            proxy.freezeWhenClientSends("set_config");
            long start = System.nanoTime();
            Future<?> borrow = exec.submit(() -> scope.withTenant(TENANT, ctx -> ctx.selectOne().fetch()));
            Throwable failure = null;
            try {
                borrow.get(STAMP_MS * 4L, TimeUnit.MILLISECONDS);
            } catch (TimeoutException hung) {
                failure = hung;
            } catch (ExecutionException e) {
                failure = e.getCause();
            } finally {
                proxy.close();
            }
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

            assertThat(failure)
                .as("the stamp must FAIL, not hang past %d ms (no network bound on the stamp?)", STAMP_MS * 4L)
                .isNotInstanceOf(TimeoutException.class);
            assertThat(causeChain(failure)).as("the failure is a socket read timeout")
                .anyMatch(t -> t instanceof SocketTimeoutException);
            assertThat(elapsedMs).as("fails at about the stamp bound (%d ms: the 5 s floor, above the %d ms margin)",
                    STAMP_MS, MARGIN_MS)
                .isBetween((long) STAMP_MS - 300, (long) STAMP_MS + 3_000);
        }
    }

    // ── (c) restored after the borrow, on a pinned physical connection ─────────────────────────

    @Test
    void theNetworkTimeoutIsRestoredToZeroAfterTheBoundedBorrow_onTheSamePhysicalConnection() throws Exception {
        PgSession.setNetworkBoundMarginMsForTests(MARGIN_MS);
        try (HikariDataSource one = svcPool(pg.getJdbcUrl(), 1)) {
            var scope = new TenantScope(one);

            int[] pidInside = new int[1];
            int[] netInside = new int[1];
            scope.withTenant(TENANT, ctx -> {
                PgSession.setSearchStatementTimeout(ctx, SEARCH_BOUND_MS);
                pidInside[0] = ctx.select(DSL.function("pg_backend_pid", Integer.class)).fetchOne(0, Integer.class);
                ctx.connection(c -> netInside[0] = c.getNetworkTimeout());
                return null;
            });
            assertThat(netInside[0]).as("inside the bounded borrow: bound + margin")
                .isEqualTo(SEARCH_BOUND_MS + MARGIN_MS);

            // Pool size 1: this borrow is the SAME physical connection, so the restore is observed on the
            // connection that carried the bound, not on a fresh one.
            try (Connection c = one.getConnection()) {
                int pidAfter = DSL.using(c, SQLDialect.POSTGRES)
                    .select(DSL.function("pg_backend_pid", Integer.class)).fetchOne(0, Integer.class);
                assertThat(pidAfter).as("the pool of one reuses the physical connection").isEqualTo(pidInside[0]);
                assertThat(c.getNetworkTimeout()).as("restored: no read bound outlives the borrow").isZero();
            }
        }
    }

    // ── (b) which paths are bound, and which are not ───────────────────────────────────────────

    @Test
    void everyBoundedPathIsGivenBoundPlusMargin_andNoUnboundedPathIsGivenAny() throws Exception {
        PgSession.setNetworkBoundMarginMsForTests(MARGIN_MS);
        var recording = new RecordingDataSource(directDs);
        var scope = new TenantScope(recording);
        var repo = new PgVectorRepository(scope, embedder, embedder);
        var reaper = new ReaperRepository(scope);

        // Bounded: SweepBounds (sweeps, tuple subspace list, token store, plans, scratch all use it).
        recording.clear();
        scope.withTenant(TENANT, ctx -> {
            SweepBounds.applyStatementTimeout(ctx, Duration.ofSeconds(30));
            return null;
        });
        assertThat(pathBounds(recording)).as("SweepBounds: 30 s + margin").containsExactly(30_000 + MARGIN_MS);

        // Bounded: the reaper's own statements, and the taxonomy assign bounds, through
        // setStatementAndLockBounds.
        recording.clear();
        reaper.holdsNothing(TENANT, Duration.ofSeconds(25));
        assertThat(pathBounds(recording)).as("reaper statement: 25 s + margin").containsExactly(25_000 + MARGIN_MS);

        // Bounded: the census, only when a bound is passed...
        recording.clear();
        repo.manifestLessCensusBounded(TENANT, COLLECTION, 300, 0, Duration.ofSeconds(60));
        assertThat(pathBounds(recording)).as("bounded census: 60 s + margin").containsExactly(60_000 + MARGIN_MS);

        // ...and UNBOUNDED when none is: the same method, no bound, no network timeout.
        recording.clear();
        repo.manifestLessCensus(TENANT, COLLECTION, 300, 0);
        assertThat(pathBounds(recording)).as("the unbounded census is not given a network timeout").isEmpty();
        assertThat(recording.calls()).as("its stamp was bounded, then reset to none").endsWith(STAMP_MS, 0);

        // Unbounded: a write (the re-home, quarantine, purge and delete/rename paths are writes of this
        // kind: no engine statement bound, minutes long) is not given one either.
        recording.clear();
        String text = TENANT + "|unbounded-write";
        embedder.register(text, 1f, 0f);
        repo.upsertChunks(TENANT, COLLECTION, List.of(chash(text)), List.of(text), List.of(Map.of("kind", "odd")));
        assertThat(pathBounds(recording)).as("an unbounded write is not given a network timeout").isEmpty();
        assertThat(recording.calls()).as("the write runs with no timeout: the last value set is 0").endsWith(0);

        // Unbounded: VACUUM (ANALYZE) borrows a connection directly and has no statement bound.
        recording.clear();
        scope.vacuumAnalyze(List.of("nexus.catalog_documents"));
        assertThat(recording.calls()).as("VACUUM is not given a network timeout").isEmpty();

        // The operator escape hatch: margin 0 turns the per-path bound off everywhere.
        PgSession.setNetworkBoundMarginMsForTests(0);
        recording.clear();
        repo.manifestLessCensusBounded(TENANT, COLLECTION, 300, 0, Duration.ofSeconds(60));
        assertThat(recording.calls()).as("margin 0 disables the per-path bound").isEmpty();
    }

    /**
     * The network timeouts a path set for its own statements: the recorded calls with every borrow's
     * leading tenant-stamp pair ({@code STAMP_MS}, then 0) removed. Per borrow, not over the flat list:
     * every borrow must OPEN with that pair (the stamp is the first round
     * trip of a {@code TenantScope} borrow), and the number of pairs must equal the number of borrows the
     * test made, so a borrow whose stamp lost its bound, or one that stamps twice, fails here instead of
     * being stripped as noise. Asserts at least one stamp pair was seen.
     */
    private static List<Integer> pathBounds(RecordingDataSource recording) {
        var out = new ArrayList<Integer>();
        var borrows = recording.borrows();
        int stamps = 0;
        for (int b = 0; b < borrows.size(); b++) {
            var seq = borrows.get(b);
            assertThat(seq).as("borrow %d of %s opens with the bounded stamp, then its reset", b, borrows)
                .startsWith(STAMP_MS, 0);
            stamps++;
            out.addAll(seq.subList(2, seq.size()));
        }
        assertThat(stamps).as("every tenant borrow bounds its stamp (borrows %s)", borrows).isPositive();
        assertThat(stamps).as("one stamp pair per tenant borrow (borrows %s)", borrows).isEqualTo(borrows.size());
        return out;
    }

    private static List<Throwable> causeChain(Throwable t) {
        var chain = new ArrayList<Throwable>();
        for (Throwable x = t; x != null && !chain.contains(x); x = x.getCause()) {
            chain.add(x);
            for (Throwable s : x.getSuppressed()) {
                chain.add(s);
            }
        }
        return chain;
    }

    /**
     * Wraps a pool so every connection records the {@code setNetworkTimeout} values it is given, in call
     * order, and otherwise delegates. The delegate is the real Hikari connection, so the pool's own
     * restore-on-close behaviour is untouched.
     */
    static final class RecordingDataSource implements DataSource {
        private final DataSource delegate;
        /** One list per borrow, in borrow order; empty for a borrow that set no network timeout. */
        private final List<List<Integer>> borrows = new CopyOnWriteArrayList<>();

        RecordingDataSource(DataSource delegate) {
            this.delegate = delegate;
        }

        /** Every recorded value, flat, in order. */
        List<Integer> calls() {
            return borrows.stream().flatMap(List::stream).toList();
        }

        /** The recorded values per borrow, empty lists included (VACUUM, margin 0). */
        List<List<Integer>> borrows() {
            return borrows.stream().map(List::copyOf).toList();
        }

        void clear() {
            borrows.clear();
        }

        @Override
        public Connection getConnection() throws java.sql.SQLException {
            Connection real = delegate.getConnection();
            var mine = new CopyOnWriteArrayList<Integer>();
            borrows.add(mine);
            return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(),
                new Class<?>[] {Connection.class}, (proxy, method, args) -> {
                    if (method.getName().equals("setNetworkTimeout")) {
                        mine.add((Integer) args[1]);
                    }
                    try {
                        return method.invoke(real, args);
                    } catch (InvocationTargetException e) {
                        throw e.getCause();
                    }
                });
        }

        @Override
        public Connection getConnection(String u, String p) throws java.sql.SQLException {
            return getConnection();
        }

        @Override
        public java.io.PrintWriter getLogWriter() {
            return null;
        }

        @Override
        public void setLogWriter(java.io.PrintWriter out) {
        }

        @Override
        public void setLoginTimeout(int seconds) {
        }

        @Override
        public int getLoginTimeout() {
            return 0;
        }

        @Override
        public java.util.logging.Logger getParentLogger() {
            return java.util.logging.Logger.getGlobal();
        }

        @Override
        public <T> T unwrap(Class<T> iface) throws java.sql.SQLException {
            return delegate.unwrap(iface);
        }

        @Override
        public boolean isWrapperFor(Class<?> iface) throws java.sql.SQLException {
            return delegate.isWrapperFor(iface);
        }
    }

    /**
     * Loopback TCP proxy to one target. {@link #freeze()} keeps every socket open and discards all bytes
     * both ways from then on: a peer that has gone silent without an RST.
     */
    static final class FreezableProxy implements AutoCloseable {
        private final ServerSocket listener;
        private final String targetHost;
        private final int targetPort;
        private final AtomicBoolean frozen = new AtomicBoolean();
        private volatile String trigger;
        private final AtomicBoolean closed = new AtomicBoolean();
        private final List<Socket> sockets = new CopyOnWriteArrayList<>();

        FreezableProxy(String targetHost, int targetPort) throws IOException {
            this.targetHost = targetHost;
            this.targetPort = targetPort;
            this.listener = new ServerSocket(0, 50, InetAddress.getLoopbackAddress());
            Thread acceptor = new Thread(this::acceptLoop, "freezable-proxy-accept");
            acceptor.setDaemon(true);
            acceptor.start();
        }

        int port() {
            return listener.getLocalPort();
        }

        void freeze() {
            frozen.set(true);
        }

        /** Freeze at the first client-to-server write containing {@code needle}; that write is dropped. */
        void freezeWhenClientSends(String needle) {
            this.trigger = needle;
        }

        private void acceptLoop() {
            while (!closed.get()) {
                try {
                    Socket client = listener.accept();
                    Socket upstream = new Socket(targetHost, targetPort);
                    sockets.add(client);
                    sockets.add(upstream);
                    pump(client, upstream, true);
                    pump(upstream, client, false);
                } catch (IOException e) {
                    if (closed.get()) {
                        return;
                    }
                }
            }
        }

        private void pump(Socket from, Socket to, boolean clientToServer) {
            Thread t = new Thread(() -> {
                byte[] buf = new byte[8192];
                try {
                    InputStream in = from.getInputStream();
                    OutputStream out = to.getOutputStream();
                    int n;
                    while ((n = in.read(buf)) >= 0) {
                        String needle = trigger;
                        if (clientToServer && needle != null && !frozen.get()
                            && new String(buf, 0, n, java.nio.charset.StandardCharsets.ISO_8859_1).contains(needle)) {
                            frozen.set(true);
                        }
                        if (!frozen.get()) {
                            out.write(buf, 0, n);
                            out.flush();
                        }
                    }
                } catch (IOException ignored) {
                    // socket closed
                }
            }, "freezable-proxy-pump");
            t.setDaemon(true);
            t.start();
        }

        @Override
        public void close() {
            if (!closed.compareAndSet(false, true)) {
                return;
            }
            try {
                listener.close();
            } catch (IOException ignored) {
                // best effort: the test is over
            }
            for (Socket s : sockets) {
                try {
                    s.setSoLinger(true, 0);                  // RST, so a blocked peer read ends
                    s.close();
                } catch (IOException ignored) {
                    // best effort: the test is over
                }
            }
        }
    }
}
