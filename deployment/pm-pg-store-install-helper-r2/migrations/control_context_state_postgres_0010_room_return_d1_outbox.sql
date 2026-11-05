-- Candidate 0010: one immutable ROOM_RETURN record and its existing D1 outbox duty.
-- Install only after exact P23 candidate/binding/effect admission. 0001-0009 stay byte-identical.
CREATE SEQUENCE public.cerebro_room_return_ref_seq;
CREATE TABLE public.cerebro_room_return_d1_outbox (
 room_return_ref text PRIMARY KEY DEFAULT ('rr:'||nextval('public.cerebro_room_return_ref_seq')),
 owner_seq bigserial NOT NULL UNIQUE CHECK(owner_seq>0),
 tenant_ref text NOT NULL, workspace_ref text NOT NULL, project_ref text NOT NULL,
 owner_ref text NOT NULL, assignment_ref text NOT NULL,
 route_owner_ref text NOT NULL, route_ref text NOT NULL, receiver_ref text NOT NULL,
 producer_principal_ref text NOT NULL, producer_consumer_ref text NOT NULL, producer_session_ref text NOT NULL,
 producer_session_binding_id text NOT NULL,
 recipient_ref text NOT NULL, task_ref text NOT NULL, material_revision text NOT NULL,
 source_original_ref text NOT NULL, expected_action text NOT NULL,
 parent_sha256 text NOT NULL CHECK(parent_sha256 ~ '^[a-f0-9]{64}$'),
 material_sha256 text NOT NULL CHECK(material_sha256 ~ '^[a-f0-9]{64}$'),
 envelope_sha256 text NOT NULL CHECK(envelope_sha256 ~ '^[a-f0-9]{64}$'),
 material_bytes integer NOT NULL CHECK(material_bytes BETWEEN 1 AND 16384),
 material text NOT NULL, envelope text NOT NULL,
 idempotency_key text NOT NULL, registration_key text NOT NULL CHECK(registration_key ~ '^[a-f0-9]{64}$'),
 event_fingerprint text NOT NULL CHECK(event_fingerprint ~ '^[a-f0-9]{64}$'),
 owner_event_key text NOT NULL, binding_revision bigint NOT NULL CHECK(binding_revision>0),
 message_id text UNIQUE CHECK(message_id IS NULL OR message_id ~ '^d1:[a-f0-9]{40}$'),
 pointer bytea, pointer_sha256 text CHECK(pointer_sha256 IS NULL OR pointer_sha256 ~ '^[a-f0-9]{64}$'),
 state text NOT NULL CHECK(state IN ('INTENDED','SEND_CLAIMED','PUBLISHED','UNKNOWN_PENDING',
   'REJECTED_CAPACITY','CONFLICT_HOLD','RECONCILE_HOLD')),
 claim_token text, claimed_at timestamptz, attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 stream text, stream_seq bigint, duplicate boolean, puback_latency_ms double precision, last_reason text,
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 enqueued_at timestamptz NOT NULL DEFAULT now(),
 recipient_action text NOT NULL DEFAULT 'PENDING' CHECK(recipient_action IN
   ('PENDING','HANDOFF_INTENDED','HANDOFF_UNKNOWN','DISPOSITION_OBSERVED','HOLD')),
 handoff_ref text, recipient_turn_ref text, disposition_sha256 text,
 UNIQUE(tenant_ref,workspace_ref,owner_ref,idempotency_key),
 UNIQUE(tenant_ref,workspace_ref,owner_ref,registration_key),
 CHECK(material_bytes=octet_length(convert_to(material,'UTF8'))),
 CHECK(expires_at>created_at AND expires_at<=created_at+interval '120 seconds'),
 CHECK((pointer IS NULL AND message_id IS NULL AND pointer_sha256 IS NULL)
    OR (pointer IS NOT NULL AND message_id IS NOT NULL AND pointer_sha256 IS NOT NULL))
);
ALTER TABLE public.cerebro_bk11_service_assignments
 ADD COLUMN owner_family text NOT NULL DEFAULT 'PM_OWNER'
   CHECK(owner_family IN ('PM_OWNER','ROOM_RETURN')),
 ADD COLUMN room_owner_ref text,
 ADD COLUMN route_binding_revision bigint,
 ADD COLUMN pointer_ttl_seconds integer,
 ADD CONSTRAINT bk11_owner_family_room_ref CHECK(
   (owner_family='PM_OWNER' AND room_owner_ref IS NULL)
   OR (owner_family='ROOM_RETURN' AND room_owner_ref IS NOT NULL
     AND route_binding_revision>0 AND pointer_ttl_seconds BETWEEN 1 AND 120));
