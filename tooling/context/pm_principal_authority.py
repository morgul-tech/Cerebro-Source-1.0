"""Unactivated protected Context office/PM-operation custody.

Human appointment and holder operation signatures are Ed25519. The service
stores PUBLIC verification keys, never Human/Principal signing secrets.
One commissioned, protected SQLite store is global to this service scope;
intake directories cannot select a different spend database. No environment,
worker argument or raw plan commissions this factory. Provisioning is separate.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class AuthorityRefused(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise AuthorityRefused(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 'AUTH_DUPLICATE_JSON_FIELD')
            out[key] = value
        return out
    require(type(raw) is bytes and 0 < len(raw) <= 1048576, 'AUTH_BOUNDED_RAW_BYTES_REQUIRED')
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(AuthorityRefused('AUTH_NONFINITE_JSON')))


def text(value):
    return type(value) is str and bool(value) and value == value.strip()


def interval(value, now, maximum=None):
    a, b = value['not_before'], value['expires_at']
    require(all(type(x) in (int, float) and math.isfinite(x) for x in (a, b, now))
            and a <= now < b and (maximum is None or 0 < b-a <= maximum),
            'AUTH_EXPIRED_OR_INVALID_INTERVAL')


def verify_signature(raw, public_hex):
    envelope = decode(raw)
    require(type(envelope) is dict and set(envelope) == {'payload', 'signature'},
            'AUTH_SIGNED_ENVELOPE_FIELDS')
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
            bytes.fromhex(envelope['signature']), canonical(envelope['payload']))
    except (InvalidSignature, ValueError, TypeError, KeyError) as exc:
        raise AuthorityRefused('AUTH_SIGNATURE_INVALID') from exc
    return envelope['payload']


CONFIG_FIELDS = {'schema', 'human_public_key', 'tenant_ref', 'workspace_ref', 'target', 'backend_sha256'}
APPOINTMENT_FIELDS = {'schema', 'holder_ref', 'holder_generation', 'holder_public_key',
                      'epoch', 'previous_epoch', 'active', 'scope', 'not_before',
                      'expires_at', 'human_decision_sha256'}
OPERATION_FIELDS = {'schema', 'issuer', 'holder_generation', 'epoch', 'operation_id',
                    'nonce', 'tenant_ref', 'workspace_ref', 'action', 'target',
                    'backend_sha256', 'raw_owner_sha256', 'raw_plan_sha256',
                    'plan_sha256', 'before_head_sha256', 'candidate_generation',
                    'entry_sha256', 'adapter_sha256', 'not_before', 'expires_at'}


def protected_path(path, directory=False):
    """Production Linux/root boundary. Test custody never claims this proof."""
    require(os.name == 'posix' and os.geteuid() == 0, 'AUTH_ROOT_CUSTODIAN_REQUIRED')
    path = Path(path)
    require(path.is_absolute(), 'AUTH_ABSOLUTE_PROTECTED_PATH_REQUIRED')
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0
                and not (info.st_mode & 0o022), 'AUTH_UNPROTECTED_PATH')
    info = path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)),
            'AUTH_ORDINARY_FILE_OR_DIRECTORY_REQUIRED')
    require(not (info.st_mode & 0o077) and (directory or info.st_nlink == 1), 'AUTH_PRIVATE_CUSTODY_REQUIRED')


class PrincipalAuthorityFactory:
    """Construct only at the protected Context custodian composition root.

    from_protected_context is the production reader. provision is an explicit
    installation consumer, NOT an appointment or ordinary operation. The test
    factory uses actual crypto/SQLite but carries an unavoidable test-only mark.
    """
    def __init__(self, *args, **kwargs):
        raise AuthorityRefused('AUTH_USE_PROTECTED_FACTORY')

    @classmethod
    def _open(cls, directory, config, clock, test_only):
        require(type(config) is dict and set(config) == CONFIG_FIELDS
                and config['schema'] == 'cerebro-protected-principal-source/v1', 'AUTH_CONFIG_FIELDS')
        require(all(text(config[k]) for k in ('tenant_ref', 'workspace_ref', 'backend_sha256'))
                and type(config['target']) is dict
                and set(config['target']) == {'project', 'environment', 'service', 'deployment'}
                and all(text(x) for x in config['target'].values()), 'AUTH_CONFIG_SCOPE')
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(config['human_public_key']))
        except (ValueError, TypeError) as exc:
            raise AuthorityRefused('AUTH_HUMAN_ROOT_NOT_COMMISSIONED') from exc
        obj = object.__new__(cls)
        obj.directory, obj.config = Path(directory), copy.deepcopy(config)
        obj.clock, obj.test_only = clock, test_only
        obj.database = obj.directory / 'principal-authority.sqlite3'
        require(obj.database.is_file(), 'AUTH_STORE_NOT_COMMISSIONED')
        if not test_only:
            protected_path(obj.database)
        with obj._db() as db:
            require(db.execute('PRAGMA user_version').fetchone()[0] == 1, 'AUTH_STORE_SCHEMA')
        return obj

    @classmethod
    def from_protected_context(cls, directory, *, clock=time.time):
        directory = Path(directory)
        protected_path(directory, directory=True)
        protected_path(directory / 'authority-config.json')
        return cls._open(directory, decode((directory/'authority-config.json').read_bytes()), clock, False)

    @classmethod
    def provision(cls, directory, *, clock=time.time):
        """Explicit root-only one-time schema provisioning under pinned config.
        No keys generated; Human root must already be genuinely commissioned.
        This installs NO assignment and NO operation acceptance.
        """
        directory = Path(directory)
        protected_path(directory, directory=True)
        protected_path(directory/'authority-config.json')
        cls._initialize(directory)
        return cls.from_protected_context(directory, clock=clock)

    @classmethod
    def isolated_test_custody(cls, directory, config, *, clock):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        cls._initialize(directory)
        return cls._open(directory, config, clock, True)

    @staticmethod
    def _initialize(directory):
        p = directory/'principal-authority.sqlite3'
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with contextlib.closing(sqlite3.connect(p)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
                CREATE TABLE appointments(epoch INTEGER PRIMARY KEY, original BLOB NOT NULL,
                    decision BLOB NOT NULL, fingerprint TEXT NOT NULL UNIQUE);
                CREATE TABLE operations(operation_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE,
                    authorization BLOB NOT NULL, original BLOB NOT NULL, rawplan BLOB NOT NULL,
                    state TEXT NOT NULL, result BLOB);
                PRAGMA user_version=1;
            ''')

    def _db(self):
        if not self.test_only:
            protected_path(self.database)
        db = sqlite3.connect(self.database.as_uri()+'?mode=rw', uri=True, timeout=5,
                             isolation_level=None)
        db.execute('PRAGMA synchronous=FULL')
        return contextlib.closing(db)

    def _scope(self):
        return {k: self.config[k] for k in ('tenant_ref', 'workspace_ref', 'target', 'backend_sha256')}

    def register_human_appointment(self, signed_bytes, retained_decision_bytes):
        """Protected installation/change port. Verify Human signature, not JSON authority."""
        p = verify_signature(signed_bytes, self.config['human_public_key'])
        require(type(p) is dict and set(p) == APPOINTMENT_FIELDS
                and p['schema'] == 'cerebro-human-principal-appointment/v1', 'AUTH_APPOINTMENT_FIELDS')
        require(type(retained_decision_bytes) is bytes and sha(retained_decision_bytes) == p['human_decision_sha256']
                and p['scope'] == self._scope() and type(p['active']) is bool
                and all(text(p[k]) for k in ('holder_ref', 'holder_generation')), 'AUTH_APPOINTMENT_SCOPE')
        interval(p, self.clock())
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(p['holder_public_key']))
        except (TypeError, ValueError) as exc:
            raise AuthorityRefused('AUTH_HOLDER_KEY_INVALID') from exc
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT COALESCE(MAX(epoch),0) FROM appointments').fetchone()[0]
            require(type(p['previous_epoch']) is int and type(p['epoch']) is int
                    and p['previous_epoch'] == previous and p['epoch'] == previous+1, 'AUTH_EPOCH_CHANGED')
            db.execute('INSERT INTO appointments VALUES(?,?,?,?)',
                       (p['epoch'], signed_bytes, retained_decision_bytes, sha(signed_bytes)))
            db.execute('COMMIT')
        return {'epoch': p['epoch'], 'appointment_sha256': sha(signed_bytes), 'test_only': self.test_only}

    def _current(self, db):
        row = db.execute('SELECT original,decision FROM appointments ORDER BY epoch DESC LIMIT 1').fetchone()
        require(row is not None, 'AUTH_NO_HUMAN_COMMISSIONED_PRINCIPAL')
        p = verify_signature(row[0], self.config['human_public_key'])
        require(sha(row[1]) == p['human_decision_sha256'] and p['scope'] == self._scope()
                and p['active'] is True, 'AUTH_PRINCIPAL_REVOKED_OR_SCOPE_CHANGED')
        interval(p, self.clock())
        return p

    def _operation(self, db, signed_bytes, original, rawplan):
        office = self._current(db)
        p = verify_signature(signed_bytes, office['holder_public_key'])
        require(type(p) is dict and set(p) == OPERATION_FIELDS
                and p['schema'] == 'cerebro-principal-pm-operation/v1', 'AUTH_OPERATION_FIELDS')
        require(p['issuer'] == office['holder_ref'] and p['holder_generation'] == office['holder_generation']
                and type(p['epoch']) is int and p['epoch'] == office['epoch'], 'AUTH_STALE_OR_UNKNOWN_HOLDER')
        require(p['action'] in ('STAGE', 'TRANSFER') and text(p['operation_id']) and text(p['nonce']),
                'AUTH_OPERATION_ACTION_NONCE')
        require(all(p[k] == self.config[k] for k in ('tenant_ref', 'workspace_ref', 'target', 'backend_sha256')),
                'AUTH_OPERATION_TARGET_BACKEND_SCOPE')
        interval(p, self.clock(), 1200)
        plan = decode(rawplan)
        require(type(plan) is dict and set(plan) == {'action', 'before', 'after', 'decision'}
                and p['action'] == plan['action'] and p['raw_plan_sha256'] == sha(rawplan)
                and p['raw_owner_sha256'] == sha(original)
                and p['plan_sha256'] == sha(canonical(plan))
                and p['before_head_sha256'] == sha(canonical(plan['before']))
                and p['candidate_generation'] == plan['decision']['generation_ref']
                and all(plan['decision'][k] == p[k] for k in ('tenant_ref','workspace_ref')), 'AUTH_EXACT_RAW_OPERATION')
        return p

    def accept_operation(self, signed_bytes, *, owner_original_bytes, raw_plan_bytes):
        """Real separate acceptance producer; does not execute or claim the action."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            p = self._operation(db, signed_bytes, owner_original_bytes, raw_plan_bytes)
            try:
                db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,NULL)',
                           (p['operation_id'], p['nonce'], signed_bytes, owner_original_bytes,
                            raw_plan_bytes, 'ACCEPTED'))
            except sqlite3.IntegrityError as exc:
                raise AuthorityRefused('AUTH_OPERATION_OR_NONCE_ALREADY_ACCEPTED_NO_RETRY') from exc
            db.execute('COMMIT')
        return {'operation_id': p['operation_id'], 'authorization_sha256': sha(signed_bytes),
                'test_only': self.test_only}

    def accepted_operation(self, operation_id, *, witness, actual_target):
        from pm_protected_owner_entry import BackendWitness
        require(type(witness) is BackendWitness and actual_target == self.config['target']
                and witness.expected == self.config['backend_sha256'], 'AUTH_ACTUAL_BACKEND_TARGET_REQUIRED')
        with self._db() as db:
            row = db.execute('SELECT authorization,original,rawplan,state FROM operations WHERE operation_id=?',
                             (operation_id,)).fetchone()
            require(row is not None and row[3] == 'ACCEPTED', 'AUTH_ACCEPTED_OPERATION_MISSING_OR_SPENT')
            self._operation(db, *row[:3])
        obj = object.__new__(AcceptedPmOperation)
        obj.factory, obj.operation_id, obj.witness = self, operation_id, witness
        obj.db = None
        return obj


