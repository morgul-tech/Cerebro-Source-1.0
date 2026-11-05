-- Prospective0009 chosen ONLY after actual observed0001–0008. Not historical0009 replay.
-- Existing runner verifies exactprefix/collision under its migration transaction lock;
-- installation-role membranes/F1 qualification must succeed before the SAME commit.
SET LOCAL search_path = pg_catalog, public, pg_temp;
DO $new_bk11_only$
BEGIN
 IF EXISTS(SELECT 1 FROM unnest(ARRAY['cerebro_pm_owner_d1_outbox_meta','cerebro_pm_owner_d1_routes','cerebro_pm_owner_d1_outbox','cerebro_bk11_service_assignments','cerebro_bk11_service_leases','cerebro_bk11_operation_attempts','cerebro_bk11_backend_dispositions']) relation
           WHERE to_regclass('public.'||relation) IS NOT NULL)
    OR to_regprocedure('public.cerebro_pm_owner_d1_outbox_guard()') IS NOT NULL THEN
  RAISE EXCEPTION 'BK11 unledgered relation/function collision; no silent reuse or rewrite';
 END IF;
END
$new_bk11_only$;
-- BK11 durable PM-owner D1 outbox (candidate). Additive only; no existing relation is altered.
-- DEFAULT OFF: this migration creates NO route row. Without an installation-owned route row with
-- enqueue_enabled=true, commit_pm_owner_ready behaves exactly as before and nothing is enqueued.
-- Rows hold metadata + the exact D1 pointer bytes (a reference to owner truth); never material bodies,
-- mailbox/result copies, tokens or secrets. Pending rows are never deleted (no DELETE policy, delete trigger).
-- Safe rollback = set enqueue_enabled/drain_enabled false on the route (lineage retained); see
-- deployment/proposals/bk11_pm_owner_d1_outbox_disable.sql. No down-migration drops pending obligations.

CREATE TABLE public.cerebro_pm_owner_d1_outbox_meta (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    outbox_schema_version integer NOT NULL CHECK (outbox_schema_version >= 1)
);
INSERT INTO cerebro_pm_owner_d1_outbox_meta (singleton, outbox_schema_version)
VALUES (true, 1) ON CONFLICT (singleton) DO NOTHING;

-- Installation-owned delivery route. The runtime role can only SELECT it (RLS policy below, no write policy);
-- browser/MCP arguments never select a route, stream, receiver, credential or publisher state.
CREATE TABLE public.cerebro_pm_owner_d1_routes (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    project_ref text NOT NULL,
    owner_ref text NOT NULL,
    route_ref text NOT NULL CHECK (route_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}$'),
    receiver_ref text NOT NULL CHECK (receiver_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}$'),
    delivery_class text NOT NULL CHECK (delivery_class = 'D1_POINTER_HINT'),
    subject text NOT NULL CHECK (subject ~ '^[A-Za-z0-9_.-]{1,128}$'),
    stream text NOT NULL CHECK (stream ~ '^[A-Za-z0-9_-]{1,64}$'),
    x9_config_ref text NOT NULL,
    receiver_assignment_ref text NOT NULL,
    publisher_principal_ref text NOT NULL,
    publisher_consumer_ref text NOT NULL,
    publisher_session_ref text NOT NULL,
    publisher_session_binding_id text NOT NULL,
    pointer_ttl_seconds integer NOT NULL CHECK (pointer_ttl_seconds BETWEEN 1 AND 120),
    binding_revision bigint NOT NULL CHECK (binding_revision >= 1),
    enqueue_enabled boolean NOT NULL DEFAULT false,
    drain_enabled boolean NOT NULL DEFAULT false,
    source_ref text NOT NULL CHECK (length(trim(source_ref)) > 0),
    PRIMARY KEY (tenant_ref, workspace_ref, owner_ref, route_ref),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref)
        REFERENCES cerebro_project_instances (tenant_ref, workspace_ref, project_ref)
);
ALTER TABLE cerebro_pm_owner_d1_routes ENABLE ROW LEVEL SECURITY;
CREATE POLICY d1_route_read ON cerebro_pm_owner_d1_routes FOR SELECT USING (
    tenant_ref = current_setting('cerebro.tenant_ref', true)
    AND workspace_ref = current_setting('cerebro.workspace_ref', true));

