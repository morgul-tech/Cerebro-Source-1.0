# CB-P01 contracts (frozen for CB-P02 / CB-P03 consumption)

## Config `cerebrobase.config/v1` (strict JSON: unknown or duplicate keys rejected)

Required keys:

| Key | Rule |
|---|---|
| `config_schema` | `cerebrobase.config/v1` |
| `environment` | `DEV`, `STAGING` or `PROD` |
| `build_id` | 16 hex, or `SOURCE_TREE` in DEV only |
| `db_path`, `private_data_root`, `public_assets_root`, `runtime_root` | absolute paths |
| `bind_host` | numeric IP |
| `port` | 1024–65535 |
| `public_origin` | `scheme://host[:port]` |
| `auth_mode` | `SYNTHETIC` or `EXTERNAL` |
| `cookie_secure` | bool |

Optional keys:

| Key | Rule |
|---|---|
| `session_ttl_seconds` | 60–43200, default 3600 |
| `secret_refs` | `{name: "env:NAME" \| "file:/abs"}` |
| `integrations` | `{name: {enabled, secret_ref?}}` |
| `capabilities` | `{tools: {tool_id: bool}}` |
| `production_roots` | `{name: abs path}`; required in STAGING |
| `limits` | `{max_body_bytes, rate_per_minute, max_header_bytes}` |
| `min_free_bytes` | non-negative int |

Validation (each failure is `{code, field, next}`):
* **Paths:** all are resolved (realpath, including for paths that do not exist yet).
  * DB, private and runtime roots never overlap the public root.
  * The private and runtime roots are separate.
  * In STAGING, no DB, private or runtime root may be equal to, inside, or an alias of a `production_roots` entry.
    Aliases include symlinks, `..` spellings and inode aliases of existing ancestors. A production `*db_path` guards
    its whole directory.
* **Bind:**
  * a wildcard bind is refused;
  * a non-numeric host is refused, including `localhost`;
  * synthetic auth on a non-loopback IP is refused.
* **Cookie and origin:**
  * `cookie_secure=true` requires an `https://` origin;
  * `cookie_secure=false` is allowed only in DEV or on an `http://` loopback origin.
* **PROD:**
  * `SYNTHETIC` is refused;
  * preflight is never ready (it reports `AUTH_ADAPTER_NOT_QUALIFIED` and `PROD_DEPLOYMENT_NOT_QUALIFIED`);
  * `seed` is refused.
* **Secrets:** values are never inline. An enabled integration with an unresolved ref blocks with
  `SECRET_REF_UNRESOLVED` (field `secret_refs.<name>`). A present secret still needs a qualified port
  (`INTEGRATION_PORT_NOT_QUALIFIED`). A disabled integration is `NOT_REQUIRED` and does not block the scaffold.

Examples: `deploy/config/{dev,staging,prod}.example.json`.

## Schema (SQLite, `schema_meta.schema_version`, head = 2)

| Version | Tables |
|---|---|
| 1 | `accounts(id, handle, display_name, synthetic, created_at)`, `rooms(id, slug, owner_account_id, created_at)`, `memberships(account_id, room_id, created_at)` |
| 2 | `accounts.app_role` (ADMIN\|PILOT, default PILOT: least privilege on upgrade) + `status`; `rooms.kind` (ADMIN_PRIVATE\|PILOT_PRIVATE); `memberships.member_role` + `status` (ACTIVE\|REVOKED); `capabilities(account_id, room_id, capability)`; `sessions(token_sha256, csrf_sha256, expires_at, revoked_at)`; `audit_events(event_id, at, kind, outcome, account_id, room_id, tool_id)`; reserved `p02_file_metadata_reserved`, `p02_quota_reserved`, `p03_mirror_metadata_reserved` (state RESERVED only) |

Policy:
* **Migration:** forward only. Each step is one transaction, and the version is recorded in the same transaction.
  A failure rolls back to the prior usable schema.
* **Compatibility:** an app runs only on exactly its schema version (`SCHEMA_NEWER_THAN_APP` /
  `SCHEMA_MIGRATION_REQUIRED`). Unversioned DBs are refused.
