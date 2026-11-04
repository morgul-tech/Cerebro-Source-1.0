BEGIN;
SET LOCAL search_path = pg_catalog, public, pg_temp;
LOCK TABLE public.cerebro_bk11_service_assignments,
           public.cerebro_bk11_backend_dispositions IN ACCESS EXCLUSIVE MODE;
DO $assignment_family_key$
DECLARE
    old_constraint text;
    old_count integer;
BEGIN
    SELECT count(*), min(conname) INTO old_count, old_constraint
      FROM pg_constraint
     WHERE conrelid = 'public.cerebro_bk11_service_assignments'::regclass
       AND contype = 'u'
       AND pg_get_constraintdef(oid) =
           'UNIQUE (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref)';
    IF old_count <> 1 THEN
        RAISE EXCEPTION 'BK11_EXPECTED_EXACTLY_ONE_LEGACY_SERVICE_ASSIGNMENT_UNIQUE';
    END IF;
    EXECUTE format('ALTER TABLE public.cerebro_bk11_service_assignments DROP CONSTRAINT %I', old_constraint);
END
$assignment_family_key$;
ALTER TABLE public.cerebro_bk11_service_assignments
    ADD CONSTRAINT bk11_service_assignment_scope_family_key
    UNIQUE (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, owner_family);

ALTER TABLE public.cerebro_bk11_backend_dispositions
    ADD COLUMN owner_family text NOT NULL DEFAULT 'PM_OWNER'
    CHECK (owner_family IN ('PM_OWNER', 'ROOM_RETURN'));
ALTER TABLE public.cerebro_bk11_backend_dispositions
    DROP CONSTRAINT cerebro_bk11_backend_dispositions_pkey;
ALTER TABLE public.cerebro_bk11_backend_dispositions
    ADD CONSTRAINT bk11_backend_disposition_scope_family_key
    PRIMARY KEY (tenant_ref, workspace_ref, route_ref, receiver_ref, message_id, owner_family);

DROP POLICY bk11_disposition_read ON public.cerebro_bk11_backend_dispositions;
CREATE POLICY bk11_disposition_read ON public.cerebro_bk11_backend_dispositions FOR SELECT
 USING (EXISTS(SELECT 1 FROM public.cerebro_bk11_service_assignments a
  WHERE a.tenant_ref=cerebro_bk11_backend_dispositions.tenant_ref
   AND a.workspace_ref=cerebro_bk11_backend_dispositions.workspace_ref
   AND a.route_ref=cerebro_bk11_backend_dispositions.route_ref
   AND a.receiver_ref=cerebro_bk11_backend_dispositions.receiver_ref
   AND a.owner_family=cerebro_bk11_backend_dispositions.owner_family
   AND a.enabled AND a.expires_at>now()));
DROP POLICY bk11_disposition_insert ON public.cerebro_bk11_backend_dispositions;
CREATE POLICY bk11_disposition_insert ON public.cerebro_bk11_backend_dispositions FOR INSERT
 WITH CHECK (EXISTS(SELECT 1 FROM public.cerebro_bk11_service_assignments a
  WHERE a.assignment_ref=cerebro_bk11_backend_dispositions.receiver_assignment_ref
   AND a.tenant_ref=cerebro_bk11_backend_dispositions.tenant_ref
   AND a.workspace_ref=cerebro_bk11_backend_dispositions.workspace_ref
   AND a.route_ref=cerebro_bk11_backend_dispositions.route_ref
   AND a.receiver_ref=cerebro_bk11_backend_dispositions.receiver_ref
   AND a.owner_family=cerebro_bk11_backend_dispositions.owner_family
   AND a.service_role='receiver' AND a.enabled AND a.expires_at>now()));
COMMIT;
