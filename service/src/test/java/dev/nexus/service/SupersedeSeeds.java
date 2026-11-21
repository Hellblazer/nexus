// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;

import java.util.HashMap;
import java.util.Map;

/**
 * RDR-229 P3.1 stage 2b (nexus-p2m1x.17), residual R7: {@code supersedeCollection} resolves the successor's registry id
 * in its own statement and refuses a successor with no registry row ({@code UnregisteredCollectionException}). A test
 * that retires a name to a successor it never registered registers the successor first, through the repository's own
 * registration path, with the retired collection's content type, owner and model (the tenant's profile model when the
 * profile names another).
 */
public final class SupersedeSeeds {

    private SupersedeSeeds() {}

    /**
     * {@code repo.supersedeCollection(tenant, name, successor, at)}, after registering {@code successor} if it has no
     * row. The retired collection's own row supplies the attributes; a name with no row registers as a knowledge
     * collection of the default model.
     */
    public static int supersede(CatalogRepository repo, String tenant, String name, String successor, String at) {
        if (repo.getCollection(tenant, successor) == null) {
            Map<String, Object> source = repo.getCollection(tenant, name);
            Map<String, Object> reg = new HashMap<>();
            reg.put("name", successor);
            reg.put("content_type", orElse(source, "content_type", "knowledge"));
            reg.put("owner_id", orElse(source, "owner_id", "seed"));
            reg.put("embedding_model", orElse(source, "embedding_model", "minilm-l6-v2-384"));
            reg.put("model_version", orElse(source, "model_version", "v1"));
            try {
                repo.upsertCollection(tenant, reg);
            } catch (CatalogRepository.EmbeddingProfileConflictException profileNamesAnotherModel) {
                // the tenant's embedding profile for this content type names another model than the retired source's
                // (a re-embed source is the old model by design): a registration that names none takes the profile's
                reg.remove("embedding_model");
                repo.upsertCollection(tenant, reg);
            }
        }
        return repo.supersedeCollection(tenant, name, successor, at);
    }

    /** The source row's value when it has a non-blank one, else {@code fallback} (a new row requires each of these). */
    private static Object orElse(Map<String, Object> source, String key, String fallback) {
        Object v = source == null ? null : source.get(key);
        return v == null || v.toString().isBlank() ? fallback : v;
    }
}
