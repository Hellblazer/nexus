// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.type.TypeReference;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The wire shape of one {@code catalog_documents} row: a jOOQ {@code Record.intoMap()} of
 * {@link CatalogRepository#documentFields} turned into the strongly-typed doc map every reader returns. Moved, unchanged,
 * out of {@link CatalogRepository} so that class stays under its file-size ceiling (RDR-229 P3.5c assembly, nexus-p2m1x.25).
 */
final class CatalogDocRow {

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private CatalogDocRow() { /* static */ }

    /** Convert a jOOQ Record.intoMap() to a strongly-typed doc map. */
    static Map<String, Object> of(Map<String, Object> raw) {
        Map<String, Object> m = new LinkedHashMap<>();
        // Column names in intoMap() are the unqualified column names
        m.put("tumbler",             raw.getOrDefault("tumbler", null));
        m.put("title",               raw.getOrDefault("title", null));
        m.put("author",              raw.getOrDefault("author", null));
        m.put("year",                raw.getOrDefault("year", null));
        // hygiene-001 (nexus-tk070.p6a follow-on): these five columns are NOT NULL
        // now — CatalogRepository.nne()/0 here is belt-and-suspenders documentation of that guarantee
        // (every write site already stamps ""/0; see registerDocumentWithOutcome,
        // doImportDocument, buildUpdateDocumentQuery's null-skip), not a live gap.
        m.put("content_type",        CatalogRepository.nne((String) raw.getOrDefault("content_type", null)));
        m.put("file_path",           CatalogRepository.nne((String) raw.getOrDefault("file_path", null)));
        m.put("corpus",              CatalogRepository.nne((String) raw.getOrDefault("corpus", null)));
        m.put("physical_collection", CatalogRepository.nne((String) raw.getOrDefault("physical_collection", null)));
        m.put("chunk_count",         raw.getOrDefault("chunk_count", null) != null
                                          ? raw.get("chunk_count") : 0);
        m.put("head_hash",           raw.getOrDefault("head_hash", null));
        // nexus-cefa1.2: indexed_at is timestamptz now (catalog-031-1-documents-temporal)
        // — .intoMap() hands back an OffsetDateTime, not a String; format for the wire.
        // Stays nullable, matching the pre-migration nullable-TEXT-no-default contract.
        m.put("indexed_at",          CatalogRepository.utcIso((java.time.OffsetDateTime) raw.get("indexed_at")));
        Object rawMeta = raw.get("metadata");
        if (rawMeta != null) {
            try {
                m.put("metadata", CatalogRepository.MAPPER.readValue(rawMeta.toString(), MAP_TYPE));
            } catch (Exception e) {
                m.put("metadata", null);
            }
        } else {
            m.put("metadata", null);
        }
        m.put("source_mtime", raw.getOrDefault("source_mtime", 0.0));
        m.put("alias_of",     CatalogRepository.nne((String) raw.getOrDefault("alias_of", null)));
        m.put("source_uri",   CatalogRepository.nne((String) raw.getOrDefault("source_uri", null)));
        m.put("bib_year",                raw.getOrDefault("bib_year", 0));
        m.put("bib_authors",             CatalogRepository.nne((String) raw.getOrDefault("bib_authors", null)));
        m.put("bib_venue",               CatalogRepository.nne((String) raw.getOrDefault("bib_venue", null)));
        m.put("bib_citation_count",      raw.getOrDefault("bib_citation_count", 0));
        m.put("bib_semantic_scholar_id", CatalogRepository.nne((String) raw.getOrDefault("bib_semantic_scholar_id", null)));
        m.put("bib_openalex_id",         CatalogRepository.nne((String) raw.getOrDefault("bib_openalex_id", null)));
        m.put("bib_doi",                 CatalogRepository.nne((String) raw.getOrDefault("bib_doi", null)));
        // nexus-cefa1.2: bib_enriched_at is timestamptz now — was NOT NULL DEFAULT ''
        // before catalog-031-1-documents-temporal, so preserve the always-non-null ""
        // wire contract via CatalogRepository.nne() around the formatted read.
        m.put("bib_enriched_at",         CatalogRepository.nne(CatalogRepository.utcIso((java.time.OffsetDateTime) raw.get("bib_enriched_at"))));
        // nexus-5xn3k.2 (RUNFENCE): index_state stays NULL-able (NULL = unknown,
        // catalog-020's deliberate no-backfill default) — do NOT CatalogRepository.nne() it, unlike
        // the other three (NOT NULL DEFAULT '' columns, same CatalogRepository.nne() treatment as
        // alias_of/source_uri above).
        m.put("index_state",        raw.getOrDefault("index_state", null));
        m.put("index_content_hash", CatalogRepository.nne((String) raw.getOrDefault("index_content_hash", null)));
        m.put("index_run_id",       CatalogRepository.nne((String) raw.getOrDefault("index_run_id", null)));
        // nexus-cefa1.2: index_started_at is timestamptz now — same NOT-NULL-DEFAULT-''
        // wire-contract preservation as bib_enriched_at above.
        m.put("index_started_at",   CatalogRepository.nne(CatalogRepository.utcIso((java.time.OffsetDateTime) raw.get("index_started_at"))));
        // nexus-rfvyy.6.5 (RDR-243): always a JSON boolean, never null (the column is NOT NULL DEFAULT false).
        m.put("sensitive",          Boolean.TRUE.equals(raw.get("sensitive")));
        return m;
    }
}
