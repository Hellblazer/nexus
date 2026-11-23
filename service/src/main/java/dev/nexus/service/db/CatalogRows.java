// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.type.TypeReference;

import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.Map;

import static dev.nexus.service.db.CatalogRepository.MAPPER;
import static dev.nexus.service.db.CatalogRepository.utcIso;

/**
 * The wire rows of {@link CatalogRepository}'s owner, link and collection reads: one {@code Map} per row, keys and null rules
 * as the routes answer them. Moved out of {@code CatalogRepository} unchanged (nexus-p2m1x.23, to keep that file under its
 * size ceiling); it has no state and no database access.
 */
final class CatalogRows {

    private CatalogRows() {}

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private static String nne(String v) { return v != null ? v : ""; }

    /**
     * Owner row PLUS {@code next_seq} (nexus-0ehwe item 3).
     *
     * <p>next_seq was exposed on NO read path: not {@code ownerByPrefix},
     * not {@code /owners/show}, not {@code /owners/list}. The only way to tell
     * a drifted owner from a healthy one was to attempt a real registration and
     * see whether it 409'd — a MUTATION used as a diagnostic. That is why the
     * original wedge (nexus-pbawi) took a long investigation to localize, and
     * why "how many other owners are already wedged?" was unanswerable.
     *
     * <p>An overload rather than a signature change: {@code ownerRow} has seven
     * callers and most have no next_seq in scope.
     */
    static Map<String, Object> ownerRow(String prefix, String name, String type,
                                                  String repo, String desc, String root, String head,
                                                  Long nextSeq) {
        Map<String, Object> m = ownerRow(prefix, name, type, repo, desc, root, head);
        m.put("next_seq", nextSeq == null ? 0L : nextSeq);
        return m;
    }

    /**
     * nexus-cw262: {@code listOwners(tenant, includeDeactivated)} overload — carries
     * BOTH next_seq and deactivated_at. An overload rather than a signature change to
     * the 8-arg (nextSeq-only) form above for the same reason that one is itself an
     * overload of the 7-arg base: {@code ownerRow} has many callers and most have
     * neither next_seq nor deactivated_at in scope.
     */
    static Map<String, Object> ownerRow(String prefix, String name, String type,
                                                  String repo, String desc, String root, String head,
                                                  Long nextSeq, OffsetDateTime deactivatedAt) {
        Map<String, Object> m = ownerRow(prefix, name, type, repo, desc, root, head, nextSeq);
        m.put("deactivated_at", deactivatedAt);
        return m;
    }

    /**
     * nexus-cw262: {@code ownersByType(tenant, type, includeDeactivated)} overload —
     * deactivated_at without next_seq (the {@code /owners/by_type} route never
     * selected next_seq; not changing that here).
     */
    static Map<String, Object> ownerRow(String prefix, String name, String type,
                                                  String repo, String desc, String root, String head,
                                                  OffsetDateTime deactivatedAt) {
        Map<String, Object> m = ownerRow(prefix, name, type, repo, desc, root, head);
        m.put("deactivated_at", deactivatedAt);
        return m;
    }

