// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.AdminConnection;
import org.junit.jupiter.api.Test;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * What {@link CompletionWatch#close} waits for, measured (nexus-p2m1x.21). The watch's thread opens a session to read the move's row, and, when the
 * row says the move is held and {@code closed} is still false after the read, a second session to cancel. Each session is bounded alone (the
 * network timeout {@link DataMoveOrphans#CANCEL_SOCKET_SECONDS}, 5 s, cuts a server that stalls; a lock is given up after 2 s). Whether the two
 * bounds ADD in a close() is the question: close() sets {@code closed} first and then joins, and the thread reads {@code closed} between the read
 * and the cancel, so
 *
 * <ul>
 *   <li>a close() that lands while the read is stalled makes the thread skip the cancel: close() waits out the rest of the read, and the cancel
 *       session is never opened (the first test: the proxy sees one connection, ever);</li>
 *   <li>a close() that lands after the thread has passed that check waits only for the cancel, because the read has already finished (the second
 *       test: the read answered, then the server stalled for the cancel).</li>
 * </ul>
 *
 * So the wait is one session's bound, not two. Both tests measure close() on a real engine database behind a proxy that can go silent: it passes
 * bytes while it is open and, once frozen, accepts connections and drops everything either side says (a hung server, as in
 * {@link DataMoveOrphansHungServerTest}, but one the watch can first read the move's row through). The measured duration is in each assertion's
 * message and in the {@code event=completion_watch_close_measured} log line.
 */
class CompletionWatchCloseBoundIntegrationTest extends DataMoveIntegrationBase {

    private static final Logger log = LoggerFactory.getLogger(CompletionWatchCloseBoundIntegrationTest.class);

    /** What the watch thread is called (CompletionWatch's constructor); the tests find their own by the threads that were not there before. */
    private static final String WATCH_THREAD = "data-move-completion-watch";

    /**
     * One session's bound plus the time to open it and close it: the longest a close() may block. The sum the join was widened to cover (two
     * sessions, 10 s) must not be reached.
     */
    private static final long ONE_SESSION_BOUND_MS = DataMoveOrphans.CANCEL_SOCKET_SECONDS * 1_000L + 2_000L;

    /**
     * A TCP relay in front of the test's database that can go silent: while open it copies bytes both ways; once frozen it still accepts
     * connections and still reads what a client sends, and drops everything (never a byte to the server, never a byte back), so a client's
     * session stalls until its own network timeout, the way a hung server does. Never unfrozen: a frozen relay is closed with the test.
     */
    private static final class StallableProxy implements AutoCloseable {
        private final ServerSocket listener;
        private final String upstreamHost;
        private final int upstreamPort;
        private volatile boolean frozen;
        private final AtomicInteger accepted = new AtomicInteger();
        private final AtomicInteger acceptedFrozen = new AtomicInteger();
        private final List<Socket> sockets = new CopyOnWriteArrayList<>();

        StallableProxy(String upstreamHost, int upstreamPort, boolean frozenFromStart) throws IOException {
            this.upstreamHost = upstreamHost;
            this.upstreamPort = upstreamPort;
            this.frozen = frozenFromStart;
            this.listener = new ServerSocket(0, 10, InetAddress.getLoopbackAddress());
            daemon("proxy-acceptor", () -> {
                while (!listener.isClosed()) {
                    try {
                        Socket client = listener.accept();
                        sockets.add(client);
                        boolean hold = frozen;
                        accepted.incrementAndGet();
                        if (hold) {
                            acceptedFrozen.incrementAndGet();
                        }
                        serve(client, hold);
                    } catch (IOException e) {
                        return;
                    }
                }
            });
        }

        int port() {
            return listener.getLocalPort();
        }

        void freeze() {
            frozen = true;
        }

        /** Every connection the relay has accepted. */
        int accepted() {
            return accepted.get();
        }

        /** The connections accepted after the relay froze: the sessions that found the server silent from their first byte. */
        int acceptedFrozen() {
            return acceptedFrozen.get();
        }

        private void serve(Socket client, boolean hold) throws IOException {
            Socket upstream = null;
            if (!hold) {
                upstream = new Socket(upstreamHost, upstreamPort);
                sockets.add(upstream);
            }
            Socket server = upstream;
            daemon("proxy-up", () -> pump(client, server));
            if (server != null) {
                daemon("proxy-down", () -> pump(server, client));
            }
        }

        /** Copies {@code from} to {@code to} until either ends; a frozen relay (or a held connection, which has no {@code to}) reads and drops. */
        private void pump(Socket from, Socket to) {
            byte[] buffer = new byte[8192];
            try {
                InputStream in = from.getInputStream();
                OutputStream out = to == null ? null : to.getOutputStream();
                int n;
                while ((n = in.read(buffer)) >= 0) {
                    if (!frozen && out != null) {
                        out.write(buffer, 0, n);
                        out.flush();
                    }
                }
            } catch (IOException e) {
                // an end of the connection: this direction is over
            }
        }

        private static void daemon(String name, Runnable body) {
            Thread t = new Thread(body, name);
            t.setDaemon(true);
            t.start();
        }

        @Override
        public void close() throws IOException {
            listener.close();
            for (Socket s : sockets) {
                try {
                    s.close();
                } catch (IOException ignored) {
                    // closing the rest all the same
                }
            }
        }
    }

    private AdminConnection through(StallableProxy proxy) {
        String url = adminConnection.url().replaceFirst("(jdbc:postgresql://)[^/]+(/)", "$1127.0.0.1:" + proxy.port() + "$2");
        return new AdminConnection(url, adminConnection.user(), adminConnection.password());
    }

    private static void pause(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(e);
        }
    }

    private static Set<Thread> liveThreads() {
        return new HashSet<>(Thread.getAllStackTraces().keySet());
    }

    /** The watch threads started since {@code before}: the test's own, whatever else the JVM is running. */
    private static List<Thread> watchThreadsSince(Set<Thread> before) {
        return Thread.getAllStackTraces().keySet().stream().filter(t -> WATCH_THREAD.equals(t.getName()) && !before.contains(t)).toList();
    }

    private static boolean waitFor(java.util.function.BooleanSupplier condition, long timeoutMs) {
        long end = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs);
        while (System.nanoTime() < end) {
            if (condition.getAsBoolean()) {
                return true;
            }
            pause(20);
        }
        return condition.getAsBoolean();
    }

    /**
     * The read stalls to its bound (the server is silent from the first byte: the 5 s network timeout), and close() lands while it waits. The thread
     * must not go on to the cancel: the proxy sees the read's one connection and no other, before and after close() returns and for two watch
     * intervals after, so the bounds do not add. close() blocks for what is left of the read's bound, and the thread has ended when it returns.
     */
    @Test
    void aCloseDuringAStalledReadWaitsOutTheReadOnlyAndOpensNoCancelSession() throws Exception {
        try (var proxy = new StallableProxy(pg.getHost(), pg.getMappedPort(PostgreSQLContainer.POSTGRESQL_PORT), true)) {
            Set<Thread> before = liveThreads();
            var watch = CompletionWatch.start(through(proxy), "p33c-bound-read-" + System.nanoTime(), 1, "nexus-data-move-bound-read",
                java.time.OffsetDateTime.now());
            boolean reached = waitFor(() -> proxy.accepted() >= 1, 15_000);
            assertThat(reached).as("non-vacuity: the watch's read reached the silent server and waits on it").isTrue();
            List<Thread> threads = watchThreadsSince(before);
            assertThat(threads).as("the watch's thread is running").hasSize(1);

            long closing = System.nanoTime();
            watch.close();
            long closeMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - closing);
            log.info("event=completion_watch_close_measured scenario=read_stalled close_ms={}", closeMs);

            assertThat(closeMs).as("close() waited on a read stalled to its bound: measured %d ms (the one-session bound is %d ms)", closeMs, ONE_SESSION_BOUND_MS)
                .isBetween(1_000L, ONE_SESSION_BOUND_MS);
            assertThat(threads.get(0).isAlive()).as("the watch's thread was not alive when close() returned (it took %d ms)", closeMs).isFalse();
            assertThat(watch.ended()).as("and it had left its loop").isTrue();
            assertThat(watch.fired()).as("a read that never answered cancelled nothing").isFalse();
            int connectionsAtClose = proxy.accepted();
            pause(2 * CompletionWatch.INTERVAL_MS + 500);
            assertThat(proxy.accepted()).as("no session was opened after close() returned, and none for a cancel before it (measured close() %d ms)", closeMs)
                .isEqualTo(connectionsAtClose).isEqualTo(1);
        }
    }

    /**
     * The read answers (the move's row is absent, which the watch takes as held) and the server then goes silent, before the thread's check of
     * {@code closed}: the thread goes on to the cancel, whose session stalls to its bound. close() is called the moment the cancel's connection
     * reaches the silent server, so it waits for the cancel's whole bound, the longest wait the thread's design allows. It returns with the thread
     * ended, and no further session is opened after it returns.
     */
    @Test
    void aCloseDuringAStalledCancelAfterASuccessfulReadWaitsOutTheCancelOnly() throws Exception {
        try (var proxy = new StallableProxy(pg.getHost(), pg.getMappedPort(PostgreSQLContainer.POSTGRESQL_PORT), false)) {
            var read = new CountDownLatch(1);
            Set<Thread> before = liveThreads();
            var watch = CompletionWatch.start(through(proxy), "p33c-bound-cancel-" + System.nanoTime(), 1, "nexus-data-move-bound-cancel",
                java.time.OffsetDateTime.now(), () -> {
                    proxy.freeze();   // the read has its answer; from here the server says nothing to anyone
                    read.countDown();
                });
            assertThat(read.await(15, TimeUnit.SECONDS)).as("the watch read the move's row through the open relay").isTrue();
            boolean cancelling = waitFor(() -> proxy.acceptedFrozen() >= 1, 15_000);
            assertThat(cancelling).as("non-vacuity: the cancel's session reached the silent server and waits on it").isTrue();
            assertThat(watch.fired()).as("the thread passed its check of closed and went for the cancel").isTrue();
            List<Thread> threads = watchThreadsSince(before);
            assertThat(threads).as("the watch's thread is running").hasSize(1);

            long closing = System.nanoTime();
            watch.close();
            long closeMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - closing);
            log.info("event=completion_watch_close_measured scenario=cancel_stalled close_ms={}", closeMs);

            assertThat(closeMs).as("close() waited on a cancel stalled to its bound: measured %d ms (the one-session bound is %d ms)", closeMs, ONE_SESSION_BOUND_MS)
                .isBetween(1_000L, ONE_SESSION_BOUND_MS);
            assertThat(threads.get(0).isAlive()).as("the watch's thread was not alive when close() returned (it took %d ms)", closeMs).isFalse();
            assertThat(watch.ended()).as("and it had left its loop").isTrue();
            int connectionsAtClose = proxy.accepted();
            pause(2 * CompletionWatch.INTERVAL_MS + 500);
            assertThat(proxy.accepted()).as("no cancel statement or any other session was issued after close() returned (measured close() %d ms)", closeMs)
                .isEqualTo(connectionsAtClose).isEqualTo(2);
        }
    }
}
