"""Root-custodian ordinary PM support. No CLI/MCP, key issuer or birth API.

The installed composition supplies its actual target and BackendWitness. This
module never discovers credentials. All supported mutable entrypoints share
one persistent lock; deployment/config drift and unfinished rebind fail closed.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import inspect
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

from pm_principal_authority import (
    PrincipalAuthorityFactory, APPOINTMENT_FIELDS, CONFIG_FIELDS,
    canonical, decode, interval, protected_path, require, sha, verify_signature,
)

CUSTODY = Path('/var/lib/cerebro-pm-authority/authority')
PHASES = ('STAGE', 'ROLE_ATTACH', 'TRANSFER', 'PROFILE', 'FIRST_USE',
          'FUNCTION_RETIRE', 'HANDOFF')


def _target():
    return {k: os.environ.get(v, '') for k, v in {
        'project': 'RAILWAY_PROJECT_ID', 'environment': 'RAILWAY_ENVIRONMENT_ID',
        'service': 'RAILWAY_SERVICE_ID', 'deployment': 'RAILWAY_DEPLOYMENT_ID',
    }.items()}


def _private_atomic(path, raw):
    """Same-directory durable replacement; never used to replace the ledger."""
    temporary = path.with_name(path.name + '.pending')
    if temporary.exists():
        protected_path(temporary)
        require(temporary.read_bytes() == raw, 'SUPPORT_UNKNOWN_CONFIG_TEMP_REFUSED')
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _ledger_digest(db,exclude_accepted_operation=None):
    """Bind all retained appointment and operation bytes, states and results."""
    digest = hashlib.sha256()
    tables = [('appointments', 'epoch'), ('operations', 'operation_id')]
    if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='support_steps'").fetchone():
        tables.append(('support_steps', 'routine_ref,phase'))
    for table, order in tables:
        for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}'):
            if table=='operations' and row[0]==exclude_accepted_operation:
                require(row[5]=='ACCEPTED','SUPPORT_HISTORY_CANNOT_EXCLUDE_SPENT_OPERATION')
                continue
            for value in row:
                raw = value if isinstance(value, bytes) else canonical(value)
                digest.update(len(raw).to_bytes(8, 'big'))
                digest.update(raw)
    return digest.hexdigest()


def _schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS support_rebinds(
            decision_sha TEXT PRIMARY KEY, old_config BLOB NOT NULL,
            new_config BLOB NOT NULL, appointment BLOB NOT NULL,
            decision BLOB NOT NULL, ledger_sha TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS support_steps(
            routine_ref TEXT NOT NULL, phase TEXT NOT NULL, subject BLOB NOT NULL,
            state TEXT NOT NULL, receipt BLOB, PRIMARY KEY(routine_ref,phase));
    ''')


def _protected_code(path):
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0
                and not info.st_mode & 0o022, 'SUPPORT_SOURCE_CUSTODY')
    require(path.is_file(), 'SUPPORT_SOURCE_FILE_REQUIRED')


class OrdinaryPmSupport:
    """Instantiate ONLY in the sealed, protected service composition root.

    expected_target/backend and source_files are pinned installation inputs,
    never request JSON. Non-SQL phases use explicitly manual owner records;
    no callback can manufacture native/use/retirement proof.
    """
    def __init__(self, *, expected_target, expected_backend_sha256, expected_volume_ref,source_files,clock=time.time):
        self.directory = CUSTODY
        self.target = dict(expected_target)
        self.backend = expected_backend_sha256
        self.volume_ref=expected_volume_ref
        self.source_files = dict(source_files)
        self.clock = clock
        require(set(self.target) == {'project', 'environment', 'service', 'deployment'}
                and all(type(v) is str and v for v in self.target.values()), 'SUPPORT_EXACT_TARGET')
        require(type(self.backend) is str and len(self.backend) == 64, 'SUPPORT_BACKEND_PIN')
        require(bool(self.source_files), 'SUPPORT_INSTALLED_SOURCE_PINS_REQUIRED')
        require(type(self.volume_ref)is str and bool(self.volume_ref),'SUPPORT_ACTUAL_SELECTED_VOLUME_REQUIRED')

    @contextlib.contextmanager
    def _locked(self):
        import fcntl  # production Linux only, no Windows custody claim
        protected_path(self.directory, directory=True)
        path = self.directory / 'ordinary-support.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                    and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600,
                    'SUPPORT_LOCK_CUSTODY')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            required = {str(Path(__file__).resolve())}
            for name in ('pm_principal_authority','pm_warm_owner_adapter','pm_ordinary_owner_operator',
                         'pm_admission_owner','pm_bounded_qualification','pm_protected_owner_entry',
                         'control_context_registry','authority_mount_preflight','pm_custodian_history'):
                required.add(str(Path(importlib.import_module(name).__file__).resolve()))
            require(required <= {str(Path(p).resolve()) for p in self.source_files},
                    'SUPPORT_DIRECT_CONSUMER_SOURCE_PINS_REQUIRED')
            for source, expected in self.source_files.items():
                _protected_code(Path(source))
                require(sha(Path(source).read_bytes()) == expected, 'SUPPORT_INSTALLED_SOURCE_CHANGED')
            require(_target() == self.target, 'SUPPORT_ACTUAL_DEPLOYMENT_CHANGED')
            from authority_mount_preflight import observe_selected_mount
            observe_selected_mount()  # mandatory fixed dc72 mount/custody gate for ALL entries
            yield
        finally:
            os.close(fd)

    def _config(self, *, match_target=True):
        path = self.directory / 'authority-config.json'
        protected_path(path)
        raw = path.read_bytes()
        value = decode(raw)
        require(type(value) is dict and set(value) == CONFIG_FIELDS,
                'SUPPORT_CONFIG_FIELDS')
        if match_target:
            require(value['target'] == self.target and value['backend_sha256'] == self.backend,
                    'SUPPORT_REBIND_REQUIRED_PRESERVE_LEDGER')
        return raw, value

    def _db(self):
        path = self.directory / 'principal-authority.sqlite3'
        protected_path(path)
        db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                             isolation_level=None, timeout=5)
        db.execute('PRAGMA synchronous=FULL')
        return contextlib.closing(db)

    def _factory(self):
        self._config()
        factory = PrincipalAuthorityFactory.from_protected_context(self.directory, clock=self.clock)
        with self._db() as db:
            # Schema is created only by the explicit support provision/upgrade.
            pending = db.execute("SELECT COUNT(*) FROM support_rebinds WHERE state!='COMPLETE'").fetchone()[0]
            require(pending == 0, 'SUPPORT_REBIND_INCOMPLETE_NO_EFFECT')
        return factory

    def _require_history_fit(self, *,kind='INSPECT',operation_id=None,raw_owner=None,
                             raw_plan=None,decision_bytes=None,config_raw=None):
        from pm_custodian_history import read_selected_history
        with self._db() as db:
            # The current exact ACCEPTED row is created after the independently
            # archived pre-dispatch snapshot. It is not a spent action and may
            # alone be excluded to join the SAME prepared operation's record.
            ledger=_ledger_digest(db,operation_id if kind=='OPERATION' else None)
        if config_raw is None:config_raw=self._config(match_target=False)[0]
        try:
            result=read_selected_history(target=self.target,backend_sha256=self.backend,
                expected_volume_ref=self.volume_ref,
                config_raw=config_raw,ledger_sha256=ledger,kind=kind,operation_id=operation_id,
                raw_owner=raw_owner,raw_plan=raw_plan,decision=decision_bytes)
            if result['record']['restore_disposition']=='FIRST_EXCLUSIVE_COMMISSION_CONFIRMED':
                with self._db() as db:
                    require(all(db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]==0
                                for t in ('appointments','operations','support_steps')),
                            'SUPPORT_FIRST_COMMISSION_CANNOT_REPLACE_HISTORY')
            return result
        except FileNotFoundError as exc:
            raise RuntimeError('SUPPORT_HISTORY_ORIGINAL_MISSING_HOLD_CURRENT_PM_TO_D1_CUSTODIAN') from exc

    def export_history(self):
        """Private owned SSH/capture output for existing independent archive.

        No public endpoint or currentness grant. Preserves exact BLOB bytes in
        a bounded canonical export; pending states remain visible unchanged.
        """
        with self._locked():
            config_raw,_=self._config(match_target=False)
            with self._db() as db:
                rows={}
                for table,order in (('appointments','epoch'),('operations','operation_id'),
                                    ('support_steps','routine_ref,phase'),('support_rebinds','decision_sha')):
                    rows[table]=[[{'blob_hex':v.hex()} if isinstance(v,bytes) else v for v in row]
                                 for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}')]
                ledger=_ledger_digest(db)
            raw=canonical({'schema':'cerebro-pm-private-full-history-export/v1',
                           'config_sha256':sha(config_raw),'ledger_sha256':ledger,'rows':rows})
            require(len(raw)<=16*1024*1024,'SUPPORT_HISTORY_EXPORT_BOUND')
            return raw

    def provision(self):
        """Explicit support installation only. Config/root trust must exist.

        Does not create a directory, config/key, appointment or operation. An
        existing store refuses; never initialize a replacement after UNKNOWN.
        """
        with self._locked():
            self._config()
            factory = PrincipalAuthorityFactory.provision(self.directory, clock=self.clock)
            with self._db() as db:
                _schema(db)
            return {'support': 'PROVISIONED_NOT_APPOINTED', 'authority': 'NONE',
                    'candidate': 'NOT_SELECTED', 'acceptance': 'NOT_ACCEPTED'}

    def upgrade_support_schema(self):
        """Selected installation of support tables in an existing full ledger."""
        with self._locked():
            self._config()
            PrincipalAuthorityFactory.from_protected_context(self.directory, clock=self.clock)
            with self._db() as db:
                before = _ledger_digest(db)
                _schema(db)
                require(_ledger_digest(db) == before, 'SUPPORT_LEDGER_CHANGED_DURING_UPGRADE')
            return {'support_schema': 'INSTALLED', 'ledger_sha256': before}

    def readiness(self):
        """Actual installed-support read. Never a candidate/operation PASS."""
        with self._locked():
            from authority_mount_preflight import observe_selected_mount
            mount = observe_selected_mount()
            config = self.directory / 'authority-config.json'
            if not config.exists():
                return {'support': 'INSTALLED_NOT_COMMISSIONED', 'mount_observation': mount, 'authority': 'NONE',
                        'candidate': 'NOT_SELECTED', 'acceptance': 'NOT_ACCEPTED',
                        'execution_allowed': False}
            raw, _ = self._config()
            if not (self.directory / 'principal-authority.sqlite3').exists():
                return {'support': 'CONFIG_PRESENT_STORE_NOT_PROVISIONED', 'mount_observation': mount,
                        'config_sha256': sha(raw), 'authority': 'NONE',
                        'candidate': 'NOT_SELECTED', 'acceptance': 'NOT_ACCEPTED',
                        'execution_allowed': False}
            factory = self._factory()
            with self._db() as db:
                count = db.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]
                ledger = _ledger_digest(db)
                office=None
                if count:office=factory._current(db)  # actual signature/scope/time/revocation recheck
            return {'support': 'INSTALLED_STORE_READBACK', 'mount_observation': mount, 'config_sha256': sha(raw),
                    'ledger_sha256': ledger, 'appointment_history_present': bool(count),
                    'current_holder_eligibility': 'VERIFIED_CURRENT_OFFICE' if office else 'NOT_APPOINTED',
                    'office_epoch':office['epoch'] if office else None,'authority': 'NONE',
                    'restore_history_fit': 'NOT_ASSESSED_NO_RESTORE_PERMISSION',
                    'candidate': 'NOT_SELECTED', 'acceptance': 'NOT_ACCEPTED',
                    'backend_freshness': 'NOT_ASSESSED', 'execution_allowed': False}

    def preflight(self, *, owner_original_bytes, caller_original_bytes, plan):
        from pm_warm_owner_adapter import preflight_material, PreparedPmOwnerMaterial, validate_operation_material, __file__ as adapter
        from pm_ordinary_owner_operator import __file__ as operator
        with self._locked():
            self._factory()
            return preflight_material(owner_original_bytes=owner_original_bytes,
                caller_original_bytes=caller_original_bytes, plan=plan,
                actual_target=self.target, backend_sha256=self.backend,
                adapter_sha256=sha(Path(adapter).read_bytes()),
                owner_operator_sha256=sha(Path(operator).read_bytes()),clock=self.clock)

    def register_appointment(self, signed_bytes, retained_decision_bytes):
        with self._locked():
            self._require_history_fit(kind='OFFICE',decision_bytes=retained_decision_bytes)
            return self._factory().register_human_appointment(signed_bytes, retained_decision_bytes)

    def accept_operation(self, signed_bytes, *, owner_original_bytes, raw_plan_bytes,
                         caller_original_bytes):
        from pm_warm_owner_adapter import preflight_material, PreparedPmOwnerMaterial, validate_operation_material, __file__ as adapter
        from pm_ordinary_owner_operator import __file__ as operator
        with self._locked():
            factory = self._factory()
            from pm_custodian_history import verify_operation_history_nonce
            material_args=dict(owner_original_bytes=owner_original_bytes,
                caller_original_bytes=caller_original_bytes, plan=decode(raw_plan_bytes),
                actual_target=self.target, backend_sha256=self.backend,
                adapter_sha256=sha(Path(adapter).read_bytes()),
                owner_operator_sha256=sha(Path(operator).read_bytes()))
            preflight_material(**material_args,clock=self.clock)
            material=PreparedPmOwnerMaterial(**material_args)
            with factory._db() as db:
                verified=factory._operation(db,signed_bytes,owner_original_bytes,raw_plan_bytes)
                validate_operation_material(verified,material)
            history=self._require_history_fit(kind='OPERATION',operation_id=verified['operation_id'],
                raw_owner=owner_original_bytes,raw_plan=raw_plan_bytes)
            verify_operation_history_nonce(verified,history['history_original_sha256'])
            return factory.accept_operation(signed_bytes, owner_original_bytes=owner_original_bytes,
                                             raw_plan_bytes=raw_plan_bytes)

    def progress(self, routine_ref, subject):
        with self._locked():
            factory = self._factory()
            with factory._db() as epoch_db:
                require(subject.get('principal_epoch') == factory._current(epoch_db)['epoch'],
                        'SUPPORT_CHECKPOINT_EPOCH_CHANGED')
            with self._db() as db:
                return _progress(db, routine_ref, subject)

    def execute(self, directory, manifest_sha256, *, witness, accepted_operation_id,
                routine_ref, subject):
        """Takes ownership of this invocation's witness, including refusals."""
        from pm_protected_owner_entry import BackendWitness
        require(type(witness) is BackendWitness, 'SUPPORT_REAL_BACKEND_WITNESS_REQUIRED')
        try:
            return self._execute(directory,manifest_sha256,witness=witness,
                accepted_operation_id=accepted_operation_id,routine_ref=routine_ref,subject=subject)
        finally:
            primary=sys.exception()
            if not witness.closed:
                try:
                    witness.close()
                except BaseException:
                    if primary is not None:
                        primary.add_note('SUPPORT_OWN_WITNESS_CLOSURE_UNKNOWN_NO_RETRY')
                    else:
                        raise

    def _execute(self, directory, manifest_sha256, *, witness, accepted_operation_id,
                 routine_ref, subject):
        from pm_warm_owner_adapter import execute_intake, read_intake_material, preflight_material, WarmPmOwnerAuthority, __file__ as adapter
        from pm_ordinary_owner_operator import __file__ as operator
        from pm_protected_owner_entry import BackendWitness
        require(type(witness) is BackendWitness and witness.expected == self.backend,
                'SUPPORT_REAL_BACKEND_WITNESS_REQUIRED')
        with self._locked():
            factory = self._factory()
            operation = factory.accepted_operation(accepted_operation_id, witness=witness,
                                                   actual_target=self.target)
            with factory._db() as db:
                original, rawplan = db.execute('SELECT original,rawplan FROM operations WHERE operation_id=?',
                                               (accepted_operation_id,)).fetchone()
            plan = decode(rawplan)
            from pm_custodian_history import verify_operation_history_nonce
            history=self._require_history_fit(kind='OPERATION',operation_id=accepted_operation_id,
                raw_owner=original,raw_plan=rawplan)
            with factory._db() as db:
                authorization=db.execute('SELECT authorization FROM operations WHERE operation_id=?',
                                         (accepted_operation_id,)).fetchone()[0]
                verified=factory._operation(db,authorization,original,rawplan)
            verify_operation_history_nonce(verified,history['history_original_sha256'])
            packet_owner, packet_caller, packet_plan = read_intake_material(directory, manifest_sha256)
            require(packet_owner == original and packet_plan == plan, 'SUPPORT_PACKET_NOT_ACCEPTED_ORIGINAL')
            preflight_material(owner_original_bytes=packet_owner,caller_original_bytes=packet_caller,
                plan=packet_plan,actual_target=self.target,backend_sha256=self.backend,
                adapter_sha256=sha(Path(adapter).read_bytes()),owner_operator_sha256=sha(Path(operator).read_bytes()),clock=self.clock)
            # Check actual signed acceptance/release/code/validity before any
            # checkpoint becomes STARTED; live consumer checks it again.
            WarmPmOwnerAuthority(owner_original_bytes=packet_owner,caller_original_bytes=packet_caller,
                plan=packet_plan,witness=witness,actual_target=self.target,
                adapter_sha256=sha(Path(adapter).read_bytes()),owner_operator_sha256=sha(Path(operator).read_bytes()),
                accepted_operation=operation,clock=self.clock)
            phase = plan['action']
            with factory._db() as epoch_db:
                epoch = factory._current(epoch_db)['epoch']
            require(subject == _subject(plan, self.target, epoch), 'SUPPORT_EXACT_ROUTINE_SUBJECT')
            require(phase in ('STAGE', 'TRANSFER'), 'SUPPORT_ORDINARY_ACTION_ONLY')
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                require(_progress(db, routine_ref, subject)['next'] == phase,
                        'SUPPORT_FIRST_UNFINISHED_STEP_ONLY')
                db.execute('INSERT INTO support_steps VALUES(?,?,?,?,NULL)',
                           (routine_ref, phase, canonical(subject), 'STARTED_UNKNOWN_NO_RETRY'))
                db.execute('COMMIT')
            # The existing accepted-operation guard performs the actual global
            # spend and epoch lock; the support journal cannot authorize it.
            result = execute_intake(directory, manifest_sha256, witness=witness,
                authority_factory=factory, accepted_operation_id=accepted_operation_id,
                clock=self.clock)
            # COMPLETE requires independent backend readback AND own closure.
            # A closure failure leaves the durable checkpoint UNKNOWN/NO_RETRY.
            witness.close()
            receipt = {'operation_id': operation.operation_id, 'result': result}
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE support_steps SET state='COMPLETE',receipt=? WHERE routine_ref=? AND phase=? AND state='STARTED_UNKNOWN_NO_RETRY'",
                           (canonical(receipt), routine_ref, phase))
                require(db.execute('SELECT changes()').fetchone()[0] == 1, 'SUPPORT_CHECKPOINT_CHANGED')
                db.execute('COMMIT')
            return receipt

    def record_owner_observation(self, *, routine_ref, subject, phase, original_bytes,
                                 owner_ref,next_owner,method_ref):
        """Manual existing owner return, NOT automatic native/use proof.

        Only the existing protected custodian invokes this after actual owner
        material return. A record cannot replace real SQL/native transfer gates
        or authorize any profile/use/retirement action.
        """
        require(phase in PHASES and phase not in ('STAGE', 'TRANSFER'), 'SUPPORT_OBSERVATION_PHASE')
        require(type(original_bytes)is bytes and 0<len(original_bytes)<=1048576
                and all(type(v)is str and v for v in (owner_ref,next_owner,method_ref)),
                'SUPPORT_ACTUAL_MANUAL_OWNER_RETURN_REQUIRED')
        require(method_ref=='OPERATOR-SUPPORT1.3/CURRENT_PM/EXISTING_OWNER_MATERIAL_RETURN',
                'SUPPORT_EXISTING_MANUAL_OWNER_PORT_ONLY')
        with self._locked():
            factory = self._factory()
            with factory._db() as epoch_db:
                require(subject.get('principal_epoch') == factory._current(epoch_db)['epoch'],
                        'SUPPORT_CHECKPOINT_EPOCH_CHANGED')
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                require(_progress(db, routine_ref, subject)['next'] == phase,
                        'SUPPORT_FIRST_UNFINISHED_STEP_ONLY')
                db.execute('INSERT INTO support_steps VALUES(?,?,?,?,?)',
                           (routine_ref, phase, canonical(subject), 'OWNER_RECORDED', canonical({
                               'kind':'MANUAL_OWNER_RECORD_NOT_AUTOMATED_NATIVE_PROOF',
                               'owner_ref':owner_ref,'next_owner':next_owner,'method_ref':method_ref,
                               'original_hex':original_bytes.hex(),'original_sha256':sha(original_bytes)})))
                db.execute('COMMIT')
            return {'phase':phase,'state':'OWNER_RECORDED','receipt_sha256':sha(original_bytes),
                    'effect_performed':False,'automated_native_or_use_proof':False,'next_owner':next_owner}

    def prepare_rebind(self, new_config_bytes):
        """Read-only proposal; no signature, config change or epoch insertion."""
        with self._locked():
            old_raw, old = self._config(match_target=False)
            new = decode(new_config_bytes)
            _rebind_config_fit(old, new, self.target, self.backend)
            with self._db() as db:
                require(db.execute("SELECT COUNT(*) FROM support_rebinds WHERE state!='COMPLETE'").fetchone()[0] == 0,
                        'SUPPORT_REBIND_ALREADY_PENDING')
                epoch = db.execute('SELECT COALESCE(MAX(epoch),0) FROM appointments').fetchone()[0]
                ledger = _ledger_digest(db)
            return {'schema': 'cerebro-pm-support-rebind/v1',
                    'old_config_sha256': sha(old_raw), 'new_config_sha256': sha(new_config_bytes),
                    'ledger_sha256': ledger, 'previous_epoch': epoch,
                    'new_epoch': epoch + 1}

    def rebind(self, *, new_config_bytes, signed_appointment_bytes, retained_decision_bytes):
        """Explicit genuine Human-signed scope/epoch change, same full ledger.

        Uses the EXISTING appointment signature contract. The signature binds
        the retained exact migration decision by human_decision_sha256. Root
        key replacement and cross-service/tenant moves are outside this port.
        """
        with self._locked():
            self._require_history_fit(kind='REBIND',decision_bytes=retained_decision_bytes)
            old_raw, old = self._config(match_target=False)
            new = decode(new_config_bytes)
            _rebind_config_fit(old, new, self.target, self.backend)
            decision = decode(retained_decision_bytes)
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                require(db.execute("SELECT COUNT(*) FROM support_rebinds WHERE state!='COMPLETE'").fetchone()[0] == 0,
                        'SUPPORT_REBIND_ALREADY_PENDING')
                epoch = db.execute('SELECT COALESCE(MAX(epoch),0) FROM appointments').fetchone()[0]
                ledger = _ledger_digest(db)
                _verify_rebind(old_raw, new_config_bytes, signed_appointment_bytes,
                               retained_decision_bytes, epoch, ledger, self.clock())
                key = sha(retained_decision_bytes)
                db.execute('INSERT INTO support_rebinds VALUES(?,?,?,?,?,?,?)',
                    (key, old_raw, new_config_bytes, signed_appointment_bytes,
                     retained_decision_bytes, ledger, 'PREPARED'))
                db.execute('COMMIT')  # pending intent fences every supported entry
            return self._finish_rebind(key)

    def recover_rebind(self, decision_sha256):
        """Explicit recovery of this same durable migration, never PM replay."""
        with self._locked():
            with self._db() as db:
                row = db.execute('SELECT decision,state,old_config FROM support_rebinds WHERE decision_sha=?',
                                 (decision_sha256,)).fetchone()
                require(row is not None and row[1] == 'PREPARED', 'SUPPORT_REBIND_NOT_PENDING')
            self._require_history_fit(kind='REBIND',decision_bytes=row[0],config_raw=row[2])
            return self._finish_rebind(decision_sha256)

    def _finish_rebind(self, key):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT old_config,new_config,appointment,decision,ledger_sha,state FROM support_rebinds WHERE decision_sha=?', (key,)).fetchone()
            require(row is not None and row[5] == 'PREPARED', 'SUPPORT_REBIND_NOT_PENDING')
            old_raw, new_raw, signed, decision, ledger, _ = row
            old, new = decode(old_raw), decode(new_raw)
            _rebind_config_fit(old, new, self.target, self.backend)
            epoch = db.execute('SELECT COALESCE(MAX(epoch),0) FROM appointments').fetchone()[0]
            require(_ledger_digest(db) == ledger, 'SUPPORT_REBIND_LEDGER_CHANGED')
            appointment = _verify_rebind(old_raw, new_raw, signed, decision, epoch, ledger, self.clock())
            actual, _ = self._config(match_target=False)
            require(actual in (old_raw, new_raw), 'SUPPORT_REBIND_CONFIG_DRIFT')
            # Config replacement + appointment cannot be one FS/SQLite atomic
            # write. PREPARED remains durable across either crash point and all
            # ordinary entries refuse until explicit recovery commits both.
            _private_atomic(self.directory / 'authority-config.json', new_raw)
            db.execute('INSERT INTO appointments VALUES(?,?,?,?)',
                       (appointment['epoch'], signed, decision, sha(signed)))
            db.execute("UPDATE support_rebinds SET state='COMPLETE' WHERE decision_sha=? AND state='PREPARED'", (key,))
            db.execute('COMMIT')
        self._factory()  # independently reopen config/schema/pending state
        return {'rebind': 'COMMITTED_CONFIG_AND_EPOCH_READBACK', 'decision_sha256': key,
                'preserved_ledger_sha256': ledger, 'epoch': appointment['epoch'],
                'candidate': 'NOT_SELECTED', 'acceptance': 'NOT_ACCEPTED'}


