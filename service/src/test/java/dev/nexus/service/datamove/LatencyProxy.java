// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.ArrayList;
import java.util.List;

/**
 * A TCP relay that holds every chunk of bytes for {@code oneWayMs} before forwarding it, in both directions: a client that
 * connects to it sees a database {@code 2 x oneWayMs} away (a laptop on an SSM forward to a remote database). No clock is
 * faked and no statement is changed: only the network is slow, so a test of a time budget tells apart server time from
 * round-trip time.
 */
final class LatencyProxy implements AutoCloseable {

    private final ServerSocket listener;
    private final String targetHost;
    private final int targetPort;
    private final long oneWayMs;
    private final List<Socket> open = new ArrayList<>();
    private volatile boolean closed;

    LatencyProxy(String targetHost, int targetPort, long oneWayMs) throws IOException {
        this.targetHost = targetHost;
        this.targetPort = targetPort;
        this.oneWayMs = oneWayMs;
        this.listener = new ServerSocket(0, 50, InetAddress.getLoopbackAddress());
        Thread.ofPlatform().daemon().name("latency-proxy-accept").start(this::acceptLoop);
    }

    int port() {
        return listener.getLocalPort();
    }

    private void acceptLoop() {
        while (!closed) {
            try {
                Socket client = listener.accept();
                Socket server = new Socket(targetHost, targetPort);
                client.setTcpNoDelay(true);
                server.setTcpNoDelay(true);
                synchronized (open) {
                    open.add(client);
                    open.add(server);
                }
                pump(client, server);
                pump(server, client);
            } catch (IOException e) {
                if (!closed) {
                    throw new IllegalStateException("the latency proxy could not relay a connection", e);
                }
            }
        }
    }

    /** One direction: a reader stamps each chunk with its release time, a writer waits for that time and forwards it. */
    private void pump(Socket from, Socket to) {
        var queue = new java.util.concurrent.LinkedBlockingQueue<Object[]>();
        Thread.ofPlatform().daemon().name("latency-proxy-read").start(() -> {
            byte[] buf = new byte[16 * 1024];
            try (InputStream in = from.getInputStream()) {
                int n;
                while ((n = in.read(buf)) >= 0) {
                    queue.put(new Object[] {System.nanoTime() + oneWayMs * 1_000_000L, java.util.Arrays.copyOf(buf, n)});
                }
            } catch (IOException | InterruptedException ignored) {
                // the connection ended
            } finally {
                queue.add(new Object[] {0L, null});
            }
        });
        Thread.ofPlatform().daemon().name("latency-proxy-write").start(() -> {
            try (OutputStream out = to.getOutputStream()) {
                while (true) {
                    Object[] item = queue.take();
                    if (item[1] == null) {
                        return;
                    }
                    long waitNs = (Long) item[0] - System.nanoTime();
                    if (waitNs > 0) {
                        Thread.sleep(waitNs / 1_000_000L, (int) (waitNs % 1_000_000L));
                    }
                    out.write((byte[]) item[1]);
                    out.flush();
                }
            } catch (IOException | InterruptedException ignored) {
                // the connection ended
            } finally {
                try {
                    to.close();
                    from.close();
                } catch (IOException ignored) {
                    // already closed
                }
            }
        });
    }

    @Override
    public void close() throws IOException {
        closed = true;
        listener.close();
        synchronized (open) {
            for (Socket s : open) {
                try {
                    s.close();
                } catch (IOException ignored) {
                    // already closed
                }
            }
        }
    }
}