CREATE TABLE public.cerebro_pm_owner_d1_outbox (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    project_ref text NOT NULL,
    owner_ref text NOT NULL,
    route_ref text NOT NULL,
    receiver_ref text NOT NULL,
    delivery_class text NOT NULL CHECK (delivery_class = 'D1_POINTER_HINT'),
    binding_revision bigint NOT NULL CHECK (binding_revision >= 1),
    -- original canonical PM identity (never replaced)
    event_id text NOT NULL,
    receipt_ref text NOT NULL,
    commit_ref text NOT NULL,
    referent_id text NOT NULL,
    revision_after text NOT NULL,                       -- PM opaque string revision
    owner_seq bigint NOT NULL CHECK (owner_seq >= 1),   -- PM owner sequence == D1 numeric owner_revision
    snapshot_sha256 text NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    packet_sha256 text NOT NULL CHECK (packet_sha256 ~ '^[0-9a-f]{64}$'),
    -- derived D1 transport identity + exact immutable wire bytes
    message_id text NOT NULL CHECK (message_id ~ '^d1:[0-9a-f]{40}$'),
    pointer bytea NOT NULL CHECK (octet_length(pointer) BETWEEN 1 AND 4096),
    pointer_sha256 text NOT NULL CHECK (pointer_sha256 ~ '^[0-9a-f]{64}$'),
    mapping_payload jsonb NOT NULL CHECK (jsonb_typeof(mapping_payload) = 'object'),
    mapping_sha256 text NOT NULL CHECK (mapping_sha256 ~ '^[0-9a-f]{64}$'),
    created_at text NOT NULL,
    expires_at text NOT NULL,
    -- publication obligation state (D1 publisher claim-before-send compare-and-set)
    state text NOT NULL CHECK (state IN ('INTENDED', 'SEND_CLAIMED', 'PUBLISHED', 'UNKNOWN_PENDING',
                                         'REJECTED_CAPACITY', 'CONFLICT_HOLD', 'RECONCILE_HOLD')),
    claim_token text,
    claimed_at text,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    stream text,
    stream_seq bigint CHECK (stream_seq IS NULL OR stream_seq >= 1),
    duplicate boolean,
    puback_latency_ms double precision,
    last_reason text,
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, owner_ref, event_id, route_ref),
    UNIQUE (tenant_ref, workspace_ref, message_id),
    CHECK (pointer_sha256 = encode(sha256(pointer), 'hex')),
    CHECK (state <> 'PUBLISHED' OR (stream IS NOT NULL AND stream_seq IS NOT NULL)),
    FOREIGN KEY (tenant_ref, workspace_ref, owner_ref, receipt_ref)
        REFERENCES cerebro_pm_owner_receipts (tenant_ref, workspace_ref, owner_ref, receipt_ref),
    FOREIGN KEY (tenant_ref, workspace_ref, owner_ref, route_ref)
        REFERENCES cerebro_pm_owner_d1_routes (tenant_ref, workspace_ref, owner_ref, route_ref)
);
CREATE INDEX cerebro_pm_owner_d1_outbox_pending
    ON cerebro_pm_owner_d1_outbox (tenant_ref, workspace_ref, route_ref, enqueued_at, event_id)
    WHERE state <> 'PUBLISHED';

