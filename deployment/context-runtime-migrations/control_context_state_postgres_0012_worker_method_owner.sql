-- Additive owner policy and own-reader evidence, inside the existing Context DB.
-- Applied only by the separately qualified existing migration owner. No role creation.
CREATE TABLE IF NOT EXISTS public.cerebro_worker_method_owner_authorization_revisions (
 tenant_ref text NOT NULL, workspace_ref text NOT NULL, principal_ref text NOT NULL,
 consumer_ref text NOT NULL, session_ref text NOT NULL, generation_ref text NOT NULL,
 revision bigint NOT NULL CHECK (revision > 0), record_payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_ref,workspace_ref,principal_ref,consumer_ref,session_ref,generation_ref,revision),
 CHECK (jsonb_typeof(record_payload)='object' AND record_payload ?& ARRAY['revision','authorization','expected_files','expected_role_contracts','publisher','decision_material','acknowledgment_fingerprint']),
 CHECK (jsonb_typeof(record_payload->'authorization')='object' AND (record_payload->'authorization') ?& ARRAY['scope','generation_ref','identity']),
 CHECK ((record_payload #> '{authorization,identity}') ?& ARRAY['tenant_ref','workspace_ref','principal_ref','consumer_ref','session_ref']),
 CHECK (record_payload->>'revision' = revision::text),
 CHECK (record_payload #>> '{authorization,scope}' = 'WORKER_METHOD_ADOPTION_EXACT_DELTA'),
 CHECK (record_payload #>> '{authorization,generation_ref}' = generation_ref),
 CHECK (record_payload #>> '{authorization,identity,tenant_ref}' = tenant_ref),
 CHECK (record_payload #>> '{authorization,identity,workspace_ref}' = workspace_ref),
 CHECK (record_payload #>> '{authorization,identity,principal_ref}' = principal_ref),
 CHECK (record_payload #>> '{authorization,identity,consumer_ref}' = consumer_ref),
 CHECK (record_payload #>> '{authorization,identity,session_ref}' = session_ref)
);
CREATE TABLE IF NOT EXISTS public.cerebro_worker_method_read_acknowledgments (
 tenant_ref text NOT NULL, workspace_ref text NOT NULL, principal_ref text NOT NULL,
 consumer_ref text NOT NULL, session_ref text NOT NULL, generation_ref text NOT NULL,
 acknowledgment_ref text NOT NULL, acknowledgment_payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_ref,workspace_ref,principal_ref,consumer_ref,session_ref,generation_ref,acknowledgment_ref),
 CHECK (jsonb_typeof(acknowledgment_payload)='object' AND acknowledgment_payload ?& ARRAY['acknowledgment_ref','generation_ref','identity','fingerprint']),
 CHECK ((acknowledgment_payload->'identity') ?& ARRAY['tenant_ref','workspace_ref','principal_ref','consumer_ref','session_ref']),
 CHECK (acknowledgment_payload->>'acknowledgment_ref' = acknowledgment_ref),
 CHECK (acknowledgment_payload->>'generation_ref' = generation_ref),
 CHECK (acknowledgment_payload #>> '{identity,tenant_ref}' = tenant_ref),
 CHECK (acknowledgment_payload #>> '{identity,workspace_ref}' = workspace_ref),
 CHECK (acknowledgment_payload #>> '{identity,principal_ref}' = principal_ref),
 CHECK (acknowledgment_payload #>> '{identity,consumer_ref}' = consumer_ref),
 CHECK (acknowledgment_payload #>> '{identity,session_ref}' = session_ref)
);
DO $worker_method_owner$
DECLARE table_name text;
BEGIN
 FOREACH table_name IN ARRAY ARRAY['cerebro_worker_method_owner_authorization_revisions',
                                   'cerebro_worker_method_read_acknowledgments'] LOOP
  EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',table_name);
  EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',table_name);
  EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',table_name);
  EXECUTE format('DROP POLICY IF EXISTS cerebro_worker_method_identity ON public.%I',table_name);
  EXECUTE format('CREATE POLICY cerebro_worker_method_identity ON public.%I USING '
   || '(tenant_ref=current_setting(''cerebro.tenant_ref'',true) '
   || 'AND workspace_ref=current_setting(''cerebro.workspace_ref'',true) '
   || 'AND principal_ref=current_setting(''cerebro.principal_ref'',true) '
   || 'AND consumer_ref=current_setting(''cerebro.consumer_ref'',true) '
   || 'AND session_ref=current_setting(''cerebro.session_ref'',true)) '
   || 'WITH CHECK (tenant_ref=current_setting(''cerebro.tenant_ref'',true) '
   || 'AND workspace_ref=current_setting(''cerebro.workspace_ref'',true) '
   || 'AND principal_ref=current_setting(''cerebro.principal_ref'',true) '
   || 'AND consumer_ref=current_setting(''cerebro.consumer_ref'',true) '
   || 'AND session_ref=current_setting(''cerebro.session_ref'',true))',table_name);
  EXECUTE format('DROP TRIGGER IF EXISTS cerebro_worker_method_append_only ON public.%I',table_name);
  EXECUTE format('CREATE TRIGGER cerebro_worker_method_append_only BEFORE UPDATE OR DELETE ON public.%I '
   || 'FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation()',table_name);
 END LOOP;
END
$worker_method_owner$;
-- No guessed runtime principal in DDL. Qualified migration owner must grant SELECT,INSERT
-- runtime SELECT on authorization revisions; SELECT,INSERT on own acknowledgments ONLY.
-- Privileged existing migration/operator custody alone inserts authorization/revocation revisions.
-- Never runtime UPDATE/DELETE/TRUNCATE or INSERT to the authorization ledger.
-- Existing runtime DSN and migration role remain separate. RLS setting is server-derived.
