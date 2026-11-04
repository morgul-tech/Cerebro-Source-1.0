"""Proposed small canonical constructor component; no process/env/credential IO.

The actual deploy-only app selects the profile and explicit existing factory.
This module cannot authorize intake, role attachment or CURRENT transfer.
"""
from __future__ import annotations


def construct_pm_admission_owner(connection_factory, profile):
    from pm_admission_owner import PostgresPmAdmissionOwner, PROFILE
    from control_context_state_postgres import PostgresControlContextStatePort
    if profile not in ('', PROFILE):
        raise RuntimeError('PM_ADMISSION_EXPLICIT_PROFILE_REQUIRED')
    return (PostgresPmAdmissionOwner(PostgresControlContextStatePort(connection_factory))
            if profile else None)


def bind_pm_admission_owner(runtime, owner, attestor):
    if owner is not None:
        runtime.service._tools.bind_pm_admission(owner, attestor)
