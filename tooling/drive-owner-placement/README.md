# Drive owner placement normalwriter — E1 Parallell8 candidate

This isolated component is proposed for `tooling/drive-owner-placement/`. Its exact candidate-relative to proposed Source path mapping is in `SOURCE-PATH-MAP.json`. All runtime code, public placement contract, caller recipe, controls, and their deterministic fixtures are inside this component. It has no dependency on the preserved R1 archive or any file above this directory.

The current contract binds E1 / `P23-X9-DAG-PARALLELL8-B-E-POLICY-SIGNALVEV-20261013-001` / `HUMAN_HANDOFF` to OUTBOX `14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc`, as `text/markdown` and `.md`, under shared parent `165VAUNZ9bdUuI9JOgeUr34Mrg0OY7-A8`. The payload must contain the exact original owner-home URL in the contract. The old first-wave parent is not a fallback.

`normalwriter.py` only returns local plan/readback/repair results. `PLAN_ONLY` does not call a provider, approve an operation, prove freshness, admit Source, or establish authority. E1's already qualified normal managed caller remains responsible for current metadata, any write, exact-ID readback, separate content readback, and handling an unknown outcome. A1 owns final fit, Source admission, and publication.

Run the component-local focused controls from the candidate root:

```powershell
python -I -B -m unittest discover -s tooling/drive-owner-placement/tests -v
```

Fixtures use a synthetic owner email and local Markdown payload. They make no network or managed-provider call.
