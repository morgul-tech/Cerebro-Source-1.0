-- Cerebro Control Context State Service — provider-neutral PostgreSQL data contract.
-- Live rows are runtime-derived governing state, never Cerebro Source authority.

CREATE TABLE IF NOT EXISTS cerebro_schema_migrations (
    migration_id text PRIMARY KEY,
    schema_version text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    checksum_sha256 text NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS cerebro_project_instances (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    project_ref text NOT NULL,
    aggregate_id text NOT NULL,
    source_revision text NOT NULL,
    project_status text NOT NULL CHECK (
        project_status IN ('ACTIVE', 'PAUSED', 'BLOCKED', 'COMPLETED', 'CANCELLED')
    ),
    default_context_ref text,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision >= 1),
    aggregate_fingerprint text NOT NULL CHECK (aggregate_fingerprint ~ '^[0-9a-f]{64}$'),
    next_sequence bigint NOT NULL DEFAULT 2 CHECK (next_sequence >= 2),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, project_ref),
    UNIQUE (tenant_ref, workspace_ref, aggregate_id)
);

CREATE TABLE IF NOT EXISTS cerebro_project_basis_revisions (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    project_ref text NOT NULL,
    basis_revision bigint NOT NULL CHECK (basis_revision >= 1),
    basis_ref text NOT NULL,
    basis_fingerprint text NOT NULL CHECK (basis_fingerprint ~ '^[0-9a-f]{64}$'),
    basis_payload jsonb NOT NULL,
    created_from_event_ref text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, project_ref, basis_revision),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref)
        REFERENCES cerebro_project_instances (tenant_ref, workspace_ref, project_ref)
);

CREATE TABLE IF NOT EXISTS cerebro_principal_project_bindings (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    active_project_ref text NOT NULL,
    binding_revision bigint NOT NULL CHECK (binding_revision >= 1),
    binding_fingerprint text NOT NULL CHECK (binding_fingerprint ~ '^[0-9a-f]{64}$'),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref),
    FOREIGN KEY (tenant_ref, workspace_ref, active_project_ref)
        REFERENCES cerebro_project_instances (tenant_ref, workspace_ref, project_ref)
);

CREATE TABLE IF NOT EXISTS cerebro_control_contexts (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    project_ref text NOT NULL,
    context_id text NOT NULL,
    parent_context_ref text,
    derived_from_context_ref text,
    lifecycle text NOT NULL CHECK (lifecycle IN ('OPEN', 'RETURNED', 'CLOSED', 'CANCELLED')),
    control_condition text CHECK (
        control_condition IS NULL OR
        control_condition IN ('READY', 'PAUSED_BY_USER', 'WAITING_HUMAN', 'STALLED', 'SAFE_HOLD')
    ),
    disposition text CHECK (
        disposition IS NULL OR
        disposition IN ('NONE', 'PENDING_JOIN', 'INCORPORATED', 'PRESERVED', 'SUPERSEDED')
    ),
    sequence bigint NOT NULL CHECK (sequence >= 1),
    context_payload jsonb NOT NULL,
    context_fingerprint text NOT NULL CHECK (context_fingerprint ~ '^[0-9a-f]{64}$'),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, project_ref, context_id),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref)
        REFERENCES cerebro_project_instances (tenant_ref, workspace_ref, project_ref),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref, parent_context_ref)
        REFERENCES cerebro_control_contexts (tenant_ref, workspace_ref, project_ref, context_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref, derived_from_context_ref)
        REFERENCES cerebro_control_contexts (tenant_ref, workspace_ref, project_ref, context_id)
        DEFERRABLE INITIALLY DEFERRED
);

-- Bootstrap is not a normal control-session transition: no session or open
-- control event exists yet.  Keep its immutable receipt in a separate ledger
-- instead of weakening the normal transition foreign-key contract.
CREATE TABLE IF NOT EXISTS cerebro_project_bootstrap_receipts (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    project_ref text NOT NULL,
    event_id text NOT NULL,
    receipt_id text NOT NULL,
    decision_ref text NOT NULL,
    request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    project_revision_after bigint NOT NULL CHECK (project_revision_after = 1),
    project_fingerprint_after text NOT NULL CHECK (project_fingerprint_after ~ '^[0-9a-f]{64}$'),
    bootstrap_payload jsonb NOT NULL,
    receipt_fingerprint text NOT NULL CHECK (receipt_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, receipt_id),
    UNIQUE (tenant_ref, workspace_ref, project_ref, event_id),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref)
        REFERENCES cerebro_project_instances (tenant_ref, workspace_ref, project_ref)
);

