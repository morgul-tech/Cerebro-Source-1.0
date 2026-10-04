-- NAL-01 candidate migration v1. NOT installed by this branch.
-- Deployment must provision cerebro_nal_owner separately from Context's owner.
BEGIN;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cerebro_nal_owner') THEN
        RAISE EXCEPTION 'cerebro_nal_owner role required before NAL migration';
    END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS nal;
REVOKE ALL ON SCHEMA nal FROM PUBLIC;
ALTER SCHEMA nal OWNER TO cerebro_nal_owner;

CREATE TABLE IF NOT EXISTS nal.schema_migration (
    version integer PRIMARY KEY CHECK (version > 0),
    name text NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nal.actor_state (
    actor_ref text PRIMARY KEY,
    generation_ref text NOT NULL,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision >= 1),
    state_json jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (state_json->>'schema' = 'cerebro.nal.lifecycle/v1'),
    CHECK (state_json->>'actor_ref' = actor_ref),
    CHECK (state_json->>'generation_ref' = generation_ref)
);

CREATE TABLE IF NOT EXISTS nal.binding_issue (
    binding_ref text PRIMARY KEY,
    actor_ref text NOT NULL REFERENCES nal.actor_state(actor_ref),
    owner_revision text NOT NULL,
    binding_json jsonb NOT NULL,
    UNIQUE (actor_ref, binding_ref)
);
CREATE TABLE IF NOT EXISTS nal.binding_supersession (
    old_binding_ref text PRIMARY KEY REFERENCES nal.binding_issue(binding_ref),
    new_binding_ref text NOT NULL UNIQUE REFERENCES nal.binding_issue(binding_ref),
    owner_revision text NOT NULL,
    event_json jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS nal.terminal_occurrence (
    actor_ref text NOT NULL REFERENCES nal.actor_state(actor_ref),
    terminal_ref text NOT NULL,
    terminal_digest text NOT NULL CHECK (terminal_digest ~ '^[0-9a-f]{64}$'),
    binding_ref text NOT NULL REFERENCES nal.binding_issue(binding_ref),
    claim_ref text NOT NULL,
    packet_ref text NOT NULL,
    queue_ref text NOT NULL,
    occurrence_json jsonb NOT NULL,
    PRIMARY KEY (actor_ref, terminal_ref)
);
CREATE TABLE IF NOT EXISTS nal.close_receipt (
    receipt_ref text PRIMARY KEY,
    actor_ref text NOT NULL REFERENCES nal.actor_state(actor_ref),
    terminal_ref text NOT NULL,
    receipt_json jsonb NOT NULL,
    UNIQUE (actor_ref, terminal_ref),
    FOREIGN KEY (actor_ref, terminal_ref)
        REFERENCES nal.terminal_occurrence(actor_ref, terminal_ref)
);
CREATE TABLE IF NOT EXISTS nal.outbox_intent (
    event_ref text PRIMARY KEY,
    receipt_ref text NOT NULL UNIQUE REFERENCES nal.close_receipt(receipt_ref),
    event_json jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS nal.outbox_delivery (
    event_ref text PRIMARY KEY REFERENCES nal.outbox_intent(event_ref),
    delivery_receipt text NOT NULL
);

CREATE OR REPLACE FUNCTION nal.reject_immutable_ledger_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'NAL immutable ledger mutation rejected';
END $$;

DO $$ DECLARE table_name text; BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'binding_issue', 'binding_supersession', 'terminal_occurrence',
        'close_receipt', 'outbox_intent', 'outbox_delivery'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS immutable_row ON nal.%I', table_name);
        EXECUTE format('CREATE TRIGGER immutable_row BEFORE UPDATE OR DELETE ON nal.%I '
                       'FOR EACH ROW EXECUTE FUNCTION nal.reject_immutable_ledger_change()', table_name);
    END LOOP;
END $$;

INSERT INTO nal.schema_migration(version, name)
VALUES (1, 'natural-actor-lifecycle-v0.1')
ON CONFLICT (version) DO NOTHING;

DO $$ DECLARE table_name text; BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'schema_migration', 'actor_state', 'binding_issue', 'binding_supersession',
        'terminal_occurrence', 'close_receipt', 'outbox_intent', 'outbox_delivery'
    ] LOOP
        EXECUTE format('ALTER TABLE nal.%I OWNER TO cerebro_nal_owner', table_name);
        EXECUTE format('REVOKE ALL ON nal.%I FROM PUBLIC', table_name);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cerebro_context_owner') THEN
            EXECUTE format('REVOKE ALL ON nal.%I FROM cerebro_context_owner', table_name);
        END IF;
    END LOOP;
END $$;
ALTER FUNCTION nal.reject_immutable_ledger_change() OWNER TO cerebro_nal_owner;
COMMIT;
