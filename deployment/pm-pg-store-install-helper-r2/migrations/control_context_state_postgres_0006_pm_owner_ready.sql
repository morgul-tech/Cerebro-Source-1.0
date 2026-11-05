-- BK07 PM-owned ready receipt custody. No packet is seeded by this migration.
CREATE TABLE IF NOT EXISTS cerebro_pm_owner_heads (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    project_ref text NOT NULL,
    owner_ref text NOT NULL,
    next_seq bigint NOT NULL CHECK (next_seq >= 1),
    PRIMARY KEY (tenant_ref,workspace_ref,owner_ref),
    FOREIGN KEY (tenant_ref,workspace_ref,project_ref)
        REFERENCES cerebro_project_instances (tenant_ref,workspace_ref,project_ref)
);

CREATE TABLE IF NOT EXISTS cerebro_pm_owner_receipts (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    project_ref text NOT NULL,
    owner_ref text NOT NULL,
    receipt_ref text NOT NULL,
    referent_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    snapshot_sha256 text NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    record_payload jsonb NOT NULL,
    projection_payload jsonb NOT NULL,
    claim_payload jsonb NOT NULL,
    packet_payload jsonb NOT NULL,
    queue_payload jsonb NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref,workspace_ref,owner_ref,receipt_ref),
    UNIQUE (tenant_ref,workspace_ref,owner_ref,idempotency_key),
    FOREIGN KEY (tenant_ref,workspace_ref,owner_ref)
        REFERENCES cerebro_pm_owner_heads (tenant_ref,workspace_ref,owner_ref),
    FOREIGN KEY (tenant_ref,workspace_ref,project_ref)
        REFERENCES cerebro_project_instances (tenant_ref,workspace_ref,project_ref)
);

CREATE TABLE IF NOT EXISTS cerebro_pm_owner_current (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    owner_ref text NOT NULL,
    referent_id text NOT NULL,
    receipt_ref text NOT NULL,
    PRIMARY KEY (tenant_ref,workspace_ref,owner_ref,referent_id),
    FOREIGN KEY (tenant_ref,workspace_ref,owner_ref,receipt_ref)
        REFERENCES cerebro_pm_owner_receipts (tenant_ref,workspace_ref,owner_ref,receipt_ref)
);

DROP TRIGGER IF EXISTS cerebro_pm_owner_receipts_immutable ON cerebro_pm_owner_receipts;
CREATE TRIGGER cerebro_pm_owner_receipts_immutable
    BEFORE UPDATE OR DELETE ON cerebro_pm_owner_receipts
    FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation();

DO $pm_owner_rls$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_pm_owner_heads', 'cerebro_pm_owner_receipts', 'cerebro_pm_owner_current'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY cerebro_workspace_isolation ON %I USING '
            || '(tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true)) '
            || 'WITH CHECK (tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true))', table_name);
    END LOOP;
END
$pm_owner_rls$;
