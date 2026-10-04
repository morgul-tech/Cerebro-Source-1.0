-- P1625 disposable parent-authority candidate. Apply only in an isolated DB.
-- Provision NOLOGIN cerebro_pm_authority_owner and distinct LOGIN
-- cerebro_human_rights_issuer, cerebro_pm_admin_issuer,
-- cerebro_pm_decision_issuer, cerebro_nal_owner before this migration.
BEGIN;
DO $$ BEGIN
  IF (SELECT count(*) FROM pg_roles WHERE rolname IN
    ('cerebro_pm_authority_owner','cerebro_human_rights_issuer',
     'cerebro_pm_admin_issuer','cerebro_pm_decision_issuer',
     'cerebro_nal_owner')) <> 5 THEN
    RAISE EXCEPTION 'P1625 isolated roles required';
  END IF;
END $$;
CREATE SCHEMA pm_authority AUTHORIZATION cerebro_pm_authority_owner;
REVOKE ALL ON SCHEMA pm_authority FROM PUBLIC;
GRANT USAGE ON SCHEMA pm_authority TO cerebro_human_rights_issuer,
  cerebro_pm_admin_issuer, cerebro_pm_decision_issuer, cerebro_nal_owner;

CREATE TABLE pm_authority.rights (
  mandate_ref text PRIMARY KEY,
  owner_revision bigint NOT NULL CHECK (owner_revision > 0),
  status text NOT NULL CHECK (status IN ('ACTIVE','REVOKED','SUPERSEDED','EXPIRED')),
  expires_at timestamptz,
  body jsonb NOT NULL,
  CHECK (body->>'mandate_ref' = mandate_ref),
  CHECK ((body->>'owner_revision')::bigint = owner_revision),
  CHECK (body->>'grantor_role' = 'HUMAN')
);
ALTER TABLE pm_authority.rights OWNER TO cerebro_pm_authority_owner;
CREATE TABLE pm_authority.delegation (
  delegation_ref text PRIMARY KEY,
  mandate_ref text NOT NULL REFERENCES pm_authority.rights(mandate_ref),
  owner_revision bigint NOT NULL CHECK (owner_revision > 0),
  fence_revision bigint NOT NULL CHECK (fence_revision > 0),
  status text NOT NULL CHECK (status IN ('ACTIVE','REVOKED','SUPERSEDED','EXPIRED')),
  expires_at timestamptz,
  body jsonb NOT NULL,
  CHECK (body->>'delegation_ref' = delegation_ref),
  CHECK (body->>'mandate_ref' = mandate_ref),
  CHECK ((body->>'owner_revision')::bigint = owner_revision),
  CHECK ((body->>'fence_revision')::bigint = fence_revision),
  CHECK (body->>'authorized_by_role' = 'HUMAN_ADMIN_OWNER')
);
ALTER TABLE pm_authority.delegation OWNER TO cerebro_pm_authority_owner;
CREATE UNIQUE INDEX pm_one_active_delegation ON pm_authority.delegation
 (mandate_ref,(body->>'pm_actor_ref'),(body->>'pm_generation_ref'))
 WHERE status='ACTIVE';
CREATE TABLE pm_authority.decision (
  decision_ref text PRIMARY KEY,
  task_ref text NOT NULL,
  mandate_ref text NOT NULL REFERENCES pm_authority.rights(mandate_ref),
  delegation_ref text NOT NULL REFERENCES pm_authority.delegation(delegation_ref),
  task_revision bigint NOT NULL CHECK (task_revision > 0),
  task_sha256 text NOT NULL CHECK (task_sha256 ~ '^[0-9a-f]{64}$'),
  claim_ref text NOT NULL,
  packet_ref text NOT NULL,
  queue_ref text NOT NULL,
  provenance_ref text NOT NULL,
  basis_fingerprint text NOT NULL CHECK (basis_fingerprint ~ '^[0-9a-f]{64}$'),
  basis jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('ACTIVE','REVOKED','SUPERSEDED','EXPIRED')),
  expires_at timestamptz,
  CHECK (basis->>'decision_ref' = decision_ref),
  CHECK (basis->>'task_ref' = task_ref),
  CHECK ((basis->>'task_revision')::bigint = task_revision),
  CHECK (basis->>'task_sha256' = task_sha256),
  CHECK (basis->>'mandate_ref' = mandate_ref),
  CHECK (basis->>'delegation_ref' = delegation_ref)
);
ALTER TABLE pm_authority.decision OWNER TO cerebro_pm_authority_owner;
CREATE UNIQUE INDEX pm_one_active_task_decision ON pm_authority.decision(task_ref)
  WHERE status = 'ACTIVE';
