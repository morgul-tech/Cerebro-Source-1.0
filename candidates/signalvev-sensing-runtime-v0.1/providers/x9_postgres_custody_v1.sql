-- Scoped optional receipt ledger INSIDE the existing control/owner PostgreSQL provider.
-- Candidate only. No automatic/global migration. A1 applies only to the admitted selected provider.
CREATE TABLE IF NOT EXISTS cerebro_x9_channel_records (
 tenant_ref text NOT NULL, workspace_ref text NOT NULL,
 channel text NOT NULL CHECK (channel='PROSJEKTMANN_CHANNEL'),
 record_kind text NOT NULL CHECK (record_kind IN ('POINTER','DISPOSITION')),
 event_id text NOT NULL,
 writer_principal text NOT NULL, writer_consumer text NOT NULL, writer_session text NOT NULL,
 content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
 record_payload jsonb NOT NULL CHECK (jsonb_typeof(record_payload)='object'),
 provider_revision bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
 committed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_ref,workspace_ref,channel,record_kind,event_id),
 FOREIGN KEY(tenant_ref,workspace_ref,writer_principal,writer_consumer,writer_session)
 REFERENCES cerebro_control_session_bindings(tenant_ref,workspace_ref,principal_ref,consumer_ref,session_ref),
 CHECK(record_payload->>'event_id'=event_id),
 CHECK((record_kind='POINTER' AND writer_principal='CURRENT_PM_PROJECT_MANAGER_C1A05B39'
       AND record_payload->>'producer_id'=writer_principal
       AND record_payload->>'receiver_ref'='codex:local01a0e1b4-eb38-7e80-83cb-fff0f30a253e') OR
       (record_kind='DISPOSITION' AND writer_principal='X9_PROSJEKTMANN_8F7290F3'
       AND writer_session='codex:local01a0e1b4-eb38-7e80-83cb-fff0f30a253e'))
);
ALTER TABLE cerebro_x9_channel_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_x9_channel_records FORCE ROW LEVEL SECURITY;
-- No UPDATE/DELETE policy: original receipts are immutable under the existing runtime role.
CREATE POLICY x9_channel_read ON cerebro_x9_channel_records FOR SELECT USING (
 tenant_ref=current_setting('cerebro.tenant_ref',true) AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND current_setting('cerebro.principal_ref',true) IN ('X9_PROSJEKTMANN_8F7290F3','CURRENT_PM_PROJECT_MANAGER_C1A05B39'));
CREATE POLICY x9_channel_insert ON cerebro_x9_channel_records FOR INSERT WITH CHECK (
 tenant_ref=current_setting('cerebro.tenant_ref',true) AND workspace_ref=current_setting('cerebro.workspace_ref',true)
 AND writer_principal=current_setting('cerebro.principal_ref',true));
-- No GRANT, token issuance or role creation. Existing selected runtime DB role only.
