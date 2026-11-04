-- Assistant overlay current owner policy + decision heads.
-- Read-only from the MCP surface. Population remains an explicit owner/control operation.

CREATE TABLE IF NOT EXISTS cerebro_assistant_overlay_policy_heads (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    consumer_ref text NOT NULL,
    session_ref text NOT NULL,
    policy_ref text NOT NULL,
    policy_revision bigint NOT NULL CHECK (policy_revision >= 1),
    current boolean NOT NULL,
    revoked boolean NOT NULL,
    owner_authorization_ref text NOT NULL,
    allowed_role text NOT NULL CHECK (allowed_role = 'ASSISTANT'),
    source_revision text NOT NULL CHECK (source_revision ~ '^[0-9a-f]{40}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, policy_ref
    ),
    UNIQUE (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
        policy_ref, policy_revision
    ),
    CHECK (NOT (current AND revoked))
);

CREATE TABLE IF NOT EXISTS cerebro_assistant_overlay_decision_heads (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    consumer_ref text NOT NULL,
    session_ref text NOT NULL,
    generation_ref text NOT NULL,
    decision_ref text NOT NULL,
    policy_ref text NOT NULL,
    policy_revision bigint NOT NULL CHECK (policy_revision >= 1),
    owner_revision bigint NOT NULL CHECK (owner_revision >= 1),
    current boolean NOT NULL,
    revoked boolean NOT NULL,
    pre_role_fingerprint text NOT NULL CHECK (pre_role_fingerprint ~ '^[0-9a-f]{64}$'),
    ready_unbound_receipt_fingerprint text NOT NULL CHECK (ready_unbound_receipt_fingerprint ~ '^[0-9a-f]{64}$'),
    expected_revision bigint NOT NULL CHECK (expected_revision >= 1),
    source_revision text NOT NULL CHECK (source_revision ~ '^[0-9a-f]{40}$'),
    overlay_ref text NOT NULL,
    actor_ref text NOT NULL,
    actor_generation_ref text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, generation_ref),
    UNIQUE (tenant_ref, workspace_ref, decision_ref),
    CHECK (NOT (current AND revoked)),
    FOREIGN KEY (tenant_ref, workspace_ref, generation_ref)
        REFERENCES cerebro_pre_role_generation_heads (tenant_ref, workspace_ref, generation_ref),
    FOREIGN KEY (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
        policy_ref, policy_revision
    ) REFERENCES cerebro_assistant_overlay_policy_heads (
        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
        policy_ref, policy_revision
    )
);

CREATE INDEX IF NOT EXISTS cerebro_assistant_overlay_policy_current_idx
    ON cerebro_assistant_overlay_policy_heads
       (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
        policy_ref, current, revoked, policy_revision);

CREATE INDEX IF NOT EXISTS cerebro_assistant_overlay_decision_current_idx
    ON cerebro_assistant_overlay_decision_heads
       (tenant_ref, workspace_ref, generation_ref, current, revoked, owner_revision);
