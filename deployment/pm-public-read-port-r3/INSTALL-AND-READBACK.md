# PM read-port r3: own PM status

P24 owner selection 1424673a06e9ac22ef6f52e1478d47ec445eff73873422cbd54f2bf30307d581
selects this bounded read in the same existing runtime. It accepts an empty
object and requires project_state:read. The server obtains all five authenticated
identity/session fields, selects exactly one own immutable admission decision,
loads actual singleton head, own session/ACTIVE project revision and native
shadow, then uses unchanged PostgresPmAdmissionOwner.verify. No project argument,
new project/event, attestation, admission, transfer or work rights are returned.

The read returns only schema/status/reason/profile/global verifier/authority NONE.
No identity, session, project, count, other owner, decision, binding fingerprint or
bounded scope details leave the service. Only the exact real verifier refusal
PM_PROFILE_NOT_SOLE_CURRENT maps to NOT_SOLE_CURRENT. Missing or stale session,
unknown/ambiguous own decision, invalid caller, role, lock/statement timeout,
database access and unavailable state remain separate failures, never PASS.
An expired OAuth token is rejected at existing remote authentication. Persisted
session schema has no TTL: do not invent one from admission decision expiry.

Derivation uses an ordinary bounded transaction with session/project/native/head
locks and no persistent update. It closes before verify opens its independent
transaction and revalidates actual state. No outer locks are held during verify;
nesting would risk deadlock behind an exclusive current-head writer. A final separately locked own decision/head/session/native frame must equal the
initial frame before either PASS or NOT_SOLE_CURRENT may leave the service. PASS
also requires the actual verifier fingerprint to match that frame. Drift yields
REFUSED/OWN_PROFILE_FRAME_CHANGED, never a profile observation, without retry.
D1 must qualify these exact interleavings; E2 independently reviews the gate.
Until those returns and any necessary correction, this is an unissued draft.

R2 complete package837859e655d2667009ba04bb30b2d4ee8d7f389bc23f33007bc43c5c74382203
is the immutable parent. Four affected surfaces are the new own-profile module,
tool descriptor/dispatch, remote scope map and Docker FROM. The existing PM
owner9ee6b63e025d3e863b061f8cc727f2e7b29a788d508d532b298c9345926fa895,
public resolver89eeff, signed core63c24, persistent storec32d6, DDL e78a,
authentic bundle9e338 and canonical b1 root remain byte-for-byte unchanged.
All current Source consumers and r2 distribution remain intact.

Docker pins python:3.12-slim manifest
sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177.
Expected linux/amd64 base config is
78976a69c067520ef8be34cde307ef9f6fea6dcefa50edb57039773d241ca0db.
The exact r2 root-owned0644 system Git safe.directory remains the single b1 path.
P24 is sole Linux builder and must supply actual r3 image/dependency/startup
evidence after qualified package delivery. R2 image6126 is not relabelled r3.

Publication uses a new deployment/pm-public-read-port-r3 Source distribution
profile with guarded branch head and raw byte readback, no replacement of root
Source or r2. Source publication is not installation. Historical installed be7
remains unchanged until D1's separately admitted A installation and readback.

D1 owns actual A guard/operator/extractor and exact installed source/image,
backend/schema/rights, unchanged old head/staged/native evidence and closure.
The separate0016 install helper581ed3 and its E2 source reviewf651 are operator
material, not silently added to this read-port image. P24 owns sole B issuer,
permanent single-shot marker and new source/image eligibility. A and B are
separate future admissions; no keys, real SQL, deploy or transfer selected here.

Before B only an actual own candidate call through the installed/qualified
transport with exact noncurrent result supplies recipient fit. After B an actual
own current-profile PASS is required. Local synthetic tests, source publication,
image build and global tool listing do not prove those actual calls. Existing
stage/seven originals/2e296 and unchanged tests are reused, not replayed. Preserve
closed0afe, old SPENT UNKNOWN/NO_RETRY, candidate original2e2, strict history
UNPROVEN, outgoing sole PM, Rettsvern L11168 and all private custody boundaries.
