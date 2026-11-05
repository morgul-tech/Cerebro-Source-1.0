-- Candidate only. Privileged installation, never runtime startup migration.
-- A single current head per workspace; admission never transfers this head.
CREATE TABLE cerebro_pm_current_heads (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    head_payload jsonb NOT NULL,
    PRIMARY KEY (tenant_ref, workspace_ref),
    CHECK (head_payload ->> 'schema' = 'cerebro-pm-current-head/v1'),
    CHECK (head_payload ->> 'tenant_ref' = tenant_ref),
    CHECK (head_payload ->> 'workspace_ref' = workspace_ref),
    CHECK ((head_payload ->> 'revision')::bigint = revision)
);
CREATE TABLE cerebro_pm_current_revisions (
    tenant_ref text NOT NULL, workspace_ref text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0), head_payload jsonb NOT NULL,
    PRIMARY KEY (tenant_ref, workspace_ref, revision),
    FOREIGN KEY (tenant_ref, workspace_ref) REFERENCES cerebro_pm_current_heads
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE cerebro_pm_admission_decisions (
    tenant_ref text NOT NULL, workspace_ref text NOT NULL, generation_ref text NOT NULL,
    decision_ref text NOT NULL, decision_payload jsonb NOT NULL,
    PRIMARY KEY (tenant_ref, workspace_ref, generation_ref),
    UNIQUE (tenant_ref, workspace_ref, decision_ref),
    CHECK (decision_payload ->> 'schema' = 'cerebro-pm-admission-decision/v1'),
    CHECK (decision_payload ->> 'tenant_ref' = tenant_ref),
    CHECK (decision_payload ->> 'workspace_ref' = workspace_ref),
    CHECK (decision_payload ->> 'generation_ref' = generation_ref),
    CHECK (decision_payload ->> 'decision_ref' = decision_ref)
);
CREATE TRIGGER cerebro_pm_decisions_immutable BEFORE UPDATE OR DELETE
    ON cerebro_pm_admission_decisions FOR EACH ROW
    EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation();
CREATE TRIGGER cerebro_pm_current_revisions_immutable BEFORE UPDATE OR DELETE
    ON cerebro_pm_current_revisions FOR EACH ROW
    EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation();
CREATE TRIGGER cerebro_pm_current_no_delete BEFORE DELETE ON cerebro_pm_current_heads
    FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation();
DO $pm_rls$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['cerebro_pm_current_heads',
        'cerebro_pm_current_revisions', 'cerebro_pm_admission_decisions'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY cerebro_workspace_isolation ON %I USING '
            || '(tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true)) '
            || 'WITH CHECK (tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true))', table_name);
    END LOOP;
END
$pm_rls$;
-- No role names or grants guessed here. Custodian grants runtime SELECT only
-- on heads/decisions; privileged owner operator writes heads/history/decisions.