def _rebind_config_fit(old, new, target, backend):
    require(type(new) is dict and set(new) == CONFIG_FIELDS
            and new['schema'] == old['schema'] == 'cerebro-protected-principal-source/v1',
            'SUPPORT_REBIND_CONFIG_FIELDS')
    require(all(new[k] == old[k] for k in ('human_public_key', 'tenant_ref', 'workspace_ref'))
            and all(new['target'][k] == old['target'][k] for k in ('project', 'environment', 'service'))
            and new['target'] == target and new['backend_sha256'] == backend,
            'SUPPORT_REBIND_SAME_ROOT_SERVICE_ONLY')


def _verify_rebind(old_raw, new_raw, signed, decision_raw, epoch, ledger, now):
    old, new, decision = decode(old_raw), decode(new_raw), decode(decision_raw)
    require(decision == {'schema': 'cerebro-pm-support-rebind/v1',
            'old_config_sha256': sha(old_raw), 'new_config_sha256': sha(new_raw),
            'ledger_sha256': ledger, 'previous_epoch': epoch, 'new_epoch': epoch + 1},
            'SUPPORT_REBIND_EXACT_DECISION')
    appointment = verify_signature(signed, old['human_public_key'])
    require(type(appointment) is dict and set(appointment) == APPOINTMENT_FIELDS
            and appointment['schema'] == 'cerebro-human-principal-appointment/v1'
            and appointment['human_decision_sha256'] == sha(decision_raw)
            and type(appointment['epoch']) is int and type(appointment['previous_epoch']) is int
            and appointment['epoch'] == epoch + 1 and appointment['previous_epoch'] == epoch
            and type(appointment['active']) is bool
            and appointment['scope'] == {k: new[k] for k in ('tenant_ref','workspace_ref','target','backend_sha256')},
            'SUPPORT_REBIND_GENUINE_NEXT_APPOINTMENT_REQUIRED')
    interval(appointment, now)
    # Validate the holder public key using the same existing signature parser;
    # accepting a new signature is not necessary to validate a public key.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(appointment['holder_public_key']))
    require(all(type(appointment[k]) is str and appointment[k] for k in ('holder_ref','holder_generation')),
            'SUPPORT_REBIND_HOLDER_REQUIRED')
    return appointment


