-- Candidate only: Rom A admission custody in the existing owner transaction
-- service. No migration is authorized by this Source candidate itself.
-- These records are not Project/Quality/Convergence owner-effect receipts.
CREATE TABLE cerebro_rom_a_owner_episodes (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    episode_ref text NOT NULL,
    owner_revision bigint NOT NULL CHECK (owner_revision >= 1),
    owner_payload jsonb NOT NULL,
    admission_basis text CHECK (admission_basis IS NULL OR admission_basis ~ '^[0-9a-f]{64}$'),
    admission_state text NOT NULL DEFAULT 'NONE'
        CHECK (admission_state IN ('NONE', 'ADMITTED_UNCERTAIN')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, episode_ref),
    CHECK ((admission_basis IS NULL AND admission_state = 'NONE')
        OR (admission_basis IS NOT NULL AND admission_state = 'ADMITTED_UNCERTAIN'))
);

CREATE TABLE cerebro_rom_a_admissions (
    tenant_ref text NOT NULL,
    workspace_ref text NOT NULL,
    principal_ref text NOT NULL,
    episode_ref text NOT NULL,
    basis_fingerprint text NOT NULL CHECK (basis_fingerprint ~ '^[0-9a-f]{64}$'),
    owner_revision bigint NOT NULL CHECK (owner_revision >= 1),
    admission_state text NOT NULL DEFAULT 'ADMITTED_UNCERTAIN'
        CHECK (admission_state = 'ADMITTED_UNCERTAIN'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, episode_ref, basis_fingerprint),
    FOREIGN KEY (tenant_ref, workspace_ref, principal_ref, episode_ref)
        REFERENCES cerebro_rom_a_owner_episodes
            (tenant_ref, workspace_ref, principal_ref, episode_ref)
);

CREATE TRIGGER cerebro_rom_a_admissions_immutable
    BEFORE UPDATE OR DELETE ON cerebro_rom_a_admissions
    FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation();
CREATE TRIGGER cerebro_rom_a_owner_episodes_no_delete
    BEFORE DELETE ON cerebro_rom_a_owner_episodes
    FOR EACH ROW EXECUTE FUNCTION cerebro_reject_immutable_ledger_mutation();

ALTER TABLE cerebro_rom_a_owner_episodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_rom_a_owner_episodes FORCE ROW LEVEL SECURITY;
ALTER TABLE cerebro_rom_a_admissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cerebro_rom_a_admissions FORCE ROW LEVEL SECURITY;
CREATE POLICY cerebro_rom_a_episode_principal_isolation ON cerebro_rom_a_owner_episodes
    USING (tenant_ref = current_setting('cerebro.tenant_ref', true)
        AND workspace_ref = current_setting('cerebro.workspace_ref', true)
        AND principal_ref = current_setting('cerebro.principal_ref', true))
    WITH CHECK (tenant_ref = current_setting('cerebro.tenant_ref', true)
        AND workspace_ref = current_setting('cerebro.workspace_ref', true)
        AND principal_ref = current_setting('cerebro.principal_ref', true));
CREATE POLICY cerebro_rom_a_admission_principal_isolation ON cerebro_rom_a_admissions
    USING (tenant_ref = current_setting('cerebro.tenant_ref', true)
        AND workspace_ref = current_setting('cerebro.workspace_ref', true)
        AND principal_ref = current_setting('cerebro.principal_ref', true))
    WITH CHECK (tenant_ref = current_setting('cerebro.tenant_ref', true)
        AND workspace_ref = current_setting('cerebro.workspace_ref', true)
        AND principal_ref = current_setting('cerebro.principal_ref', true));
