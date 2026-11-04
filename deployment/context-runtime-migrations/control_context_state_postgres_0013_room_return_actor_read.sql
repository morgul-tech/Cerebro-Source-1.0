-- Additive 0013, installer-owned exact actor read attenuation only.
-- No new ledger/table, state update, role grant, lease or business disposition.
-- Host validates actual identity/current/project and installs the exact immutable
-- grant into a transaction-local GUC, like the existing identity RLS boundary.
-- Runtime credentials remain trusted server custody; GUCs are not cryptographic proofs.
-- Runtime SELECT on both existing tables must already be qualified separately.
CREATE POLICY room_return_intended_actor_read ON public.cerebro_room_return_d1_outbox
 FOR SELECT USING (
 tenant_ref=current_setting('cerebro.tenant_ref',true)
 AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND (COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'reader_principal_ref')=current_setting('cerebro.principal_ref',true)
 AND now()>=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'read_not_before')::timestamptz
 AND now()<(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'read_expires_at')::timestamptz
 AND tenant_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'tenant_ref')
 AND workspace_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'workspace_ref')
 AND project_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'project_ref')
 AND owner_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'owner_ref')
 AND assignment_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'assignment_ref')
 AND route_owner_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'route_owner_ref')
 AND route_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'route_ref')
 AND receiver_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'receiver_ref')
 AND recipient_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'recipient_ref')
 AND task_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'task_ref')
 AND material_revision::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'material_revision')
 AND source_original_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'source_original_ref')
 AND expected_action::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'expected_action')
 AND parent_sha256::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'parent_sha256')
 AND material_sha256::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'material_sha256')
 AND envelope_sha256::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'envelope_sha256')
 AND producer_principal_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'producer_principal_ref')
 AND producer_consumer_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'producer_consumer_ref')
 AND producer_session_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'producer_session_ref')
 AND producer_session_binding_id::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'producer_session_binding_id')
 AND binding_revision::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'binding_revision'));
CREATE POLICY bk11_intended_actor_accepted_read ON public.cerebro_bk11_backend_dispositions
 FOR SELECT USING (
 owner_family='ROOM_RETURN'
 AND status IN ('ACCEPTED_PENDING_WORK','EXPIRED_NEEDS_OWNER_DISPOSITION')
 AND EXISTS (SELECT 1 FROM public.cerebro_room_return_d1_outbox o
   WHERE o.tenant_ref=cerebro_bk11_backend_dispositions.tenant_ref
    AND o.workspace_ref=cerebro_bk11_backend_dispositions.workspace_ref
    AND o.route_ref=cerebro_bk11_backend_dispositions.route_ref
    AND o.receiver_ref=cerebro_bk11_backend_dispositions.receiver_ref
    AND o.message_id=cerebro_bk11_backend_dispositions.message_id
    AND o.pointer_sha256=cerebro_bk11_backend_dispositions.pointer_sha256
    AND o.owner_event_key=cerebro_bk11_backend_dispositions.owner_event_id
    AND o.owner_seq=cerebro_bk11_backend_dispositions.owner_seq
    AND receiver_assignment_ref=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'receiver_assignment_ref')
    AND (COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'reader_principal_ref')=current_setting('cerebro.principal_ref',true)
    AND now()>=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'read_not_before')::timestamptz
    AND now()<(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'read_expires_at')::timestamptz
    AND o.tenant_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'tenant_ref')
    AND o.workspace_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'workspace_ref')
    AND o.project_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'project_ref')
    AND o.owner_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'owner_ref')
    AND o.assignment_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'assignment_ref')
    AND o.route_owner_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'route_owner_ref')
    AND o.route_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'route_ref')
    AND o.receiver_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'receiver_ref')
    AND o.recipient_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'recipient_ref')
    AND o.task_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'task_ref')
    AND o.material_revision::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'material_revision')
    AND o.source_original_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'source_original_ref')
    AND o.expected_action::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'expected_action')
    AND o.parent_sha256::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'parent_sha256')
    AND o.material_sha256::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'material_sha256')
    AND o.envelope_sha256::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'envelope_sha256')
    AND o.producer_principal_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'producer_principal_ref')
    AND o.producer_consumer_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'producer_consumer_ref')
    AND o.producer_session_ref::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'producer_session_ref')
    AND o.producer_session_binding_id::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'producer_session_binding_id')
    AND o.binding_revision::text=(COALESCE(NULLIF(current_setting('cerebro.room_return_actor_read',true),''),'{}')::jsonb->>'binding_revision')));
