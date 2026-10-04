-- C995 disposable same-database owner-binding candidate. Not a production migration.
-- Provision these roles in the isolated test DB before applying this file:
-- cerebro_pm_binding_owner (NOLOGIN), cerebro_pm_binding_issuer (LOGIN),
-- cerebro_nal_owner (LOGIN, from NAL migration). The PM owner role owns the
-- table/functions. Issuer and closer receive only EXECUTE on distinct ports.
BEGIN;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cerebro_pm_binding_owner') OR
       NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cerebro_pm_binding_issuer') OR
       NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cerebro_nal_owner') THEN
        RAISE EXCEPTION 'disposable PM owner, issuer and NAL closer roles required';
    END IF;
END $$;

CREATE SCHEMA pm_binding AUTHORIZATION cerebro_pm_binding_owner;
REVOKE ALL ON SCHEMA pm_binding FROM PUBLIC;
GRANT USAGE ON SCHEMA pm_binding TO cerebro_pm_binding_issuer, cerebro_nal_owner;

CREATE SEQUENCE pm_binding.revision_seq AS bigint START WITH 1;
ALTER SEQUENCE pm_binding.revision_seq OWNER TO cerebro_pm_binding_owner;
REVOKE ALL ON SEQUENCE pm_binding.revision_seq FROM PUBLIC;

CREATE TABLE pm_binding.current_binding (
    binding_ref text PRIMARY KEY,
    actor_ref text NOT NULL,
    generation_ref text NOT NULL,
    task_ref text NOT NULL,
    claim_ref text NOT NULL,
    packet_ref text NOT NULL,
    queue_ref text NOT NULL,
    provider_revision bigint NOT NULL CHECK (provider_revision > 0),
    status text NOT NULL CHECK (status IN ('ACTIVE', 'REVOKED', 'SUPERSEDED')),
    binding_json jsonb NOT NULL,
    successor_ref text,
    CHECK (binding_json->>'binding_ref' = binding_ref),
    CHECK (binding_json->>'actor_ref' = actor_ref),
    CHECK (binding_json->>'provider_revision' = provider_revision::text)
);
ALTER TABLE pm_binding.current_binding OWNER TO cerebro_pm_binding_owner;
REVOKE ALL ON pm_binding.current_binding FROM PUBLIC;
CREATE UNIQUE INDEX one_active_binding_per_task ON pm_binding.current_binding
    (actor_ref, generation_ref, task_ref) WHERE status = 'ACTIVE';

