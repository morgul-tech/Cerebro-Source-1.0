-- UNAPPLIED proposal. Existing rightful migration custodian selects exact
-- backend, schema and dedicated role privileges before any real installation.
-- No source import or constructor executes this DDL or inserts an enrolment.
CREATE TABLE cerebro_pm_2872_enrolments (
    enrolment_id TEXT PRIMARY KEY,
    original BYTEA NOT NULL CHECK (octet_length(original) BETWEEN 1 AND 1048576),
    active BOOLEAN NOT NULL
);
CREATE TABLE cerebro_pm_2872_accepted (
    operation_id TEXT PRIMARY KEY,
    nonce TEXT UNIQUE NOT NULL,
    enrolment_id TEXT NOT NULL REFERENCES cerebro_pm_2872_enrolments(enrolment_id),
    token BYTEA NOT NULL CHECK (octet_length(token) BETWEEN 1 AND 1048576),
    state TEXT NOT NULL CHECK (state IN ('ACCEPTED','STARTED_UNKNOWN_NO_RETRY','COMPLETED')),
    result BYTEA,
    CHECK ((state = 'COMPLETED') = (result IS NOT NULL))
);
REVOKE ALL ON cerebro_pm_2872_enrolments, cerebro_pm_2872_accepted FROM PUBLIC;
-- No enrolled original can be rewritten or deleted. Revocation is terminal and
-- shares the executor's transaction lock through backend effect/readback.
CREATE FUNCTION cerebro_pm_2872_guard_enrolment() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('cerebro_pm_2872_enrolment:' || OLD.enrolment_id));
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'PM2872_ENROLMENT_DELETE_FORBIDDEN';
    END IF;
    IF NEW.enrolment_id IS DISTINCT FROM OLD.enrolment_id
       OR NEW.original IS DISTINCT FROM OLD.original
       OR OLD.active = false OR NEW.active <> false THEN
        RAISE EXCEPTION 'PM2872_ENROLMENT_IMMUTABLE_REVOCATION_ONLY';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION cerebro_pm_2872_guard_enrolment() FROM PUBLIC;
CREATE TRIGGER cerebro_pm_2872_enrolment_immutable
BEFORE UPDATE OR DELETE ON cerebro_pm_2872_enrolments
FOR EACH ROW EXECUTE FUNCTION cerebro_pm_2872_guard_enrolment();
-- Enrolment is an independently authorized immutable original plus a revocation
-- flag. Runtime read-only rights on enrolments; NO runtime INSERT/UPDATE there.
-- Accepted-operation runtime rights: SELECT/INSERT and UPDATE(state,result).
-- No runtime DELETE/TRUNCATE/DDL; complete operation+nonce history is retained.
