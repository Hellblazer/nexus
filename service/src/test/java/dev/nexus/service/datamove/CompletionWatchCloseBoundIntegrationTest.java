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
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * What {@link CompletionWatch#close} waits for, measured (nexus-p2m1x.21). The watch's thread opens a session to read the move's row and, when the
 * row says the move is held and {@code closed} is still false after the read, a second session to cancel. Each session is bounded alone: the network
 * timeout {@link DataMoveOrphans#CANCEL_SOCKET_SECONDS} (5 s) cuts a server that stalls, and a lock is given up after 2 s. Two things decide how long
 * close() blocks.
 *
 * <p><b>The bounds do not add.</b> close() sets {@code closed} first, then interrupts and joins, and the thread reads {@code closed} between the
 * read and the cancel. A close() that lands during the read makes the thread skip the cancel; one that lands after that check waits only for the
 * cancel, because the read has finished. The wait is one session's bound, never two.
 *
 * <p><b>The interrupt cuts a stalled connect and not a stalled statement</b> (pgjdbc 42.7.4, JDK 25, a silent server). A session that cannot get its
 * startup answer is opened by the driver on a helper thread that the caller waits on (the login timeout), and an interrupt of the caller ends that
 * wait at once ("Interrupted while attempting to connect"): close() returns in about a millisecond. A statement that has been sent and not answered
 * is a read on the watch thread's own socket (the thread is RUNNABLE in {@code NioSocketImpl.timedRead}), and an interrupt does nothing to it:
 * close() waits for the socket timeout. So the join bounds the stalled STATEMENT, and four cases are measured: the read's and the cancel's session,
 * each stalled at its connect and at its statement.
 *
 * <p>All four run on a real engine database behind a relay that goes silent: while open it copies bytes both ways, and once frozen it accepts
 * connections, reads what a client sends and drops everything (a hung server, as in {@link DataMoveOrphansHungServerTest}, but one the watch can
 * first read the move's row through). A relay set to freeze on a client message that names something freezes at the statement that names it and
 * drops that statement, so the server never sees it. Before each close() the test waits until the watch's thread is blocked (parked on the driver's
 * connect, or in a socket read), so the measurement starts with the stall in place. The measured duration is in each assertion's message and in the
 * {@code event=completion_watch_close_measured} log line.
 */
class CompletionWatchCloseBoundIntegrationTest extends DataMoveIntegrationBase {

    private static final Logger log = LoggerFactory.getLogger(CompletionWatchCloseBoundIntegrationTest.class);

    /** What the watch thread is called (CompletionWatch's constructor); the tests find their own by the threads that were not there before. */
    private static final String WATCH_THREAD = "data-move-completion-watch";

    /** One session's bound plus the time to open and close it: the longest a close() may block. The 10 s of two sessions must not be reached. */
    private static final long ONE_SESSION_BOUND_MS = DataMoveOrphans.CANCEL_SOCKET_SECONDS * 1_000L + 2_000L;

    /** A close() that an interrupt cuts short returns well inside this. */
    private static final long PROMPT_MS = 1_500L;

    /** The statement that reads the move's row names this table; the cancel's statement names this function. */
    private static final String READ_STATEMENT_NAMES = "data_moves";
    private static final String CANCEL_STATEMENT_NAMES = "pg_cancel_backend";

    /**
     * A TCP relay in front of the test's database that can go silent. While open it copies bytes both ways. Frozen, it still accepts connections and
     * still reads what a client sends, and drops everything (never a byte to the server, never a byte back), so a client stalls until its own network
     * timeout, as with a hung server. It freezes when told to, or when a client message holds the bytes of {@code freezeOn}: that message is dropped.
     * Never unfrozen; a frozen relay is closed with the test.
     */
    private static final class StallableProxy implements AutoCloseable {
        private final ServerSocket listener;
        private final String upstreamHost;
        private final int upstreamPort;
        private volatile boolean frozen;
        private volatile byte[] freezeOn;
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

        /** Freezes, and drops the message, when a client message holds {@code text}. */
        void freezeOnClientMessageNaming(String text) {
            freezeOn = text.getBytes(StandardCharsets.UTF_8);
        }

        boolean isFrozen() {
            return frozen;
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
            daemon("proxy-up", () -> pump(client, server, true));
            if (server != null) {
                daemon("proxy-down", () -> pump(server, client, false));
            }
        }

        /** Copies {@code from} to {@code to} until either ends; a frozen relay (or a held connection, which has no {@code to}) reads and drops. */
        private void pump(Socket from, Socket to, boolean fromClient) {
            byte[] buffer = new byte[8192];
            try {
                InputStream in = from.getInputStream();
                OutputStream out = to == null ? null : to.getOutputStream();
                int n;
                while ((n = in.read(buffer)) >= 0) {
                    byte[] trigger = freezeOn;
                    if (fromClient && trigger != null && !frozen && contains(buffer, n, trigger)) {
                        frozen = true;
                    }
                    if (!frozen && out != null) {
                        out.write(buffer, 0, n);
                        out.flush();
                    }
                }
            } catch (IOException e) {
                // an end of the connection: this direction is over
            }
        }

        private static boolean contains(byte[] haystack, int length, byte[] needle) {
            outer:
            for (int i = 0; i + needle.length <= length; i++) {
                for (int j = 0; j < needle.length; j++) {
                    if (haystack[i + j] != needle[j]) {
                        continue outer;
                    }
                }
                return true;
            }
            return false;
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

    private StallableProxy relay(boolean frozenFromStart) throws IOException {
        return new StallableProxy(pg.getHost(), pg.getMappedPort(PostgreSQLContainer.POSTGRESQL_PORT), frozenFromStart);
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
            pause(10);
        }
        return condition.getAsBoolean();
    }

    /** Parked: the driver's caller waiting on the helper thread that opens the connection (a stalled connect). */
    private static boolean parked(Thread t) {
        Thread.State s = t.getState();
        return s == Thread.State.WAITING || s == Thread.State.TIMED_WAITING;
    }

    /** In a read on its own socket: RUNNABLE with a socket implementation frame on its stack (a statement sent and not answered). */
    private static boolean inSocketRead(Thread t) {
        return t.getState() == Thread.State.RUNNABLE
            && Arrays.stream(t.getStackTrace()).anyMatch(f -> f.getClassName().contains("SocketImpl") || f.getClassName().contains("SocketDispatcher"));
    }

    private static String where(Thread t) {
        StackTraceElement[] stack = t.getStackTrace();
        return t.getState() + (stack.length == 0 ? "" : " at " + stack[0]);
    }

    /** Waits for the watch thread this test started to exist (it is created by {@code start}, but the stack snapshot can lag it), and returns it. */
    private static Thread awaitThread(Set<Thread> before) {
        boolean seen = waitFor(() -> !watchThreadsSince(before).isEmpty(), 5_000);
        assertThat(seen).as("the watch's thread started").isTrue();
        List<Thread> threads = watchThreadsSince(before);
        assertThat(threads).as("the test's watch thread is running").hasSize(1);
        return threads.get(0);
    }

    /** The thread is not alive when close() has returned, and the relay accepts no connection after it: no cancel statement follows a close. */
    private static void assertEndedWithNoFurtherSession(CompletionWatch watch, Thread thread, StallableProxy proxy, int sessionsOpened, long closeMs) {
        assertThat(thread.isAlive()).as("the watch's thread was not alive when close() returned (it took %d ms)", closeMs).isFalse();
        assertThat(watch.ended()).as("and it had left its loop").isTrue();
        int connectionsAtClose = proxy.accepted();
        pause(2 * CompletionWatch.INTERVAL_MS + 500);
        assertThat(proxy.accepted()).as("no session was opened after close() returned (measured close() %d ms)", closeMs)
            .isEqualTo(connectionsAtClose).isEqualTo(sessionsOpened);
    }

    /**
     * The read's connect stalls (the server is silent from the first byte), and close() lands while the thread is parked on the driver's connect. The
     * interrupt ends that wait at once: close() returns promptly, the thread has ended, and the proxy saw the read's one connection and no other,
     * before and after close() returns, so no cancel session was opened.
     */
    @Test
    void aCloseDuringAStalledReadConnectIsCutShortByTheInterruptAndOpensNoCancelSession() throws Exception {
        try (var proxy = relay(true)) {
            Set<Thread> before = liveThreads();
            var watch = CompletionWatch.start(through(proxy), "p33c-bound-readconn-" + System.nanoTime(), 1, "nexus-data-move-bound-readconn",
                java.time.OffsetDateTime.now());
            Thread thread = awaitThread(before);
            boolean reached = waitFor(() -> proxy.accepted() >= 1, 15_000);
            boolean blocked = waitFor(() -> parked(thread), 5_000);
            assertThat(reached).as("non-vacuity: the watch's read reached the silent server").isTrue();
            assertThat(blocked).as("non-vacuity: the watch's thread waits on the connect (%s)", where(thread)).isTrue();
            String stalledAt = where(thread);

            long closing = System.nanoTime();
            watch.close();
            long closeMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - closing);
            log.info("event=completion_watch_close_measured scenario=read_connect_stalled close_ms={} thread_was=\"{}\"", closeMs, stalledAt);

            assertThat(closeMs).as("the interrupt cut the stalled connect: measured %d ms with the thread at %s", closeMs, stalledAt).isLessThan(PROMPT_MS);
            assertEndedWithNoFurtherSession(watch, thread, proxy, 1, closeMs);
            assertThat(watch.fired()).as("a read that never answered cancelled nothing").isFalse();
        }
    }

    /**
     * The read's STATEMENT stalls: the session opens, the statement that reads the move's row is sent and the server goes silent, so the thread is
     * in a read on its own socket, which an interrupt does not reach. close() waits out the socket timeout (5 s, less the moment since the stall
     * began), the thread skips the cancel because {@code closed} is set, and has ended when close() returns. This is the longest close() blocks on a
     * read; the cancel session is never opened (the proxy saw one connection).
     */
    @Test
    void aCloseDuringAStalledReadStatementWaitsOutTheSocketTimeoutAndOpensNoCancelSession() throws Exception {
        try (var proxy = relay(false)) {
            proxy.freezeOnClientMessageNaming(READ_STATEMENT_NAMES);
            Set<Thread> before = liveThreads();
            var watch = CompletionWatch.start(through(proxy), "p33c-bound-readstmt-" + System.nanoTime(), 1, "nexus-data-move-bound-readstmt",
                java.time.OffsetDateTime.now());
            Thread thread = awaitThread(before);
            boolean dropped = waitFor(proxy::isFrozen, 15_000);
            boolean blocked = waitFor(() -> inSocketRead(thread), 5_000);
            assertThat(dropped).as("non-vacuity: the relay froze at the statement that reads the move's row").isTrue();
            assertThat(blocked).as("non-vacuity: the watch's thread is in a socket read (%s)", where(thread)).isTrue();
            String stalledAt = where(thread);

            long closing = System.nanoTime();
            watch.close();
            long closeMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - closing);
            log.info("event=completion_watch_close_measured scenario=read_statement_stalled close_ms={} thread_was=\"{}\"", closeMs, stalledAt);

            assertThat(closeMs).as("close() waited out a read statement stalled to its socket timeout: measured %d ms with the thread at %s "
                    + "(the one-session bound is %d ms)", closeMs, stalledAt, ONE_SESSION_BOUND_MS)
                .isBetween(3_000L, ONE_SESSION_BOUND_MS);
            assertEndedWithNoFurtherSession(watch, thread, proxy, 1, closeMs);
            assertThat(watch.fired()).as("a read that never answered cancelled nothing").isFalse();
        }
    }

    /**
     * The read answers (the move's row is absent, which the watch takes as held) and the server then goes silent, before the thread's check of
     * {@code closed}: the thread goes on to the cancel, and its session cannot get a startup answer. The interrupt cuts that connect at once:
     * close() returns promptly with the thread ended and no further session opened.
     */
    @Test
    void aCloseDuringAStalledCancelConnectAfterASuccessfulReadIsCutShortByTheInterrupt() throws Exception {
        try (var proxy = relay(false)) {
            var read = new CountDownLatch(1);
            Set<Thread> before = liveThreads();
            var watch = CompletionWatch.start(through(proxy), "p33c-bound-cancelconn-" + System.nanoTime(), 1, "nexus-data-move-bound-cancelconn",
                java.time.OffsetDateTime.now(), () -> {
                    proxy.freeze();   // the read has its answer; from here the server says nothing to anyone
                    read.countDown();
                });
            Thread thread = awaitThread(before);
            assertThat(read.await(15, TimeUnit.SECONDS)).as("the watch read the move's row through the open relay").isTrue();
            boolean reached = waitFor(() -> proxy.acceptedFrozen() >= 1, 15_000);
            boolean blocked = waitFor(() -> parked(thread), 5_000);
            assertThat(reached).as("non-vacuity: the cancel's session reached the silent server").isTrue();
            assertThat(blocked).as("non-vacuity: the watch's thread waits on the cancel's connect (%s)", where(thread)).isTrue();
            assertThat(watch.fired()).as("the thread passed its check of closed and went for the cancel").isTrue();
            String stalledAt = where(thread);

            long closing = System.nanoTime();
            watch.close();
            long closeMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - closing);
            log.info("event=completion_watch_close_measured scenario=cancel_connect_stalled close_ms={} thread_was=\"{}\"", closeMs, stalledAt);

            assertThat(closeMs).as("the interrupt cut the stalled connect: measured %d ms with the thread at %s", closeMs, stalledAt).isLessThan(PROMPT_MS);
            assertEndedWithNoFurtherSession(watch, thread, proxy, 2, closeMs);
        }
    }

    /**
     * The read answers, the cancel's session opens, and the server goes silent at the cancel's STATEMENT (the relay drops the message that names
     * {@code pg_cancel_backend}, so the server never sees a cancel): the thread is in a read on its own socket and the interrupt does not reach it.
     * close() is called the moment the thread blocks, so it waits for the cancel's whole socket timeout, the longest wait the thread's design allows.
     * The thread has ended when it returns, and no further session is opened.
     */
    @Test
    void aCloseDuringAStalledCancelStatementAfterASuccessfulReadWaitsOutTheSocketTimeout() throws Exception {
        try (var proxy = relay(false)) {
            proxy.freezeOnClientMessageNaming(CANCEL_STATEMENT_NAMES);
            Set<Thread> before = liveThreads();
            var watch = CompletionWatch.start(through(proxy), "p33c-bound-cancelstmt-" + System.nanoTime(), 1, "nexus-data-move-bound-cancelstmt",
                java.time.OffsetDateTime.now());
            Thread thread = awaitThread(before);
            boolean dropped = waitFor(proxy::isFrozen, 15_000);
            boolean blocked = waitFor(() -> inSocketRead(thread), 5_000);
            assertThat(dropped).as("non-vacuity: the relay froze at the cancel's statement, so the read before it was answered").isTrue();
            assertThat(blocked).as("non-vacuity: the watch's thread is in a socket read (%s)", where(thread)).isTrue();
            assertThat(watch.fired()).as("the thread passed its check of closed and went for the cancel").isTrue();
            assertThat(proxy.accepted()).as("the read's session and the cancel's session were opened").isEqualTo(2);
            String stalledAt = where(thread);

            long closing = System.nanoTime();
            watch.close();
            long closeMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - closing);
            log.info("event=completion_watch_close_measured scenario=cancel_statement_stalled close_ms={} thread_was=\"{}\"", closeMs, stalledAt);

            assertThat(closeMs).as("close() waited out a cancel statement stalled to its socket timeout: measured %d ms with the thread at %s "
                    + "(the one-session bound is %d ms)", closeMs, stalledAt, ONE_SESSION_BOUND_MS)
                .isBetween(3_000L, ONE_SESSION_BOUND_MS);
            assertEndedWithNoFurtherSession(watch, thread, proxy, 2, closeMs);
        }
    }
}