CREATE TABLE pm_binding.binding_receipt (
    receipt_ref text PRIMARY KEY,
    binding_ref text NOT NULL,
    event_revision bigint NOT NULL UNIQUE,
    operation text NOT NULL CHECK (operation IN ('ISSUE', 'REVOKE', 'SUPERSEDE')),
    receipt_json jsonb NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE pm_binding.binding_receipt OWNER TO cerebro_pm_binding_owner;
REVOKE ALL ON pm_binding.binding_receipt FROM PUBLIC;

CREATE FUNCTION pm_binding.reject_receipt_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN RAISE EXCEPTION 'PM binding receipt is immutable'; END $$;
ALTER FUNCTION pm_binding.reject_receipt_change() OWNER TO cerebro_pm_binding_owner;
CREATE TRIGGER immutable_receipt BEFORE UPDATE OR DELETE ON pm_binding.binding_receipt
FOR EACH ROW EXECUTE FUNCTION pm_binding.reject_receipt_change();

CREATE FUNCTION pm_binding.reserve_revision() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT nextval('pm_binding.revision_seq'::regclass)
$$;
ALTER FUNCTION pm_binding.reserve_revision() OWNER TO cerebro_pm_binding_owner;

CREATE FUNCTION pm_binding.issue(p jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE r bigint; ref text; receipt text; issued jsonb;
BEGIN
    ref := p->>'binding_ref';
    r := (p->>'provider_revision')::bigint;
    IF ref IS NULL OR ref = '' OR r IS NULL OR r < 1 OR
       r > (SELECT last_value FROM pm_binding.revision_seq) OR
       p->>'schema' IS DISTINCT FROM 'cerebro.task-semantics-binding/v1' OR
       p->>'actor_ref' IS NULL OR p->>'generation_ref' IS NULL OR
       p->>'task_ref' IS NULL OR p->>'claim_ref' IS NULL OR
       p->>'packet_ref' IS NULL OR p->>'queue_ref' IS NULL OR
       p->>'attempt_ref' IS NULL OR p->>'actor_role' IS NULL OR
       p->>'source_revision' IS NULL OR p->>'role_projection_fingerprint' IS NULL OR
       p->>'task_payload_sha256' IS NULL OR p->>'admission_receipt' IS NULL OR
       p->>'owner_revision' IS NULL OR
       p->>'supersedes' IS NOT NULL OR
       COALESCE(p->>'canonical_fingerprint','') !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid PM binding issue payload';
    END IF;
    receipt := 'PMB-I-' || r::text;
    issued := p || jsonb_build_object('evidence_ref', ref, 'issue_receipt', receipt,
                                      'current', true, 'revoked', false);
    INSERT INTO pm_binding.current_binding
      (binding_ref, actor_ref, generation_ref, task_ref, claim_ref, packet_ref,
       queue_ref, provider_revision, status, binding_json)
    VALUES (ref, p->>'actor_ref', p->>'generation_ref', p->>'task_ref',
            p->>'claim_ref', p->>'packet_ref', p->>'queue_ref', r, 'ACTIVE', issued);
    INSERT INTO pm_binding.binding_receipt
      (receipt_ref, binding_ref, event_revision, operation, receipt_json)
    VALUES (receipt, ref, r, 'ISSUE', jsonb_build_object(
      'receipt_ref', receipt, 'binding_ref', ref, 'provider_revision', r,
      'operation', 'ISSUE', 'actor_ref', p->>'actor_ref'));
    RETURN issued;
END $$;
ALTER FUNCTION pm_binding.issue(jsonb) OWNER TO cerebro_pm_binding_owner;

CREATE FUNCTION pm_binding.read_current(p_ref text) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT binding_json FROM pm_binding.current_binding WHERE binding_ref = p_ref
$$;
ALTER FUNCTION pm_binding.read_current(text) OWNER TO cerebro_pm_binding_owner;

CREATE FUNCTION pm_binding.read_receipt(p_ref text) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT receipt_json FROM pm_binding.binding_receipt WHERE receipt_ref = p_ref
$$;
ALTER FUNCTION pm_binding.read_receipt(text) OWNER TO cerebro_pm_binding_owner;

CREATE FUNCTION pm_binding.revoke(p_ref text, p_expected bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE old_row pm_binding.current_binding%ROWTYPE; r bigint; receipt text; changed jsonb;
BEGIN
    SELECT * INTO old_row FROM pm_binding.current_binding
     WHERE binding_ref = p_ref FOR UPDATE;
    IF NOT FOUND OR old_row.status <> 'ACTIVE' OR old_row.provider_revision <> p_expected THEN
        RAISE EXCEPTION 'stale or noncurrent PM binding revoke';
    END IF;
    r := nextval('pm_binding.revision_seq'::regclass);
    receipt := 'PMB-R-' || r::text;
    changed := old_row.binding_json || jsonb_build_object(
      'provider_revision', r::text, 'current', false, 'revoked', true,
      'revoke_receipt', receipt);
    UPDATE pm_binding.current_binding SET status='REVOKED', provider_revision=r,
      binding_json=changed WHERE binding_ref=p_ref;
    INSERT INTO pm_binding.binding_receipt
      (receipt_ref, binding_ref, event_revision, operation, receipt_json)
    VALUES (receipt, p_ref, r, 'REVOKE', jsonb_build_object(
      'receipt_ref', receipt, 'binding_ref', p_ref, 'provider_revision', r,
      'operation', 'REVOKE', 'previous_revision', p_expected));
    RETURN (SELECT receipt_json FROM pm_binding.binding_receipt WHERE receipt_ref=receipt);
END $$;
ALTER FUNCTION pm_binding.revoke(text,bigint) OWNER TO cerebro_pm_binding_owner;

CREATE FUNCTION pm_binding.supersede(p_old text, p_expected bigint, p_new jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE old_row pm_binding.current_binding%ROWTYPE; new_row jsonb;
        r bigint; old_r bigint; receipt text; old_receipt text; old_changed jsonb;
BEGIN
    SELECT * INTO old_row FROM pm_binding.current_binding
     WHERE binding_ref=p_old FOR UPDATE;
    IF NOT FOUND OR old_row.status <> 'ACTIVE' OR old_row.provider_revision <> p_expected THEN
        RAISE EXCEPTION 'stale or noncurrent PM binding supersession';
    END IF;
    IF p_new->>'supersedes' IS DISTINCT FROM p_old OR
       p_new->>'binding_ref' IS NULL OR p_new->>'binding_ref' = p_old OR
       (p_new->>'actor_ref', p_new->>'generation_ref', p_new->>'task_ref',
        p_new->>'claim_ref', p_new->>'packet_ref', p_new->>'queue_ref',
        p_new->>'attempt_ref', p_new->>'actor_role', p_new->>'source_revision',
        p_new->>'role_projection_fingerprint', p_new->>'task_payload_sha256',
        p_new->>'admission_receipt') IS DISTINCT FROM
       (old_row.actor_ref, old_row.generation_ref, old_row.task_ref,
        old_row.claim_ref, old_row.packet_ref, old_row.queue_ref,
        old_row.binding_json->>'attempt_ref', old_row.binding_json->>'actor_role',
        old_row.binding_json->>'source_revision',
        old_row.binding_json->>'role_projection_fingerprint',
        old_row.binding_json->>'task_payload_sha256',
        old_row.binding_json->>'admission_receipt') THEN
        RAISE EXCEPTION 'PM binding supersession lineage mismatch';
    END IF;
    r := (p_new->>'provider_revision')::bigint;
    IF r IS NULL OR r < 1 OR r > (SELECT last_value FROM pm_binding.revision_seq) OR
       p_new->>'schema' IS DISTINCT FROM 'cerebro.task-semantics-binding/v1' OR
       COALESCE(p_new->>'canonical_fingerprint','') !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid successor binding';
    END IF;
    receipt := 'PMB-I-' || r::text;
    new_row := p_new || jsonb_build_object('evidence_ref', p_new->>'binding_ref',
              'issue_receipt', receipt, 'current', true, 'revoked', false);
    -- Retire the old active row first within this transaction, so the partial
    -- unique index forbids any second current binding for the same task.
    old_r := nextval('pm_binding.revision_seq'::regclass);
    old_receipt := 'PMB-S-' || old_r::text;
    old_changed := old_row.binding_json || jsonb_build_object(
      'provider_revision', old_r::text, 'current', false, 'revoked', false,
      'successor_ref', p_new->>'binding_ref', 'supersede_receipt', old_receipt);
    UPDATE pm_binding.current_binding SET status='SUPERSEDED', provider_revision=old_r,
      binding_json=old_changed, successor_ref=p_new->>'binding_ref' WHERE binding_ref=p_old;
    INSERT INTO pm_binding.current_binding
      (binding_ref, actor_ref, generation_ref, task_ref, claim_ref, packet_ref,
       queue_ref, provider_revision, status, binding_json)
    VALUES (p_new->>'binding_ref', old_row.actor_ref, old_row.generation_ref,
            old_row.task_ref, old_row.claim_ref, old_row.packet_ref,
            old_row.queue_ref, r, 'ACTIVE', new_row);
    INSERT INTO pm_binding.binding_receipt
      (receipt_ref, binding_ref, event_revision, operation, receipt_json)
    VALUES (receipt, p_new->>'binding_ref', r, 'ISSUE', jsonb_build_object(
      'receipt_ref', receipt, 'binding_ref', p_new->>'binding_ref',
      'provider_revision', r, 'operation', 'ISSUE', 'supersedes', p_old));
    INSERT INTO pm_binding.binding_receipt
      (receipt_ref, binding_ref, event_revision, operation, receipt_json)
    VALUES (old_receipt, p_old, old_r, 'SUPERSEDE', jsonb_build_object(
      'receipt_ref', old_receipt, 'binding_ref', p_old,
      'provider_revision', old_r, 'operation', 'SUPERSEDE',
      'successor_ref', p_new->>'binding_ref', 'previous_revision', p_expected));
    RETURN jsonb_build_object('issue_receipt', receipt,
                              'supersede_receipt', old_receipt, 'binding', new_row);
END $$;
ALTER FUNCTION pm_binding.supersede(text,bigint,jsonb) OWNER TO cerebro_pm_binding_owner;

CREATE FUNCTION pm_binding.acquire_close_fence(
    p_ref text, p_revision bigint, p_actor text, p_generation text,
    p_task text, p_claim text, p_packet text, p_queue text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE row pm_binding.current_binding%ROWTYPE;
BEGIN
    SELECT * INTO row FROM pm_binding.current_binding
     WHERE binding_ref=p_ref FOR UPDATE;
    IF NOT FOUND OR row.status <> 'ACTIVE' OR row.provider_revision <> p_revision OR
       (row.actor_ref,row.generation_ref,row.task_ref,row.claim_ref,row.packet_ref,row.queue_ref)
         IS DISTINCT FROM (p_actor,p_generation,p_task,p_claim,p_packet,p_queue) THEN
        RAISE EXCEPTION 'PM binding close fence stale or wrong actor/lineage';
    END IF;
    RETURN row.binding_json;
END $$;
ALTER FUNCTION pm_binding.acquire_close_fence(text,bigint,text,text,text,text,text,text)
OWNER TO cerebro_pm_binding_owner;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA pm_binding FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pm_binding.reserve_revision() TO cerebro_pm_binding_issuer;
GRANT EXECUTE ON FUNCTION pm_binding.issue(jsonb) TO cerebro_pm_binding_issuer;
GRANT EXECUTE ON FUNCTION pm_binding.revoke(text,bigint) TO cerebro_pm_binding_issuer;
GRANT EXECUTE ON FUNCTION pm_binding.supersede(text,bigint,jsonb) TO cerebro_pm_binding_issuer;
GRANT EXECUTE ON FUNCTION pm_binding.read_current(text)
 TO cerebro_pm_binding_issuer, cerebro_nal_owner;
GRANT EXECUTE ON FUNCTION pm_binding.read_receipt(text)
 TO cerebro_pm_binding_issuer, cerebro_nal_owner;
GRANT EXECUTE ON FUNCTION
 pm_binding.acquire_close_fence(text,bigint,text,text,text,text,text,text)
 TO cerebro_nal_owner;
COMMIT;
