Run the controls from the candidate root with:

```powershell
python -I -B -m unittest discover -s tooling/drive-owner-placement/tests -v
```

All data needed by the controls is in `tooling/drive-owner-placement/tests/fixtures/`. Tests use standard-library code and synthetic local evidence only; they make no provider calls and do not read the preserved R1 archive.
