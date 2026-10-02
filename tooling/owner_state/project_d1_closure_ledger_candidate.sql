-- Isolated Project Engine-owned D1 return candidate. Apply only in a disposable
-- owner-state PostgreSQL schema for this proof; it is not in the deploy manifest.
CREATE TABLE cerebro_project_d1_closure_ledger (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    project_ref text NOT NULL,
    receiver_ref text NOT NULL,
    closure_id text NOT NULL,
    closure_revision bigint NOT NULL DEFAULT 1 CHECK (closure_revision = 1),
    closure_fingerprint text NOT NULL CHECK (closure_fingerprint ~ '^[0-9a-f]{64}$'),
    owner_event_key text NOT NULL,
    owner_event_fingerprint text NOT NULL CHECK (owner_event_fingerprint ~ '^[0-9a-f]{64}$'),
    receipt_ref text NOT NULL,
    receipt_fingerprint text NOT NULL CHECK (receipt_fingerprint ~ '^[0-9a-f]{64}$'),
    receipt_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, project_ref, receiver_ref, closure_id),
    UNIQUE (tenant_ref, workspace_ref, receipt_ref)
);

CREATE TRIGGER cerebro_project_d1_closure_ledger_immutable
    BEFORE UPDATE OR DELETE ON cerebro_project_d1_closure_ledger
    FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation();

ALTER TABLE cerebro_project_d1_closure_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_project_d1_closure_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY cerebro_project_d1_closure_scope ON cerebro_project_d1_closure_ledger
    USING (tenant_ref = current_setting('cerebro.tenant_ref', true)
       AND workspace_ref = current_setting('cerebro.workspace_ref', true)
       AND project_ref = current_setting('cerebro.project_ref', true))
    WITH CHECK (tenant_ref = current_setting('cerebro.tenant_ref', true)
       AND workspace_ref = current_setting('cerebro.workspace_ref', true)
       AND project_ref = current_setting('cerebro.project_ref', true));
