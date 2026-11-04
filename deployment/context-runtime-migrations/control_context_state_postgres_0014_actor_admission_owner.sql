-- 0014: typed project-owner admission data, not another grant/task database.
-- Existing runtime credential remains server-held. All GUCs below are set from
-- verified caller + constructor-bound issuer or committed owner records only.
-- No role, table, scheduler, native enum, session identity or service assignment is created.
ALTER TABLE public.cerebro_bk11_service_assignments
 ADD COLUMN room_return_scope text NOT NULL DEFAULT 'EXACT_OWNER'
 CHECK (room_return_scope IN ('EXACT_OWNER','PROJECT_ADMITTED_ACTORS')),
 ADD CONSTRAINT actor_project_room_scope CHECK
 (room_return_scope='EXACT_OWNER' OR owner_family='ROOM_RETURN');
CREATE FUNCTION public.cerebro_room_scope_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.room_return_scope IS DISTINCT FROM OLD.room_return_scope
   AND NEW.assignment_revision<=OLD.assignment_revision THEN
  RAISE EXCEPTION 'ROOM_SCOPE_CHANGE_REQUIRES_NEW_ASSIGNMENT_REVISION';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER room_scope_revision_guard BEFORE UPDATE ON public.cerebro_bk11_service_assignments
 FOR EACH ROW EXECUTE FUNCTION public.cerebro_room_scope_revision_guard();
CREATE POLICY actor_admission_reserved ON public.cerebro_owner_state_heads AS RESTRICTIVE FOR ALL
 USING (state_schema<>'cerebro-project-actor-admissions/v1' OR (((COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'project_ref')=project_ref AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'principal_ref')=current_setting('cerebro.principal_ref',true)) AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'mode') IN ('ISSUER_READ','ISSUER_COMMIT','ACTOR_READ','ACTOR_ACTIVATE','ACTOR_MATERIAL','OWN_DESTINATION','SERVICE_READ')))
 WITH CHECK (state_schema<>'cerebro-project-actor-admissions/v1' OR (((COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'project_ref')=project_ref AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'principal_ref')=current_setting('cerebro.principal_ref',true)) AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'mode') IN ('ISSUER_COMMIT','ACTOR_ACTIVATE','ACTOR_MATERIAL','OWN_DESTINATION')));
CREATE POLICY actor_admission_reserved ON public.cerebro_owner_state_revisions AS RESTRICTIVE FOR ALL
 USING (state_schema<>'cerebro-project-actor-admissions/v1' OR (((COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'project_ref')=project_ref AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'principal_ref')=current_setting('cerebro.principal_ref',true)) AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'mode') IN ('ISSUER_READ','ISSUER_COMMIT','ACTOR_READ','ACTOR_ACTIVATE','ACTOR_MATERIAL','OWN_DESTINATION','SERVICE_READ')))
 WITH CHECK (state_schema<>'cerebro-project-actor-admissions/v1' OR (((COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'project_ref')=project_ref AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'principal_ref')=current_setting('cerebro.principal_ref',true)) AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'mode') IN ('ISSUER_COMMIT','ACTOR_ACTIVATE','ACTOR_MATERIAL','OWN_DESTINATION')));
CREATE POLICY actor_admission_reserved_receipts ON public.cerebro_owner_state_commit_receipts
 AS RESTRICTIVE FOR ALL USING (commit_ref NOT LIKE 'actor-admission-commit:%' OR (((COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'project_ref')=project_ref AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'principal_ref')=current_setting('cerebro.principal_ref',true)) AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'mode') IN ('ISSUER_READ','ISSUER_COMMIT','ACTOR_READ','ACTOR_ACTIVATE','ACTOR_MATERIAL','OWN_DESTINATION','SERVICE_READ')))
 WITH CHECK (commit_ref NOT LIKE 'actor-admission-commit:%' OR (((COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'project_ref')=project_ref AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'principal_ref')=current_setting('cerebro.principal_ref',true)) AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'mode') IN ('ISSUER_COMMIT','ACTOR_ACTIVATE','ACTOR_MATERIAL','OWN_DESTINATION')));
CREATE POLICY actor_admission_appointed_issuer_read ON public.cerebro_pm_owner_d1_routes FOR SELECT USING (
 tenant_ref=current_setting('cerebro.tenant_ref',true) AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND ((COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'project_ref')=project_ref AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'principal_ref')=current_setting('cerebro.principal_ref',true)) AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'mode') IN ('ISSUER_READ','ISSUER_COMMIT'));
