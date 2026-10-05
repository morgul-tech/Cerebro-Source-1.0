# BK-04 offline episode verifier candidate

This candidate checks the internal consistency of a typed, read-only episode
snapshot. It cannot authenticate a PM row, approve a fixture, grant authority,
read a provider, or cause deployment or live effect. Every rev3 result carries
`authority=NONE`.

`bk04_verify.py` preserves the original rev2 CLI gate: it accepts only the
exact approved rev2 byte count and SHA-256 already encoded in the file. The
rev2 snapshot is deliberately not included here. `bk04_rev3.py` supplies the
typed rev3 rule. Without an owner-approved, hash-bound rev3 snapshot, normal
rev3 classification remains `UNKNOWN`, even when its structure is consistent.

Run the public, synthetic tests from this directory:

```sh
python -B test_bk04_rev3.py
```

The tests use invented task, claim, packet, queue and source-row IDs. They call
the pure rule module to exercise scoped structural PASS, contradiction
`CONFLICT`, and missing evidence `UNKNOWN`. The main classifier has no test
override for owner approval. The
historical rev2 suite was run separately against its retained private fixture
before publication (62/62); no private snapshot or coordination row is copied
into this public candidate.

Source basis: X1 local offline implementation commit
`ba2b08231d67595a83965e1bb8ba6634274b856e`. A separately curated and
approved rev3 byte basis, source-row verification and adoption decision are
future work.
