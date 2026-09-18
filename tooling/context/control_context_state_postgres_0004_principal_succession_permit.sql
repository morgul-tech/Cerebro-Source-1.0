-- P1074: additive custody in the SAME service, never semantic/issuance authority.
CREATE TABLE IF NOT EXISTS cerebro_principal_succession_permit_heads (
    tenant_ref text NOT NULL, workspace_ref text NOT NULL, principal_ref text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    request_ref text NOT NULL, permit_ref text NOT NULL,
    record_fingerprint text NOT NULL CHECK (record_fingerprint ~ '^[0-9a-f]{64}$'),
    record_payload jsonb NOT NULL,
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref),
    CHECK ((record_payload -> 'identity' ->> 'tenant_ref') = tenant_ref),
    CHECK ((record_payload -> 'identity' ->> 'workspace_ref') = workspace_ref),
    CHECK ((record_payload -> 'identity' ->> 'principal_ref') = principal_ref),
    CHECK ((record_payload ->> 'revision') = revision::text),
    CHECK ((record_payload ->> 'request_ref') = request_ref),
    CHECK ((record_payload ->> 'fingerprint') = record_fingerprint),
    CHECK ((record_payload -> 'permit' ->> 'permit_id') = permit_ref)
);
CREATE TABLE IF NOT EXISTS cerebro_principal_succession_permit_revisions (
    LIKE cerebro_principal_succession_permit_heads INCLUDING CONSTRAINTS,
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, revision),
    UNIQUE (tenant_ref, workspace_ref, principal_ref, request_ref),
    UNIQUE (tenant_ref, workspace_ref, principal_ref, permit_ref)
);
ALTER TABLE cerebro_principal_succession_permit_heads ADD CONSTRAINT succession_current_version
    FOREIGN KEY (tenant_ref, workspace_ref, principal_ref, revision)
    REFERENCES cerebro_principal_succession_permit_revisions DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE IF NOT EXISTS cerebro_principal_succession_permit_receipts (
    tenant_ref text NOT NULL, workspace_ref text NOT NULL, principal_ref text NOT NULL,
    revision bigint NOT NULL, receipt_payload jsonb NOT NULL,
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, revision),
    FOREIGN KEY (tenant_ref, workspace_ref, principal_ref, revision)
        REFERENCES cerebro_principal_succession_permit_revisions DEFERRABLE INITIALLY DEFERRED,
    CHECK ((receipt_payload ->> 'authority') = 'CUSTODY_ONLY'),
    CHECK ((receipt_payload ->> 'revision') = revision::text)
);
DO $succession_custody$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_principal_succession_permit_heads',
        'cerebro_principal_succession_permit_revisions',
        'cerebro_principal_succession_permit_receipts'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY succession_isolation ON %I USING '
            || '(tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true) '
            || 'AND principal_ref = current_setting(''cerebro.principal_ref'', true)) WITH CHECK '
            || '(tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true) '
            || 'AND principal_ref = current_setting(''cerebro.principal_ref'', true))', table_name);
        IF table_name = 'cerebro_principal_succession_permit_heads' THEN
            EXECUTE format('CREATE TRIGGER succession_no_mutation BEFORE DELETE ON %I '
                || 'FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation()', table_name);
        ELSE
            EXECUTE format('CREATE TRIGGER succession_no_mutation BEFORE UPDATE OR DELETE ON %I '
                || 'FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation()', table_name);
        END IF;
    END LOOP;
END
$succession_custody$;