CREATE FUNCTION public.cerebro_bk11_owner_family_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.owner_family,NEW.room_owner_ref,NEW.route_binding_revision,NEW.pointer_ttl_seconds)
    IS DISTINCT FROM ROW(OLD.owner_family,OLD.room_owner_ref,OLD.route_binding_revision,OLD.pointer_ttl_seconds)
    AND NEW.assignment_revision<=OLD.assignment_revision THEN
  RAISE EXCEPTION 'BK11_OWNER_FAMILY_REQUIRES_NEW_ASSIGNMENT_REVISION';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER bk11_owner_family_revision_guard BEFORE UPDATE ON public.cerebro_bk11_service_assignments
 FOR EACH ROW EXECUTE FUNCTION public.cerebro_bk11_owner_family_revision_guard();
ALTER TABLE public.cerebro_room_return_d1_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cerebro_room_return_d1_outbox FORCE ROW LEVEL SECURITY;
REVOKE ALL ON public.cerebro_room_return_d1_outbox, public.cerebro_room_return_ref_seq FROM PUBLIC;
CREATE POLICY room_return_producer_read ON public.cerebro_room_return_d1_outbox FOR SELECT USING (
 tenant_ref=current_setting('cerebro.tenant_ref',true)
 AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND producer_principal_ref=current_setting('cerebro.principal_ref',true)
 );
CREATE POLICY room_return_producer_insert ON public.cerebro_room_return_d1_outbox FOR INSERT WITH CHECK (
 tenant_ref=current_setting('cerebro.tenant_ref',true)
 AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND producer_principal_ref=current_setting('cerebro.principal_ref',true)
 );
CREATE POLICY room_return_service_read ON public.cerebro_room_return_d1_outbox FOR SELECT USING (
 EXISTS (SELECT 1 FROM public.cerebro_bk11_service_assignments a
  WHERE a.principal_ref=current_setting('cerebro.principal_ref',true)
   AND a.owner_family='ROOM_RETURN' AND a.enabled AND a.expires_at>now()
   AND a.tenant_ref=cerebro_room_return_d1_outbox.tenant_ref
   AND a.workspace_ref=cerebro_room_return_d1_outbox.workspace_ref
   AND a.route_ref=cerebro_room_return_d1_outbox.route_ref
   AND a.owner_ref=cerebro_room_return_d1_outbox.route_owner_ref
   AND a.room_owner_ref=cerebro_room_return_d1_outbox.owner_ref
   AND a.receiver_ref=cerebro_room_return_d1_outbox.receiver_ref));
CREATE POLICY room_return_service_settle ON public.cerebro_room_return_d1_outbox FOR UPDATE USING (
 EXISTS (SELECT 1 FROM public.cerebro_bk11_service_assignments a
  WHERE a.principal_ref=current_setting('cerebro.principal_ref',true)
   AND a.owner_family='ROOM_RETURN' AND a.service_role='publisher' AND a.enabled AND a.expires_at>now()
   AND a.tenant_ref=cerebro_room_return_d1_outbox.tenant_ref
   AND a.workspace_ref=cerebro_room_return_d1_outbox.workspace_ref
   AND a.route_ref=cerebro_room_return_d1_outbox.route_ref
   AND a.owner_ref=cerebro_room_return_d1_outbox.route_owner_ref
   AND a.room_owner_ref=cerebro_room_return_d1_outbox.owner_ref
   AND a.receiver_ref=cerebro_room_return_d1_outbox.receiver_ref
   AND EXISTS (SELECT 1 FROM public.cerebro_pm_owner_d1_routes r
    WHERE r.tenant_ref=a.tenant_ref AND r.workspace_ref=a.workspace_ref AND r.owner_ref=a.owner_ref
     AND r.route_ref=a.route_ref AND r.receiver_ref=a.receiver_ref
     AND r.publisher_principal_ref=a.principal_ref AND r.publisher_consumer_ref=a.consumer_ref
     AND r.publisher_session_ref=a.session_ref AND r.publisher_session_binding_id=a.session_binding_id
     AND r.binding_revision=a.route_binding_revision AND r.pointer_ttl_seconds=a.pointer_ttl_seconds
     AND r.drain_enabled)))
 WITH CHECK (EXISTS (SELECT 1 FROM public.cerebro_bk11_service_assignments a
  WHERE a.principal_ref=current_setting('cerebro.principal_ref',true)
   AND a.owner_family='ROOM_RETURN' AND a.service_role='publisher' AND a.enabled AND a.expires_at>now()
   AND EXISTS (SELECT 1 FROM public.cerebro_pm_owner_d1_routes r
    WHERE r.tenant_ref=a.tenant_ref AND r.workspace_ref=a.workspace_ref AND r.owner_ref=a.owner_ref
     AND r.route_ref=a.route_ref AND r.receiver_ref=a.receiver_ref
     AND r.publisher_principal_ref=a.principal_ref AND r.publisher_consumer_ref=a.consumer_ref
     AND r.publisher_session_ref=a.session_ref AND r.publisher_session_binding_id=a.session_binding_id
     AND r.binding_revision=a.route_binding_revision AND r.pointer_ttl_seconds=a.pointer_ttl_seconds
     AND r.drain_enabled)));
