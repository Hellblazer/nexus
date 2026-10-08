// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;

/**
 * Minimal {@link HttpExchange} for driving a handler directly in a test (no socket): serves a
 * fixed request body and captures the response status, headers and body. The shared form of the
 * capturing exchange several handler tests carry privately.
 */
final class CapturingExchange extends HttpExchange {
    private final String method;
    private final URI uri;
    private final InputStream requestBody;
    final Headers requestHeaders = new Headers();
    final Headers responseHeaders = new Headers();
    private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
    int status = -1;

    CapturingExchange(String method, URI uri, String body) {
        this.method = method;
        this.uri = uri;
        this.requestBody = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
    }

    byte[] bodyBytes() { return responseBody.toByteArray(); }

    String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

    @Override public Headers getRequestHeaders() { return requestHeaders; }
    @Override public Headers getResponseHeaders() { return responseHeaders; }
    @Override public URI getRequestURI() { return uri; }
    @Override public String getRequestMethod() { return method; }
    @Override public HttpContext getHttpContext() { return null; }
    @Override public void close() {}
    @Override public InputStream getRequestBody() { return requestBody; }
    @Override public OutputStream getResponseBody() { return responseBody; }
    @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
    @Override public InetSocketAddress getRemoteAddress() { return null; }
    @Override public int getResponseCode() { return status; }
    @Override public InetSocketAddress getLocalAddress() { return null; }
    @Override public String getProtocol() { return "HTTP/1.1"; }
    @Override public Object getAttribute(String name) { return null; }
    @Override public void setAttribute(String name, Object value) {}
    @Override public void setStreams(InputStream i, OutputStream o) {}
    @Override public HttpPrincipal getPrincipal() { return null; }
}
