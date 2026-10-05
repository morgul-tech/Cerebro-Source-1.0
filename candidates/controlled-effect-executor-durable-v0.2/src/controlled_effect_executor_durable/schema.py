"""DDL for the adapter's three tables (admission, event ledger, progress projection) in ONE caller-chosen schema.

This is a standalone helper for a disposable test schema, NOT a Cerebro migration and not a production schema
decision. Integrity that the database itself enforces against a writer that holds INSERT/UPDATE but cannot disable triggers:

* ``cee_admission``: immutable (no UPDATE/DELETE/TRUNCATE); ``batch_digest`` must equal SHA-256 of the stored
  canonical BatchSpec text; one row per ``idempotency_key`` (UNIQUE).
* ``cee_event``: append-only; PRIMARY KEY (admission_ref, seq); the first event is ADMISSION_FENCED chained to the
  admission receipt fingerprint; every later event must chain to its predecessor's fingerprint and follow a legal
  transition (the pairs are GENERATED from the pinned V0.1 ``TRANSITIONS`` table, not retyped). The queryable columns
  must equal the fields of the stored canonical JSON (admission_ref, seq, kind, state, attempt, event_ref and both
  fingerprints), and a NO_COMMIT event must carry settlement_status SETTLED_ABSENT plus a settlement digest.
* ``cee_progress``: a projection (state, last_seq, attempt_ref, ack flag). It may only move forward one seq at a
  time and only to a state that an existing ledger event already recorded; it can never be deleted.

WHAT THIS DOES NOT PROVIDE (honest limits): a privileged role can disable triggers; that is detected (not prevented)
by ``verify_projection`` and ``verify_provenance_durable``. There are no roles, grants, row-level security or
signatures here: any role that may INSERT into ``cee_admission`` can fabricate a self-consistent admission (the receipt
fingerprint is a hash, not a signature; only the owner's own checks at admission time gate it), and the triggers do not
recompute event fingerprints. A deployment MUST give the executor's role INSERT/UPDATE only through its own account and
keep owners/other writers away from these tables; that separation is a production decision this candidate does not make.
"""
from __future__ import annotations

import re

from controlled_effect_executor.states import STATES, TRANSITIONS

SCHEMA_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
UNRESOLVED_STATES = ("FENCED", "IN_FLIGHT", "UNKNOWN_EFFECT")
PROGRESS_STATES = tuple(s for s in STATES if s != "DENIED")


class SchemaNameError(ValueError):
    pass


def validate_schema(schema: str) -> str:
    if not isinstance(schema, str) or SCHEMA_NAME.fullmatch(schema) is None or schema.startswith("pg_"):
        raise SchemaNameError("schema-name-invalid")
    return schema


def _pairs() -> str:
    pairs = sorted((a, b) for a, targets in TRANSITIONS.items() for b in targets)
    return ", ".join(f"('{a}','{b}')" for a, b in pairs)


def _quoted_states(states) -> str:
    return ", ".join(f"'{s}'" for s in states)


