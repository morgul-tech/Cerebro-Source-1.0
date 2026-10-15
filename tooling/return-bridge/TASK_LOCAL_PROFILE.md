# Task-local profile binding, version 1
This optional contract accompanies strict Rom A dispatch-capture/v1 without adding
fields to its request, capture receipt or historical original. Profile selection
is not identity, a grant, tool availability or evidence of actual use.

## Ordinary dispatch and consumer
Pass a profile JSON with exactly source_ref, source_revision, primary_coat,
lenses, packages and required_tools to the existing capture CLI:
python -B tooling/return-bridge/rom_a_dispatch_capture.py --request REQUEST \
  --original ORIGINAL --capture-root ROOT --profile-binding PROFILE
The existing atomic capture directory additionally contains task_local_profile.json.
It binds the exact canonical capture receipt hash, exact original hash, task/revision,
actor/generation and the existing effect/scope/path invariants to the chosen profile.
Old capture bytes and key remain unchanged. Same-task profile drift is a collision;
historical captures without a sidecar cannot be retrofitted by this API.

The existing rom_a_normal_path.py CLI automatically reads a present sidecar,
revalidates its exact bytes and capture/task/scope binding, and records it in the
normal receipt. A malformed present sidecar refuses, before output creation.
Absent sidecars follow the unchanged legacy flow. The sidecar must travel together
with capture.json and original_task.txt in a profile-bound assignment; deleting
it makes that capture legacy and cannot establish a profile-binding claim.
Profile information never modifies selected original/capsule bytes or sender grants.

Optional --profile-observation OBSERVATION consumes a separately sourced return
record with schema cerebro-task-local-profile/v1-observation and exactly:
binding_sha256, task_ref, task_revision, actor_ref, generation_ref,
profile_source_ref, profile_source_revision, uptake, result, recipient_use,
relevant_delta, available_tools (plus schema).
uptake: {state: NOT_ATTESTED|LOADED|USED, evidence_ref: null or actual source ref}.
result: NOT_OBSERVED|DELIVERED; recipient_use: NOT_OBSERVED|USED;
relevant_delta: NOT_OBSERVED|NONE_REPORTED|REPORTED; same two-field objects.
Every positive state requires a nonblank caller source ref. Unknown states require null.
available_tools items: {tool_ref, state: AVAILABLE|UNAVAILABLE|NOT_ATTESTED,
evidence_ref}. USED uptake requires every necessary tool locally recorded AVAILABLE.
A necessary UNAVAILABLE tool yields PROFILE_REQUIRED_TOOL_UNAVAILABLE:<exact tool>.
Observation task/revision/worker/generation/profile source+revision and exact
sidecar hash must match. Recipient USED requires a separately recorded DELIVERED
result. A suggested profile alone defaults to NOT_ATTESTED, not LOADED or USED.

All records say LOCAL_RECORDED_NOT_IDENTITY_TOOL_GRANT_OR_USE_ATTESTATION.
authority=NONE and identity/tools/recipient-use attestation flags remain false
even with locally sourced USED reports. The existing normal receipt's outer
recipient_use remains NOT_OBSERVED: that is selected-payload delivery/use, distinct
from caller-reported use of the worker result. No provider/native probe or grant.
Unknown generation strings remain caller values, not a new invented generation.

## Care and natural uptake
The consumer retains relevant_delta's actual source reference for room -> X9 ->
bounded Gartner care if material; it never wakes Gartner or invents a care result.
Same actor/generation may choose a different primary coat on a different task.
A new profile revision for an already frozen task revision requires a new task
revision; never rewrite history. No BOOT or formal role update is involved.

## Selected A–E workwave method fit
Use these clarifications only within an already selected task and the existing
mechanisms below. Before substantive action, confirm the owner-selected task/revision/original
binding, reached stage, pending obligations and next action. Reuse the held
original within the same arc when it is current and sufficient; read the full
original when the task/revision changes or the held basis is missing, stale or
insufficient. Before an independent assignment, check the selected actor's current work,
pending intent/assignment, uptake and required-tool fit; a material input to the
same active task is not a new assignment. Existing owner observations are not an
atomic scheduler lock.