ALTER TABLE cerebro_project_instances
    DROP CONSTRAINT IF EXISTS cerebro_default_context_ref_fk;

ALTER TABLE cerebro_project_instances
    ADD CONSTRAINT cerebro_default_context_ref_fk
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref, default_context_ref)
    REFERENCES cerebro_control_contexts (tenant_ref, workspace_ref, project_ref, context_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS cerebro_control_session_bindings (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    consumer_ref text NOT NULL,
    session_ref text NOT NULL,
    session_binding_id text NOT NULL,
    project_ref text NOT NULL,
    active_context_ref text,
    project_revision bigint NOT NULL CHECK (project_revision >= 1),
    session_revision bigint NOT NULL CHECK (session_revision >= 1),
    session_fingerprint text NOT NULL CHECK (session_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref),
    UNIQUE (tenant_ref, workspace_ref, session_binding_id),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref)
        REFERENCES cerebro_project_instances (tenant_ref, workspace_ref, project_ref),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref, active_context_ref)
        REFERENCES cerebro_control_contexts (tenant_ref, workspace_ref, project_ref, context_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS cerebro_continuation_bindings (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    consumer_ref text NOT NULL,
    session_ref text NOT NULL,
    project_ref text NOT NULL,
    binding_id text NOT NULL,
    binding_revision bigint NOT NULL CHECK (binding_revision >= 1),
    context_ref text NOT NULL,
    basis_project_revision bigint NOT NULL CHECK (basis_project_revision >= 1),
    basis_session_revision bigint NOT NULL CHECK (basis_session_revision >= 1),
    active boolean NOT NULL DEFAULT false,
    binding_payload jsonb NOT NULL,
    binding_fingerprint text NOT NULL CHECK (binding_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz,
    PRIMARY KEY (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
        binding_id, binding_revision
    ),
    FOREIGN KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref)
        REFERENCES cerebro_control_session_bindings (
            tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref
        ),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref, context_ref)
        REFERENCES cerebro_control_contexts (
            tenant_ref, workspace_ref, project_ref, context_id
        ) DEFERRABLE INITIALLY DEFERRED
);

CREATE UNIQUE INDEX IF NOT EXISTS cerebro_one_active_binding_per_session
    ON cerebro_continuation_bindings (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref
    )
    WHERE active;

CREATE TABLE IF NOT EXISTS cerebro_control_events (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    consumer_ref text NOT NULL,
    session_ref text NOT NULL,
    project_ref text NOT NULL,
    event_id text NOT NULL,
    idempotency_key text NOT NULL,
    begin_request_fingerprint text NOT NULL CHECK (begin_request_fingerprint ~ '^[0-9a-f]{64}$'),
    completion_request_fingerprint text CHECK (
        completion_request_fingerprint IS NULL OR completion_request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    completion_fingerprint text CHECK (
        completion_fingerprint IS NULL OR completion_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    expected_project_revision bigint NOT NULL CHECK (expected_project_revision >= 1),
    expected_session_revision bigint NOT NULL CHECK (expected_session_revision >= 1),
    expected_project_fingerprint text NOT NULL CHECK (expected_project_fingerprint ~ '^[0-9a-f]{64}$'),
    expected_session_fingerprint text NOT NULL CHECK (expected_session_fingerprint ~ '^[0-9a-f]{64}$'),
    event_state text NOT NULL CHECK (event_state IN ('OPEN', 'COMPLETED', 'EXPIRED', 'BLOCKED')),
    lease_expires_at timestamptz,
    event_payload jsonb NOT NULL,
    opened_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id),
    UNIQUE (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, idempotency_key),
    FOREIGN KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref)
        REFERENCES cerebro_control_session_bindings (
            tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref
        )
);