CREATE POLICY actor_admission_appointed_issuer_read ON public.cerebro_bk11_service_assignments FOR SELECT USING (
 tenant_ref=current_setting('cerebro.tenant_ref',true) AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND ((COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'project_ref')=project_ref AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'principal_ref')=current_setting('cerebro.principal_ref',true)) AND (COALESCE(NULLIF(current_setting('cerebro.actor_admission_access',true),''),'{}')::jsonb->>'mode') IN ('ISSUER_READ','ISSUER_COMMIT'));
CREATE POLICY room_project_service_read ON public.cerebro_room_return_d1_outbox FOR SELECT USING (EXISTS (SELECT 1 FROM public.cerebro_bk11_service_assignments a
 WHERE a.tenant_ref=cerebro_room_return_d1_outbox.tenant_ref
 AND a.workspace_ref=cerebro_room_return_d1_outbox.workspace_ref
 AND a.project_ref=cerebro_room_return_d1_outbox.project_ref
 AND a.route_ref=cerebro_room_return_d1_outbox.route_ref
 AND a.owner_ref=cerebro_room_return_d1_outbox.route_owner_ref
 AND a.receiver_ref=cerebro_room_return_d1_outbox.receiver_ref
 AND a.owner_family='ROOM_RETURN' AND a.room_return_scope='PROJECT_ADMITTED_ACTORS'
 AND a.principal_ref=current_setting('cerebro.principal_ref',true)
 AND a.enabled AND a.expires_at>now()));
CREATE POLICY room_project_service_settle ON public.cerebro_room_return_d1_outbox FOR UPDATE USING (EXISTS (SELECT 1 FROM public.cerebro_bk11_service_assignments a
 WHERE a.tenant_ref=cerebro_room_return_d1_outbox.tenant_ref
 AND a.workspace_ref=cerebro_room_return_d1_outbox.workspace_ref
 AND a.project_ref=cerebro_room_return_d1_outbox.project_ref
 AND a.route_ref=cerebro_room_return_d1_outbox.route_ref
 AND a.owner_ref=cerebro_room_return_d1_outbox.route_owner_ref
 AND a.receiver_ref=cerebro_room_return_d1_outbox.receiver_ref
 AND a.owner_family='ROOM_RETURN' AND a.room_return_scope='PROJECT_ADMITTED_ACTORS'
 AND a.principal_ref=current_setting('cerebro.principal_ref',true)
 AND a.enabled AND a.expires_at>now() AND a.service_role='publisher'
 AND EXISTS (SELECT 1 FROM public.cerebro_pm_owner_d1_routes r
 WHERE r.tenant_ref=a.tenant_ref AND r.workspace_ref=a.workspace_ref AND r.project_ref=a.project_ref
 AND r.owner_ref=a.owner_ref AND r.route_ref=a.route_ref AND r.receiver_ref=a.receiver_ref
 AND r.publisher_principal_ref=a.principal_ref AND r.publisher_consumer_ref=a.consumer_ref
 AND r.publisher_session_ref=a.session_ref AND r.publisher_session_binding_id=a.session_binding_id
 AND r.binding_revision=a.route_binding_revision AND r.pointer_ttl_seconds=a.pointer_ttl_seconds AND r.drain_enabled))) WITH CHECK (EXISTS (SELECT 1 FROM public.cerebro_bk11_service_assignments a
 WHERE a.tenant_ref=cerebro_room_return_d1_outbox.tenant_ref
 AND a.workspace_ref=cerebro_room_return_d1_outbox.workspace_ref
 AND a.project_ref=cerebro_room_return_d1_outbox.project_ref
 AND a.route_ref=cerebro_room_return_d1_outbox.route_ref
 AND a.owner_ref=cerebro_room_return_d1_outbox.route_owner_ref
 AND a.receiver_ref=cerebro_room_return_d1_outbox.receiver_ref
 AND a.owner_family='ROOM_RETURN' AND a.room_return_scope='PROJECT_ADMITTED_ACTORS'
 AND a.principal_ref=current_setting('cerebro.principal_ref',true)
 AND a.enabled AND a.expires_at>now() AND a.service_role='publisher'
 AND EXISTS (SELECT 1 FROM public.cerebro_pm_owner_d1_routes r
 WHERE r.tenant_ref=a.tenant_ref AND r.workspace_ref=a.workspace_ref AND r.project_ref=a.project_ref
 AND r.owner_ref=a.owner_ref AND r.route_ref=a.route_ref AND r.receiver_ref=a.receiver_ref
 AND r.publisher_principal_ref=a.principal_ref AND r.publisher_consumer_ref=a.consumer_ref
 AND r.publisher_session_ref=a.session_ref AND r.publisher_session_binding_id=a.session_binding_id
 AND r.binding_revision=a.route_binding_revision AND r.pointer_ttl_seconds=a.pointer_ttl_seconds AND r.drain_enabled)));