* **Rollback:** take a snapshot before upgrading. To go back, restore into a NEW isolated root and point the older
  release at it. There is no destructive down-migration.

## Identity and authorization

`AuthProvider`:
* `authenticate(identity) -> (session_token, csrf_token) | None`
* `resolve(token) -> Principal | None`
* `revoke(token) -> bool`

`SyntheticAuthProvider`:
* serves only the two fixtures, and only in DEV/STAGING on a loopback bind;
* tokens are 256-bit random values stored as SHA-256, with bounded expiry;
* revoked, expired or disabled accounts give `None`.

`ExternalAuthProviderUnqualified` never authenticates anyone; it is replaced by the owner's qualified provider.

Server-side rules:
* principal, role and membership are resolved server-side; headers, form `role`/`account_id`/`room_id` are ignored;
* ADMIN is an application role and does **not** imply membership. `/admin` needs the role AND an active ADMIN_PRIVATE
  membership;
* `room_for(con, principal, room_id)` returns `None` for both foreign and missing rooms;
* every mutating request needs Origin/Referer equal to `public_origin` (when present) plus the CSRF token. The CSRF
  token is per session, the client holds it in an HttpOnly SameSite=Strict cookie, and the server checks it against
  a hash.

## P02 / P03 interfaces (`cerebrobase/interfaces.py`, contracts only)

* `RoomFileStore`: `list`, `put`, `get`, `tombstone`, `quota`
* `RoomMirror`: `list`, `read`
* Data shapes: `FileRef`, `QuotaView`, `MirrorRef`

Each takes the server-resolved `Principal` plus a `room_id` referent, and MUST call `room_for` first (None means
NOT_FOUND). HTTP placeholders answer members with 503 `CAPABILITY_UNAVAILABLE` and non-members with 404. Nothing
stores, uploads, restores or mirrors yet.

## Tools (`cerebrobase/tools.py`)

`ToolDefinition` fields: `tool_id`, `title_no`, `kind`, `required_role`, `required_capability`, `environments`,
`status`.

A tool runs only if it is QUALIFIED with an adapter, the environment is allowed, the config enables it, and the
principal has the required role, an active membership and the exact capability.

| Tool | Status |
|---|---|
| `fixture.echo` | the only adapter; LOCAL_SYNTHETIC_ONLY, returns length + digest only |
| `postkasse.lese`, `signalvev.status`, `drive.navigasjon` | UNQUALIFIED: 409 `TOOL_UNQUALIFIED`; nothing is invoked; the UI shows "Ikke kvalifisert — utilgjengelig" |

There is no plugin execution, URL fetch, command or dynamic loading.

Audit records only `event_id`, time, kind, outcome, `account_id`, `room_id` and `tool_id`. It never records params,
bodies, tokens or secret refs.

## HTTP limits and logs

* Limits: body ≤ `max_body_bytes` (413), URI ≤ 2048 (414), only `application/x-www-form-urlencoded` (415), at most 32
  concurrent requests (503), a token-bucket rate limit per client (429), socket timeout 10 s.
* Typed responses: JSON errors with `request_id`; every response carries an `X-Request-ID` header.
* Headers: CSP `default-src 'none'`, nosniff, `frame-ancestors 'none'`, `no-store`.
* Logs: JSON lines in `<runtime_root>/logs/app.jsonl` with fields `t`, `rid`, `method`, `route` template, `status`,
  `ms` and `acct` only. Never cookies, queries, bodies or tokens.
* Errors: stderr tracebacks are suppressed.

## Build

* `build_id` is the first 16 hex of a SHA-256 over the sorted (path, bytes) of every packaged file plus the label.
  `BUILD_INFO.json` inside the artifact declares it; at runtime it is recomputed (integrity `MATCH` or
  `BUILD_INTEGRITY_MISMATCH`).
* The ZIP is deterministic: sorted entries, 1980-01-01 timestamps, mode 0644, deflate level 9. It contains no tests,
  `.git`, `.pyc`, host paths or time.
* The manifest lists path, bytes and sha256 per entry, plus the artifact sha256, `build_id`, `schema_version` and
  `config_schema`.
