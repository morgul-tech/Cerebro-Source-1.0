-- Candidate contract only. Do not install until the host role-assignment adapter
-- supplies two independently assigned, current receiver/producer session tuples.
-- No aliases, credentials, config rows, grants, roles, or automatic migration here.

CREATE TABLE IF NOT EXISTS cerebro_x9_channel_role_assignments (
 tenant_ref text NOT NULL,
 workspace_ref text NOT NULL,
 project_ref text NOT NULL,
 channel text NOT NULL CHECK (channel='PROSJEKTMANN_CHANNEL'),
 role text NOT NULL CHECK (role IN ('receiver','producer')),
 config_ref text NOT NULL,
 assignment_ref text NOT NULL,
 source_ref text NOT NULL,
 principal_ref text NOT NULL,
 consumer_ref text NOT NULL,
 session_ref text NOT NULL,
 project_revision bigint NOT NULL CHECK (project_revision>0),
 session_binding_id text NOT NULL,
 session_revision bigint NOT NULL CHECK (session_revision>0),
 session_fingerprint text NOT NULL CHECK (session_fingerprint ~ '^[0-9a-f]{64}$'),
 active boolean NOT NULL DEFAULT true,
 PRIMARY KEY (tenant_ref,workspace_ref,project_ref,channel,role),
 UNIQUE (tenant_ref,workspace_ref,project_ref,channel,assignment_ref),
 UNIQUE (tenant_ref,workspace_ref,project_ref,channel,session_ref),
 UNIQUE (tenant_ref,workspace_ref,project_ref,channel,config_ref,role,assignment_ref),
 UNIQUE (tenant_ref,workspace_ref,project_ref,channel,config_ref,role,assignment_ref,principal_ref,consumer_ref,session_ref),
 UNIQUE (tenant_ref,workspace_ref,project_ref,channel,config_ref,role,assignment_ref,session_ref),
 FOREIGN KEY (tenant_ref,workspace_ref,principal_ref,consumer_ref,session_ref)
   REFERENCES cerebro_control_session_bindings(tenant_ref,workspace_ref,principal_ref,consumer_ref,session_ref),
 FOREIGN KEY (tenant_ref,workspace_ref,project_ref)
   REFERENCES cerebro_project_instances(tenant_ref,workspace_ref,project_ref),
 CHECK (length(trim(assignment_ref))>0 AND length(trim(source_ref))>0)
);
ALTER TABLE cerebro_x9_channel_role_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_x9_channel_role_assignments FORCE ROW LEVEL SECURITY;
-- Runtime can read same-project role tuples after authenticating and verifying its own current session.
-- No runtime INSERT/UPDATE/DELETE policy: role assignment is an independent host-control operation.
CREATE POLICY x9_role_assignment_read ON cerebro_x9_channel_role_assignments FOR SELECT USING (
 tenant_ref=current_setting('cerebro.tenant_ref',true)
 AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND project_ref=current_setting('cerebro.project_ref',true));

