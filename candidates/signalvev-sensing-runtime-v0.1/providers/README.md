# PM owner ready-hint provider candidate

`pm_owner_read.py` is callable server-side code, default disabled. A trusted PM
host must construct `ServerPmOwnerProvider(owner_ref=..., audience=...,
credentials=..., snapshots=..., enabled=True)`. It accepts a presented
credential only through `PmCredentialPort.authenticate_and_authorize` for the
exact action and audience. The credential is not returned or logged.

`PmAtomicSnapshotPort.read_receipt` and `read_current` must read the PM owner's
committed claim, immutable packet SHA-256 and queue under one revision. The
owner must issue stable event/referent IDs and a monotonic sequence, preserve
event-ID immutability across process restarts, and expose a commit/readback
reference. This candidate validates those returned fields and performs an
authorized current reread. A collection of independent Google Sheets row
reads does **not** satisfy the atomic snapshot contract.

No production `PmAtomicSnapshotPort`, `PmCredentialPort`, scoped credential,
ACL or control-host registration is present in this candidate. Tests use local
synthetic ports. The first missing production edge belongs to the current PM
provider owner: expose the two authenticated snapshot methods with a concrete
transaction/revision and identity/ACL contract. X5 owns any shared dispatcher
registration; Claude's reserved client `PmOwnerReadPort` adapter remains a
separate slice. X9 channel/session binding and live canary are separate.

`recover_current_ready_hint` performs one current owner read for one referent;
it never treats transport delivery or a channel row as X9 consumption.