-- Identity/bytes immutable; forward-only publication state machine; no delete (pending lineage retained).
CREATE FUNCTION public.cerebro_pm_owner_d1_outbox_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $d1_outbox_guard$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'cerebro D1 outbox obligations cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF (NEW.tenant_ref, NEW.workspace_ref, NEW.project_ref, NEW.owner_ref, NEW.route_ref, NEW.receiver_ref,
        NEW.delivery_class, NEW.binding_revision, NEW.event_id, NEW.receipt_ref, NEW.commit_ref, NEW.referent_id,
        NEW.revision_after, NEW.owner_seq, NEW.snapshot_sha256, NEW.packet_sha256, NEW.message_id, NEW.pointer,
        NEW.pointer_sha256, NEW.mapping_payload, NEW.mapping_sha256, NEW.created_at, NEW.expires_at, NEW.enqueued_at)
       IS DISTINCT FROM
       (OLD.tenant_ref, OLD.workspace_ref, OLD.project_ref, OLD.owner_ref, OLD.route_ref, OLD.receiver_ref,
        OLD.delivery_class, OLD.binding_revision, OLD.event_id, OLD.receipt_ref, OLD.commit_ref, OLD.referent_id,
        OLD.revision_after, OLD.owner_seq, OLD.snapshot_sha256, OLD.packet_sha256, OLD.message_id, OLD.pointer,
        OLD.pointer_sha256, OLD.mapping_payload, OLD.mapping_sha256, OLD.created_at, OLD.expires_at, OLD.enqueued_at)
    THEN
        RAISE EXCEPTION 'cerebro D1 outbox identity and bytes are immutable' USING ERRCODE = '55000';
    END IF;
    IF NEW.attempts < OLD.attempts THEN
        RAISE EXCEPTION 'cerebro D1 outbox attempts cannot decrease' USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (OLD.state = 'INTENDED' AND NEW.state IN ('SEND_CLAIMED', 'CONFLICT_HOLD'))
        OR (OLD.state = 'SEND_CLAIMED' AND NEW.state IN ('PUBLISHED', 'UNKNOWN_PENDING', 'REJECTED_CAPACITY',
                                                       'CONFLICT_HOLD', 'RECONCILE_HOLD'))
        OR (OLD.state = 'SEND_CLAIMED' AND NEW.state = 'SEND_CLAIMED'
            AND NEW.claim_token IS DISTINCT FROM OLD.claim_token)
        OR (OLD.state IN ('UNKNOWN_PENDING', 'RECONCILE_HOLD')
            AND NEW.state IN ('SEND_CLAIMED', 'PUBLISHED', 'CONFLICT_HOLD', 'RECONCILE_HOLD'))
    ) THEN
        RAISE EXCEPTION 'cerebro D1 outbox transition % -> % refused', OLD.state, NEW.state
            USING ERRCODE = '55000';
    END IF;
    IF NEW.state = 'SEND_CLAIMED' AND (NEW.claim_token IS NULL OR NEW.claimed_at IS NULL
                                       OR NEW.attempts <> OLD.attempts + 1) THEN
        RAISE EXCEPTION 'cerebro D1 outbox claim requires token, time and one attempt' USING ERRCODE = '55000';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END
$d1_outbox_guard$;
CREATE TRIGGER cerebro_pm_owner_d1_outbox_guard
    BEFORE UPDATE OR DELETE ON cerebro_pm_owner_d1_outbox
    FOR EACH ROW EXECUTE FUNCTION cerebro_pm_owner_d1_outbox_guard();

ALTER TABLE cerebro_pm_owner_d1_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_pm_owner_d1_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY d1_outbox_read ON cerebro_pm_owner_d1_outbox FOR SELECT USING (
    tenant_ref = current_setting('cerebro.tenant_ref', true)
    AND workspace_ref = current_setting('cerebro.workspace_ref', true));
-- Outer-row columns are table-qualified: inside EXISTS an unqualified name would bind to routes r.
-- Insert only from the PM owner commit transaction, for an enabled installation route.
CREATE POLICY d1_outbox_enqueue ON cerebro_pm_owner_d1_outbox FOR INSERT WITH CHECK (
    tenant_ref = current_setting('cerebro.tenant_ref', true)
    AND workspace_ref = current_setting('cerebro.workspace_ref', true)
    AND current_setting('cerebro.d1_actor', true) = 'pm_owner_commit'
    AND state = 'INTENDED' AND attempts = 0 AND claim_token IS NULL
    AND EXISTS (SELECT 1 FROM cerebro_pm_owner_d1_routes r
                WHERE (r.tenant_ref, r.workspace_ref, r.owner_ref, r.route_ref, r.receiver_ref, r.binding_revision)
                    = (cerebro_pm_owner_d1_outbox.tenant_ref, cerebro_pm_owner_d1_outbox.workspace_ref, cerebro_pm_owner_d1_outbox.owner_ref,
                       cerebro_pm_owner_d1_outbox.route_ref, cerebro_pm_owner_d1_outbox.receiver_ref, cerebro_pm_owner_d1_outbox.binding_revision)
                  AND r.enqueue_enabled));