At the first unexpected failure, preserve the exact attempt, reached stage,
mutation and effect state; compare a challenged validator premise with the actual
installed contract before changing the target, and reconcile UNKNOWN effects on
the same attempt before any repeat. Before return, derive hashes, counts, dates
and source/chain mappings from the final materialized candidate; keep mechanical
self-check separate from semantic review. For a declared owner dependency, close
only on a matched current owner release and the next consumer's USED evidence
linked to that release. Report read operations, unique files/bytes and cumulative
bytes separately; distinguish emitted/model-context bytes from actual token
telemetry. After the task's material budget, return the supported partial result
and named rest. These are method clarifications, not new runtime gates or proof
of actor use.

Validation: python -B tooling/validator/task_local_profile_validation.py
The fixture assertions are isolated test observations, never actual actor/tool proof.
After solewriter publication + exact parent/path/byte readback, use this binding
in one already necessary consumer job and later natural other-room reuse.
Candidate tests do not prove deployment, identity, recipient use or civilization-wide
adoption. A1 retains Source admission/publication. Way home: omit the optional
profile on future legacy assignments; preserve old immutable captures and stop
on current sidecar mismatch. No global dispatch engine, service or gate.

## Versioned first-error/material-return/context-break encounter
The ordinary rom_a_normal_path.prepare API now catches its own local preparation
ValueError/OSError and attaches work_error_reflex metadata while preserving the
original exception/refusal. This reports LOCAL_PREPARATION, UNKNOWN cause and no
native effect; it is not an assertion about another worker's tool/runtime failure.
CLI refusal exposes that metadata. No provider/credential/BOOT operation occurs.

For a worker's actual relevant error, material B->A finding or context break:
put work_error.json beside the normal return observation, or pass
--work-error-observation ABS_FILE. The unchanged strict return/request schemas
remain unchanged. The new independent schema cerebro-work-error-reflex/v1 binds:
binding={task_ref,task_revision,actor_ref,generation_ref,carrier_ref,arc_ref,
original_sha256}; exact profile_binding_sha256 (null only for legacy no-profile);
trigger FIRST_UNEXPECTED_ERROR|MATERIAL_RETURN|CONTEXT_BREAK; observation_ref;
attempt_ref; reached_stage; effect_state; cause={state,layer,evidence_ref};
challenged_premise={ref,state}; inherited_countermodel_refs;
next_step={kind,justification_ref,proof_limit_ref,attempt_ref,uses_premise_ref};
retry; runtime_evidence={code,startup,operation,cleanup,evidence_refs}.
All source refs and states are attributed caller metadata, not proven causality,
identity or permission. Never include raw private logs, Doc body, keys or credentials.

Consumer evaluate runs before new normal output; it preserves exact task/profile,
original and challenged premise in the receipt. Falsified-premise reuse, wrong
task/profile and causes assigned to unreached layers refuse. Unknown cause
qualifies diagnosis/return finding, not an assumed quickfix. Unknown mutation
requires RECONCILE with the SAME attempt before a next material dispatch.
Existing _dispatch_selected rejects an unqualified next material step before
intent or sender call. This controls this selected Source path, not every Work
tool or runtime actor. Reading a finding receipt remains possible while held.

For Human-directed Cerebro BOOT, apply the learning-loop's existing first-failure
seam `selected_workwave_refinement.first_failure.human_directed_boot_model_policy`:
Human selects Sol 6.1 with at least Medium reasoning. Luna, lower reasoning or
another substitution requires a new explicit Human choice; other task-model
policies stay unchanged. At BOOT failure inspect actual selected model/reasoning
(UNKNOWN if unavailable), observed error and concrete required-sequence deviation
together before technical rebuilding. Selected configuration is not serving-model
proof. Human's repeated low-reasoning shortcuts and Sol 6.1 Medium BOOT PASS are
first-hand operational testimony, not a controlled model study or proof that all
Luna configurations fail. A demonstrated server defect still needs its actual
repair. This method text changes no model setting, BOOT, role or runtime state.

At task entry or handoff/environment change, compare the required environment
with the actual selection using available held evidence: WorkMode does not prove
local disk or callable filesystem access. If selection is unknown for a local
file/exec gap, obtain one targeted Human observation for the named actor before
provider/credential/host diagnosis; no room hunt or routine per-step check.
Human currently controls window opening/transfer. The A3/A4 cloud/disk mismatch
is Human-observed; post-transfer local access remains unobserved.