CREATE TABLE IF NOT EXISTS cerebro_transition_receipts (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    consumer_ref text NOT NULL,
    session_ref text NOT NULL,
    event_id text NOT NULL,
    receipt_id text NOT NULL,
    result text NOT NULL CHECK (result IN ('PASS', 'BLOCKED')),
    mutated boolean NOT NULL,
    project_revision_before bigint NOT NULL,
    project_revision_after bigint NOT NULL,
    session_revision_before bigint NOT NULL,
    session_revision_after bigint NOT NULL,
    project_fingerprint_before text NOT NULL CHECK (project_fingerprint_before ~ '^[0-9a-f]{64}$'),
    project_fingerprint_after text NOT NULL CHECK (project_fingerprint_after ~ '^[0-9a-f]{64}$'),
    session_fingerprint_before text NOT NULL CHECK (session_fingerprint_before ~ '^[0-9a-f]{64}$'),
    session_fingerprint_after text NOT NULL CHECK (session_fingerprint_after ~ '^[0-9a-f]{64}$'),
    decision_ref text NOT NULL,
    active_context_ref_after text,
    transition_payload jsonb NOT NULL,
    receipt_fingerprint text NOT NULL CHECK (receipt_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, receipt_id),
    UNIQUE (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id),
    FOREIGN KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id)
        REFERENCES cerebro_control_events (
            tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id
        ),
    CHECK (
        result <> 'BLOCKED' OR (
            mutated = false AND
            project_fingerprint_before = project_fingerprint_after AND
            session_fingerprint_before = session_fingerprint_after
        )
    )
);

-- A domain transition receipt proves that the candidate passed semantic
-- validation.  This second immutable receipt proves that the exact transition
-- and exact after-state were accepted by the State Service transaction.  It is
-- only returned to a caller after the surrounding database COMMIT succeeds.
CREATE TABLE IF NOT EXISTS cerebro_state_commit_receipts (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    consumer_ref text NOT NULL,
    session_ref text NOT NULL,
    project_ref text NOT NULL,
    event_id text NOT NULL,
    commit_ref text NOT NULL,
    transition_receipt_id text NOT NULL,
    transition_receipt_fingerprint text NOT NULL CHECK (transition_receipt_fingerprint ~ '^[0-9a-f]{64}$'),
    transition_directive_fingerprint text NOT NULL CHECK (transition_directive_fingerprint ~ '^[0-9a-f]{64}$'),
    owner_effect_candidate_ref text,
    owner_effect_candidate_fingerprint text CHECK (
        owner_effect_candidate_fingerprint IS NULL OR owner_effect_candidate_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    owner_effect_candidate_payload jsonb,
    project_revision_after bigint NOT NULL CHECK (project_revision_after >= 1),
    session_revision_after bigint NOT NULL CHECK (session_revision_after >= 1),
    project_fingerprint_after text NOT NULL CHECK (project_fingerprint_after ~ '^[0-9a-f]{64}$'),
    session_fingerprint_after text NOT NULL CHECK (session_fingerprint_after ~ '^[0-9a-f]{64}$'),
    commit_payload jsonb NOT NULL,
    commit_fingerprint text NOT NULL CHECK (commit_fingerprint ~ '^[0-9a-f]{64}$'),
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, commit_ref),
    UNIQUE (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id),
    FOREIGN KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id)
        REFERENCES cerebro_control_events (
            tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id
        ),
    FOREIGN KEY (tenant_ref, workspace_ref, transition_receipt_id)
        REFERENCES cerebro_transition_receipts (tenant_ref, workspace_ref, receipt_id),
    FOREIGN KEY (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id
    ) REFERENCES cerebro_transition_receipts (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id
    ),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref)
        REFERENCES cerebro_project_instances (tenant_ref, workspace_ref, project_ref),
    CHECK (
        (owner_effect_candidate_ref IS NULL AND owner_effect_candidate_fingerprint IS NULL
            AND owner_effect_candidate_payload IS NULL)
        OR (owner_effect_candidate_ref IS NOT NULL AND owner_effect_candidate_fingerprint IS NOT NULL
            AND owner_effect_candidate_payload IS NOT NULL)
    )
);

