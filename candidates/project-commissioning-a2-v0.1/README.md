# Project Engine commissioning A2 — isolated Source candidate

Authority: P22 A2 Human YES, no live effect. This directory and the changed
Source files are a review candidate only. The new internal service method is
not in the MCP tool registry or HTTP routes, and the deployed Context app does
not inject a Project issuer or lineage authorizer. No Project instance, Project
Basis, owner state, closure record or Signalvev event is created by admission.

## Reuse provenance

The deployed Context repository at commit
`74a60cd58c67500a7043bcb54aba22128a53bab4`,
`deployment/app.py:251-260`, constructs one private
`HmacControlResolutionAttestor` and passes that same object as the Context
resolution verifier and Boot birth issuer. A2 extends this constructor custody
pattern only if the host explicitly injects that **same object** for Project
commissioning. A different issuer is rejected. No new secret or key is supplied
by this candidate. This proves a reuse path, not a deployed Project bridge.

## Candidate coverage

| Contract edge | Candidate state |
| --- | --- |
| OAuth-derived identity and both scopes | Enforced by internal service method and bridge |
| `params.meta` excluded from commissioning authority | Internal method accepts no request metadata; bridge uses server-minted context |
| Exact X9 project/aggregate lineage | Required constructor-bound authorizer; no IDs frozen in Source |
| Project collision | Provider read before bootstrap; existing project HOLD except exact partial recovery |
| Private operation-scoped attestation | Same issuer/verifier object; only `create_project_control_instance` sealed internally |
| Explicit non-default Context bootstrap | `make_default=False`; provider before/after binding reads including absence and fingerprint validation |
| Server-minted session | 256-bit CSPRNG selector; separate binding ID; exact scoped bind and provider readback |
| Prior/duplicate session | Pre-read plus PostgreSQL transaction advisory lock and project-scoped exclusive check |
| Partial Context recovery | Explicit exact-request replay through existing event ID and request fingerprint; only if prior project exists and no commissioning session is bound |
| Project Basis rev1 and INITIALIZE receipt | Not implemented; Gate B only after separate proof |
| Project executor/capability | Not implemented; requires X4/X5 next review |
| Project-owned selected closure ReturnSink | Not implemented; ACK_READ with HOLD_RETURN remains NOT_CLOSED |
| DB role exclusivity, deployed signer injection, independent live readback | Unproven; X3/host evidence required before Gate B |

## Stop rules

Any mismatched identity, project, signer, existing session, default binding,
provider readback or replay request holds. Unknown bootstrap outcome is not
retried automatically. A bootstrap success followed by bind/readback failure
returns `PARTIAL_CONTEXT_ONLY`; Project Basis must not advance. All fixture
attestations in offline tests are test-only and prove no production authority.

Test entry: `python tooling/validator/project_commissioning_candidate_validation.py`.
Writer-distinct next owner: X5. Gate B remains reserved.