Activate that existing reflex at whole-job selection and a material dependency or
architecture change. Name the shortest necessary host/surface/caller/read-write/
permission-owner path to the next consumer. Reuse valid held evidence first;
resolve only indispensable uncertain ports with the least sufficient observation
before dependent building. Recheck on relevant access change, expiry, lost basis
or actual error, not every step or service. Preserve pending/full originals and
continue independent work. Human DONE is not own-surface permission readback.

For an actual unlock, read the existing protected receipt for the selected session
first, checking actual release fingerprint and freshness. PID or empty window title
is not prompt state; missing receipt is UNKNOWN, not proof of a failed key or Human
error. UI is for necessary prompt guidance or absent qualified receipt. Validate
the actually finished whole release against its existing typed launcher contract
before calling Human. Reuse its status/result path, not per-step model calls/ACKs.
Explicit launcher state transitions remain named next maintenance, not a new gate
or change to the running frozen package.

Only a relevant external contract/tool/metadata exposure change activates the
consumer chain: implemented -> deployed -> wire-advertised -> consumer registered/
callable -> own authenticated action -> useful result. Server catalog/test/health
does not prove consumer use. Plan needed Human metadata action before a short
privileged window; refresh after descriptors are actually available, before the
dependent consumer action, not on local code alone. Context Refresh tools and OAuth
Reconnect are distinct; do not assume substitution. After a genuinely new completed
action the same consumer checks its own catalog/binding once. If uptake is missing,
qualify that link and any documented same-actor client continuity path; do not
default to new worker/BOOT, redeploy, reconnect loop or privilege-window extension.
Keep source publication and actual uptake/use as separate evidence. The R1-R3
owner case preserves planning/serialization/observation mistakes without Human/key
blame; later own W ACK, one committed publication, method950 adoption and bounded
entry use qualify the publication boundary, not an isolated cache-cause claim.
Historical W method950 adoption is never rewritten or repeated just for a new HEAD.

At an actual access boundary, apply the existing learning-loop
`human_action_first_normal_return.access_boundary_clarification` before building
alternate access or repeating host/device investigation. Human's first-hand
reconnect/login/unlock observations are material evidence, not a diagnosis of
every refusal. Return the actual actor, service, original action and observed
error through the main conversation, with the exact known safe window/locator,
expected prompt and one needed Human action; say UNKNOWN rather than inventing
a window or leaving Human to search rooms. Prepare coherent work before unlock,
reuse only actually valid authorized access, and keep secrets out of chat.
Reconnect grants no missing write right. After Human reports done, continue the
original path in a bounded way with existing same-attempt/UNKNOWN-effect rules;
an unchanged failure gets a precise remainder, not another prompt loop. This is
method guidance, not a new schema, helper, universal approval gate or runtime
enforcement. Actual use in the A3/A4 case was stopping old-carrier diagnosis and
preparing Human-selected Work continuation; transfer remains NOT_OBSERVED.

Known PROVEN narrow local fix remains DIRECT_KNOWN_FIX without deep ceremony.
BOUNDED_RETRY requires exact {contract_ref,evidence_ref,qualified,idempotent,
attempt_number,max_attempts,expires_at,failure_class}; KNOWN_TRANSIENT, positive
bounded count and unexpired finite epoch. Time alone never qualifies semantic retry;
unknown effect still takes precedence. No new universal retry count is imposed.

Deeper relevant encounters call existing relevance_engine.retrieve with
SOURCE_PUBLIC_ENCOUNTER_V1; empty/omitted Source encounter is retained honestly,
not a fabricated inherited countermodel. Caller countermodel refs are attributed
separately. Existing VINKELPASS resolve_solution_escalation_preflight is invoked
only for actual proposed STRUCTURAL_CHANGE/new-mechanism, after unknown-effect
reconciliation priority. Existing learning-loop/reflective-loop files are hashed
method references; no duplicate loop or A7 admission, Wisdom or review ring is created.

Code PASS, startup ACTIVE/FAILED, operation READBACK/REFUSED and ERROR_STOP_CLOSED
cleanup are separate owner-reported checkpoints. runtime_closed always remains
false here. D1's public seven failures, narrow process/socket diagnosis, E2 fix,
eighth ACTIVE/R7 and R10 unknown module/ERROR_STOP are semantic witnesses only,
not A3 runtime audit or a live replay. JS refusal cause likewise stays UNKNOWN.

