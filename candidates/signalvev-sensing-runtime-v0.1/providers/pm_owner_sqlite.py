"""Durable PM-owner commits for PR66, within one existing control host.

SQLite supplies the transaction, persistence, sequence and CAS, not authority.
Host credential/session custody must authorize every operation. No Sheets
import, credential issuance, host install, publisher or receiver is included.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .pm_owner_read import (AuthenticatedPmPrincipal, PmProviderError,
                            PmReadyRecord, _valid, snapshot_sha256)
from .pm_owner_projection import (AtomicProjection, PmProjectionBinding,
                                 create_pm_source_port)


def _need(ok, code):
    if not ok:
        raise PmProviderError(code)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 4096 \
        and "\n" not in value and "\r" not in value


def _json(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    _need(len(raw.encode("utf-8")) <= 65536, "PM_COMMIT_BOUND_EXCEEDED")
    return raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class PmReadyTransition:
    """PM's new admission, not a claim that external row reads were atomic.

    expected_revision is the store's last revision, or None for the first cut
    of this referent. Packet/material bytes must be the exact owner-selected
    payloads; the provider calculates their hashes. active_hold is mandatory:
    None means an explicit owner decision that there is no active hold.
    """
    attempt_ref: str
    referent_key: str
    expected_revision: str | None
    claim_ref: str
    packet_ref: str
    queue_ref: str
    packet_bytes: bytes
    material_bytes: bytes
    ready_state: str
    source_cut: str
    active_hold: dict | None
    way_home: tuple[str, ...]


@dataclass(frozen=True)
class PmContextCustody:
    """Existing custodian's exact PM-to-Context binding; never inferred from scope."""
    owner_ref: str
    audience: str
    tenant_ref: str
    workspace_ref: str
    project_ref: str
    principal_ref: str
    consumer_ref: str
    session_ref: str
    project_revision: int
    session_binding_id: str
    session_revision: int
    session_fingerprint: str
    allowed_actions: frozenset[str]


class ContextPmCredentialPort:
    """Use existing OAuthBearerAuthenticator + Postgres read_session unchanged.

    The custodian must declare that the pinned identity/project is the PM
    producer/reader and supply the allowed actions. A generic Context token
    does not itself grant PM ownership. No token cache or new auth service.
    """
    def __init__(self, *, custody: PmContextCustody, authenticator, state_port,
                 credential_reader):
        _need(type(custody) is PmContextCustody
              and all(_text(getattr(custody, k)) for k in (
                  "owner_ref", "audience", "tenant_ref", "workspace_ref", "project_ref",
                  "principal_ref", "consumer_ref", "session_ref", "session_binding_id"))
              and type(custody.project_revision) is int and custody.project_revision >= 1
              and type(custody.session_revision) is int and custody.session_revision >= 1
              and isinstance(custody.session_fingerprint, str)
              and len(custody.session_fingerprint) == 64
              and all(c in "0123456789abcdef" for c in custody.session_fingerprint)
              and type(custody.allowed_actions) is frozenset
              and bool(custody.allowed_actions)
              and custody.allowed_actions <= {"read_receipt", "read_current", "initialize", "commit_ready"},
              "PM_CONTEXT_CUSTODY_INVALID")
        _need(callable(getattr(authenticator, "authenticate", None))
              and callable(getattr(state_port, "read_session", None))
              and callable(credential_reader), "PM_CONTEXT_PORT_UNBOUND")
        self.custody, self.authenticator = custody, authenticator
        self.state_port, self.credential_reader = state_port, credential_reader
        self.SYNTHETIC_TEST_ONLY = any(getattr(x, "SYNTHETIC_TEST_ONLY", False) is True
            for x in (authenticator, state_port, credential_reader))

    def authenticate_and_authorize(self, credential, *, owner_ref, audience, action):
        c = self.custody
        if (owner_ref, audience) != (c.owner_ref, c.audience) or action not in c.allowed_actions:
            return None
        scope = "project_state:transition" if action in {"initialize", "commit_ready"} else "project_state:read"
        try:
            identity = self.authenticator.authenticate(
                {"Authorization": "Bearer " + credential}, required_scope=scope)
            if identity.token_verified is not True or \
                (identity.tenant_ref, identity.workspace_ref, identity.principal_ref,
                 identity.consumer_ref) != (c.tenant_ref, c.workspace_ref, c.principal_ref, c.consumer_ref):
                return None
            session = self.state_port.read_session(tenant_ref=c.tenant_ref,
                workspace_ref=c.workspace_ref, principal_ref=c.principal_ref,
                consumer_ref=c.consumer_ref, session_ref=c.session_ref,
                scopes=set(identity.scopes))
            if any(session.get(k) != getattr(c, k) for k in
                   ("tenant_ref", "workspace_ref", "project_ref", "principal_ref", "consumer_ref", "session_ref",
                    "project_revision", "session_binding_id", "session_revision", "session_fingerprint")):
                return None
        except Exception:
            raise PmProviderError("PM_CONTEXT_AUTH_OR_SESSION_UNAVAILABLE") from None
        return AuthenticatedPmPrincipal(c.principal_ref, c.owner_ref, c.audience)

    def session_check(self, principal_ref, session_ref, action):
        c = self.custody
        if (principal_ref, session_ref) != (c.principal_ref, c.session_ref):
            return False
        return self.authenticate_and_authorize(self.credential_reader(),
            owner_ref=c.owner_ref, audience=c.audience, action=action) is not None