CREATE FUNCTION public.cerebro_room_return_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'ROOM_RETURN_D1 obligations cannot be deleted'; END IF;
 IF ROW(NEW.room_return_ref,NEW.owner_seq,NEW.tenant_ref,NEW.workspace_ref,NEW.project_ref,NEW.owner_ref,
   NEW.assignment_ref,NEW.route_owner_ref,NEW.route_ref,NEW.receiver_ref,NEW.producer_principal_ref,
   NEW.producer_consumer_ref,NEW.producer_session_ref,NEW.producer_session_binding_id,NEW.recipient_ref,NEW.task_ref,NEW.material_revision,
   NEW.source_original_ref,NEW.expected_action,NEW.parent_sha256,NEW.material_sha256,NEW.envelope_sha256,
   NEW.material_bytes,NEW.material,NEW.envelope,NEW.idempotency_key,NEW.registration_key,NEW.event_fingerprint,
   NEW.owner_event_key,NEW.binding_revision,NEW.message_id,NEW.pointer,NEW.pointer_sha256,NEW.created_at,NEW.expires_at)
   IS DISTINCT FROM
   ROW(OLD.room_return_ref,OLD.owner_seq,OLD.tenant_ref,OLD.workspace_ref,OLD.project_ref,OLD.owner_ref,
   OLD.assignment_ref,OLD.route_owner_ref,OLD.route_ref,OLD.receiver_ref,OLD.producer_principal_ref,
   OLD.producer_consumer_ref,OLD.producer_session_ref,OLD.producer_session_binding_id,OLD.recipient_ref,OLD.task_ref,OLD.material_revision,
   OLD.source_original_ref,OLD.expected_action,OLD.parent_sha256,OLD.material_sha256,OLD.envelope_sha256,
   OLD.material_bytes,OLD.material,OLD.envelope,OLD.idempotency_key,OLD.registration_key,OLD.event_fingerprint,
   OLD.owner_event_key,OLD.binding_revision,OLD.message_id,OLD.pointer,OLD.pointer_sha256,OLD.created_at,OLD.expires_at)
 THEN RAISE EXCEPTION 'ROOM_RETURN_IMMUTABLE_OWNER_RECORD'; END IF;
 IF NEW.attempts<OLD.attempts THEN RAISE EXCEPTION 'ROOM_RETURN_ATTEMPTS_CANNOT_DECREASE'; END IF;
 IF NOT ((OLD.state='INTENDED' AND NEW.state IN ('SEND_CLAIMED','CONFLICT_HOLD'))
   OR (OLD.state='SEND_CLAIMED' AND NEW.state IN ('PUBLISHED','UNKNOWN_PENDING','REJECTED_CAPACITY','CONFLICT_HOLD','RECONCILE_HOLD'))
   OR (OLD.state='SEND_CLAIMED' AND NEW.state='SEND_CLAIMED' AND NEW.claim_token IS DISTINCT FROM OLD.claim_token)
   OR (OLD.state IN ('UNKNOWN_PENDING','RECONCILE_HOLD') AND NEW.state IN ('SEND_CLAIMED','PUBLISHED','CONFLICT_HOLD','RECONCILE_HOLD')))
 THEN RAISE EXCEPTION 'ROOM_RETURN_D1_TRANSITION_REFUSED'; END IF;
 IF NEW.state='SEND_CLAIMED' AND (NEW.claim_token IS NULL OR NEW.claimed_at IS NULL OR NEW.attempts<>OLD.attempts+1)
 THEN RAISE EXCEPTION 'ROOM_RETURN_D1_CLAIM_REQUIRES_TOKEN_TIME_ATTEMPT'; END IF;
 IF NEW.state='PUBLISHED' AND (NEW.stream IS NULL OR NEW.stream_seq IS NULL OR NEW.stream_seq<=0
   OR NEW.duplicate IS NULL OR NEW.puback_latency_ms IS NULL OR NEW.puback_latency_ms<0)
 THEN RAISE EXCEPTION 'ROOM_RETURN_D1_PUBLISHED_REQUIRES_PUBACK'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER room_return_immutable_guard BEFORE UPDATE OR DELETE ON public.cerebro_room_return_d1_outbox
 FOR EACH ROW EXECUTE FUNCTION public.cerebro_room_return_immutable_guard();
CREATE INDEX room_return_service_recovery ON public.cerebro_room_return_d1_outbox
 (tenant_ref,workspace_ref,route_owner_ref,route_ref,receiver_ref,enqueued_at,owner_event_key);
-- Runtime grants, owner-family assignment, and exact current producer assignment are deliberately not provisioned here.
-- A1/P23 must qualify those against the actual target before any effect.