Validation: python -B tooling/validator/work_error_reflex_validation.py
Only new affected-risk fixtures; previous task-profile/Human-method results retained.
Recovery/version: preserve previous immutable captures/sidecars and exact old
candidate artifacts; new event is optional, versioned and never edits v1 originals.
On unknown next effect keep same-attempt evidence and no-repeat disposition.
Later solewriter publication/exact-byte readback, one necessary worker encounter
and recipient-used return, then natural other-room use remain separate proof.

## Optional task-local capacity and earned-care binding
The additive `cerebro-task-capacity-care/v1` sidecar binds one owner-supplied
canonical capacity observation to the existing capture receipt, original bytes,
task/revision, and task-local profile binding. Use
`rom_a_dispatch_capture.py --profile-binding PROFILE --capacity-observation CUT`.
The observation is compact sorted UTF-8 JSON with one trailing LF and binds a
cut ref/revision, observer, task/revision, worker records, and prior care readback.
The capture request/receipt remain strict v1 and unchanged; the capacity sidecar
is optional and same-key byte changes refuse as a collision.

Each worker record carries its own state, whole-task fit (`FIT`, `NO_FIT`,
`INPUT_ONLY`, or `UNKNOWN`), evidence references, profile source/revision, and
separate UI state. Non-unknown availability and fit each require an evidence ref.
`RUNNING`, `RESERVED`, and `DEPENDENCY_WAIT` are occupied even
when UI says `IDLE`; only `AVAILABLE` plus `FIT` is eligible. `INPUT_ONLY` is
reported separately and cannot become a whole-task bottleneck. The selected
task's actor/generation must match the observed eligible worker. If another
eligible worker exists, the path returns `CAPACITY_REDISTRIBUTION_REQUIRED` to
the owner route; it does not start a Human choice loop or rewrite the frozen
task. A refusal includes cut/task refs and bounded whole-fit/input-only worker
refs with evidence pointers so the owner route has concrete redistribution
input. These are caller-observed records, not a native lock or scheduler.

For capacity-bound results, the normal host accepts an
`OwnerCapacityObservationReader` with two fixed absolute paths:
`dispatch_record` and `uptake_record`. Each file is one canonical LF-terminated
`cerebro-task-capacity-owner-observation/v1` record with exactly `record_ref`,
`owner_ref`, `stage` (`DISPATCH` or `UPTAKE`), exact `task_ref` and
`task_revision`, `source_ref`, `source_revision`, `currentness`, timezone-aware
`observed_at`, the existing capacity observation, `authority=NONE`, and
`assertion=OWNER_ASSERTED_NOT_NATIVE_LOCK`. Missing, symlinked, malformed,
wrong-task, non-current, or conflicting records refuse. The reader validates
the embedded observation against the immutable capacity binding. Dispatch reads
the named owner record before intent and again immediately before the sender;
changed capacity after intent becomes `NOT_SENT_CAPACITY_RECHECK_FAILED` with no
retry. Legacy captures without a capacity sidecar keep their existing path.

The ordinary pre-START callsite is
`BoundControlResolutionHost.accept_rom_a_task_uptake(capture_record=...)`.
It reads the existing capture/profile/capacity binding and the configured
`UPTAKE` owner record. Uptake passes only when the exact actor/generation is
`RESERVED` or `RUNNING`, whole-job fit remains `FIT`, and
`reservation_owner_ref` equals the exact `task_ref`; the profile source/revision
must still match. It returns the owner-record provenance and local uptake
receipt so the caller can proceed to its already-existing START gate. It does
not create a reservation, authorize START, or claim a native lock. The record
contract still requires the current owner to supply both stage records.

The normal return path can take `--capacity-care-observation CARE`. A reported
relevant delta may yield `CARE_PENDING` only with named caretaker and next
consumer; `REUSED` names the same bounded care route; `CARE_DONE` carries the
care ref, delta ref, updated source/revision/hash, and readback ref. The code
never starts or duplicates Gartner work. On a later ordinary capture, an
optional `prior_care_readback` is consumed only if its source ref/revision match
the new task's profile binding. The resulting fields remain caller-reported,
`authority=NONE`; synthetic validation is not real capacity, care completion,
profile use, recipient use, installation, or adoption evidence.

