-- schema 2: application role, room kind, membership status, scoped capabilities, sessions, audit,
-- reserved P02 file/quota and P03 mirror metadata namespaces (contracts only, no services).
-- Existing accounts get the LEAST privilege role PILOT; ADMIN is granted explicitly, never inferred.
ALTER TABLE accounts ADD COLUMN app_role TEXT NOT NULL DEFAULT 'PILOT' CHECK (app_role IN ('ADMIN', 'PILOT'));
ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DISABLED'));
ALTER TABLE rooms ADD COLUMN kind TEXT NOT NULL DEFAULT 'PILOT_PRIVATE' CHECK (kind IN ('ADMIN_PRIVATE', 'PILOT_PRIVATE'));
ALTER TABLE memberships ADD COLUMN member_role TEXT NOT NULL DEFAULT 'OWNER' CHECK (member_role IN ('OWNER', 'MEMBER'));
ALTER TABLE memberships ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'REVOKED'));
CREATE TABLE capabilities (
  account_id TEXT NOT NULL REFERENCES accounts(id),
  room_id TEXT NOT NULL REFERENCES rooms(id),
  capability TEXT NOT NULL,
  granted_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, room_id, capability)
);
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  token_sha256 TEXT NOT NULL UNIQUE,
  csrf_sha256 TEXT NOT NULL,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER
);
CREATE TABLE audit_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  at INTEGER NOT NULL,
  kind TEXT NOT NULL,
  outcome TEXT NOT NULL,
  account_id TEXT,
  room_id TEXT,
  tool_id TEXT
);
CREATE TABLE p02_file_metadata_reserved (
  room_id TEXT NOT NULL REFERENCES rooms(id),
  file_id TEXT NOT NULL,
  revision TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL CHECK (bytes >= 0),
  state TEXT NOT NULL CHECK (state IN ('RESERVED')),
  PRIMARY KEY (room_id, file_id, revision)
);
CREATE TABLE p02_quota_reserved (
  room_id TEXT PRIMARY KEY REFERENCES rooms(id),
  limit_bytes INTEGER NOT NULL CHECK (limit_bytes >= 0),
  used_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_bytes >= 0)
);
CREATE TABLE p03_mirror_metadata_reserved (
  room_id TEXT NOT NULL REFERENCES rooms(id),
  mirror_kind TEXT NOT NULL CHECK (mirror_kind IN ('STAMBOK', 'DAGBOK')),
  source_ref TEXT NOT NULL,
  source_revision TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('RESERVED')),
  PRIMARY KEY (room_id, mirror_kind, source_ref, source_revision)
);