REVOKE ALL ON ALL TABLES IN SCHEMA pm_authority FROM PUBLIC;

CREATE FUNCTION pm_authority.issue_rights(p jsonb, p_expires timestamptz)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
  IF p->>'schema' IS DISTINCT FROM 'cerebro.pm-human-rights/v2'
     OR p->>'grantor_role' IS DISTINCT FROM 'HUMAN'
     OR p->>'readback_verified' IS DISTINCT FROM 'true'
     OR p->>'current' IS DISTINCT FROM 'true'
     OR p->>'revoked' IS DISTINCT FROM 'false'
     OR p->>'mandate_ref' IS NULL
     OR (p->>'owner_revision')::bigint < 1
     OR p->'human_decision_refs' IS NULL THEN
    RAISE EXCEPTION 'invalid Human rights evidence';
  END IF;
  INSERT INTO pm_authority.rights VALUES
    (p->>'mandate_ref',(p->>'owner_revision')::bigint,'ACTIVE',p_expires,p);
  RETURN p;
END $$;
ALTER FUNCTION pm_authority.issue_rights(jsonb,timestamptz)
  OWNER TO cerebro_pm_authority_owner;

CREATE FUNCTION pm_authority.issue_delegation(p jsonb, p_expires timestamptz)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE r pm_authority.rights%ROWTYPE;
BEGIN
  SELECT * INTO r FROM pm_authority.rights
    WHERE mandate_ref=p->>'mandate_ref' FOR UPDATE;
  IF NOT FOUND OR r.status <> 'ACTIVE'
     OR (r.expires_at IS NOT NULL AND r.expires_at <= clock_timestamp())
     OR p->>'schema' IS DISTINCT FROM 'cerebro.pm-generation-delegation/v2'
     OR p->>'authorized_by_role' IS DISTINCT FROM 'HUMAN_ADMIN_OWNER'
     OR p->>'readback_verified' IS DISTINCT FROM 'true'
     OR p->>'current' IS DISTINCT FROM 'true'
     OR p->>'revoked' IS DISTINCT FROM 'false'
     OR (p->>'mandate_semantic_revision')::bigint IS DISTINCT FROM
        (r.body->>'semantic_revision')::bigint
     OR (p->>'owner_revision')::bigint < 1
     OR (p->>'fence_revision')::bigint < 1
     OR p->>'administrative_grant_ref' IS NULL THEN
    RAISE EXCEPTION 'invalid administrative delegation';
  END IF;
  INSERT INTO pm_authority.delegation VALUES
    (p->>'delegation_ref',p->>'mandate_ref',
     (p->>'owner_revision')::bigint,(p->>'fence_revision')::bigint,
     'ACTIVE',p_expires,p);
  RETURN p;
END $$;
ALTER FUNCTION pm_authority.issue_delegation(jsonb,timestamptz)
  OWNER TO cerebro_pm_authority_owner;

CREATE FUNCTION pm_authority.issue_decision(
  p_decision text,p_task text,p_revision bigint,p_sha text,
  p_claim text,p_packet text,p_queue text,p_provenance text,
  p_basis jsonb,p_fingerprint text,
  p_expires timestamptz)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE r pm_authority.rights%ROWTYPE; d pm_authority.delegation%ROWTYPE;