def _subject(plan, target, epoch):
    decision = plan['decision']
    return {'candidate_generation': decision['generation_ref'],
            'candidate_chat': decision['candidate_chat'],
            'decision_ref': decision['decision_ref'],
            'decision_sha256': sha(canonical(decision)),
            'source_revision': decision['source_revision'],
            'method_fingerprint': decision['method_fingerprint'],
            'predecessor': decision['predecessor_owner_ref'],
            'expected_current_revision': decision['expected_current_revision'],
            'target': dict(target), 'principal_epoch': epoch}


def _progress(db, routine_ref, subject):
    require(type(routine_ref) is str and 0 < len(routine_ref) <= 160,
            'SUPPORT_ROUTINE_REF_REQUIRED')
    rows = db.execute('SELECT phase,subject,state,receipt FROM support_steps WHERE routine_ref=?',
                      (routine_ref,)).fetchall()
    states = {}
    for phase, rawsubject, state, receipt in rows:
        require(decode(rawsubject) == subject and phase in PHASES, 'SUPPORT_CHECKPOINT_SUBJECT_CHANGED')
        expected_state='COMPLETE' if phase in ('STAGE','TRANSFER') else 'OWNER_RECORDED'
        require(state==expected_state and receipt is not None,'SUPPORT_UNKNOWN_SPENT_NO_RETRY')
        states[phase] = sha(receipt)
    completed = [p for p in PHASES if p in states]
    require(completed == list(PHASES[:len(completed)]), 'SUPPORT_CHECKPOINT_GAP')
    return {'next': PHASES[len(completed)] if len(completed) < len(PHASES) else 'JOURNAL_CLOSED_OWNER_RECORDS_RETAINED',
            'completed_receipt_sha256': states,
            'manual_owner_phases':[p for p in completed if p not in ('STAGE','TRANSFER')],
            'automated_native_or_use_proof':False,'automatic_effect_or_retry': False}
