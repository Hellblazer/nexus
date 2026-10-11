// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-632u4 — a handler that answers without reading the request body must leave the
 * connection usable, or tell the client it is closing.
 *
 * <p>The JDK server, when an exchange ends with request body unread, takes what has already
 * arrived and CLOSES the connection if the rest is not there yet, with no
 * {@code Connection: close} on the response it already sent. A keep-alive client then sends
 * its next request into a closed socket ({@code java.net.http.HttpClient}: "HTTP/1.1 header
 * parser received no bytes", and a POST is not retried). Routes that answer early are the
 * retired ones (410), an unknown path (404) and every refusal made before the body is parsed.
 * On a loaded host the body lags the headers often enough to hit it: ChashVectorConcurrencyTest
 * lost one request of 1,200 that way on the lab pool.
 *
 * <p>These tests hold the body back on a raw socket, so the lag is certain.
 */
class HttpUtilUnreadBodyTest {

    private HttpServer server;
    private int port;

    @BeforeEach
    void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        server.createContext("/gone", ex -> HttpUtil.send(ex, 410, "{\"error\":\"gone\"}"));
        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
        server.start();
        port = server.getAddress().getPort();
    }

    @AfterEach
    void stop() {
        server.stop(0);
    }

    @Test
    @Timeout(value = 30, unit = TimeUnit.SECONDS)
    void aBodyThatArrivesAfterTheHandlerAnsweredDoesNotCostTheConnection() throws Exception {
        byte[] body = "x".repeat(4096).getBytes(StandardCharsets.US_ASCII);
        try (Socket s = new Socket(InetAddress.getLoopbackAddress(), port)) {
            s.setSoTimeout(10_000);
            OutputStream out = s.getOutputStream();
            out.write(head(body.length));
            out.write(body, 0, 100);
            out.flush();
            Thread.sleep(300); // the handler has run by now; most of the body has not been sent
            out.write(body, 100, body.length - 100);
            out.flush();
            assertThat(readResponse(s.getInputStream())).startsWith("HTTP/1.1 410");

            out.write(head(body.length));
            out.write(body);
            out.flush();
            assertThat(readResponse(s.getInputStream()))
                    .as("the second request on the same connection is answered")
                    .startsWith("HTTP/1.1 410");
        }
    }

    @Test
    @Timeout(value = 30, unit = TimeUnit.SECONDS)
    void aBodyOverTheDrainCapIsAnsweredWithConnectionClose() throws Exception {
        long declared = HttpUtil.UNREAD_BODY_DRAIN_CAP_BYTES + 1024;
        try (Socket s = new Socket(InetAddress.getLoopbackAddress(), port)) {
            s.setSoTimeout(10_000);
            OutputStream out = s.getOutputStream();
            out.write(head(declared));
            byte[] chunk = new byte[64 * 1024];
            for (long sent = 0; sent < declared; sent += chunk.length) {
                out.write(chunk, 0, (int) Math.min(chunk.length, declared - sent));
            }
            out.flush();
            String response = readResponse(s.getInputStream());
            assertThat(response).startsWith("HTTP/1.1 410");
            assertThat(response.toLowerCase()).as("the server says it will not reuse the connection")
                    .contains("connection: close");
        }
    }

    private byte[] head(long contentLength) {
        return ("POST /gone HTTP/1.1\r\nHost: 127.0.0.1:" + port + "\r\nContent-Type: application/json\r\n"
                + "Content-Length: " + contentLength + "\r\n\r\n").getBytes(StandardCharsets.US_ASCII);
    }

    /** Reads one response: headers, then exactly Content-Length body bytes. Fails on EOF before that. */
    private static String readResponse(InputStream in) throws IOException {
        ByteArrayOutputStream head = new ByteArrayOutputStream();
        int b;
        while ((b = in.read()) != -1) {
            head.write(b);
            byte[] h = head.toByteArray();
            int n = h.length;
            if (n >= 4 && h[n - 4] == '\r' && h[n - 3] == '\n' && h[n - 2] == '\r' && h[n - 1] == '\n') break;
        }
        String headers = head.toString(StandardCharsets.US_ASCII);
        assertThat(headers).as("the connection was closed before a response arrived").endsWith("\r\n\r\n");
        int length = 0;
        for (String line : headers.split("\r\n")) {
            if (line.toLowerCase().startsWith("content-length:")) length = Integer.parseInt(line.substring(15).trim());
        }
        byte[] body = in.readNBytes(length);
        assertThat(body.length).isEqualTo(length);
        return headers + new String(body, StandardCharsets.UTF_8);
    }
}
