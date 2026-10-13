"""Source fixed ASSISTANT journal and host-mediated proposals. No network or authority.

The authenticated host must qualify genuine caller/owner/grant boundaries.
Expected JSON and provider JSON passed here are caller inputs, not attestations.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import copy
import types
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = ROOT.parents[1]
REGISTRY_SHA = "a4717aa4de5c6230a1ee08cfc0e90d4d03bb33fbdd3dad9dc7f89aa2e9e9a7fd"
MAX_REPLAY_EVENTS = 4096
ATTEMPT = re.compile(r"CEREBRO-BOOT-[A-Z0-9-]{8,96}\Z")
DEST = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
SECRET_KEYS = {"authorization", "access_token", "refresh_token", "secret", "bearer", "control_resolution_attestation"}
EVIDENCE_CLASSES = {"SYNTHETIC_MODEL", "HOST_PROVIDER_READ_NOT_VERIFIED_BY_CLIENT"}


class Hold(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Hold("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def decode(text):
    if len(text.encode("utf-8")) > 2_000_000:
        raise Hold("INPUT_TOO_LARGE")
    return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(Hold("NONFINITE_JSON")))


def no_secrets(value):
    if isinstance(value, dict):
        if SECRET_KEYS.intersection(k.lower() for k in value):
            raise Hold("SECRET_OR_SEAL_INPUT_PROHIBITED")
        for child in value.values():
            no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            no_secrets(child)


def text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 512 or "\0" in value:
        raise Hold(label + "_INVALID")
    return value


def validate_request(request):
    no_secrets(request)
    fields = {'schema','boot_attempt_id','source_revision','method_fingerprint',
              'destination_ref','owner_expectation','selected_role','role_method_projection'}
    if type(request) is not dict or set(request) != fields or request['schema'] != 'cerebro-role-dressing-request/v1':
        raise Hold('REQUEST_SCHEMA_INVALID')
    if request['selected_role'] != 'ASSISTANT':
        raise Hold('FIXED_ASSISTANT_ROLE_REQUIRED')
    if type(request['boot_attempt_id']) is not str or ATTEMPT.fullmatch(request['boot_attempt_id']) is None:
        raise Hold('ATTEMPT_INVALID')
    for key, pattern in (('source_revision',r'[0-9a-f]{40}'),('method_fingerprint',r'[0-9a-f]{64}')):
        if type(request[key]) is not str or re.fullmatch(pattern,request[key]) is None:
            raise Hold('REQUEST_SOURCE_METHOD_INVALID')
    if type(request['destination_ref']) is not str or DEST.fullmatch(request['destination_ref']) is None:
        raise Hold('DESTINATION_INVALID')
    projection = request['role_method_projection']
    registry().validate_role_method_projection(projection, expected_role='ASSISTANT',
        expected_source_revision=request['source_revision'], expected_method_fingerprint=request['method_fingerprint'])
    expected = request['owner_expectation']
    fields = {'origin','ref','revision','tenant_ref','workspace_ref','principal_ref',
              'assigned_consumers','session_ref','target'}
    if type(expected) is not dict or set(expected) != fields or expected['origin'] not in {'SYNTHETIC_MODEL','OWNER_DOCUMENT_REF'}:
        raise Hold('INDEPENDENT_EXPECTATION_REQUIRED')
    for key in fields - {'assigned_consumers','target'}:
        text_value = expected[key]
        text(text_value,'OWNER_'+key.upper())
    consumers = expected['assigned_consumers']
    if type(consumers) is not list or not consumers or not all(type(v) is str for v in consumers) or len(consumers) != len(set(consumers)):
        raise Hold('ASSIGNED_CONSUMERS_REQUIRED')
    for consumer in consumers:text(consumer,'CONSUMER')
    expected_target = expected['target']
    if type(expected_target) is not dict or set(expected_target) != {'overlay_ref','actor_ref','actor_generation_ref','recipient_ref'} or expected_target['recipient_ref'] != 'A1':
        raise Hold('ONE_ORIENTATION_TARGET_REQUIRED')
    for key,value in expected_target.items():text(value,'TARGET_'+key.upper())


def registry():
    path = SOURCE_ROOT / 'tooling/context/control_context_registry.py'
    raw = path.read_bytes()
    if hashlib.sha256(raw.replace(b'\r\n',b'\n')).hexdigest() != REGISTRY_SHA:
        raise Hold('PINNED_REGISTRY_DRIFT')
    # Execute the same captured Source bytes; no hash then reopen.
    module = types.ModuleType('role_client_captured_registry')
    module.__file__ = str(path)
    exec(compile(raw,str(path),'exec'),module.__dict__)
    return module


def qualify_read(request,state,binding):
    no_secrets({'state':state,'binding':binding})
    registry().validate_pre_role_generation(state)
    expected = request['owner_expectation']
    if type(binding) is not dict or any(binding.get(k) != expected[k] for k in ('tenant_ref','workspace_ref','principal_ref','session_ref')) or binding.get('consumer_ref') not in expected['assigned_consumers']:
        raise Hold('OWNER_ASSIGNMENT_MISMATCH')
    if state['tenant_ref'] != expected['tenant_ref'] or state['workspace_ref'] != expected['workspace_ref']:
        raise Hold('STATE_SCOPE_MISMATCH')
    if state['generation_ref'] != request['boot_attempt_id'] or state['source_revision'] != request['source_revision'] or state['civilization_method_attestation']['method_fingerprint'] != request['method_fingerprint']:
        raise Hold('ORIGINAL_GENERATION_SOURCE_METHOD_MISMATCH')
    if state['lifecycle'] == 'ROLE_ATTACHED':
        overlay = state['role_overlay']
        if overlay['role'] != 'ASSISTANT' or any(overlay[k] != expected['target'][k] for k in ('overlay_ref','actor_ref','actor_generation_ref')) or overlay['method_projection'] != request['role_method_projection']:
            raise Hold('FROZEN_ROLE_TARGET_PROJECTION_MISMATCH')


def compare_progress(previous, current):
    if previous is not None and current["revision"] < previous["revision"]:
        raise Hold("STALE_STATE_READ")
    if previous is not None and current["revision"] == previous["revision"] and current != previous:
        raise Hold("SAME_REVISION_CONFLICT")


def safe_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            st = part.lstat()
            if part.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                raise Hold("REPARSE_OR_SYMLINK_JOURNAL")
    if not path.parent.is_dir():
        raise Hold("JOURNAL_PARENT_MISSING")
    return path


class Journal:
    def __init__(self, path, *, source_pins):
        # Public expected Source pins are data, never provider/currentness proof.
        if type(source_pins) is not dict or set(source_pins) != {'source_revision','method_fingerprint','role_method_projection'}:
            raise Hold('EXACT_PUBLIC_SOURCE_PINS_REQUIRED')
        no_secrets(source_pins)
        self._source_pins = copy.deepcopy(source_pins)
        self.path = safe_path(path)

    def _match_source_pins(self, request):
        if any(request[k] != self._source_pins[k] for k in self._source_pins):
            raise Hold('IMMUTABLE_EXPECTED_SOURCE_PINS_MISMATCH')

    @contextmanager
    def transaction(self, initialize=False):
        enrollment = safe_path(str(self.path) + ".enrolled")
        first = False
        if not self.path.exists() and initialize:
            if enrollment.exists():
                raise Hold("ENROLLED_JOURNAL_MISSING_NO_RECREATE")
            marker = canonical({"schema": "b1-boot-journal-enrollment/v1", "path": str(self.path)}) + "\n"
            with enrollment.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(marker)
                stream.flush()
                os.fsync(stream.fileno())
            first = True
        if not self.path.exists() and not initialize:
            raise Hold("MISSING_JOURNAL_NO_NEW_ATTEMPT")
        if not first:
            if not enrollment.exists() or decode(enrollment.read_text(encoding="utf-8")) != {"schema": "b1-boot-journal-enrollment/v1", "path": str(self.path)}:
                raise Hold("JOURNAL_ENROLLMENT_UNQUALIFIED")
        conn = sqlite3.connect(self.path.as_uri() + ("?mode=rwc" if initialize else "?mode=rw"), uri=True, timeout=10)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if initialize and version == 0:
                if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                    raise Hold("FOREIGN_JOURNAL_SCHEMA")
                conn.execute("CREATE TABLE attempts (id TEXT PRIMARY KEY, request TEXT NOT NULL, fingerprint TEXT NOT NULL, phase TEXT NOT NULL, state TEXT, method_decision TEXT)")
                conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt TEXT NOT NULL, body TEXT NOT NULL, sha TEXT NOT NULL)")
                conn.execute("CREATE TRIGGER immutable_original BEFORE UPDATE ON attempts WHEN NEW.request != OLD.request OR NEW.fingerprint != OLD.fingerprint OR NEW.id != OLD.id BEGIN SELECT RAISE(ABORT,'immutable-original'); END")
                conn.execute("CREATE TRIGGER preserve_events BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only-events'); END")
                conn.execute("CREATE TRIGGER retain_events BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'retain-events'); END")
                conn.execute("PRAGMA user_version=1")
                version = 1
            if version != 1 or conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise Hold("JOURNAL_CORRUPT_OR_UNKNOWN_SCHEMA")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def row(self, conn, attempt, expected_fingerprint):
        row = conn.execute("SELECT request,fingerprint,phase,state,method_decision FROM attempts WHERE id=?", (attempt,)).fetchone()
        if row is None:
            raise Hold("ATTEMPT_NOT_IN_JOURNAL")
        request = decode(row[0])
        validate_request(request)
        self._match_source_pins(request)
        if request["boot_attempt_id"] != attempt or digest(request) != row[1] or row[1] != expected_fingerprint:
            raise Hold("FROZEN_ORIGINAL_FINGERPRINT_CONFLICT")
        derived_phase, derived_state, derived_decision = "PREPARED", None, None
        events = list(conn.execute("SELECT body,sha FROM events WHERE attempt=? ORDER BY seq LIMIT 4097", (attempt,)))
        if len(events) > MAX_REPLAY_EVENTS:
            raise Hold("REPLAY_BOUND_EXCEEDED_UNKNOWN_COVERAGE")
        if not events:
            raise Hold("ORIGINAL_EVENT_MISSING")
        for body, sha in events:
            event = decode(body)
            if digest(event) != sha:
                raise Hold("JOURNAL_EVENT_CORRUPT")
            if event["kind"] == "ORIGINAL_PERSISTED":
                if event["fingerprint"] != expected_fingerprint:
                    raise Hold("ORIGINAL_EVENT_CONFLICT")
            elif event["kind"] == "PROPOSED_EXISTING_TOOL":
                if event["tool"] == "create_ready_unbound_boot_generation":
                    if derived_phase != "PREPARED":
                        raise Hold("REPEATED_CREATE_HISTORY")
                    derived_phase = "CREATE_REQUESTED"
                elif event["tool"] == "resume_ready_unbound_assistant_overlay":
                    derived_phase = "ATTACH_REQUESTED"
                elif event["tool"] != "read_boot_generation_state":
                    raise Hold("UNEXPECTED_TOOL_HISTORY")
            elif event["kind"] == "EXACT_READ_OBSERVED":
                incoming = event["state"]
                if event.get("evidence_class") not in EVIDENCE_CLASSES:
                    raise Hold("READ_EVIDENCE_CLASS_REQUIRED")
                qualify_read(request, incoming, event["binding"])
                compare_progress(derived_state, incoming)
                derived_state = incoming
                derived_phase = {"BIRTH_PENDING": "HOLD_BIRTH_PENDING", "READY_UNBOUND": "OBSERVED_READY", "ROLE_ATTACHED": "OBSERVED_ATTACHED"}[derived_state["lifecycle"]]
            elif event["kind"] == "UNKNOWN_OUTCOME":
                derived_phase = "HOLD_UNKNOWN_OUTCOME"
            elif event["kind"] == "METHOD_CONSUMER_DECISION":
                derived_decision = event["decision"]
            else:
                raise Hold("UNEXPECTED_EVENT_HISTORY")
        state = decode(row[3]) if row[3] is not None else None
        if row[2] != derived_phase or state != derived_state or row[4] != derived_decision:
            raise Hold("JOURNAL_STATE_HISTORY_CONFLICT")
        return request, row[2], state, row[4]

    @staticmethod
    def event(conn, attempt, value):
        conn.execute("INSERT INTO events(attempt,body,sha) VALUES(?,?,?)", (attempt, canonical(value), digest(value)))

    def prepare(self, request):
        validate_request(request)
        self._match_source_pins(request)
        attempt = request["boot_attempt_id"]
        fingerprint = digest(request)
        with self.transaction(initialize=True) as conn:
            existing = conn.execute("SELECT fingerprint FROM attempts WHERE id=?", (attempt,)).fetchone()
            if existing is not None:
                self.row(conn, attempt, fingerprint)
                result = "EXACT_ORIGINAL_REUSE"
            else:
                conn.execute("INSERT INTO attempts VALUES(?,?,?,'PREPARED',NULL,NULL)", (attempt, canonical(request), fingerprint))
                self.event(conn, attempt, {"kind": "ORIGINAL_PERSISTED", "fingerprint": fingerprint})
                result = "PREPARED_DURABLE_LOCAL_REQUEST"
        return {"result": result, "attempt": attempt, "request_fingerprint": fingerprint, "authority": "NONE", "native_effect": False}

    def next_call(self, attempt, fingerprint, *, attach=False):
        with self.transaction() as conn:
            request, phase, state, _ = self.row(conn, attempt, fingerprint)
            tool = "read_boot_generation_state"
            args = {"generation_ref": attempt}
            if phase == "PREPARED" and not attach:
                tool = "create_ready_unbound_boot_generation"
                args = {"boot_attempt_id": attempt, "source_revision": request["source_revision"]}
                conn.execute("UPDATE attempts SET phase='CREATE_REQUESTED' WHERE id=?", (attempt,))
            elif phase == "OBSERVED_READY" and attach:
                if state is None or state["lifecycle"] != "READY_UNBOUND":
                    raise Hold("ATTACH_REQUIRES_EXACT_READY_READ")
                if self.row(conn,attempt,fingerprint)[3] not in {'APPLIED','NO_CHANGE'}:
                    raise Hold('PRE_ATTACH_METHOD_PROJECTION_NOT_CONSUMED')
                previous_attach = any(decode(body).get("tool") == "resume_ready_unbound_assistant_overlay" for (body,) in conn.execute("SELECT body FROM events WHERE attempt=?", (attempt,)))
                if not previous_attach:
                    tool = "resume_ready_unbound_assistant_overlay"
                    args = {"expected_revision": state["revision"], "generation_ref": attempt, "source_revision": request["source_revision"]}
                    conn.execute("UPDATE attempts SET phase='ATTACH_REQUESTED' WHERE id=?", (attempt,))
            elif phase == "PREPARED" and attach:
                raise Hold("ATTACH_BEFORE_BIRTH_PROHIBITED")
            self.event(conn, attempt, {"kind": "PROPOSED_EXISTING_TOOL", "tool": tool, "args": args})
        return {"tool": tool, "args": args, "request_fingerprint": fingerprint, "authority": "NONE", "native_effect": False, "requires_authenticated_host_gate": True}

    def ingest_read(self, attempt, fingerprint, payload, *, evidence_class):
        if evidence_class not in EVIDENCE_CLASSES:
            raise Hold("READ_EVIDENCE_CLASS_REQUIRED")
        no_secrets(payload)
        if isinstance(payload, dict) and payload.get('isError') is True:
            raise Hold('HANDLER_REFUSAL_NOT_STATE_OBSERVATION')
        content = payload.get("structuredContent", payload) if isinstance(payload, dict) else None
        if not isinstance(content, dict) or "authenticated_binding" not in content or "pre_role_generation" not in content:
            raise Hold("EXACT_READ_BINDING_AND_STATE_REQUIRED")
        state = content["pre_role_generation"]
        registry().validate_pre_role_generation(state)
        binding = content["authenticated_binding"]
        with self.transaction() as conn:
            request, phase, old_state, _ = self.row(conn, attempt, fingerprint)
            qualify_read(request, state, binding)
            compare_progress(old_state, state)
            phase = {"BIRTH_PENDING": "HOLD_BIRTH_PENDING", "READY_UNBOUND": "OBSERVED_READY", "ROLE_ATTACHED": "OBSERVED_ATTACHED"}[state["lifecycle"]]
            conn.execute("UPDATE attempts SET phase=?,state=? WHERE id=?", (phase, canonical(state), attempt))
            self.event(conn, attempt, {"kind": "EXACT_READ_OBSERVED", "state": state, "binding": {k: binding.get(k) for k in ("tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref")}, "evidence_class": evidence_class})
        return {"result": phase, "generation_ref": attempt, "revision": state["revision"], "evidence_class": evidence_class, "native_authentication": "OUTSIDE_CLIENT", "destination_grant_qualification": "OUTSIDE_CLIENT", "job_authority": "NOT_ASSIGNED"}

    def mark_unknown(self, attempt, fingerprint, reason):
        text(reason, "OUTCOME_REASON")
        with self.transaction() as conn:
            self.row(conn, attempt, fingerprint)
            conn.execute("UPDATE attempts SET phase='HOLD_UNKNOWN_OUTCOME' WHERE id=?", (attempt,))
            self.event(conn, attempt, {"kind": "UNKNOWN_OUTCOME", "reason": reason})
        return {"result": "HOLD_UNKNOWN_OUTCOME", "resume": "exact-read of same attempt only", "native_effect": False}

    def consume_method(self, attempt, fingerprint, method_fingerprint, decision):
        if decision not in {"APPLIED","NO_CHANGE","NOT_APPLICABLE"}:raise Hold("METHOD_DECISION_INVALID")
        with self.transaction() as conn:
            request, _, _, _ = self.row(conn, attempt, fingerprint)
            if method_fingerprint != request["method_fingerprint"]:
                raise Hold("METHOD_FINGERPRINT_MISMATCH")
            conn.execute("UPDATE attempts SET method_decision=? WHERE id=?", (decision, attempt))
            self.event(conn, attempt, {"kind": "METHOD_CONSUMER_DECISION", "decision": decision, "fingerprint": method_fingerprint, "assertion": "CALLER_RECORDED_NOT_PROVIDER_ATTESTED"})
        return {"result": "CONSUMER_DECISION_RECORDED", "authority": "NONE"}

    def status(self,attempt,fingerprint):
        with self.transaction() as conn:
            request,phase,state,decision = self.row(conn,attempt,fingerprint)
            count = conn.execute('SELECT COUNT(*) FROM events WHERE attempt=?',(attempt,)).fetchone()[0]
        return {'phase':phase,'attempt':attempt,'selected_role':'ASSISTANT',
                'revision':state['revision'] if state else None,
                'method_decision':decision,'journal_events':count,'authority':'NONE'}


def envelope(value):
    status = value.get('result') or value.get('phase') or ('PROPOSAL' if 'tool' in value else 'OBSERVATION')
    return {'schema':'cerebro-role-dressing-client-result/v1','status':status,
            'details':value,'authority':'NONE','native_effect':False}


def _wrapped(method):
    def call(self,*args,**kwargs):return envelope(method(self,*args,**kwargs))
    return call

for _method in ('prepare','next_call','ingest_read','mark_unknown','consume_method','status'):
    setattr(Journal,_method,_wrapped(getattr(Journal,_method)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-pins', required=True,
                        help='Frozen expected public Source pins, not provider/owner authority')
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--request", required=True)
    for name in ("next", "ingest", "unknown", "consume", "status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--attempt", required=True)
        cmd.add_argument("--fingerprint", required=True, help="Separately frozen original request fingerprint")
        if name == "next":
            cmd.add_argument("--attach", action="store_true")
        if name == "ingest":
            cmd.add_argument("--result", required=True)
            cmd.add_argument("--evidence-class", required=True)
        if name == "unknown":
            cmd.add_argument("--reason", required=True)
        if name == "consume":
            cmd.add_argument("--method", required=True)
            cmd.add_argument("--decision", required=True)
    args = parser.parse_args()
    try:
        journal = Journal(args.db, source_pins=decode(Path(args.source_pins).read_text(encoding='utf-8')))
        if args.command == "prepare":
            result = journal.prepare(decode(Path(args.request).read_text(encoding="utf-8")))
        elif args.command == "next":
            result = journal.next_call(args.attempt, args.fingerprint, attach=args.attach)
        elif args.command == "ingest":
            result = journal.ingest_read(args.attempt, args.fingerprint, decode(Path(args.result).read_text(encoding="utf-8")), evidence_class=args.evidence_class)
        elif args.command == "unknown":
            result = journal.mark_unknown(args.attempt, args.fingerprint, args.reason)
        elif args.command == "consume":
            result = journal.consume_method(args.attempt, args.fingerprint, args.method, args.decision)
        else:
            result = journal.status(args.attempt, args.fingerprint)
        print(canonical(result))
        return 0
    except (Hold, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        print(canonical({'schema':'cerebro-role-dressing-client-result/v1','status':'REFUSED',
                         'details':{'reason':str(exc)},'authority':'NONE','native_effect':False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