-- Publication-state updates only by the route's authenticated publisher while draining is enabled.
CREATE POLICY d1_outbox_settle ON cerebro_pm_owner_d1_outbox FOR UPDATE USING (
    tenant_ref = current_setting('cerebro.tenant_ref', true)
    AND workspace_ref = current_setting('cerebro.workspace_ref', true)
    AND current_setting('cerebro.d1_actor', true) = 'd1_publisher'
    AND EXISTS (SELECT 1 FROM cerebro_pm_owner_d1_routes r
                WHERE (r.tenant_ref, r.workspace_ref, r.owner_ref, r.route_ref)
                    = (cerebro_pm_owner_d1_outbox.tenant_ref, cerebro_pm_owner_d1_outbox.workspace_ref, cerebro_pm_owner_d1_outbox.owner_ref,
                       cerebro_pm_owner_d1_outbox.route_ref)
                  AND r.publisher_principal_ref = current_setting('cerebro.principal_ref', true)
                  AND r.drain_enabled))
WITH CHECK (
    tenant_ref = current_setting('cerebro.tenant_ref', true)
    AND workspace_ref = current_setting('cerebro.workspace_ref', true)
    AND current_setting('cerebro.d1_actor', true) = 'd1_publisher');
-- No DELETE policy.

-- All-state bounded ownertruth recovery needs an index including PUBLISHED duties.
CREATE INDEX cerebro_pm_owner_d1_outbox_recovery ON cerebro_pm_owner_d1_outbox
 (tenant_ref,workspace_ref,owner_ref,route_ref,receiver_ref,enqueued_at,event_id);
-- INERT PROPOSAL, NO MIGRATION SLOT OR MANIFEST ENTRY.
-- Compose ONLY after actual Context authority/BOOT0008/installed-ledger qualification.
-- Installation owner provisions assignment rows and exact runtime grants. Runtime
-- must not INSERT/UPDATE/DELETE assignments or self-provision identities/leases policy.
CREATE TABLE public.cerebro_bk11_service_assignments (
 assignment_ref text PRIMARY KEY,
 tenant_ref text NOT NULL, workspace_ref text NOT NULL, project_ref text NOT NULL,
 principal_ref text NOT NULL, consumer_ref text NOT NULL, session_ref text NOT NULL,
 session_binding_id text NOT NULL, route_ref text NOT NULL, owner_ref text NOT NULL,
 service_role text NOT NULL CHECK(service_role IN ('publisher','receiver')),
 receiver_ref text NOT NULL, profile text NOT NULL,
 enabled boolean NOT NULL DEFAULT false, assignment_revision bigint NOT NULL CHECK(assignment_revision>0),
 lease_ttl_s integer NOT NULL CHECK(lease_ttl_s BETWEEN 10 AND 600),
 expires_at timestamptz NOT NULL,
 UNIQUE(tenant_ref,workspace_ref,principal_ref,consumer_ref,session_ref)
);
CREATE TABLE public.cerebro_bk11_service_leases (
 assignment_ref text PRIMARY KEY REFERENCES cerebro_bk11_service_assignments(assignment_ref),
 lease_ref text NOT NULL UNIQUE, instance_ref text NOT NULL,
 fencing_epoch bigint NOT NULL CHECK(fencing_epoch>0), assignment_revision bigint NOT NULL,
 expires_at timestamptz NOT NULL
);
CREATE TABLE public.cerebro_bk11_operation_attempts (
 assignment_ref text NOT NULL REFERENCES cerebro_bk11_service_assignments(assignment_ref),
 operation_ref text NOT NULL, attempt_ref text NOT NULL,
 phase text NOT NULL, obligations jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(obligations)='array' AND jsonb_array_length(obligations)<=8),
 outcome text NOT NULL DEFAULT 'OPEN', observed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(assignment_ref,attempt_ref)
);
CREATE TABLE public.cerebro_bk11_backend_dispositions (
 tenant_ref text NOT NULL, workspace_ref text NOT NULL, route_ref text NOT NULL, receiver_ref text NOT NULL,
 message_id text NOT NULL CHECK(message_id ~ '^d1:[0-9a-f]{40}$'),
 pointer_sha256 text NOT NULL CHECK(pointer_sha256 ~ '^[0-9a-f]{64}$'),
 owner_event_id text NOT NULL, owner_seq bigint NOT NULL,
 status text NOT NULL CHECK(status IN ('ACCEPTED_PENDING_WORK','EXPIRED_NEEDS_OWNER_DISPOSITION')),
 receiver_assignment_ref text NOT NULL REFERENCES cerebro_bk11_service_assignments(assignment_ref),
 accepted_at timestamptz NOT NULL DEFAULT now(),
 downstream_work text NOT NULL DEFAULT 'PENDING_NOT_MODEL_EXECUTED' CHECK(downstream_work='PENDING_NOT_MODEL_EXECUTED'),
 PRIMARY KEY(tenant_ref,workspace_ref,route_ref,receiver_ref,message_id)
);
-- No body, token, password, arbitrary cursor or substitute business event is stored.
-- All four tables require FORCE RLS + exact service runtime grants before admission.
ALTER TABLE cerebro_bk11_service_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_bk11_service_assignments FORCE ROW LEVEL SECURITY;
ALTER TABLE cerebro_bk11_service_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_bk11_service_leases FORCE ROW LEVEL SECURITY;
ALTER TABLE cerebro_bk11_operation_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_bk11_operation_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE cerebro_bk11_backend_dispositions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_bk11_backend_dispositions FORCE ROW LEVEL SECURITY;
REVOKE ALL ON cerebro_bk11_service_assignments,cerebro_bk11_service_leases,
 cerebro_bk11_operation_attempts,cerebro_bk11_backend_dispositions FROM PUBLIC;
