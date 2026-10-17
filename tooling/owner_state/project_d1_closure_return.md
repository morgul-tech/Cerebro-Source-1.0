# Project D1 closure — inactive Source candidate

The original PR37 port and its existing cleanup correction are integrated
against Source `8b83398981970019444d0d8dcc3268759d497192`. The public Python
entry remains `tooling.owner_state.project_d1_closure_return` with explicit
`connection_factory` and `ProjectOwnerCommitReader` constructor dependencies.
Importing it selects no event, creates no connection and registers no host.

## Integration decision

Project owns this return's semantics, so the candidate remains in the existing
`tooling/owner_state` home. It reuses current `_fetchone`, `_sha256` and
`_safe_close` helpers. Its receiver/closure idempotency key is distinct from the
ordinary Project owner-commit key; it neither replaces nor changes that port.

No runtime reader adapter is installed. Source integration of this inactive
candidate needs no live consumer binding, and the two reader contracts must not
be equated by renaming a field:

| Candidate contract | Current owner-state contract and boundary |
|---|---|
| `owner_event_key` | A Project event identity; current owner heads bind `commit.event_id`, not `commit_ref`. |
| `read_committed_owner_event(...)` | `read_owner_commit_evidence(...)` additionally needs the authenticated principal, consumer, session, scopes and exact commit reference. |
| `owner_event_fingerprint` | Project-owned event meaning; not automatically a request, commit, candidate or state fingerprint. |
| `CLOSED` | Exact immutable closure-row and current owner-head readback on a separate connection; not external delivery, JetStream ACK or recipient use. |

The existing SQL is still an isolated candidate schema, absent from the normal
migration manifest. The original CI workflow remains restricted to pushes on
`candidate/project-engine-d1-closure-ledger-v01`; this main integration adds no
main/PR trigger. No deployment, runtime role, database or migration requirement
is activated. Project D1 is unrelated to Room D1 or the paused BK11 work.

## Preserved correction and evidence

- Original PR37 head: `a8f1780f62ee2cf91354af0156318610880a9476`.
- Existing correction: `702dc039d40ad79815d8d6c1c5e2013777c77271`.
- Correction patch SHA-256:
  `080d5d52142b47cee9491e764b0e265e37ac2f271016d49ca04ae77db0959faa`.
- `_safe_close` preserves classified uncertain-commit/readback outcomes and lets
  an independent readback proceed after write-connection cleanup failure.
- The corrected port and validator are imported byte-for-byte. Their prior
  10/10 scripted cleanup results are retained as producer evidence. Historical
  disposable PostgreSQL CI belongs to the original head; it is not production
  proof or a fresh run of the correction.
- This integration checks exact blob identity, cold imports against current
  main, the no-DSN `UNRUN` boundary, syntax and inactive migration/workflow fit.
  One trailing empty CRLF line was removed from the historical workflow to
  satisfy the diff check; its trigger and job body are unchanged.

The existing database-free check can be selected explicitly:

```sh
python -B -c 'from tooling.validator.project_d1_closure_postgres_integration import local_cleanup_regressions; results = local_cleanup_regressions(); print(results); assert all(item["result"] == "PASS" for item in results)'
```

The full validator requires an explicitly disposable PostgreSQL DSN and flag.
Neither a real DB test nor an authenticated Project-event/reader action is
performed by this Source integration.
