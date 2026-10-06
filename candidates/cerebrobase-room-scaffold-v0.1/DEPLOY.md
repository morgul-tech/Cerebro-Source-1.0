# Private staging: install, upgrade, rollback (recipe; A1 performs it; Claude installed nothing)

These steps are rehearsed by `deploy/rehearse_staging.py` on synthetic isolated local roots. That is evidence for the
recipe only. A real EDGE install has not been done and is never inferred from the rehearsal.

Layout:

```
/srv/cerebrobase-staging/
  releases/<build_id>/   one directory per installed artifact (immutable)
  releases/CURRENT.json  {"build_id", "config"}   (a pointer file, portable to Windows; no symlink needed)
  data/db/rooms.sqlite3  room metadata DB (never the Postkasse DB)
  data/private/          private data root (outside every Caddy root)
  run/                   pidfile, STOP file, logs
  snapshots/             pre-upgrade DB snapshots
  restored/<name>/       isolated restore targets
/etc/cerebrobase-staging/config.json       (example: deploy/config/staging.example.json)
/srv/cerebro/www                           public Main: NOT touched
```

## First install

1. Build and verify:
   * `python3 -m cerebrobase build --out dist --label <label>`
   * `python3 -m cerebrobase check-manifest --artifact dist/<zip> --manifest dist/<zip>.manifest.json`
   * Record the artifact sha256 and `build_id`.
2. Install: `python3 -m cerebrobase install --artifact ... --manifest ... --releases /srv/cerebrobase-staging/releases`.
   This refuses an existing release directory.
3. Set `build_id` in `config.json` to the release `build_id`. Keep `bind_host` 127.0.0.1, `auth_mode` SYNTHETIC and
   `production_roots` declared.
4. From `releases/<build_id>`:
   * `python3 -m cerebrobase preflight --config ... --phase pre-migrate`
   * `migrate`
   * `seed` (synthetic only)
   * `preflight` (must be ready)
5. Start, then read back:
   * start with `serve` (or the systemd example `deploy/systemd/cerebrobase-staging.service.example`);
   * read back `GET http://127.0.0.1:8472/health` and `/ready`; both must show the exact `build_id`;
   * admin readback: `/admin/drift.json` shows the schema and checks.

## Upgrade to a new release

1. Install the new artifact as above, alongside the old one. The old release, config and artifact are retained.
2. `python3 -m cerebrobase snapshot --config <old cfg> --out snapshots/pre-<new build_id>.sqlite3`. Record the sha256.
3. Stop the old instance: `python3 -m cerebrobase stop --config ...`.
4. Write a new config with the new `build_id`, then run `preflight` → `migrate` (atomic; a no-op if the schema is
   unchanged) → `preflight`.
5. Start the new release, read back `/health` and `/ready` with the new `build_id`, then update `CURRENT.json`.

## Rollback (never a destructive down-migration)

* **A — schema unchanged:** stop, point `CURRENT.json` and the config back at the old `build_id`, start, and read
  back the old `build_id`.
* **B — schema changed or data suspect:**
  1. `python3 -m cerebrobase restore --snapshot snapshots/pre-<id>.sqlite3 --into restored/<name>/db` (always a NEW
     isolated root).
  2. Write an old-release config whose `db_path` and `private_data_root` point into `restored/<name>/`.
  3. Run preflight, start, and read back.
  4. The upgraded DB stays untouched for analysis.
* An app refuses a newer schema (`SCHEMA_NEWER_THAN_APP`), so a wrong switch-back fails closed instead of corrupting
  data.

## Caddy / network

* There is no public route. The first step needs no Caddy change (operators use a tunnel to 127.0.0.1).
* Optional later step: a WireGuard-only proxy. See `deploy/caddy/Caddyfile.staging.example`; it is an example only.
* Public Main (`/srv/cerebro/www`, cerebrobase.net/.no) and DNS are unchanged.

## Windows (local DEV/STAGING preview)

* Start: `deploy\windows\run.cmd C:\path\config.json`
* Stop: `deploy\windows\stop.cmd C:\path\config.json`
* Stop is graceful through the STOP file; no signals are needed.

## Production

* PROD is refused until three things exist: a qualified EXTERNAL AuthProvider (with session, cookie and CSRF custody
  readback), owner-supplied secret refs, and PM effect approval plus A1 EDGE deployment qualification.
* `deploy/config/prod.example.json` shows the shape. Preflight reports exactly what is missing.
