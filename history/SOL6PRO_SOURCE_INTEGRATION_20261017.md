# Sol6Pro Source integration — 17 October 2026

This change completes the Source integration of seven previously delivered
local corrections and the PR1 reconciliation. Human authorized publication and
merge on 17 October after consuming the original Sol6Pro returns. The separate
Verifier rollback correction is integrated in `morgul-tech/Verifier`.

## Exact basis and preserved deliveries

Source baseline: `8b83398981970019444d0d8dcc3268759d497192`.
The original review commits used imported snapshots. This integration uses a
full upstream checkout and real upstream ancestry; it does not transplant an
imported snapshot or overwrite newer main files with old PR versions.

| Item | Original local review | Integration disposition |
|---|---|---|
| PR77 | `a3d4786a0838ab5754a394966847bf05e47481e6` | Exact reviewed three-file delta on main; standing grant remains disabled by default. |
| PR76 | `9e272a039d76a7b97a994219fd6bf1a16f8ab27c` | Exact reviewed three-file delta on main; real unbound recipient projection continues to block. |
| PR95 | `fb4804fd3d01a329858f1a6490ffb890a0d85c01` | Corrected A1 port in its own inactive module, using current owner-state transaction helpers. No PR93/94 host transplant. |
| PR2 | `bac8f5365a176ee24db7e73569c1a67027130ffb` | Exact clarification in the existing Principal history standard; replaces the old broader proposal. |
| PR29 | `3e7c6a669a80ad31fa6f20073280f8df762cede3` | Corrected inert proposal module and validator, with current integration documentation. |
| Issue41 | `65b57c73d4b42b735b7a6cb32e92fb93b8cc8267` | Exact reviewed publication-revision and unknown-outcome correction on main. |
| PR37 | `702dc039d40ad79815d8d6c1c5e2013777c77271` | Corrected inactive Project closure port, existing isolated schema and candidate-only workflow. |
| PR1 | No code or patch | Comments clarify existing PM verifier coverage and the legacy Shared boundary; parsed YAML is unchanged. |

Original package SHA-256 identities:

- `Cerebro_PR77_PR76_review_2026-10-17.zip`:
  `0c3c40ae9290228bf5d92f808ce05bb9be2a72fc54380909d6ba6b9029033d86`.
- `Cerebro_PR95_PR2_PR29_review_2026-10-17.zip`:
  `2db1389401b6cba096331ba5d931119e1852e86314826f0b043d2cc6bd01a83b`.
- `Cerebro_remaining_GitHub_review_2026-10-17.zip`:
  `9e4cac88a38b277d98b9386dccc1c5edbca5d354aac2bb35f63a17be862486a9`.

## Integration decisions

PR95's A1 episode and admission ledger have distinct semantics from normal
Project/Quality/Convergence persistence. The corrected state validator and
port retain their ASTs in
`tooling/owner_state/rom_a_owner_episode_candidate.py`. The candidate SQL and
its opt-in manifest remain separate from normal migrations. The port supplies
no sender and creates no connection on import. Its detailed contract and
evidence boundaries are in the adjacent Markdown file.

PR37 remains in the Project owner-state home. Its corrected port and validator
are byte-identical to the delivered correction. The isolated SQL is unchanged;
only one trailing blank CRLF line was removed from the historical workflow.
The workflow still triggers only on the old candidate branch. The adjacent
contract explains why event identity, commit reference and their fingerprints
must not be substituted for each other.

PR29 is an importable, non-authoritative proposal library. It has no active
registration, scheduler or dispatch caller. Its outputs retain `authority=NONE`,
`effect_authorized=false` and the unverified-input boundaries. Existing PM
sequencing, optional governance and ordinary return semantics remain in place.

PR1's old combined proposal is superseded. The current host rejects injected
verifier objects and supplies the bound verifier separately for PM governance.
This does not impose PM verification on every host invocation. The historic
Shared EVENTS/WORKSPACES/CLAIMS/PACKETS provider has not been proved replaced by
newer PM-ready CAS/readback. Any future use of that legacy surface must identify
its actual consumer, authenticated reader/writer, append order and replay key.

Issue41's local code defect is corrected. Actual normal PM/provider publication
binding and provider-direct bypass remain distinct operational questions. The
issue must not be closed as universally enforced solely from this integration.

## Validation and evidence reuse

Unchanged delivered bytes preserve the original evidence: PR77's 22 tests;
PR76's selftest/conformance control fields and actual unbound BLOCK; PR2's
structured checks; PR29's 19 canaries; issue41's 27 checks; and PR37's 10 cleanup
checks. These are producer results with their original fixture and runtime
limits, not fresh executions attributed to this integration.

PR95 required a real module relocation and binding to today's helpers. Its
20 existing scripted DB-API cases and one new default-owner/default-migration
boundary check passed on the integrated module (21/21, Python 3.12.14).
Earlier owner-state 8/8 results were reused. Cold package/direct imports and
PR37's no-DSN `UNRUN`/exit-2 boundary were checked. No real database was used.

The final combined change has bounded syntax, structured-data, import,
registration, approved-path and repository-hygiene checks recorded in
`history/evidence/SOL6PRO_SOURCE_INTEGRATION_20261017.json`. Independent semantic
review found no material integration defect. No broad historical test campaign
was repeated. Existing CRLF in the runtime file was preserved and classified as
a line ending by the diff check, rather than rewriting unaffected source bytes.
The canonical Active Source Closure passed for the 23 integration paths and
its fixed Source invariants with zero findings in PowerShell 7.6.7 on Linux.
The evidence names the local reviewed code commit and exact Git tree;
publication verifies that tree independently of the publishing commit identity.

## Completion boundaries

The old PR76/77/95/2/29/37 proposals are superseded by this corrected integration;
PR1 is superseded by its completed reconciliation. Their original descriptions
and branches remain historical evidence. No runtime activation, owner binding,
database migration, dispatch, role change or BK11 restart follows from merging
this Source change. Native Windows/Runtime2 and PostgreSQL effect claims remain
bound to a separately exercised, explicitly selected operation.

GitHub publication and local Working Source equality are separate observations.
The authorized local continuation uses the existing clean, attached-main
currentization flow to an exact frozen remote commit, followed by local
validation, canonical `cerebro_sync` and independent equality readback. Dirty,
ahead or divergent local work must be preserved. This Source record is not a
Working Source synchronization receipt.
