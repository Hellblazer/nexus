// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;

/**
 * Reads and writes the {@code .nxtenant} backup file (the format of {@code src/nexus/backup/tenant_file.py}, which is
 * its authority) for the engine tests: the committed fixture is a real backup file, so {@code nx backup --check}
 * verifies it and a restore of it is what a user's restore is. The layout: {@code "NXTENANT"}, a u16 file format
 * version, then frames of a kind byte, a u64 payload length and the payload; {@code H} the header JSON, {@code T} a
 * table start, {@code P} a page (u32 meta length, meta JSON, the engine's binary COPY body), {@code E} the trailer.
 * Everything is big-endian. JSON is written with sorted keys and no spaces, as the client writes it.
 */
final class TenantBackupFiles {

    private static final byte[] MAGIC = "NXTENANT".getBytes(StandardCharsets.US_ASCII);
    private static final int FILE_FORMAT_VERSION = 1;
    private static final ObjectMapper JSON = new ObjectMapper()
        .configure(SerializationFeature.ORDER_MAP_ENTRIES_BY_KEYS, true)
        .configure(com.fasterxml.jackson.databind.MapperFeature.SORT_PROPERTIES_ALPHABETICALLY, true);

    private TenantBackupFiles() {
    }

    // ── writing ─────────────────────────────────────────────────────────────────────────────────

    /** The file's bytes for an export the test holds. The session fields are already gone; the open-only fields are dropped here. */
    static byte[] write(ImportTestEnv.BackupFile file) throws IOException {
        ObjectNode header = file.header.deepCopy();
        header.remove("header_sha256");
        header.remove("row_bytes");
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        MessageDigest fileDigest = sha256();
        emit(out, fileDigest, concat(MAGIC, ByteBuffer.allocate(2).putShort((short) FILE_FORMAT_VERSION).array()));
        emit(out, fileDigest, frame('H', json(header)));
        List<ObjectNode> tables = new ArrayList<>();
        long total = 0;
        for (String table : file.tableNames()) {
            List<byte[]> pages = file.pages.get(table);
            if (pages == null || pages.isEmpty()) throw new IllegalStateException("no pages for " + table);
            long rows = file.rows(table);
            emit(out, fileDigest, frame('T', json(JSON.createObjectNode().put("table", table).put("table_rows", rows))));
            MessageDigest tableDigest = sha256();
            for (int i = 0; i < pages.size(); i++) {
                byte[] body = pages.get(i);
                tableDigest.update(body);
                ObjectNode meta = JSON.createObjectNode().put("table", table).put("rows", BinaryCopyTestSupport.rowCount(body))
                    .put("done", i == pages.size() - 1).put("sha256", hex(sha256().digest(body)));
                meta.putNull("last_key");
                byte[] metaBytes = json(meta);
                emit(out, fileDigest, frame('P', concat(ByteBuffer.allocate(4).putInt(metaBytes.length).array(), metaBytes, body)));
            }
            tables.add(JSON.createObjectNode().put("name", table).put("rows", rows).put("pages", pages.size())
                .put("sha256", hex(tableDigest.digest())));
            total += rows;
        }
        ObjectNode trailer = JSON.createObjectNode().put("format_version", FILE_FORMAT_VERSION);
        trailer.putArray("tables").addAll(tables);
        trailer.put("total_rows", total).put("file_sha256", hex(fileDigest.digest()));
        out.write(frame('E', json(trailer)));
        return out.toByteArray();
    }

    // ── reading ─────────────────────────────────────────────────────────────────────────────────

    /** The export a file holds, sealed as a client seals one before it opens an import. Refuses a file that is not whole. */
    static ImportTestEnv.BackupFile read(byte[] bytes) throws IOException {
        ByteBuffer in = ByteBuffer.wrap(bytes);
        byte[] magic = new byte[MAGIC.length];
        in.get(magic);
        if (!java.util.Arrays.equals(magic, MAGIC)) throw new IllegalArgumentException("not a backup file");
        int version = in.getShort() & 0xffff;
        if (version != FILE_FORMAT_VERSION) throw new IllegalArgumentException("file format version " + version);
        ImportTestEnv.BackupFile file = null;
        while (in.hasRemaining()) {
            char kind = (char) in.get();
            long length = in.getLong();
            byte[] payload = new byte[Math.toIntExact(length)];
            in.get(payload);
            switch (kind) {
                case 'H' -> file = new ImportTestEnv.BackupFile((ObjectNode) JSON.readTree(payload));
                case 'T' -> { }
                case 'P' -> {
                    if (file == null) throw new IllegalArgumentException("a page before the header");
                    ByteBuffer p = ByteBuffer.wrap(payload);
                    byte[] meta = new byte[p.getInt()];
                    p.get(meta);
                    byte[] body = new byte[p.remaining()];
                    p.get(body);
                    String table = JSON.readTree(meta).get("table").asText();
                    file.pages.computeIfAbsent(table, k -> new ArrayList<>()).add(body);
                }
                case 'E' -> {
                    if (in.hasRemaining()) throw new IllegalArgumentException("bytes follow the trailer");
                    if (file == null) throw new IllegalArgumentException("a trailer with no header");
                    return file.seal();
                }
                default -> throw new IllegalArgumentException("frame kind " + kind);
            }
        }
        throw new IllegalArgumentException("the file ends before its trailer");
    }

    // ── framing ─────────────────────────────────────────────────────────────────────────────────

    private static byte[] frame(char kind, byte[] payload) {
        return concat(new byte[] {(byte) kind}, ByteBuffer.allocate(8).putLong(payload.length).array(), payload);
    }

    private static void emit(ByteArrayOutputStream out, MessageDigest digest, byte[] bytes) throws IOException {
        out.write(bytes);
        digest.update(bytes);
    }

    private static byte[] json(JsonNode node) throws IOException {
        return JSON.writeValueAsBytes(JSON.treeToValue(node, java.util.Map.class));
    }

    private static byte[] concat(byte[]... parts) {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        for (byte[] p : parts) o.writeBytes(p);
        return o.toByteArray();
    }

    private static MessageDigest sha256() {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String hex(byte[] bytes) {
        return HexFormat.of().formatHex(bytes);
    }
}
