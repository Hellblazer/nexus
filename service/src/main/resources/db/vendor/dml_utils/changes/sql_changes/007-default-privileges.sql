-- PostgreSQL grants EXECUTE on functions and USAGE on types to PUBLIC by
-- default. Revoke those defaults before any routine or type is created, so a
-- newly added object never inherits a world-executable/world-usable default even
-- when it is omitted from the explicit grant changeset.
ALTER DEFAULT PRIVILEGES FOR ROLE dml_utils_owner IN SCHEMA dml_utils
    REVOKE EXECUTE ON FUNCTIONS FROM public;
ALTER DEFAULT PRIVILEGES FOR ROLE dml_utils_owner IN SCHEMA dml_utils_lib
    REVOKE EXECUTE ON FUNCTIONS FROM public;
ALTER DEFAULT PRIVILEGES FOR ROLE dml_utils_owner IN SCHEMA dml_utils_data
    REVOKE EXECUTE ON FUNCTIONS FROM public;

ALTER DEFAULT PRIVILEGES FOR ROLE dml_utils_owner IN SCHEMA dml_utils
    REVOKE USAGE ON TYPES FROM public;
ALTER DEFAULT PRIVILEGES FOR ROLE dml_utils_owner IN SCHEMA dml_utils_lib
    REVOKE USAGE ON TYPES FROM public;
ALTER DEFAULT PRIVILEGES FOR ROLE dml_utils_owner IN SCHEMA dml_utils_data
    REVOKE USAGE ON TYPES FROM public;
