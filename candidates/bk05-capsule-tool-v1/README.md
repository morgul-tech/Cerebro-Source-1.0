# BK05 offline capsule tool v1

An opt-in, local Python package and `bk05-capsule` command for building, validating, measuring, and reconstructing BK05 same-arc continuation capsules. This carries the completed X2 implementation into a reviewable code home with no runtime integrations.

The output remains `authority: NONE`, `status: LOCAL_CANDIDATE_ONLY`, and `validation: STRUCTURAL_ONLY`. Caller-provided review references and currentness are explicitly unauthenticated assertions. The tool cannot verify that a manifest fully reflects parent prose: omitting one of multiple stop edges may still yield a structurally consistent capsule. Tests make that limitation adversarially explicit and prove such output never claims semantic authority, approval, binding, sending, or work effect.

## Install and run

From this directory, install locally with `python -m pip install .` (Python 3.12+; no runtime dependencies), then:

```text
bk05-capsule synthetic-fixture --out-dir fx
bk05-capsule build --parent fx/parent.txt --parent-manifest fx/parent_manifest.json --verifier fx/verifier.txt --verifier-delta fx/verifier_delta.json --currentness fx/currentness.json --full-continuation fx/full_continuation.txt --out-dir out
bk05-capsule reconstruct --capsule out/capsule.json --parent fx/parent.txt --verifier fx/verifier.txt --out-dir reconstructed
python -B -m unittest discover -s tests -p 'test_*.py'
```

The original full packet remains available in `UPSTREAM_RETURN.md` and `UPSTREAM_README.md`; the exact field contract is `CONTRACT.md`. A caller who cannot supply all bytes or a trusted manifest should use the full task instead. Existing exit codes and output schemas are preserved.

## Deliberate limits

- No provider, Drive, PM, Source, network, clock, scheduler, sender, runtime, NATS, or state access.
- No output overwrite; output goes only to explicit `--out-dir`.
- Stale parent/source revision and changed same-key payload with a prior record fail closed. Scope mismatch requires the full task.
- Fake review/currentness strings do not authenticate anything. An incomplete owner-supplied stop-edge list cannot be discovered from opaque prose by this structural validator; that is a known limitation carried in output.
- Real P1684/P1696 net saving remains `UNKNOWN`: the recorded episode included actual verifier rereads, and synthetic byte/character counts do not establish token, spend, or net savings.