-- Shared persistence mechanics for Project, Quality and Convergence owner
-- states.  The tables do not define owner semantics; each owner validator must
-- approve a complete candidate before this infrastructure may persist it.
CREATE TABLE IF NOT EXISTS cerebro_owner_state_heads (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    project_ref text NOT NULL,
    owner text NOT NULL CHECK (owner IN ('project', 'quality', 'convergence')),
    aggregate_ref text NOT NULL,
    current_state_ref text NOT NULL,
    owner_revision bigint NOT NULL CHECK (owner_revision >= 1),
    state_schema text NOT NULL,
    state_payload jsonb NOT NULL,
    state_fingerprint text NOT NULL CHECK (state_fingerprint ~ '^[0-9a-f]{64}$'),
    last_event_ref text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, project_ref, owner, aggregate_ref),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref)
        REFERENCES cerebro_project_instances (tenant_ref, workspace_ref, project_ref)
);

CREATE TABLE IF NOT EXISTS cerebro_owner_state_revisions (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    consumer_ref text NOT NULL,
    session_ref text NOT NULL,
    project_ref text NOT NULL,
    owner text NOT NULL CHECK (owner IN ('project', 'quality', 'convergence')),
    aggregate_ref text NOT NULL,
    owner_revision bigint NOT NULL CHECK (owner_revision >= 1),
    event_id text NOT NULL,
    idempotency_key text NOT NULL,
    input_state_ref text,
    input_state_fingerprint text CHECK (
        input_state_fingerprint IS NULL OR input_state_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    output_state_ref text NOT NULL,
    output_state_fingerprint text NOT NULL CHECK (output_state_fingerprint ~ '^[0-9a-f]{64}$'),
    owner_effect_candidate_ref text,
    owner_effect_candidate_fingerprint text CHECK (
        owner_effect_candidate_fingerprint IS NULL OR owner_effect_candidate_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    control_decision_ref text,
    consolidation_result_ref text,
    state_schema text NOT NULL,
    state_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
        tenant_ref, workspace_ref, project_ref, owner, aggregate_ref, owner_revision
    ),
    UNIQUE (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id),
    FOREIGN KEY (tenant_ref, workspace_ref, project_ref, owner, aggregate_ref)
        REFERENCES cerebro_owner_state_heads (
            tenant_ref, workspace_ref, project_ref, owner, aggregate_ref
        ) DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (owner_effect_candidate_ref IS NULL AND owner_effect_candidate_fingerprint IS NULL)
        OR (owner_effect_candidate_ref IS NOT NULL AND owner_effect_candidate_fingerprint IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS cerebro_owner_state_commit_receipts (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    consumer_ref text NOT NULL,
    session_ref text NOT NULL,
    project_ref text NOT NULL,
    owner text NOT NULL CHECK (owner IN ('project', 'quality', 'convergence')),
    aggregate_ref text NOT NULL,
    event_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    commit_kind text NOT NULL CHECK (commit_kind IN ('INITIALIZE', 'OWNER_EFFECT')),
    commit_ref text NOT NULL,
    owner_revision_before bigint NOT NULL CHECK (owner_revision_before >= 0),
    owner_revision_after bigint NOT NULL CHECK (owner_revision_after >= 1),
    input_state_ref text,
    input_state_fingerprint text CHECK (
        input_state_fingerprint IS NULL OR input_state_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    output_state_ref text NOT NULL,
    output_state_fingerprint text NOT NULL CHECK (output_state_fingerprint ~ '^[0-9a-f]{64}$'),
    owner_effect_candidate_ref text,
    owner_effect_candidate_fingerprint text CHECK (
        owner_effect_candidate_fingerprint IS NULL OR owner_effect_candidate_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    commit_payload jsonb NOT NULL,
    commit_fingerprint text NOT NULL CHECK (commit_fingerprint ~ '^[0-9a-f]{64}$'),
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, commit_ref),
    UNIQUE (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id),
    UNIQUE (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, idempotency_key),
    FOREIGN KEY (
        tenant_ref, workspace_ref, project_ref, owner, aggregate_ref, owner_revision_after
    ) REFERENCES cerebro_owner_state_revisions (
        tenant_ref, workspace_ref, project_ref, owner, aggregate_ref, owner_revision
    ) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id
    ) REFERENCES cerebro_owner_state_revisions (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id
    ) DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (commit_kind = 'INITIALIZE' AND owner_revision_before = 0
            AND input_state_ref IS NULL AND input_state_fingerprint IS NULL
            AND owner_effect_candidate_ref IS NULL AND owner_effect_candidate_fingerprint IS NULL)
        OR
        (commit_kind = 'OWNER_EFFECT' AND owner_revision_before >= 1
            AND input_state_ref IS NOT NULL AND input_state_fingerprint IS NOT NULL
            AND owner_effect_candidate_ref IS NOT NULL AND owner_effect_candidate_fingerprint IS NOT NULL)
    ),
    CHECK (owner_revision_after = owner_revision_before + 1)
);

CREATE OR REPLACE FUNCTION cerebro_reject_immutable_ledger_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $cerebro_immutable_ledger$
BEGIN
    RAISE EXCEPTION 'cerebro protected state rows cannot be updated or deleted'
        USING ERRCODE = '55000';
END
$cerebro_immutable_ledger$;

DO $cerebro_immutable_triggers$
DECLARE
    table_name text;
    trigger_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_project_basis_revisions',
        'cerebro_project_bootstrap_receipts',
        'cerebro_transition_receipts',
        'cerebro_state_commit_receipts',
        'cerebro_owner_state_revisions',
        'cerebro_owner_state_commit_receipts'
    ] LOOP
        trigger_name := table_name || '_immutable';
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', trigger_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
            || 'FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation()',
            trigger_name,
            table_name
        );
    END LOOP;
