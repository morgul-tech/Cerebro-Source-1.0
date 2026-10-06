# cerebrobase-room-scaffold-v0.1 (CB-P01)

This is the local/private-staging CerebroBase room scaffold.
* Assignment: `EXT-CLAUDE-CEREBROBASE-CB-P01-V01` rev 1.0.
* AUTHORITY NONE. Synthetic identities only: `fixture-andreas-admin` (ADMIN, own admin room) and
  `fixture-marianne-pilot` (PILOT, own pilot room).
* It is not a production identity system and not public Main. It does not use the Postkasse DB, does not encrypt,
  and does not recover anything.

Runtime:
* CPython 3.12 or 3.13, standard library only (`sqlite3`, `http.server`).
* No third-party packages, Node build, CDN, ORM or broker.
* One app identity and one room metadata DB.

## Commands (run from the candidate root or an installed release dir; Windows: `py -3` instead of `python3`)

```
python3 -m cerebrobase report                                          # runtime/version report (not in artifact)
python3 -m cerebrobase preflight --config CFG --phase pre-migrate      # dependency + real-config check
python3 -m cerebrobase migrate   --config CFG                          # atomic forward migration to schema 2
python3 -m cerebrobase seed      --config CFG                          # two synthetic fixtures (DEV/STAGING only)
python3 -m cerebrobase preflight --config CFG                          # pre-start: must be ready
python3 -m cerebrobase serve     --config CFG                          # loopback preview, graceful stop
python3 -m cerebrobase stop      --config CFG                          # STOP file -> graceful stop
python3 -m cerebrobase snapshot  --config CFG --out NEW_FILE           # SQLite online backup
python3 -m cerebrobase restore   --snapshot FILE --into NEW_DIR        # restore into a NEW isolated root
python3 -m cerebrobase build     --out DIST [--label release]          # deterministic ZIP + manifest
python3 -m cerebrobase check-manifest --artifact ZIP --manifest ZIP.manifest.json
python3 -m cerebrobase install   --artifact ZIP --manifest JSON --releases DIR
```

Tests:

```
cd tests && python3 -B -m unittest -v test_a_build_health test_b_migrations test_c_auth_rooms \
    test_d_config_static test_e_tools_audit test_f_limits_ui test_g_rehearsal test_postkasse_adapter
python3 -B tests/visual_check.py --out NEW_DIR        # 320px/desktop screenshots + overflow; needs python playwright
```

Rehearsal: `python3 -B deploy/rehearse_staging.py --source . --work NEW_DIR` runs the private-staging
install/upgrade/rollback on synthetic local roots.

Exit codes:

| Exit | Meaning |
|---|---|
| 0 | ok |
| 2 | refused/failed (typed JSON on stdout) |
| 3 | not ready / blocked |
| 64 | usage |

## Routes

| Route | Who | Behaviour |
|---|---|---|
| `GET /health` | public | liveness + build_id only |
| `GET /ready` | public | `ready`/`not_ready` + build_id (503 when the DB, private root, build or auth adapter is not OK) |
| `GET /logg-inn` | public | clearly marked local/staging test identities; disabled unless SYNTHETIC in DEV/STAGING |
| `POST /logg-inn` | public | prelogin double-submit + Origin check; HttpOnly SameSite=Strict session + CSRF cookies |
| `POST /logg-ut` | session + CSRF | revokes the session immediately |
| `GET /rom`, `GET /rom/{room_id}` | member | own room only; foreign and missing rooms answer an identical 404 |
| `GET /admin` | ADMIN role AND an active ADMIN_PRIVATE membership | separate admin entry: tools + operations |
| `GET /admin/drift.json` | ADMIN + `ops:read` | readiness detail (DB vs private root distinguished) |
| `POST /admin/verktoy/{tool_id}` | ADMIN + membership + scoped capability + CSRF | tool registry; unqualified tools rejected |
| `GET /api/rom/{room_id}/filer`, `GET /api/rom/{room_id}/speil` | member | 503 `CAPABILITY_UNAVAILABLE` (P02/P03 not integrated); non-members get 404 |
| `GET /static/app.css` | public | the only static file (in-memory allowlist) |
| `GET /rom/{room_id}/postkasse` | member + paired private credential | service-backed own inbox and explicit contacts only |
| `GET /rom/{room_id}/postkasse/{message_id}` | member + service-confirmed recipient | exact message; foreign/missing both 404 |
| `POST /rom/{room_id}/postkasse` | member + CSRF | short message to an explicitly allowed contact; service derives sender |
| `POST /rom/{room_id}/postkasse/{message_id}/ack` | member + CSRF + recipient | ACK only, no action authority |
| `POST /rom/{room_id}/postkasse/{message_id}/reply` | member + CSRF + recipient | reply through the service's reply endpoint |

The Postkasse adapter is disabled by default. It has only been exercised against a synthetic local fixture; no live
service, historic port or pilot room is inferred. Its private configuration and activation gates are in
`CONTRACTS.md`. The service DB is authoritative; CerebroBase adds no mailbox tables or message store.

See `CONTRACTS.md` for the interfaces, config, schema, tool and auth contracts. See `DEPLOY.md` for the
staging install, upgrade and rollback recipe and the Caddy/systemd examples. See `LIMITATIONS.md` for what is not
claimed.

Design reference: the Home/Login/Main exports were used for palette and typography only (cream `#F7F2E7`, ink
`#1C1712`, gold `#A9822E`/`#8A6A24`/`#C9A45B`, Cormorant/Inter look via local font stacks). No design runtime, no
vendor React file and no design asset was copied, so there is no third-party licence to carry. The design's
"key on your machine / nobody else has access" wording is deliberately NOT used, because that would be a claim this
scaffold cannot make.
