// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.AdminConnection;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.io.IOException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;

/**
 * The boot reaper and the cancel run on sessions with short network timeouts, so a server that accepts a connection and then says nothing costs them
 * their own bound (10 s and 5 s) and no more: the reaper's failure is logged and the boot goes on (the reaper is never fatal), and a cancel that
 * cannot be sent is logged and the caller goes on. No database: the server is a socket on a dynamic port that accepts and stays silent. Before the
 * bounds, both sessions took the admin opener's half hour network timeout (the driver's own login timeout, 30 s, was all that stopped them).
 */
@Timeout(value = 60, unit = TimeUnit.SECONDS)
class DataMoveOrphansHungServerTest {

    private ServerSocket server;
    private final List<Socket> accepted = new CopyOnWriteArrayList<>();

    @BeforeEach
    void listen() throws IOException {
        server = new ServerSocket(0, 10, InetAddress.getLoopbackAddress());
        Thread acceptor = new Thread(() -> {
            while (!server.isClosed()) {
                try {
                    accepted.add(server.accept());   // held open and never read or written: a hung server
                } catch (IOException e) {
                    return;
                }
            }
        }, "hung-server-acceptor");
        acceptor.setDaemon(true);
        acceptor.start();
    }

    @AfterEach
    void close() throws IOException {
        server.close();
        for (Socket s : accepted) {
            s.close();
        }
    }

    private AdminConnection hungAdmin() {
        return new AdminConnection("jdbc:postgresql://127.0.0.1:" + server.getLocalPort() + "/postgres", "nobody", "nothing");
    }

    @Test
    void theBootReaperGivesUpOnAHungServerWithinItsBoundAndNeverThrows() {
        long start = System.nanoTime();

        int ended = DataMoveOrphans.terminateLeftover(hungAdmin());

        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
        assertThat(ended).as("the reaper reports that it could not reach the database, and the boot goes on").isEqualTo(-1);
        assertThat(elapsedMs).as("its network timeout is %d s; the admin opener's was half an hour behind a 30 s login timeout",
            DataMoveOrphans.REAPER_SOCKET_SECONDS).isBetween(1_000L, 20_000L);
        assertThat(accepted).as("non-vacuity: the reaper did reach the server and wait on it").isNotEmpty();
    }

    @Test
    void aCancelToAHungServerEndsWithinItsBoundAndNeverThrows() {
        long start = System.nanoTime();

        DataMoveOrphans.cancelBackend(hungAdmin(), 1, "nexus-data-move-x", OffsetDateTime.now());

        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
        assertThat(elapsedMs).as("its network timeout is %d s", DataMoveOrphans.CANCEL_SOCKET_SECONDS).isBetween(1_000L, 15_000L);
        assertThat(accepted).as("non-vacuity: the cancel did reach the server and wait on it").isNotEmpty();
    }
}
