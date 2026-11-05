-- Cerebro Context-State Wave D2: role-neutral generation birth aggregate.
-- This remains inside the existing Context State Service.  It grants no role,
-- claim, scheduler authority, runtime activation or predecessor-state inheritance.

CREATE TABLE IF NOT EXISTS cerebro_pre_role_generation_heads (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    generation_ref text NOT NULL,
    lifecycle text NOT NULL CHECK (lifecycle IN ('BIRTH_PENDING', 'READY_UNBOUND', 'ROLE_ATTACHED')),
    source_revision text NOT NULL,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision >= 1),
    aggregate_fingerprint text NOT NULL CHECK (aggregate_fingerprint ~ '^[0-9a-f]{64}$'),
    generation_payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, generation_ref),
    CHECK (generation_payload #>> '{authority_envelope,state}' = 'UNBOUND'),
    CHECK (generation_payload #> '{authority_envelope,role}' = 'null'::jsonb),
    CHECK (generation_payload #>> '{identity_envelope,predecessor_live_state_inherited}' = 'false'),
    CHECK (generation_payload #>> '{identity_envelope,predecessor_claims_inherited}' = 'false'),
    CHECK (generation_payload #>> '{identity_envelope,private_state_inherited}' = 'false')
);

CREATE TABLE IF NOT EXISTS cerebro_pre_role_generation_revisions (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    generation_ref text NOT NULL,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision >= 1),
    aggregate_fingerprint text NOT NULL CHECK (aggregate_fingerprint ~ '^[0-9a-f]{64}$'),
    generation_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, generation_ref, aggregate_revision),
    FOREIGN KEY (tenant_ref, workspace_ref, generation_ref)
        REFERENCES cerebro_pre_role_generation_heads (tenant_ref, workspace_ref, generation_ref)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (generation_payload #>> '{authority_envelope,state}' = 'UNBOUND'),
    CHECK (generation_payload #> '{authority_envelope,role}' = 'null'::jsonb)
);

CREATE OR REPLACE FUNCTION cerebro_pre_role_generation_identity_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_revision IS DISTINCT FROM OLD.source_revision
       OR NEW.generation_payload #> '{identity_envelope}' IS DISTINCT FROM OLD.generation_payload #> '{identity_envelope}'
       OR NEW.generation_payload #> '{authority_envelope}' IS DISTINCT FROM OLD.generation_payload #> '{authority_envelope}'
       OR NEW.generation_payload #> '{civilization_method_attestation}' IS DISTINCT FROM OLD.generation_payload #> '{civilization_method_attestation}'
       OR NEW.generation_payload #> '{fresh_world}' IS DISTINCT FROM OLD.generation_payload #> '{fresh_world}' THEN
        RAISE EXCEPTION 'pre-role-generation-immutable-birth-envelope';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS cerebro_pre_role_generation_identity_immutable
    ON cerebro_pre_role_generation_heads;
CREATE TRIGGER cerebro_pre_role_generation_identity_immutable
    BEFORE UPDATE ON cerebro_pre_role_generation_heads
    FOR EACH ROW EXECUTE FUNCTION cerebro_pre_role_generation_identity_immutable();

DROP TRIGGER IF EXISTS cerebro_pre_role_generation_revisions_immutable
    ON cerebro_pre_role_generation_revisions;
CREATE TRIGGER cerebro_pre_role_generation_revisions_immutable
    BEFORE UPDATE OR DELETE ON cerebro_pre_role_generation_revisions
    FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation();

DROP TRIGGER IF EXISTS cerebro_pre_role_generation_heads_no_delete
    ON cerebro_pre_role_generation_heads;
CREATE TRIGGER cerebro_pre_role_generation_heads_no_delete
    BEFORE DELETE ON cerebro_pre_role_generation_heads
    FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation();

DO $cerebro_pre_role_generation_rls$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_pre_role_generation_heads',
        'cerebro_pre_role_generation_revisions'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS cerebro_workspace_isolation ON %I', table_name);
        EXECUTE format(
            'CREATE POLICY cerebro_workspace_isolation ON %I USING '
            || '(tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true)) '
            || 'WITH CHECK (tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true))',
            table_name
        );
    END LOOP;
END
$cerebro_pre_role_generation_rls$;