CREATE TABLE IF NOT EXISTS cerebro_x9_channel_records (
 tenant_ref text NOT NULL,
 workspace_ref text NOT NULL,
 project_ref text NOT NULL,
 channel text NOT NULL CHECK (channel='PROSJEKTMANN_CHANNEL'),
 config_ref text NOT NULL,
 record_kind text NOT NULL CHECK (record_kind IN ('POINTER','DISPOSITION')),
 event_id text NOT NULL,
 writer_principal text NOT NULL,
 writer_consumer text NOT NULL,
 writer_session text NOT NULL,
 writer_role text NOT NULL CHECK (writer_role IN ('receiver','producer')),
 writer_assignment_ref text NOT NULL,
 peer_role text NOT NULL CHECK (peer_role IN ('receiver','producer')),
 peer_assignment_ref text NOT NULL,
 peer_session_ref text NOT NULL,
 content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
 record_payload jsonb NOT NULL CHECK (jsonb_typeof(record_payload)='object'),
 provider_revision bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
 committed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_ref,workspace_ref,project_ref,channel,record_kind,event_id),
 FOREIGN KEY(tenant_ref,workspace_ref,writer_principal,writer_consumer,writer_session)
   REFERENCES cerebro_control_session_bindings(tenant_ref,workspace_ref,principal_ref,consumer_ref,session_ref),
 FOREIGN KEY(tenant_ref,workspace_ref,project_ref,channel,config_ref,writer_role,writer_assignment_ref,
             writer_principal,writer_consumer,writer_session)
   REFERENCES cerebro_x9_channel_role_assignments(tenant_ref,workspace_ref,project_ref,channel,config_ref,role,assignment_ref,
                                                  principal_ref,consumer_ref,session_ref),
 FOREIGN KEY(tenant_ref,workspace_ref,project_ref,channel,config_ref,peer_role,peer_assignment_ref,peer_session_ref)
   REFERENCES cerebro_x9_channel_role_assignments(tenant_ref,workspace_ref,project_ref,channel,config_ref,role,assignment_ref,session_ref),
 CHECK(record_payload->>'event_id'=event_id),
 CHECK((record_kind='POINTER' AND writer_role='producer' AND peer_role='receiver'
        AND record_payload->>'producer_id'=writer_principal
        AND record_payload->>'receiver_ref'=peer_session_ref)
    OR (record_kind='DISPOSITION' AND writer_role='receiver' AND peer_role='producer'))
);
ALTER TABLE cerebro_x9_channel_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_x9_channel_records FORCE ROW LEVEL SECURITY;
-- A current configured role tuple in the same tenant/workspace is required to read.
CREATE POLICY x9_channel_read ON cerebro_x9_channel_records FOR SELECT USING (
 tenant_ref=current_setting('cerebro.tenant_ref',true)
 AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND project_ref=current_setting('cerebro.project_ref',true)
 AND EXISTS (
   SELECT 1 FROM cerebro_x9_channel_role_assignments a
   WHERE (a.tenant_ref,a.workspace_ref,a.project_ref,a.channel,a.config_ref,a.role,a.assignment_ref,a.principal_ref,a.consumer_ref,a.session_ref,
          a.project_revision,a.session_binding_id,a.session_revision,a.session_fingerprint,a.active)=
         (current_setting('cerebro.tenant_ref',true),current_setting('cerebro.workspace_ref',true),
          current_setting('cerebro.project_ref',true),
          channel,current_setting('cerebro.config_ref',true),current_setting('cerebro.role',true),
          current_setting('cerebro.role_assignment_ref',true),
          current_setting('cerebro.principal_ref',true),
          current_setting('cerebro.consumer_ref',true),current_setting('cerebro.session_ref',true),
          current_setting('cerebro.project_revision',true)::bigint,current_setting('cerebro.session_binding_id',true),
          current_setting('cerebro.session_revision',true)::bigint,current_setting('cerebro.session_fingerprint',true),true)
 ));
-- Insert must match the configured writer role and exact authenticated session tuple.
CREATE POLICY x9_channel_insert ON cerebro_x9_channel_records FOR INSERT WITH CHECK (
 tenant_ref=current_setting('cerebro.tenant_ref',true)
 AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND project_ref=current_setting('cerebro.project_ref',true)
 AND writer_role=current_setting('cerebro.role',true)
 AND writer_assignment_ref=current_setting('cerebro.role_assignment_ref',true)
 AND writer_principal=current_setting('cerebro.principal_ref',true)
 AND writer_consumer=current_setting('cerebro.consumer_ref',true)
 AND writer_session=current_setting('cerebro.session_ref',true)
 AND EXISTS (
   SELECT 1 FROM cerebro_x9_channel_role_assignments a
   WHERE (a.tenant_ref,a.workspace_ref,a.project_ref,a.channel,a.config_ref,a.role,a.assignment_ref,
          a.principal_ref,a.consumer_ref,a.session_ref,a.project_revision,a.session_binding_id,
          a.session_revision,a.session_fingerprint,a.active)=
         (tenant_ref,workspace_ref,project_ref,channel,current_setting('cerebro.config_ref',true),
          writer_role,writer_assignment_ref,
          writer_principal,writer_consumer,writer_session,current_setting('cerebro.project_revision',true)::bigint,
          current_setting('cerebro.session_binding_id',true),current_setting('cerebro.session_revision',true)::bigint,
          current_setting('cerebro.session_fingerprint',true),true)
 ));
-- No UPDATE/DELETE policy. No grants or authority rows are created by this candidate.