def ddl_statements(schema: str) -> list:
    s = validate_schema(schema)
    forbid = f"""
CREATE FUNCTION {s}.cee_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
  RAISE EXCEPTION 'cee-immutable: % forbidden on %', TG_OP, TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
END $f$""".replace("%", "%%")
    event_guard = f"""
CREATE FUNCTION {s}.cee_event_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE prev_state text; prev_fp text; j jsonb;
BEGIN
  BEGIN
    j := NEW.event_canonical::jsonb;
  EXCEPTION WHEN others THEN
    RAISE EXCEPTION 'cee-event: canonical text is not JSON' USING ERRCODE = 'integrity_constraint_violation';
  END;
  IF jsonb_typeof(j) IS DISTINCT FROM 'object'
     OR j->>'admission_ref' IS DISTINCT FROM NEW.admission_ref
     OR j->>'seq' IS DISTINCT FROM NEW.seq::text
     OR j->>'event_kind' IS DISTINCT FROM NEW.event_kind
     OR j->>'state_after' IS DISTINCT FROM NEW.state_after
     OR j->>'attempt_ref' IS DISTINCT FROM NEW.attempt_ref
     OR j->>'event_ref' IS DISTINCT FROM NEW.event_ref
     OR j->>'event_fingerprint' IS DISTINCT FROM NEW.event_fingerprint
     OR j->>'prev_fingerprint' IS DISTINCT FROM NEW.prev_fingerprint
     OR jsonb_typeof(j->'detail') IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'cee-event: columns do not match the canonical event' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.state_after = 'NO_COMMIT' AND (j->'detail'->>'settlement_status' IS DISTINCT FROM 'SETTLED_ABSENT'
     OR COALESCE(j->'detail'->>'settlement_digest', '') = '') THEN
    RAISE EXCEPTION 'cee-event: NO_COMMIT requires explicit settlement evidence' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF NEW.seq = 1 THEN
    IF NEW.state_after <> 'FENCED' OR NEW.event_kind <> 'ADMISSION_FENCED' THEN
      RAISE EXCEPTION 'cee-event: first event must be ADMISSION_FENCED' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT receipt_fingerprint INTO prev_fp FROM {s}.cee_admission WHERE admission_ref = NEW.admission_ref;
  ELSE
    SELECT state_after, event_fingerprint INTO prev_state, prev_fp
      FROM {s}.cee_event WHERE admission_ref = NEW.admission_ref AND seq = NEW.seq - 1;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'cee-event: sequence gap' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (prev_state, NEW.state_after) NOT IN ({_pairs()}) THEN
      RAISE EXCEPTION 'cee-event: transition not allowed' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;
  IF prev_fp IS DISTINCT FROM NEW.prev_fingerprint THEN
    RAISE EXCEPTION 'cee-event: broken hash chain' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $f$"""
    progress_guard = f"""
CREATE FUNCTION {s}.cee_progress_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.last_seq <> 1 OR NEW.state <> 'FENCED' OR NEW.attempt_ref IS NOT NULL OR NEW.ack_recorded THEN
      RAISE EXCEPTION 'cee-progress: must start FENCED at seq 1' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  ELSE
    IF NEW.admission_ref <> OLD.admission_ref OR NEW.scope_ref <> OLD.scope_ref OR NEW.admit_seq <> OLD.admit_seq THEN
      RAISE EXCEPTION 'cee-progress: identity columns are immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.last_seq <> OLD.last_seq + 1 THEN
      RAISE EXCEPTION 'cee-progress: must advance exactly one seq' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.attempt_ref IS NOT NULL AND NEW.attempt_ref IS DISTINCT FROM OLD.attempt_ref THEN
      RAISE EXCEPTION 'cee-progress: attempt identity is immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.ack_recorded AND NOT NEW.ack_recorded THEN
      RAISE EXCEPTION 'cee-progress: ack flag cannot be cleared' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM {s}.cee_event e WHERE e.admission_ref = NEW.admission_ref AND e.seq = NEW.last_seq
                  AND e.state_after = NEW.state) THEN
    RAISE EXCEPTION 'cee-progress: no ledger event backs this projection' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $f$"""
    return [
        f"CREATE SCHEMA {s}",
        f"""CREATE TABLE {s}.cee_admission (
  admit_seq bigserial NOT NULL UNIQUE,
  admission_ref text PRIMARY KEY,
  scope_ref text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  batch_digest text NOT NULL CHECK (batch_digest ~ '^[0-9a-f]{{64}}$'),
  work_order_ref text NOT NULL,
  effect_ref text NOT NULL,
  spec_canonical text NOT NULL,
  receipt_canonical text NOT NULL,
  receipt_fingerprint text NOT NULL,
  CONSTRAINT cee_digest_binds_spec CHECK (encode(sha256(convert_to(spec_canonical, 'UTF8')), 'hex') = batch_digest)
)""",
        f"""CREATE TABLE {s}.cee_event (
  admission_ref text NOT NULL REFERENCES {s}.cee_admission (admission_ref),
  seq integer NOT NULL CHECK (seq >= 1),
  event_ref text NOT NULL UNIQUE,
  event_kind text NOT NULL CHECK (event_kind IN ('ADMISSION_FENCED', 'ATTEMPT_STARTED', 'PROVIDER_RESULT',
                                                  'RECONCILIATION', 'IN_FLIGHT_DECLARED_LOST')),
  state_after text NOT NULL CHECK (state_after IN ({_quoted_states(PROGRESS_STATES)})),
  attempt_ref text,
  event_canonical text NOT NULL,
  event_fingerprint text NOT NULL CHECK (event_fingerprint ~ '^[0-9a-f]{{64}}$'),
  prev_fingerprint text NOT NULL,
  PRIMARY KEY (admission_ref, seq)
)""",
        f"""CREATE TABLE {s}.cee_progress (
  admission_ref text PRIMARY KEY REFERENCES {s}.cee_admission (admission_ref),
  scope_ref text NOT NULL,
  admit_seq bigint NOT NULL,
  last_seq integer NOT NULL,
  state text NOT NULL CHECK (state IN ({_quoted_states(PROGRESS_STATES)})),
  attempt_ref text,
  ack_recorded boolean NOT NULL DEFAULT false
)""",
        f"CREATE INDEX cee_progress_unresolved ON {s}.cee_progress (scope_ref, admit_seq) "
        f"WHERE state IN ({_quoted_states(UNRESOLVED_STATES)})",
        forbid, event_guard, progress_guard,
        f"CREATE TRIGGER cee_admission_immutable BEFORE UPDATE OR DELETE ON {s}.cee_admission "
        f"FOR EACH ROW EXECUTE FUNCTION {s}.cee_forbid_mutation()",
        f"CREATE TRIGGER cee_admission_no_truncate BEFORE TRUNCATE ON {s}.cee_admission "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {s}.cee_forbid_mutation()",
        f"CREATE TRIGGER cee_event_append_only BEFORE UPDATE OR DELETE ON {s}.cee_event "
        f"FOR EACH ROW EXECUTE FUNCTION {s}.cee_forbid_mutation()",
        f"CREATE TRIGGER cee_event_no_truncate BEFORE TRUNCATE ON {s}.cee_event "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {s}.cee_forbid_mutation()",
        f"CREATE TRIGGER cee_event_guard_insert BEFORE INSERT ON {s}.cee_event "
        f"FOR EACH ROW EXECUTE FUNCTION {s}.cee_event_guard()",
        f"CREATE TRIGGER cee_progress_no_delete BEFORE DELETE ON {s}.cee_progress "
        f"FOR EACH ROW EXECUTE FUNCTION {s}.cee_forbid_mutation()",
        f"CREATE TRIGGER cee_progress_no_truncate BEFORE TRUNCATE ON {s}.cee_progress "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {s}.cee_forbid_mutation()",
        f"CREATE TRIGGER cee_progress_guard BEFORE INSERT OR UPDATE ON {s}.cee_progress "
        f"FOR EACH ROW EXECUTE FUNCTION {s}.cee_progress_guard()",
    ]


def create_schema(connect, schema: str) -> None:
    """Create the adapter tables in a NEW schema (fails if the schema already exists: never adopts existing data)."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        for statement in ddl_statements(schema):
            cur.execute(statement)
        cur.execute("COMMIT")
    except Exception:
        try:
            conn.cursor().execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        conn.close()
