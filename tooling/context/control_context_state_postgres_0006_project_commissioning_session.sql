-- A2 candidate only. Never apply under Gate A2; a later gate must authorize
-- privileged preflight and migration. Existing Context tables remain owner.
DO $commissioning_preflight$
DECLARE
    can_see_all boolean;
BEGIN
    SELECT (rolsuper OR rolbypassrls) INTO can_see_all
      FROM pg_roles WHERE rolname = current_user;
    IF can_see_all IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'commissioning-project-session-visibility-unproven'
            USING ERRCODE = '42501';
    END IF;
    IF to_regclass('cerebro_one_project_commissioning_session') IS NOT NULL THEN
        RAISE EXCEPTION 'commissioning-project-session-index-preexists-unverified'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1 FROM cerebro_control_session_bindings
         WHERE project_ref IS NOT NULL
           AND session_ref ~ '^project-commissioning:[A-Za-z0-9_-]{43}$'
         GROUP BY tenant_ref, workspace_ref, project_ref
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'commissioning-project-session-duplicates-present'
            USING ERRCODE = '23505';
    END IF;
END
$commissioning_preflight$;

CREATE UNIQUE INDEX cerebro_one_project_commissioning_session
    ON cerebro_control_session_bindings (tenant_ref, workspace_ref, project_ref)
    WHERE project_ref IS NOT NULL
      AND session_ref ~ '^project-commissioning:[A-Za-z0-9_-]{43}$';