class SqlitePmOwnerStore:
    """AtomicProjectionPort backed by local, durable SQLite transactions.

    Host passes real PmCredentialPort and credential_reader, plus a callable
    session_check(principal_ref, session_ref, action) that freshly verifies
    current session custody and action authorization. commit_ready and
    initialize require explicit producer authorization, distinct from reads.
    The callable is a trust boundary, never a caller-supplied 'authenticated'
    flag. Place the database on the owner's local protected disk, not Drive,
    NFS or a synced folder. Readers use an existing DB and never create it.
    """
    def __init__(self, *, database_path, owner_ref, provider_ref, audience,
                 principal_ref, session_ref, credentials, credential_reader,
                 session_check, enabled=False):
        _need(all(_text(v) for v in (owner_ref, provider_ref, audience,
                                    principal_ref, session_ref)), "PM_SQLITE_CONFIG_INVALID")
        p = Path(database_path)
        _need(p.is_absolute() and p.name not in {"", ":memory:"}
              and not str(p).startswith(("\\\\", "//")), "PM_LOCAL_DATABASE_PATH_REQUIRED")
        _need(callable(getattr(credentials, "authenticate_and_authorize", None))
              and callable(credential_reader) and callable(session_check),
              "PM_SQLITE_CUSTODY_UNBOUND")
        self.path = p
        self.owner_ref, self.provider_ref, self.audience = owner_ref, provider_ref, audience
        self.principal_ref, self.session_ref = principal_ref, session_ref
        self.credentials, self.credential_reader = credentials, credential_reader
        self.session_check, self.enabled = session_check, enabled is True
        self.SYNTHETIC_TEST_ONLY = any(getattr(x, "SYNTHETIC_TEST_ONLY", False) is True
            for x in (credentials, credential_reader, session_check))

    def _authorize(self, action, owner_ref=None, principal_ref=None):
        _need(self.enabled, "PM_SQLITE_DEFAULT_OFF")
        _need(owner_ref in (None, self.owner_ref)
              and principal_ref in (None, self.principal_ref), "PM_SQLITE_IDENTITY_MISMATCH")
        try:
            identity = self.credentials.authenticate_and_authorize(
                self.credential_reader(), owner_ref=self.owner_ref,
                audience=self.audience, action=action)
            allowed = (type(identity) is AuthenticatedPmPrincipal
                and identity == AuthenticatedPmPrincipal(self.principal_ref,
                                                        self.owner_ref, self.audience)
                and self.session_check(self.principal_ref, self.session_ref, action) is True)
        except Exception:
            raise PmProviderError("PM_SQLITE_AUTH_UNAVAILABLE") from None
        _need(allowed, "PM_SQLITE_AUTH_DENIED")

    @contextmanager
    def _connection(self, *, initialize=False, write=False):
        _need(self.path.parent.is_dir() and not self.path.is_symlink(),
              "PM_LOCAL_DATABASE_PATH_INVALID")
        uri = self.path.as_uri() + ("?mode=rwc" if initialize else "?mode=rw" if write else "?mode=ro")
        db = None
        try:
            db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.execute("COMMIT")
        except PmProviderError:
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            raise PmProviderError("PM_SQLITE_TRANSACTION_UNAVAILABLE") from None
        finally:
            if db is not None:
                db.close()

    def initialize(self):
        self._authorize("initialize")
        with self._connection(initialize=True, write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS pm_owner_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT NOT NULL, provider TEXT NOT NULL, seq INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS pm_owner_commits (attempt TEXT PRIMARY KEY, request_hash TEXT NOT NULL, receipt TEXT UNIQUE NOT NULL, referent TEXT NOT NULL, seq INTEGER UNIQUE NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS pm_owner_current (referent TEXT PRIMARY KEY, receipt TEXT UNIQUE NOT NULL REFERENCES pm_owner_commits(receipt))")
            db.execute("INSERT OR IGNORE INTO pm_owner_meta VALUES (1,?,?,0)", (self.owner_ref, self.provider_ref))
            self._metadata(db)

    def _metadata(self, db):
        row = db.execute("SELECT owner,provider,seq FROM pm_owner_meta WHERE singleton=1").fetchone()
        _need(row is not None and row[:2] == (self.owner_ref, self.provider_ref),
              "PM_SQLITE_DATABASE_CUSTODY_MISMATCH")
        return row[2]

    def _decode(self, row):
        if row is None:
            return None
        raw, digest = row
        _need(_sha(raw.encode("utf-8")) == digest, "PM_SQLITE_PAYLOAD_HASH_MISMATCH")
        obj = json.loads(raw)
        r = obj["record"]
        r["way_home"] = tuple(r["way_home"])
        record = _valid(PmReadyRecord(**r), self.owner_ref)
        return AtomicProjection(record, obj["material_sha256"], obj["source_cut"],
            obj["active_hold"], self.provider_ref, self.principal_ref, self.session_ref)

    def commit_ready(self, transition: PmReadyTransition):
        self._authorize("commit_ready")
        t = transition
        _need(type(t) is PmReadyTransition and all(_text(v) for v in
            (t.attempt_ref, t.referent_key, t.claim_ref, t.packet_ref, t.queue_ref,
             t.source_cut)), "PM_TRANSITION_INVALID")
        _need(t.expected_revision is None or _text(t.expected_revision), "PM_EXPECTED_REVISION_INVALID")
        _need(t.ready_state in {"MATERIAL_READY", "BIND_AVAILABLE"}
              and type(t.packet_bytes) is bytes and 0 < len(t.packet_bytes) <= 65536
              and type(t.material_bytes) is bytes and 0 < len(t.material_bytes) <= 65536
              and (t.active_hold is None or type(t.active_hold) is dict)
              and type(t.way_home) is tuple and bool(t.way_home)
              and all(_text(v) for v in t.way_home), "PM_TRANSITION_INVALID")
        request = asdict(t)
        request["packet_bytes"] = _sha(t.packet_bytes)
        request["material_bytes"] = _sha(t.material_bytes)
        canonical = _json(request)  # freezes mutable hold before any database work
        request = json.loads(canonical)
        request_hash = _sha(canonical.encode("utf-8"))
        referent = "pm-ready:" + _sha(_json([self.owner_ref, t.referent_key]).encode("utf-8"))
        with self._connection(write=True) as db:
            seq = self._metadata(db)
            prior = db.execute("SELECT request_hash,payload,payload_hash FROM pm_owner_commits WHERE attempt=?", (t.attempt_ref,)).fetchone()
            if prior is not None:
                _need(prior[0] == request_hash, "PM_ATTEMPT_CONTENT_CONFLICT")
                return self._decode(prior[1:])
            old = self._decode(db.execute("SELECT c.payload,c.payload_hash FROM pm_owner_current h JOIN pm_owner_commits c ON c.receipt=h.receipt WHERE h.referent=?", (referent,)).fetchone())
            _need((old.record.revision_after if old else None) == t.expected_revision,
                  "PM_COMMIT_REVISION_CONFLICT")
            seq += 1
            key = uuid4().hex
            revision = f"pm-revision:{seq}:{key}"
            record = PmReadyRecord(self.owner_ref, "pm-receipt:" + key,
                "pm-event:" + key, referent, seq, seq, revision,
                old.record.revision_after if old else None, t.claim_ref,
                t.packet_ref, t.queue_ref, revision, revision, revision,
                request["packet_bytes"], t.ready_state, "pm-snapshot:" + key,
                "", "pm-commit:" + key, "pm-readback:" + key,
                datetime.now(timezone.utc).isoformat(), tuple(request["way_home"]))
            record = replace(record, snapshot_sha256=snapshot_sha256(record))
            _valid(record, self.owner_ref)
            payload = _json({"record": asdict(record),
                "material_sha256": request["material_bytes"],
                "source_cut": request["source_cut"], "active_hold": request["active_hold"]})
            db.execute("INSERT INTO pm_owner_commits VALUES (?,?,?,?,?,?,?)",
                (t.attempt_ref, request_hash, record.receipt_ref, referent, seq,
                 payload, _sha(payload.encode("utf-8"))))
            db.execute("INSERT INTO pm_owner_current VALUES (?,?) ON CONFLICT(referent) DO UPDATE SET receipt=excluded.receipt", (referent, record.receipt_ref))
            db.execute("UPDATE pm_owner_meta SET seq=? WHERE singleton=1", (seq,))
            # A fresh second authorization before COMMIT catches revoked custody.
            self._authorize("commit_ready")
        return self.read_receipt(record.receipt_ref, owner_ref=self.owner_ref,
                                 principal_ref=self.principal_ref)

    def read_receipt(self, receipt_ref, *, owner_ref, principal_ref):
        return self._read("read_receipt", receipt_ref, owner_ref, principal_ref)

    def read_current(self, referent_id, *, owner_ref, principal_ref):
        return self._read("read_current", referent_id, owner_ref, principal_ref)

    def _read(self, action, ref, owner_ref, principal_ref):
        self._authorize(action, owner_ref, principal_ref)
        _need(_text(ref), "PM_SQLITE_READ_REFERENCE_INVALID")
        with self._connection() as db:
            self._metadata(db)
            if action == "read_receipt":
                row = db.execute("SELECT payload,payload_hash FROM pm_owner_commits WHERE receipt=?", (ref,)).fetchone()
            else:
                row = db.execute("SELECT c.payload,c.payload_hash FROM pm_owner_current h JOIN pm_owner_commits c ON c.receipt=h.receipt WHERE h.referent=?", (ref,)).fetchone()
            cut = self._decode(row)
            if cut is not None:
                _need((cut.record.receipt_ref if action == "read_receipt"
                       else cut.record.referent_id) == ref, "PM_SQLITE_REFERENCE_MISMATCH")
            self._authorize(action, owner_ref, principal_ref)
        return cut

    def source_port(self, *, expectation, receipt_ref):
        """Register only this source seam in the existing X4 host factory."""
        return create_pm_source_port(PmProjectionBinding(expectation, receipt_ref,
            self.audience, self.provider_ref, self.principal_ref, self.session_ref,
            self.credentials, self, self.credential_reader, self.enabled))
