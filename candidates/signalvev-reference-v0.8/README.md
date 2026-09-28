# Signalvev Reference Implementation Candidate v0.8 (stale revision admission guard)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

X4 runtime-fagpass's own crosswalk table names a material L4 gap:
"Supported live provider atomic compare-and-bind/admit remains absent.
PM8290 and PM8306 keep live provider HOLD." Falsifier 7, verbatim: "A
stale snapshot or route revision admits work." v0.1's falsifier-7 test
has a passing check, but it is a single inline closure with one
hardcoded scenario (`current_revision=5, snapshot_revision=3`) -- it
proves the concept once, is not a reusable guard, does not track a
referent's revision over time, does not cover the "route revision" half
of the falsifier's own wording, and critically does not test the case
that matters operationally: a snapshot that was valid *when read* going
stale *before admission is attempted*. This candidate builds the real,
reusable compare-and-bind guard and closes the last of the twelve X4
falsifiers that did not yet have a dedicated reference implementation.

## Base commit