    static Map<String, Object> ownerRow(String prefix, String name, String type,
                                                  String repo, String desc, String root, String head) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("tumbler_prefix", prefix);
        m.put("name",           name);
        m.put("owner_type",     type);
        m.put("repo_hash",      repo);
        m.put("description",    desc);
        m.put("repo_root",      nne(root));
        m.put("head_hash",      head);
        return m;
    }

    static Map<String, Object> linkRow(Long id, String from, String to, String type,
                                                 String fromSpan, String toSpan,
                                                 String createdBy, java.time.OffsetDateTime createdAt, Object meta) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id",           id);
        m.put("from_tumbler", from);
        m.put("to_tumbler",   to);
        m.put("link_type",    type);
        m.put("from_span",    fromSpan);
        m.put("to_span",      toSpan);
        m.put("created_by",   createdBy);
        // nexus-cefa1.2: created_at is timestamptz now (catalog-031-2-links-created-at);
        // stays nullable on the wire exactly as the pre-migration nullable TEXT column
        // did — no nne() forcing to "" here, unlike collRow's NOT-NULL-DEFAULT-'' columns.
        m.put("created_at",   utcIso(createdAt));
        if (meta != null) {
            try {
                m.put("metadata", MAPPER.readValue(meta.toString(), MAP_TYPE));
            } catch (Exception e) { m.put("metadata", null); }
        } else { m.put("metadata", null); }
        return m;
    }

    static Map<String, Object> collRow(String name, String ctype, String owner,
                                                 String embd, String mver, String dname,
                                                 Boolean legcy, String supBy, String supAt, String crAt) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name",                 name);
        m.put("content_type",         nne(ctype));
        m.put("owner_id",             nne(owner));
        m.put("embedding_model",      nne(embd));
        m.put("model_version",        nne(mver));
        m.put("display_name",         nne(dname));
        // nexus-cefa1.2: legacy_grandfathered is boolean now (catalog-031-3-collections-
        // legacy-bool) — a real JSON boolean on the wire; Python's _coerce_legacy_
        // grandfathered already normalizes via bool(x), which accepts either shape, so
        // this is a compatible wire-format tightening, not a break.
        m.put("legacy_grandfathered", legcy != null ? legcy : Boolean.FALSE);
        m.put("superseded_by",        nne(supBy));
        m.put("superseded_at",        nne(supAt));
        m.put("created_at",           nne(crAt));
        return m;
    }

    /**
     * {@link #collRow} plus {@code dimension} and {@code lifecycle_state} (RDR-204
     * Phase 2, bead nexus-ft04v.24) plus {@code aspects_enabled} (RDR bead
     * nexus-l46pu) — used ONLY by {@link #listCollections(String, String,
     * String)}. {@link #collectionForTuple} is out of both beads' scope; it
     * appends only {@code lifecycle_state} to {@link #collRow}'s 10 keys (RDR-229 P2.5); every key {@link #collRow}
     * already sets is untouched here, this only appends three keys at the end.
     * {@link #getCollection} carries {@code aspects_enabled} too, via the
     * sibling helper {@link #collRowWithAspects}, but not {@code dimension}/
     * {@code lifecycle_state}.
     *
     * @param dimension      {@code catalog_collections.dimension} — nullable (a
     *                       collection can be registered before its stats dimension
     *                       is known; see hygiene-002's own walk comments), passed
     *                       through as-is (JSON {@code null}, not coerced to 0)
     * @param lifecycleState {@code catalog_collections.lifecycle_state} — NOT NULL
     *                       on any real row since hygiene-002, but {@code nne()}'d
     *                       for defensive consistency with every other string field here
     * @param collectionId   {@code catalog_collections.collection_id} (RDR-229 P3.5a,
     *                       nexus-p2m1x.23), read-only: always a number on an engine that has
     *                       the column (the identity is NOT NULL, vectors-068-1); a NULL would
     *                       pass through as an explicit {@code null}, defensively, never omitted.
     *                       Not stable across a rename until P3.7's name-key drop and R6's
     *                       {@code ids_stable} signal
     * @param aspectsEnabled {@code catalog_collections.aspects_enabled} — {@code BOOLEAN
     *                       NULL}, no default, since catalog-040's round-2 fix (Finding A,
     *                       T2 critique-nexus-l46pu-round2-2026-09-27): {@code null} means
     *                       "nobody has set this yet" and is passed through AS {@code null}
     *                       on the wire, never coerced to {@code false} — coercing it would
     *                       make an untouched row indistinguishable from an explicit
     *                       {@code false}, which is exactly the ship-blocker this column
     *                       shape exists to avoid. The client's opinion test keys on the
     *                       value being a bool, not on key presence.
     */
    static Map<String, Object> collRowWithLifecycle(String name, String ctype, String owner,
                                                 String embd, String mver, String dname,
                                                 Boolean legcy, String supBy, String supAt, String crAt,
                                                 Integer dimension, String lifecycleState,
                                                 Boolean aspectsEnabled, Long collectionId) {
        Map<String, Object> m = collRow(name, ctype, owner, embd, mver, dname, legcy, supBy, supAt, crAt);
        m.put("dimension",       dimension);
        m.put("lifecycle_state", nne(lifecycleState));
        m.put("aspects_enabled", aspectsEnabled);
        m.put("collection_id",   collectionId);
        return m;
    }

    /**
     * {@link #collRow} plus {@code aspects_enabled} (RDR bead nexus-l46pu,
     * follow-up to nexus-kk4ut: the tenant-wide docs__ aspect-extraction
     * opt-in) — used by {@link #getCollection}. Kept as its own helper rather
     * than folded into {@link #collRowWithLifecycle} because {@code
     * getCollection} deliberately does NOT carry {@code dimension}/{@code
     * lifecycle_state} (those stay {@link #listCollections(String, String,
     * String)}-only, per that method's own javadoc) — {@code aspects_enabled}
     * is the one field both single-row and list reads now share.
     *
     * <p>{@code aspectsEnabled} is passed through AS-IS, {@code null} included
     * (see {@link #collRowWithLifecycle}'s javadoc for why: coercing a
     * {@code null} read to {@code false} was the round-2 ship-blocker, Finding
     * A, T2 critique-nexus-l46pu-round2-2026-09-27). {@code collectionId} (RDR-229
     * P3.5a) is always a number on an engine that has the column (the identity is
     * NOT NULL, vectors-068-1); see {@link #collRowWithLifecycle}.
     */
    static Map<String, Object> collRowWithAspects(String name, String ctype, String owner,
                                                 String embd, String mver, String dname,
                                                 Boolean legcy, String supBy, String supAt, String crAt,
                                                 Boolean aspectsEnabled, Long collectionId) {
        Map<String, Object> m = collRow(name, ctype, owner, embd, mver, dname, legcy, supBy, supAt, crAt);
        m.put("aspects_enabled", aspectsEnabled);
        m.put("collection_id",   collectionId);
        return m;
    }
}