BEGIN
  SELECT * INTO r FROM pm_authority.rights
    WHERE mandate_ref=p_basis->>'mandate_ref' FOR UPDATE;
  IF NOT FOUND OR r.status <> 'ACTIVE' OR
     (r.expires_at IS NOT NULL AND r.expires_at <= clock_timestamp()) THEN
    RAISE EXCEPTION 'Human rights not current';
  END IF;
  SELECT * INTO d FROM pm_authority.delegation
    WHERE delegation_ref=p_basis->>'delegation_ref' FOR UPDATE;
  IF NOT FOUND OR d.status <> 'ACTIVE' OR d.mandate_ref <> r.mandate_ref OR
     (d.expires_at IS NOT NULL AND d.expires_at <= clock_timestamp()) OR
     (p_basis->>'mandate_semantic_revision')::bigint IS DISTINCT FROM
       (r.body->>'semantic_revision')::bigint OR
     p_basis->>'mandate_basis_sha256' IS DISTINCT FROM r.body->>'basis_sha256' OR
     p_basis->'human_decision_refs' IS DISTINCT FROM r.body->'human_decision_refs' OR
     p_basis->>'pm_actor_ref' IS DISTINCT FROM d.body->>'pm_actor_ref' OR
     p_basis->>'pm_generation_ref' IS DISTINCT FROM d.body->>'pm_generation_ref' OR
     p_basis->>'task_ref' IS DISTINCT FROM p_task OR
     (p_basis->>'task_revision')::bigint IS DISTINCT FROM p_revision OR
     p_basis->>'task_sha256' IS DISTINCT FROM p_sha OR
     p_basis->>'decision_ref' IS DISTINCT FROM p_decision OR
     p_basis->>'operation' IS NULL OR
     p_basis->>'worker_actor_ref' IS NULL OR
     p_basis->>'worker_generation_ref' IS NULL OR
     p_claim IS NULL OR p_packet IS NULL OR p_queue IS NULL OR
     p_provenance IS NULL OR
     p_fingerprint !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'decision authority lineage mismatch';
  END IF;
  INSERT INTO pm_authority.decision VALUES
    (p_decision,p_task,r.mandate_ref,d.delegation_ref,p_revision,p_sha,
     p_claim,p_packet,p_queue,p_provenance,p_fingerprint,p_basis,'ACTIVE',p_expires);
  RETURN jsonb_build_object('decision_ref',p_decision,'task_ref',p_task,
    'task_revision',p_revision,'task_sha256',p_sha,
    'claim_ref',p_claim,'packet_ref',p_packet,'queue_ref',p_queue,
    'provenance_ref',p_provenance,'basis_fingerprint',p_fingerprint,'readback_verified',true);
END $$;
ALTER FUNCTION pm_authority.issue_decision(
 text,text,bigint,text,text,text,text,text,jsonb,text,timestamptz)
 OWNER TO cerebro_pm_authority_owner;

CREATE FUNCTION pm_authority.read_rights(p_ref text) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT body || jsonb_build_object(
    'current',status='ACTIVE' AND
      (expires_at IS NULL OR expires_at>clock_timestamp()),
    'revoked',status='REVOKED')
  FROM pm_authority.rights WHERE mandate_ref=p_ref
$$;
ALTER FUNCTION pm_authority.read_rights(text) OWNER TO cerebro_pm_authority_owner;
CREATE FUNCTION pm_authority.read_delegation(p_actor text,p_gen text,p_mandate text)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT body || jsonb_build_object(
    'current',status='ACTIVE' AND
      (expires_at IS NULL OR expires_at>clock_timestamp()),
    'revoked',status='REVOKED')
  FROM pm_authority.delegation WHERE mandate_ref=p_mandate AND
    body->>'pm_actor_ref'=p_actor AND body->>'pm_generation_ref'=p_gen
    AND status='ACTIVE' AND
    (expires_at IS NULL OR expires_at>clock_timestamp())
  ORDER BY owner_revision DESC LIMIT 1
$$;
ALTER FUNCTION pm_authority.read_delegation(text,text,text)
 OWNER TO cerebro_pm_authority_owner;