class AcceptedPmOperation:
    def __init__(self):
        raise AuthorityRefused('AUTH_ACCEPTANCE_FACTORY_REQUIRED')

    def _bound(self, db, release, plan, original):
        row = db.execute('SELECT authorization,original,rawplan,state FROM operations WHERE operation_id=?',
                         (self.operation_id,)).fetchone()
        require(row is not None, 'AUTH_ACCEPTANCE_DISAPPEARED')
        p = self.factory._operation(db, *row[:3])
        require(original == row[1] and decode(row[2]) == plan
                and release == {'issuer': p['issuer'], 'operation': 'PM_OWNER_'+p['action'],
                    'operation_id': p['operation_id']+':'+p['action'], 'input_sha256': p['plan_sha256'],
                    'entry_sha256': p['entry_sha256'], 'owner_original_sha256': p['raw_owner_sha256'],
                    'not_before': p['not_before'], 'expires_at': p['expires_at']}, 'AUTH_RELEASE_NOT_ACCEPTED')
        return p

    def check_prepared(self, *, release, plan, owner_original_bytes, adapter_sha256):
        with self.factory._db() as db:
            p = self._bound(db, release, plan, owner_original_bytes)
            state = db.execute('SELECT state FROM operations WHERE operation_id=?',(self.operation_id,)).fetchone()[0]
            require(state == 'ACCEPTED' and p['adapter_sha256'] == adapter_sha256, 'AUTH_ADAPTER_OR_ACCEPTED_STATE')

    def verify(self, *, connection, release, plan, owner_original_bytes):
        require(self.db is not None, 'AUTH_EPOCH_LOCK_NOT_HELD')
        self._bound(self.db, release, plan, owner_original_bytes)
        self.witness.verify(connection)
        return True

    @contextlib.contextmanager
    def execution_guard(self, *, release, plan, owner_original_bytes, isolated_test=False):
        require(self.db is None and (not self.factory.test_only or isolated_test is True),
                'AUTH_TEST_CUSTODY_NOT_PRODUCTION_AUTHORITY')
        with self.factory._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._bound(db, release, plan, owner_original_bytes)
            changed = db.execute("UPDATE operations SET state='STARTED_UNKNOWN_NO_RETRY' WHERE operation_id=? AND state='ACCEPTED'",
                                 (self.operation_id,)).rowcount
            require(changed == 1, 'AUTH_GLOBAL_OPERATION_ALREADY_SPENT_NO_RETRY')
            db.execute('COMMIT')  # Durable global claim before any backend action.
            db.execute('BEGIN IMMEDIATE')  # Fences Human epoch changes through effect+readback.
            try:
                self._bound(db, release, plan, owner_original_bytes)
                self.db = db
                yield
                db.execute("UPDATE operations SET state='COMPLETED' WHERE operation_id=?", (self.operation_id,))
                db.execute('COMMIT')
            except BaseException as exc:
                exc.add_note('AUTH_ACCEPTED_OPERATION_SPENT_NO_RETRY')
                try:
                    db.execute('ROLLBACK')  # Persistent STARTED remains spent, even on uncertain effect.
                except BaseException:
                    exc.add_note('AUTH_CUSTODY_ROLLBACK_UNKNOWN_NO_RETRY')
                raise
            finally:
                self.db = None

    def record_result(self, result):
        require(self.db is not None, 'AUTH_EPOCH_LOCK_NOT_HELD')
        self.db.execute('UPDATE operations SET result=? WHERE operation_id=?',
                        (canonical(result), self.operation_id))