Focused changed-risk validation: `python -B tooling/validator/task_capacity_care_validation.py`.
It uses only synthetic local fixtures, including currentness refusal and
reservation-owner mismatch at uptake. A1 still owns candidate admission and
Source publication.


## Owner-selected room case and ordinary D resume (version 1)
Explicit optional --resume-input ABS_JSON invokes consume_resume in
task_local_profile.py from the existing normal-path CLI before output/material
dispatch. No new dispatcher, store, fanout, grant or generic chat hook.
Schema cerebro-owner-selected-work-resume/v1:
binding (task/revision/actor/generation/carrier/arc/original_sha256);
owner_selection_ref; current_task (task/revision/owner/next_action/attempt,
evidence_refs, closed_arcs); received_order; current_blocks; inspection; cases;
learning; fact_delta. Exact metadata fields are validated by consume_resume.

Owner_selection_ref must be an actual owner-selected input; no inference from
summary, timestamps, UI activity, keywords or historical CURRENT headings.
Exactly one CURRENT must match captured task/revision. CLOSED arcs remain history;
SUPERSEDED blocks require the same explicit owner-selection reference. A genuine
new Human order is selectable with a matching new capture and explicit
OWNER_SELECTED_NEW_ORDER; an old CLOSED Liv task is not resurrected from context.
No identifier is silently trimmed; opaque IDs remain exact or input is refused.

EXPLICIT_AUTHORIZED_ROOM_INSPECTION is selected attributed input, with actual
authorization_ref and SAME existing case_ref. That case's scope, period, analysis
owner, finished criterion and pending obligations are preserved before output.
STATUS/NONE do not create an audit case. Existing cases remain retained metadata,
not a gate on independent JS production. Broad analysis remains AD1/P23.

Learning source/revision/profile/known countermodel refs and intended consumer
are distinct from actual use_state/evidence. Local record consumption is reported
separately and never attests worker cognition, identity or native permission.
Resume raw input hash/bytes and actual Source method bytes are captured. A bound
work_error encounter must match the current attempt. Fresh owner store/connection
evidence supersedes its named stale premise; reusing that premise refuses before
new output. 65 subscriptions never implies 65 clients; active_clients stays UNKNOWN.

### Intended real D entry, existing owners, exact remaining bind
D2 thread01a12a9c-9cfb-79d3-aa29-8bcb1f31b1c9 owns its existing AGENTS.md
CURRENT EXACT/owner-selected ordinary resume before next material choice or return.
D1 thread01a12abc-a100-7052-bd5d-1f8ae375d5e3 owns existing
work/BK11-production/ACTIVE.md at the same natural boundary.
P23/AD1's original documents diagnose these actual work-method seams; this build
has not read or changed their live files or interviewed/paused their jobs.

At next natural qualified resume, that owner:
1 reads actual latest received owner order plus its own current task/attempt and
closed arcs; explicitly supersedes contradictory CURRENT history, never by recency;
2 fills the supplied UNBOUND resume metadata from genuine originals, keeping
existing inspection case/pending obligations if applicable; no summary-born job;
3 after Source admission/publication/exact readback and actual route/tool fit,
runs the existing CLI with --capture-record, --return-input, --resume-input and
fresh --out-dir; if relevant, includes existing bound work_error.json;
4 consumes receipt current task, case, learning and factual delta before actual
choice; UNKNOWN effect remains F1 materialfalse, same-attempt read needed.
Read-only reconciliation uses the existing owner route, never a new RPC.
Known proven quickfix stays short. Record actual use and recipient-used return
separately; no ACK per error.

No supported Codex read_thread/actor-send port is available in this A3 tool set.
Therefore absolute D owner-workspace/caller installation/readback and automatic
native chat invocation remain NOT_ATTESTED. The prepared Source CLI seam is
executed locally with synthetic owner metadata before mock material effects;
that is not proof that D2's live AGENTS.md or D1's ACTIVE.md called it.
Exact next bind belongs to D1/D2 on their own qualified workspace/Source route.
A1 admits/delivers; P23 integrates; production remains independent.
Validation: python -B tooling/validator/work_resume_validation.py


## Optional owner-port/current-work observation v2
Use `cerebro-task-capacity-care/v2-observation` only for a task declaring a
necessary effect port. The strict capture request and original v1 bytes do not
change; old v1 sidecars and unrelated jobs do not acquire missing-field gates.
The v2 capacity binding records `owner_port_assessment` and schema v2.

