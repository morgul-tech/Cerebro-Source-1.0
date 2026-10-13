# E1 normal consumer: plan → managed write → verify → content readback

This is the callable recipe for the current `SAME_E1` Parallell8 contract. Run the local planner from the candidate root, replacing `<local-handoff.md>` with the actual UTF-8 Markdown handoff. Keep the original owner-home pointer in that file exactly as shown. Obtain fresh OUTBOX metadata through E1's existing managed reader; the metadata below shows the required current normalized shape and exact binding, not a substitute for a fresh read.

## 1. Prepare the local payload and plan input

The payload must contain:

```markdown
Original owner home: https://docs.google.com/document/d/1kg6GQi1UsMtmhMVibXIOKlPJ8_zZP36RcmRY7QBaNoo/edit?tab=t.xr76a4ex96am
```

Create `plan-input.json` with this shape. Set `local_path` to the existing Markdown file. Copy the complete current OUTBOX record from the managed metadata reader into `parent_metadata`; do not synthesize or reuse old permission evidence.

```json
{
  "owner": "E1_6F2C9A71",
  "case": "P23-X9-DAG-PARALLELL8-B-E-POLICY-SIGNALVEV-20261013-001",
  "fileclass": "HUMAN_HANDOFF",
  "local_path": "<local-handoff.md>",
  "parent_metadata": {
    "id": "14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc",
    "mime_type": "application/vnd.google-apps.folder",
    "parent_ids": ["165VAUNZ9bdUuI9JOgeUr34Mrg0OY7-A8"],
    "permissions": [
      {"type": "user", "role": "owner", "emailAddress": "Morgul@gmail.com"}
    ],
    "source_visibility_status": "permission_metadata_available"
  },
  "original_home_pointer": "https://docs.google.com/document/d/1kg6GQi1UsMtmhMVibXIOKlPJ8_zZP36RcmRY7QBaNoo/edit?tab=t.xr76a4ex96am"
}
```

Run:

```powershell
python -I -B tooling/drive-owner-placement/normalwriter.py plan --contract tooling/drive-owner-placement/placement-contract.json --input plan-input.json
```

A successful result is `PLAN_ONLY`, `authority: NONE`, tool `google_drive_upload_file`, MIME `text/markdown`, and parent ID `14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc`. Review the complete `arguments` and deterministic filename. A `HOLD_*` result is JSON on stdout with exit code 2 and `effect: NONE`; correct only the actual missing or conflicting basis and re-run locally. Conflicting normalized/provider-native aliases are a hold even if one spelling matches. Invalid or malformed URLs are typed holds; they never become a green result.

## 2. Use E1's existing managed writer once

Only E1's already qualified normal managed caller may submit the returned arguments to `mcp__codex_apps__google_drive_upload_file`. The helper does not call it. Preserve the returned Drive file ID and writer result. If the create outcome is unknown, do not create again: reconcile the same logical artifact using the exact deterministic filename and expected OUTBOX, then continue only from a positively identified existing file. If reconciliation cannot establish one exact file ID, stop in HOLD.

## 3. Verify exact ID, MIME, and sole parent

Using E1's existing managed metadata reader, fetch the returned file ID. Put the complete record in `verify-input.json` with this shape:

```json
{
  "file_id": "<exact returned file ID>",
  "expected_parent": "14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc",
  "expected_mime_type": "text/markdown",
  "metadata": {
    "id": "<same exact returned file ID>",
    "mime_type": "text/markdown",
    "parent_ids": ["14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc"],
    "url": "https://drive.google.com/file/d/<same exact returned file ID>/view"
  }
}
```

Replace each angle-bracket value with the exact corresponding value from the managed reader; use the complete current metadata object if it has additional fields. The helper also accepts the provider-native `mimeType`, `parents`, and `webViewLink` spellings when they are the only spelling present or when any simultaneously present spelling agrees.

Run:

```powershell
python -I -B tooling/drive-owner-placement/normalwriter.py verify --input verify-input.json
```

Release a recipient link only when the result is `PARENT_READBACK_VERIFIED` for the exact returned ID, MIME `text/markdown`, and sole parent `14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc`. Metadata readback proves placement only; it does not prove the payload content.

## 4. Separately read back content

Use E1's managed content reader on that same file ID. Compare the fetched Markdown with the intended handoff, including the exact original owner-home pointer and required delta. Keep `content_readback` separate from `PARENT_READBACK_VERIFIED`; do not claim content verified or Human use without that separate observation.

## 5. Same-ID repair only

For a placement mismatch, re-read the current file metadata and current OUTBOX parent metadata. Do not make a replacement. Build a `repair-input.json` using the same exact `file_id`, `expected_parent`, `metadata`, current `parent_metadata`, `owner`, `case`, and `fileclass`, plus a `repair_basis` approved by the current owner for the exact observed change. The basis must match the file ID, owner, case, `add_parent_id`, and the full observed `remove_parent_ids` list. Example for one wrong parent:

```json
{
  "file_id": "<exact existing file ID>",
  "expected_parent": "14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc",
  "metadata": {
    "id": "<same exact existing file ID>",
    "mime_type": "text/markdown",
    "parent_ids": ["<exact observed wrong parent ID>"]
  },
  "owner": "E1_6F2C9A71",
  "case": "P23-X9-DAG-PARALLELL8-B-E-POLICY-SIGNALVEV-20261013-001",
  "fileclass": "HUMAN_HANDOFF",
  "parent_metadata": {
    "id": "14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc",
    "mime_type": "application/vnd.google-apps.folder",
    "parent_ids": ["165VAUNZ9bdUuI9JOgeUr34Mrg0OY7-A8"],
    "permissions": [
      {"type": "user", "role": "owner", "emailAddress": "Morgul@gmail.com"}
    ],
    "source_visibility_status": "permission_metadata_available"
  },
  "repair_basis": {
    "status": "OWNER_APPROVED_CURRENT_PLACEMENT",
    "owner": "E1_6F2C9A71",
    "case_id": "P23-X9-DAG-PARALLELL8-B-E-POLICY-SIGNALVEV-20261013-001",
    "file_id": "<same exact existing file ID>",
    "add_parent_id": "14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc",
    "remove_parent_ids": ["<exact observed wrong parent ID>"]
  }
}
```

Run:

```powershell
python -I -B tooling/drive-owner-placement/normalwriter.py repair --contract tooling/drive-owner-placement/placement-contract.json --input repair-input.json
```

Only an exact `PLAN_ONLY` for the same file ID may proceed through E1's already authorized managed owner lane, followed by fresh exact-ID/MIME/parent and separate content readback. Missing, denied, stale, or conflicting owner basis returns a typed HOLD and exit code 2. An unknown update outcome is reconciled against that same file ID; never retry automatically. If the file already has the sole expected OUTBOX parent, the helper returns `NO_REPAIR_NEEDED` with `effect: NONE`.
