### Fixed

- The default debug plan no longer narrows every answer to catalog documents authored "nexus". Its query step carried a literal author filter, so `nx_answer` debug questions searched only documents whose catalog author contains "nexus" (and failed on an install with no catalog). A test now refuses a literal author on any builtin plan's retrieval step (8.5.6 docs audit).