CREATE FUNCTION pm_authority.acquire_parent_fence(
  p_basis jsonb,p_fingerprint text,p_rights_revision bigint,
  p_delegation_revision bigint,p_fence_revision bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE r pm_authority.rights%ROWTYPE; d pm_authority.delegation%ROWTYPE;
        t pm_authority.decision%ROWTYPE;
BEGIN
  -- Invoked on the NAL cursor after actor FOR UPDATE. All owner mutations below
  -- take the same row locks. These locks remain until the NAL COMMIT.
  SELECT * INTO r FROM pm_authority.rights
    WHERE mandate_ref=p_basis->>'mandate_ref' FOR UPDATE;
  IF FOUND AND r.expires_at IS NOT NULL THEN
    RAISE EXCEPTION 'PARENT_TIMED_EXPIRY_UNSUPPORTED' USING ERRCODE='P0T01';
  END IF;
  IF NOT FOUND OR r.status <> 'ACTIVE' OR r.owner_revision <> p_rights_revision
     OR (r.expires_at IS NOT NULL AND r.expires_at <= clock_timestamp())
     OR r.body->>'basis_sha256' IS DISTINCT FROM p_basis->>'mandate_basis_sha256'
     OR r.body->'human_decision_refs' IS DISTINCT FROM p_basis->'human_decision_refs'
     OR (r.body->>'semantic_revision')::bigint IS DISTINCT FROM
        (p_basis->>'mandate_semantic_revision')::bigint THEN
    RAISE EXCEPTION 'Human authority stale or revoked';
  END IF;
  SELECT * INTO d FROM pm_authority.delegation
    WHERE delegation_ref=p_basis->>'delegation_ref' FOR UPDATE;
  IF FOUND AND d.expires_at IS NOT NULL THEN
    RAISE EXCEPTION 'PARENT_TIMED_EXPIRY_UNSUPPORTED' USING ERRCODE='P0T01';
  END IF;
  IF NOT FOUND OR d.status <> 'ACTIVE' OR d.mandate_ref <> r.mandate_ref
     OR d.owner_revision <> p_delegation_revision
     OR d.fence_revision <> p_fence_revision
     OR (d.expires_at IS NOT NULL AND d.expires_at <= clock_timestamp())
     OR d.body->>'pm_actor_ref' IS DISTINCT FROM p_basis->>'pm_actor_ref'
     OR d.body->>'pm_generation_ref' IS DISTINCT FROM p_basis->>'pm_generation_ref' THEN
    RAISE EXCEPTION 'delegation stale or revoked';
  END IF;
  SELECT * INTO t FROM pm_authority.decision
    WHERE decision_ref=p_basis->>'decision_ref' FOR UPDATE;
  IF FOUND AND t.expires_at IS NOT NULL THEN
    RAISE EXCEPTION 'PARENT_TIMED_EXPIRY_UNSUPPORTED' USING ERRCODE='P0T01';
  END IF;
  IF NOT FOUND OR t.status <> 'ACTIVE'
     OR (t.expires_at IS NOT NULL AND t.expires_at <= clock_timestamp())
     OR t.mandate_ref <> r.mandate_ref OR t.delegation_ref <> d.delegation_ref
     OR t.basis IS DISTINCT FROM p_basis
     OR t.basis_fingerprint <> p_fingerprint
     OR t.task_ref <> p_basis->>'task_ref'
     OR t.task_revision <> (p_basis->>'task_revision')::bigint
     OR t.task_sha256 <> p_basis->>'task_sha256' THEN
    RAISE EXCEPTION 'PM task decision stale or revoked';
  END IF;
  RETURN jsonb_build_object('decision_ref',t.decision_ref,'task_ref',t.task_ref,
    'task_revision',t.task_revision,'task_sha256',t.task_sha256,
    'claim_ref',t.claim_ref,'packet_ref',t.packet_ref,'queue_ref',t.queue_ref,
    'provenance_ref',t.provenance_ref,'mandate_ref',r.mandate_ref,'delegation_ref',d.delegation_ref,
    'basis_fingerprint',t.basis_fingerprint,'readback_verified',true);
END $$;
ALTER FUNCTION pm_authority.acquire_parent_fence(jsonb,text,bigint,bigint,bigint)
 OWNER TO cerebro_pm_authority_owner;

CREATE FUNCTION pm_authority.revoke_rights(p_ref text,p_revision bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r pm_authority.rights%ROWTYPE;
BEGIN
 SELECT * INTO r FROM pm_authority.rights WHERE mandate_ref=p_ref FOR UPDATE;
 IF NOT FOUND OR r.status <> 'ACTIVE' OR r.owner_revision <> p_revision THEN
   RAISE EXCEPTION 'stale Human revoke';
 END IF;
 UPDATE pm_authority.rights SET status='REVOKED',owner_revision=owner_revision+1,
  body=body || jsonb_build_object('owner_revision',p_revision+1,
    'current',false,'revoked',true)
  WHERE mandate_ref=p_ref;
 RETURN jsonb_build_object('mandate_ref',p_ref,'revoke_owner_revision',p_revision+1);
END $$;
ALTER FUNCTION pm_authority.revoke_rights(text,bigint)
 OWNER TO cerebro_pm_authority_owner;
CREATE FUNCTION pm_authority.revoke_delegation(p_ref text,p_revision bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE d pm_authority.delegation%ROWTYPE; r pm_authority.rights%ROWTYPE;
BEGIN
 SELECT * INTO d FROM pm_authority.delegation WHERE delegation_ref=p_ref;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown delegation'; END IF;
 SELECT * INTO r FROM pm_authority.rights WHERE mandate_ref=d.mandate_ref FOR UPDATE;
 SELECT * INTO d FROM pm_authority.delegation WHERE delegation_ref=p_ref FOR UPDATE;
 IF d.status <> 'ACTIVE' OR d.owner_revision <> p_revision THEN
   RAISE EXCEPTION 'stale delegation revoke';
 END IF;
 UPDATE pm_authority.delegation
  SET status='REVOKED',owner_revision=owner_revision+1,fence_revision=fence_revision+1,
  body=body || jsonb_build_object('owner_revision',p_revision+1,
    'fence_revision',d.fence_revision+1,'current',false,'revoked',true)
  WHERE delegation_ref=p_ref;
 RETURN jsonb_build_object('delegation_ref',p_ref,'revoke_owner_revision',p_revision+1);
END $$;
ALTER FUNCTION pm_authority.revoke_delegation(text,bigint)
 OWNER TO cerebro_pm_authority_owner;
CREATE FUNCTION pm_authority.revoke_decision(p_ref text,p_revision bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE t pm_authority.decision%ROWTYPE; r pm_authority.rights%ROWTYPE;
        d pm_authority.delegation%ROWTYPE;
BEGIN
 SELECT * INTO t FROM pm_authority.decision WHERE decision_ref=p_ref;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown decision'; END IF;
 SELECT * INTO r FROM pm_authority.rights WHERE mandate_ref=t.mandate_ref FOR UPDATE;
 SELECT * INTO d FROM pm_authority.delegation WHERE delegation_ref=t.delegation_ref FOR UPDATE;
 SELECT * INTO t FROM pm_authority.decision WHERE decision_ref=p_ref FOR UPDATE;
 IF t.status <> 'ACTIVE' OR t.task_revision <> p_revision THEN
   RAISE EXCEPTION 'stale decision revoke';
 END IF;
 UPDATE pm_authority.decision SET status='REVOKED' WHERE decision_ref=p_ref;
 RETURN jsonb_build_object('decision_ref',p_ref,'revoke_task_revision',p_revision);
END $$;
ALTER FUNCTION pm_authority.revoke_decision(text,bigint)
 OWNER TO cerebro_pm_authority_owner;
CREATE FUNCTION pm_authority.retire_rights(
 p_ref text,p_revision bigint,p_status text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r pm_authority.rights%ROWTYPE;
BEGIN
 IF p_status NOT IN ('SUPERSEDED','EXPIRED') THEN
   RAISE EXCEPTION 'invalid rights retirement';
 END IF;
 SELECT * INTO r FROM pm_authority.rights WHERE mandate_ref=p_ref FOR UPDATE;
 IF NOT FOUND OR r.status <> 'ACTIVE' OR r.owner_revision <> p_revision THEN
   RAISE EXCEPTION 'stale rights retirement';
 END IF;
 UPDATE pm_authority.rights SET status=p_status,owner_revision=owner_revision+1,
  body=body || jsonb_build_object('owner_revision',p_revision+1,'current',false)
  WHERE mandate_ref=p_ref;
 RETURN jsonb_build_object('mandate_ref',p_ref,'status',p_status,
   'owner_revision',p_revision+1);
END $$;
ALTER FUNCTION pm_authority.retire_rights(text,bigint,text)
 OWNER TO cerebro_pm_authority_owner;
CREATE FUNCTION pm_authority.retire_delegation(
 p_ref text,p_revision bigint,p_status text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE d pm_authority.delegation%ROWTYPE; r pm_authority.rights%ROWTYPE;
BEGIN
 IF p_status NOT IN ('SUPERSEDED','EXPIRED') THEN
   RAISE EXCEPTION 'invalid delegation retirement';
 END IF;
 SELECT * INTO d FROM pm_authority.delegation WHERE delegation_ref=p_ref;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown delegation'; END IF;
 SELECT * INTO r FROM pm_authority.rights WHERE mandate_ref=d.mandate_ref FOR UPDATE;
 SELECT * INTO d FROM pm_authority.delegation WHERE delegation_ref=p_ref FOR UPDATE;
 IF d.status <> 'ACTIVE' OR d.owner_revision <> p_revision THEN
   RAISE EXCEPTION 'stale delegation retirement';
 END IF;
 UPDATE pm_authority.delegation SET status=p_status,
  owner_revision=owner_revision+1,fence_revision=fence_revision+1,
  body=body || jsonb_build_object('owner_revision',p_revision+1,
   'fence_revision',d.fence_revision+1,'current',false)
  WHERE delegation_ref=p_ref;
 RETURN jsonb_build_object('delegation_ref',p_ref,'status',p_status,
   'owner_revision',p_revision+1);
END $$;
ALTER FUNCTION pm_authority.retire_delegation(text,bigint,text)
 OWNER TO cerebro_pm_authority_owner;
CREATE FUNCTION pm_authority.retire_decision(
 p_ref text,p_revision bigint,p_status text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE t pm_authority.decision%ROWTYPE; r pm_authority.rights%ROWTYPE;
        d pm_authority.delegation%ROWTYPE;
BEGIN
 IF p_status NOT IN ('SUPERSEDED','EXPIRED') THEN
   RAISE EXCEPTION 'invalid decision retirement';
 END IF;
 SELECT * INTO t FROM pm_authority.decision WHERE decision_ref=p_ref;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown decision'; END IF;
 SELECT * INTO r FROM pm_authority.rights WHERE mandate_ref=t.mandate_ref FOR UPDATE;
 SELECT * INTO d FROM pm_authority.delegation WHERE delegation_ref=t.delegation_ref FOR UPDATE;
 SELECT * INTO t FROM pm_authority.decision WHERE decision_ref=p_ref FOR UPDATE;
 IF t.status <> 'ACTIVE' OR t.task_revision <> p_revision THEN
   RAISE EXCEPTION 'stale decision retirement';
 END IF;
 UPDATE pm_authority.decision SET status=p_status WHERE decision_ref=p_ref;
 RETURN jsonb_build_object('decision_ref',p_ref,'status',p_status,
   'task_revision',p_revision);
END $$;
ALTER FUNCTION pm_authority.retire_decision(text,bigint,text)
 OWNER TO cerebro_pm_authority_owner;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA pm_authority FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pm_authority.issue_rights(jsonb,timestamptz)
 TO cerebro_human_rights_issuer;
GRANT EXECUTE ON FUNCTION pm_authority.revoke_rights(text,bigint)
 TO cerebro_human_rights_issuer;
GRANT EXECUTE ON FUNCTION pm_authority.issue_delegation(jsonb,timestamptz)
 TO cerebro_pm_admin_issuer;
GRANT EXECUTE ON FUNCTION pm_authority.revoke_delegation(text,bigint)
 TO cerebro_pm_admin_issuer;
GRANT EXECUTE ON FUNCTION pm_authority.issue_decision(
 text,text,bigint,text,text,text,text,text,jsonb,text,timestamptz)
 TO cerebro_pm_decision_issuer;
GRANT EXECUTE ON FUNCTION pm_authority.revoke_decision(text,bigint)
 TO cerebro_pm_decision_issuer;
GRANT EXECUTE ON FUNCTION pm_authority.retire_rights(text,bigint,text)
 TO cerebro_human_rights_issuer;
GRANT EXECUTE ON FUNCTION pm_authority.retire_delegation(text,bigint,text)
 TO cerebro_pm_admin_issuer;
GRANT EXECUTE ON FUNCTION pm_authority.retire_decision(text,bigint,text)
 TO cerebro_pm_decision_issuer;
GRANT EXECUTE ON FUNCTION pm_authority.read_rights(text) TO cerebro_nal_owner;
GRANT EXECUTE ON FUNCTION pm_authority.read_delegation(text,text,text)
 TO cerebro_nal_owner;
GRANT EXECUTE ON FUNCTION pm_authority.acquire_parent_fence(
 jsonb,text,bigint,bigint,bigint) TO cerebro_nal_owner;
COMMIT;