V2 adds `selected_actor_ref`, exact `attempt_ref`, `input_kind` (`TASK_ASSIGNMENT`,
`MATERIAL_INPUT`, `SAME_TASK_CONTINUATION`), `work_phase` (`PREPARATION`, `EFFECT`)
and `owner_port_contract` to the existing observation. Each worker adds
`current_work`, `pending_bindings`, `pending_currentness`, `pending_evidence_ref`.
Current work is an exact task/revision/attempt + state (`NONE`, `ACTIVE`,
`COMPLETED`, `UNKNOWN`), currentness and evidence ref. Pending bindings use the
same exact key, currentness/evidence, and `DISPATCH_INTENT`, `UPTAKE`, `CONSUMED`
or `CANCELLED`. Unknown/stale/competing intent refuses the affected placement or
uptake; exact active-job material input/continuation is distinct from a new
assignment. Exact completed/consumed/cancelled attempts cannot be restarted.
An observation is not an atomic scheduler lock. Material return records pending
work disposition without imposing a global return gate.

`owner_port_contract` has exactly `case_ref` (existing civilization case),
`governing_basis_refs`, `applicable_control_refs`, `requirements`, `ports` and
`owner_disposition`. Each requirement fixes `port_ref`, `function_ref`,
`effect_scope`, `owner_ref`, `priority_owner_ref`, `preparation_actor_ref`,
`prepared_input_ref`, `next_consumer_ref`. Delegable preparation remains with the
qualified selected room, separately from the unique owner; final effect with
missing/unknown/busy unqualified port refuses. Multiple ACTIVE fronts on the
same blocked port produce a bounded bottleneck/priority-owner disposition.
CLOSED/STOPPED fronts stay historical. No PM boolean or new PM turn is added.

Each port record has exactly `port_ref`, `source_ref`, `source_revision`,
`currentness`, `state` (`AVAILABLE`, `BUSY`, `RELEASED`, `UNKNOWN`), `evidence_ref`,
`active_fronts`, `prepared_input`, `delegation`, `release`, `consumer_use`.
Prepared input uses input ref/source revision/currentness/evidence.
A delegation fixes its ref, owner, delegate, exact task, effect scope, current
port revision, currentness/evidence and existing governing/control refs. Only
current same-scope same-task delegation to the selected actor is reused; stale
or out-of-scope delegation cannot authorize effect. A blocked final effect does
not prevent a declared lawful preparation task. All assertions remain owner
observations with `authority=NONE`, without credentials or native grants.

Owner records for these bindings use outer
`cerebro-task-capacity-owner-observation/v2` with the unchanged outer field set.
`OwnerCapacityObservationReader` accepts optional fixed absolute
`material_return_record` in addition to dispatch/uptake paths. It rereads the
current named records at dispatch, uptake and MATERIAL_RETURN. The actual host
`prepare_rom_a_return` reads MATERIAL_RETURN itself before the existing `_prepare`
consumer; public caller injection of `_host_owner_port_return` refuses.
A declared v2 task without this qualified return reader holds that affected
preparation; it cannot silently use an old cached capacity declaration.

Dependency closure requires a current RELEASED port and exact owner-release
record (`state`, `evidence_ref`, `owner_ref`, `task_ref`, `effect_scope`,
`port_revision`), plus observed consumer use (`state=USED`, `evidence_ref`,
`consumer_ref`, `release_ref`) linked to the same release and required next
consumer. The familiar state/evidence-ref use contract is retained with explicit
release/consumer binding. Board rows, dispositions or requests alone keep
`PENDING_OWNER_RELEASE_AND_CONSUMER_USE`. Qualified records yield
`OWNER_REPORTED_RELEASE_AND_CONSUMER_USE`, not a native effect attestation.

First real application is X9's existing D1/E1/E2/B1 owner request. Its actual
release/consumer readback remains external required evidence; this code creates
no duplicate owner request, rights, ledger, scheduler or clock. The new Source
candidate/publication and real relief remain separate from independent live
Assistant/Context work. Only the eight `OwnerPortDelta` countercontrols are run
for this delta; earlier F1/NC1/resume evidence is inherited with attribution.