-- DENY BY DEFAULT: no policy/grant here until actual installation-role/runtime-role
-- binding is qualified. Never let generic reprovision GRANT ALL override this membrane.


-- Assignment policy permits SELECT only. Runtime never creates/enables/rebinds it.
CREATE POLICY bk11_assignment_read ON cerebro_bk11_service_assignments FOR SELECT
 USING (tenant_ref=current_setting('cerebro.tenant_ref',true)
  AND workspace_ref=current_setting('cerebro.workspace_ref',true)
  AND principal_ref=current_setting('cerebro.principal_ref',true));
CREATE POLICY bk11_lease_scope ON cerebro_bk11_service_leases
 USING (EXISTS(SELECT 1 FROM cerebro_bk11_service_assignments a
  WHERE a.assignment_ref=cerebro_bk11_service_leases.assignment_ref AND a.enabled AND a.expires_at>now()))
 WITH CHECK (EXISTS(SELECT 1 FROM cerebro_bk11_service_assignments a
  WHERE a.assignment_ref=cerebro_bk11_service_leases.assignment_ref AND a.enabled AND a.expires_at>now()
   AND a.assignment_revision=cerebro_bk11_service_leases.assignment_revision));
CREATE POLICY bk11_attempt_scope ON cerebro_bk11_operation_attempts
 USING (EXISTS(SELECT 1 FROM cerebro_bk11_service_assignments a
  WHERE a.assignment_ref=cerebro_bk11_operation_attempts.assignment_ref AND a.enabled AND a.expires_at>now()))
 WITH CHECK (EXISTS(SELECT 1 FROM cerebro_bk11_service_assignments a
  WHERE a.assignment_ref=cerebro_bk11_operation_attempts.assignment_ref AND a.enabled AND a.expires_at>now()));
CREATE POLICY bk11_disposition_read ON cerebro_bk11_backend_dispositions FOR SELECT
 USING (EXISTS(SELECT 1 FROM cerebro_bk11_service_assignments a
  WHERE a.tenant_ref=cerebro_bk11_backend_dispositions.tenant_ref
   AND a.workspace_ref=cerebro_bk11_backend_dispositions.workspace_ref
   AND a.route_ref=cerebro_bk11_backend_dispositions.route_ref
   AND a.receiver_ref=cerebro_bk11_backend_dispositions.receiver_ref AND a.enabled AND a.expires_at>now()));
CREATE POLICY bk11_disposition_insert ON cerebro_bk11_backend_dispositions FOR INSERT
 WITH CHECK (EXISTS(SELECT 1 FROM cerebro_bk11_service_assignments a
  WHERE a.assignment_ref=cerebro_bk11_backend_dispositions.receiver_assignment_ref
   AND a.tenant_ref=cerebro_bk11_backend_dispositions.tenant_ref
   AND a.workspace_ref=cerebro_bk11_backend_dispositions.workspace_ref
   AND a.route_ref=cerebro_bk11_backend_dispositions.route_ref
   AND a.receiver_ref=cerebro_bk11_backend_dispositions.receiver_ref
   AND a.service_role='receiver' AND a.enabled AND a.expires_at>now()));
-- No UPDATE/DELETE disposition policy; original identity and durable custody are immutable.
-- Reprovision must reapply exact grants (policies created ONCE by admitted migration),
-- not replay this CREATE POLICY file against existing names. Disable retains all rows.