END
$cerebro_immutable_triggers$;

DO $cerebro_mutable_state_no_delete$
DECLARE
    table_name text;
    trigger_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_project_instances',
        'cerebro_principal_project_bindings',
        'cerebro_control_contexts',
        'cerebro_control_session_bindings',
        'cerebro_continuation_bindings',
        'cerebro_control_events',
        'cerebro_owner_state_heads'
    ] LOOP
        trigger_name := table_name || '_no_delete';
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', trigger_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE DELETE ON %I '
            || 'FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation()',
            trigger_name,
            table_name
        );
    END LOOP;
END
$cerebro_mutable_state_no_delete$;

-- Defense in depth: the API transaction must SET LOCAL these values from a
-- verified OAuth identity before touching state. Missing settings match no rows.
-- The service role must not own these tables or have BYPASSRLS.
DO $cerebro_rls$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_project_instances',
        'cerebro_project_basis_revisions',
        'cerebro_control_contexts',
        'cerebro_owner_state_heads',
        'cerebro_owner_state_revisions'
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

    FOREACH table_name IN ARRAY ARRAY[
        'cerebro_principal_project_bindings',
        'cerebro_project_bootstrap_receipts',
        'cerebro_control_session_bindings',
        'cerebro_continuation_bindings',
        'cerebro_control_events',
        'cerebro_transition_receipts',
        'cerebro_state_commit_receipts',
        'cerebro_owner_state_commit_receipts'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS cerebro_principal_isolation ON %I', table_name);
        EXECUTE format(
            'CREATE POLICY cerebro_principal_isolation ON %I USING '
            || '(tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true) '
            || 'AND principal_ref = current_setting(''cerebro.principal_ref'', true)) '
            || 'WITH CHECK (tenant_ref = current_setting(''cerebro.tenant_ref'', true) '
            || 'AND workspace_ref = current_setting(''cerebro.workspace_ref'', true) '
            || 'AND principal_ref = current_setting(''cerebro.principal_ref'', true))',
            table_name
        );
    END LOOP;
END
$cerebro_rls$;

-- A service transaction must lock the project aggregate and the calling session row,
-- compare both expected revisions and fingerprints, validate the complete candidate,
-- update projections, complete the event and insert exactly one receipt before COMMIT.
-- The exact state-commit receipt is returned only after the database COMMIT call succeeds.
-- Focus-only session transitions do not increment the shared project-tree revision.
-- The service role must not be granted repository credentials or unrelated schemas.
-- Runtime grants must exclude DELETE on all state tables and UPDATE on immutable ledgers.
-- An idempotency-key replay with a different request fingerprint is a conflict, not a retry.
