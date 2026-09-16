-- P872 HG04=B additive custody, never Human or MCP admission authority.
CREATE TABLE IF NOT EXISTS cerebro_human_t3_break_glass_heads (
    tenant_ref text NOT NULL, workspace_ref text NOT NULL, principal_ref text NOT NULL,
    consumer_ref text NOT NULL, session_ref text NOT NULL, override_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    record_fingerprint text NOT NULL CHECK (record_fingerprint ~ '^[0-9a-f]{64}$'),
    arm_payload jsonb NOT NULL,
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, override_id),
    CHECK (arm_payload ->> 'authority' = 'NONAUTHORIZING_CUSTODY'),
    CHECK (arm_payload ->> 'current' = 'false'),
    CHECK (arm_payload ->> 'revision' = revision::text),
    CHECK (arm_payload ->> 'fingerprint' = record_fingerprint)
);
CREATE TABLE IF NOT EXISTS cerebro_human_t3_break_glass_revisions (
    LIKE cerebro_human_t3_break_glass_heads INCLUDING CONSTRAINTS,
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, override_id, revision),
    FOREIGN KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, override_id)
        REFERENCES cerebro_human_t3_break_glass_heads DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS cerebro_human_t3_break_glass_receipts (
    tenant_ref text NOT NULL, workspace_ref text NOT NULL, principal_ref text NOT NULL,
    consumer_ref text NOT NULL, session_ref text NOT NULL, override_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1), receipt_payload jsonb NOT NULL,
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, override_id, revision),
    FOREIGN KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, override_id, revision)
        REFERENCES cerebro_human_t3_break_glass_revisions DEFERRABLE INITIALLY DEFERRED,
    CHECK (receipt_payload ->> 'authority' = 'CUSTODY_ONLY'),
    CHECK (receipt_payload ->> 'revision' = revision::text)
);
DO $human_t3_custody$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_human_t3_break_glass_heads',
        'cerebro_human_t3_break_glass_revisions',
        'cerebro_human_t3_break_glass_receipts'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS cerebro_human_t3_isolation ON %I', table_name);
        EXECUTE format('CREATE POLICY cerebro_human_t3_isolation ON %I USING '
            || '(tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true) '
            || 'AND principal_ref = current_setting(''cerebro.principal_ref'', true)) WITH CHECK '
            || '(tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true) '
            || 'AND principal_ref = current_setting(''cerebro.principal_ref'', true))', table_name);
        EXECUTE format('DROP TRIGGER IF EXISTS human_t3_no_mutation ON %I', table_name);
        IF table_name = 'cerebro_human_t3_break_glass_heads' THEN
            EXECUTE format('CREATE TRIGGER human_t3_no_mutation BEFORE DELETE ON %I '
                || 'FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation()', table_name);
        ELSE
            EXECUTE format('CREATE TRIGGER human_t3_no_mutation BEFORE UPDATE OR DELETE ON %I '
                || 'FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation()', table_name);
        END IF;
    END LOOP;
END
$human_t3_custody$;
