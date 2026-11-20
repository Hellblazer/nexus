// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * What a collection registration (or an ETL import row) must say about the collection's own attributes, decided
 * here so {@link CatalogRepository} (over its size ceiling) does not grow (RDR-229 P2.5, bead nexus-p2m1x.10).
 *
 * <p><strong>lifecycle_state.</strong> Registration takes an explicit {@code lifecycle_state} and defaults it to
 * {@code live}; it never reads the name. The import paths take the imported row's own state and fall back to the
 * {@code quarantine-} prefix only for an export that predates the column.
 *
 * <p><strong>model_version (D1, Sam 2026-11-20).</strong> A registration of a NEW row with a blank or absent
 * {@code model_version} is refused ({@link IllegalArgumentException}, a 400 on the route) and writes nothing. An
 * import takes the version the row carries, else the {@code vN} of a conformant name, else refuses. The
 * conformant-name read is the one place the engine parses a name for a version: it is a migration source (an
 * export from another install or an older era, RDR-229 item 1), and {@code CollectionParseGateTest} declares it.
 */
public final class CollectionRegistration {

    /** {@code <content_type>__<owner>__<embedding_model>__v<n>}, as the client's {@code _CONFORMANT_COLLECTION_RE}. */
    private static final Pattern CONFORMANT_VERSION =
        Pattern.compile("^[a-z][a-z0-9-]*__.+__[a-z][a-z0-9-]*__(v[0-9]+)$");

    private CollectionRegistration() {
    }

    /**
     * The {@code vN} suffix of a conformant collection name, or {@code null} when the name is not conformant. The
     * ONE reader of a version out of a name: a conformant name, migration source (RDR-229 item 1) and the reembed
     * target's minted name; retired at P4 with the name parsers. Callers: the ETL import fallback
     * ({@link #importModelVersion}) and {@code ReembedHandler.register}.
     */
    public static String versionOfConformantName(String name) {
        if (name == null) {
            return null;
        }
        Matcher m = CONFORMANT_VERSION.matcher(name);
        return m.matches() ? m.group(1) : null;
    }

    /** The row's {@code lifecycle_state}, or {@code null} when absent or blank; a value outside the vocabulary is refused. */
    static String explicitLifecycleState(Map<String, Object> coll) {
        String requested = CatalogRepository.s(coll, "lifecycle_state");
        if (requested == null || requested.isBlank()) {
            return null;
        }
        if (!CatalogRepository.LIFECYCLE_STATES.contains(requested)) {
            throw new IllegalArgumentException("lifecycle_state '" + requested + "' is not one of "
                + new java.util.TreeSet<>(CatalogRepository.LIFECYCLE_STATES) + " (collection '"
                + CatalogRepository.s(coll, "name") + "')");
        }
        return requested;
    }

    /** The import row's {@code lifecycle_state}: its own when it carries one, else the prefix rule (migration fallback). */
    static String lifecycleStateForImport(Map<String, Object> coll) {
        String explicit = explicitLifecycleState(coll);
        if (explicit != null) {
            return explicit;
        }
        String contentType = CatalogRepository.s(coll, "content_type");
        String name = CatalogRepository.s(coll, "name");
        boolean quarantine = (contentType != null && contentType.startsWith(QuarantineOrigin.PREFIX))
                          || (name != null && name.startsWith(QuarantineOrigin.PREFIX));
        return quarantine ? "quarantine" : "live";
    }

    /**
     * Refuses an import batch holding a NEW row whose explicit {@code lifecycle_state} and name disagree: a
     * {@code quarantine-} name must say {@code quarantine}, and any other name must not (review, linda 2026-11-20).
     * Loud and whole: the import's response is a bare count with no per-row report, so the batch is refused with one
     * {@link IllegalArgumentException} (a 400) that counts and names the rows, and nothing is stored, as the
     * content_type guard already refuses a batch. A row with no state keeps the prefix fallback and cannot disagree.
     */
    static void requireStatesAgreeWithNames(List<Map<String, Object>> newRows) {
        List<String> bad = new java.util.ArrayList<>();
        for (Map<String, Object> coll : newRows) {
            String state = explicitLifecycleState(coll);
            String name = CatalogRepository.s(coll, "name");
            if (state != null && (name != null && name.startsWith(QuarantineOrigin.PREFIX)) != "quarantine".equals(state)) {
                bad.add("'" + name + "' (lifecycle_state=" + state + ")");
            }
        }
        if (!bad.isEmpty()) {
            throw new IllegalArgumentException("refusing the import: " + bad.size() + " row(s) whose lifecycle_state "
                + "disagrees with the name (a quarantine- name must be 'quarantine', any other name must not be): "
                + String.join(", ", bad.size() > 10 ? bad.subList(0, 10) : bad) + (bad.size() > 10 ? ", ..." : ""));
        }
    }

    /**
     * The client route ({@code POST /v1/catalog/collections/upsert}) registers {@code live} collections only
     * (linda 2026-11-20, rule A): an explicit state other than {@code live} is refused. Engine-internal callers of
     * {@code upsertCollection} do not pass through here and may name any state.
     */
    static void requireClientState(String requestedState, String name) {
        if (requestedState != null && !"live".equals(requestedState)) {
            throw new IllegalArgumentException("lifecycle_state '" + requestedState + "' is not accepted when "
                + "registering collection '" + name + "': this route registers live collections only (send "
                + "'live' or no lifecycle_state); the engine creates quarantine rows itself");
        }
    }

    /**
     * Rule B (linda 2026-11-20): a NEW client registration may not start with the {@code quarantine-} prefix. The
     * engine mints that name (the reaper, the SQL movers); this guard stops a client minting one.
     */
    static void requireClientNameNotQuarantine(String name) {
        if (name != null && name.startsWith(QuarantineOrigin.PREFIX)) {
            throw new IllegalArgumentException("registering collection '" + name + "' is refused: the quarantine- "
                + "name prefix is the engine's own; a client cannot register a collection under it");
        }
    }

    /** The {@code model_version} a NEW registration sends; blank or absent is refused. */
    static String requiredModelVersion(Map<String, Object> coll) {
        String v = CatalogRepository.s(coll, "model_version");
        if (v == null || v.isBlank()) {
            throw new IllegalArgumentException("registering collection '" + CatalogRepository.s(coll, "name")
                + "' requires model_version; none was supplied");
        }
        return v;
    }

    /** The {@code model_version} an import row gets: its own, else its conformant name's {@code vN}, else refused. */
    static String importModelVersion(Map<String, Object> coll) {
        String v = CatalogRepository.s(coll, "model_version");
        if (v != null && !v.isBlank()) {
            return v;
        }
        String name = CatalogRepository.s(coll, "name");
        String fromName = versionOfConformantName(name);
        if (fromName == null) {
            throw new IllegalArgumentException("importing collection '" + name + "' requires model_version: the row "
                + "carries none and its name is not a conformant collection name (content type, owner, model and version "
                + "segments) to read one from");
        }
        return fromName;
    }
}
