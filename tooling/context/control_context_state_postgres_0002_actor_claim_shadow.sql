-- Cerebro Context-State B1: actor-generation and work-claim shadow aggregates.
-- These rows are non-authoritative projections. They do not create live claims,
-- grant actor authority, activate a consumer, or cut over any runtime path.

CREATE TABLE IF NOT EXISTS cerebro_actor_generation_shadow_heads (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    actor_ref text NOT NULL,
    actor_role text NOT NULL CHECK (actor_role IN (
        'PRINCIPAL', 'ASSISTANT', 'PROJECT_MANAGER', 'IMPLEMENTER', 'WORKER', 'RESEARCHER'
    )),
    generation_ref text NOT NULL,
    lifecycle text NOT NULL CHECK (lifecycle IN ('READY', 'ACTIVE', 'RETIRED')),
    source_revision text NOT NULL,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision >= 1),
    aggregate_fingerprint text NOT NULL CHECK (aggregate_fingerprint ~ '^[0-9a-f]{64}$'),
    shadow_payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, actor_role, generation_ref),
    UNIQUE (tenant_ref, workspace_ref, actor_ref, generation_ref),
    CHECK (shadow_payload ->> 'authority' = 'SHADOW_ONLY')
);

CREATE TABLE IF NOT EXISTS cerebro_actor_generation_shadow_revisions (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    actor_role text NOT NULL,
    generation_ref text NOT NULL,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision >= 1),
    aggregate_fingerprint text NOT NULL CHECK (aggregate_fingerprint ~ '^[0-9a-f]{64}$'),
    shadow_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, actor_role, generation_ref, aggregate_revision),
    FOREIGN KEY (tenant_ref, workspace_ref, actor_role, generation_ref)
        REFERENCES cerebro_actor_generation_shadow_heads (
            tenant_ref, workspace_ref, actor_role, generation_ref
        ) DEFERRABLE INITIALLY DEFERRED,
    CHECK (shadow_payload ->> 'authority' = 'SHADOW_ONLY')
);

CREATE TABLE IF NOT EXISTS cerebro_work_claim_shadow_heads (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    claim_ref text NOT NULL,
    project_ref text NOT NULL,
    actor_ref text NOT NULL,
    actor_role text NOT NULL,
    actor_generation_ref text NOT NULL,
    scope_ref text NOT NULL,
    claim_mode text NOT NULL,
    lifecycle text NOT NULL CHECK (lifecycle IN (
        'BOUND_ACTIVE_PRESTART', 'ACTIVE', 'TERMINAL_PASS', 'TERMINAL_FAIL', 'RELEASED'
    )),
    source_revision text NOT NULL,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision >= 1),
    aggregate_fingerprint text NOT NULL CHECK (aggregate_fingerprint ~ '^[0-9a-f]{64}$'),
    shadow_payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, claim_ref),
    FOREIGN KEY (tenant_ref, workspace_ref, actor_role, actor_generation_ref)
        REFERENCES cerebro_actor_generation_shadow_heads (
            tenant_ref, workspace_ref, actor_role, generation_ref
        ) DEFERRABLE INITIALLY DEFERRED,
    CHECK (shadow_payload ->> 'authority' = 'SHADOW_ONLY'),
    CHECK (shadow_payload ->> 'live_claim' = 'false')
);

CREATE TABLE IF NOT EXISTS cerebro_work_claim_shadow_revisions (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    claim_ref text NOT NULL,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision >= 1),
    aggregate_fingerprint text NOT NULL CHECK (aggregate_fingerprint ~ '^[0-9a-f]{64}$'),
    shadow_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, claim_ref, aggregate_revision),
    FOREIGN KEY (tenant_ref, workspace_ref, claim_ref)
        REFERENCES cerebro_work_claim_shadow_heads (tenant_ref, workspace_ref, claim_ref)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (shadow_payload ->> 'authority' = 'SHADOW_ONLY'),
    CHECK (shadow_payload ->> 'live_claim' = 'false')
);

DO $cerebro_actor_claim_shadow_immutable$
DECLARE
    table_name text;
    trigger_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_actor_generation_shadow_revisions',
        'cerebro_work_claim_shadow_revisions'
    ] LOOP
        trigger_name := table_name || '_immutable';
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', trigger_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
            || 'FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation()',
            trigger_name, table_name
        );
    END LOOP;
END
$cerebro_actor_claim_shadow_immutable$;

DO $cerebro_actor_claim_shadow_no_delete$
DECLARE
    table_name text;
    trigger_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_actor_generation_shadow_heads',
        'cerebro_work_claim_shadow_heads'
    ] LOOP
        trigger_name := table_name || '_no_delete';
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', trigger_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE DELETE ON %I '
            || 'FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation()',
            trigger_name, table_name
        );
    END LOOP;
END
$cerebro_actor_claim_shadow_no_delete$;

DO $cerebro_actor_claim_shadow_rls$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_actor_generation_shadow_heads',
        'cerebro_actor_generation_shadow_revisions',
        'cerebro_work_claim_shadow_heads',
        'cerebro_work_claim_shadow_revisions'
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
$cerebro_actor_claim_shadow_rls$;
