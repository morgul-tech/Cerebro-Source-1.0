-- schema 1: earliest synthetic room core (accounts, stable rooms, memberships)
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  handle TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1)),
  created_at INTEGER NOT NULL
);
CREATE TABLE rooms (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  owner_account_id TEXT NOT NULL REFERENCES accounts(id),
  created_at INTEGER NOT NULL
);
CREATE TABLE memberships (
  account_id TEXT NOT NULL REFERENCES accounts(id),
  room_id TEXT NOT NULL REFERENCES rooms(id),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, room_id)
);
