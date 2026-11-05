"""UNACTIVATED persistence adapter for ONE exact, unchanged 63c24 transfer core.

Not a general SQLite emulator. The supported statements are the fixed store
protocol of that core only. Public-key origin/enrolment is independently owned;
this module cannot enrol a key, create a table, accept an origin or sign anything.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

CORE_SHA256 = "63c24c12306d2c37896cc5add3f9a83866693bbec64948dda1bbf3cd384c6f50"
ORIGIN_DISPOSITION = "cd91c0383ef55c4635fe90c51a0f8c198da65d6d54c83b6faad8fbd93276ab8d"
_COMPOSED = object()


class DurableTransferRefused(RuntimeError):
    pass


def need(condition, reason):
    if not condition:
        raise DurableTransferRefused(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def transaction_mode(connection, expected_status=None):
    need(getattr(connection, "autocommit", None) is False,
         "PG_TRANSFER_AUTOCOMMIT_MUST_BE_FALSE")
    if expected_status is not None:
        reader = getattr(connection, "get_transaction_status", None)
        status = reader() if callable(reader) else getattr(getattr(connection, "info", None), "transaction_status", None)
        # Both supported psycopg DB-API drivers use IDLE=0 and INTRANS=2.
        need(status is not None and status == expected_status,
             "PG_TRANSFER_TRANSACTION_STATE_NOT_PROVEN")


def load_core(path):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    need(sha(raw) == CORE_SHA256, "PG_TRANSFER_EXACT_UNCHANGED_CORE_REQUIRED")
    module = importlib.util.module_from_spec(importlib.util.spec_from_file_location(
        "cerebro_2872_pinned_transfer_core", path))
    # Execute the checked bytes, rather than reopening a potentially replaced file.
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


class _Rows:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class PostgresAcceptedStore:
    def __init__(self, *, witness, enrolment_id, expected_enrolment_sha256):
        from pm_protected_owner_entry import BackendWitness
        need(type(witness) is BackendWitness, "PG_TRANSFER_REAL_BACKEND_WITNESS_REQUIRED")
        need(type(enrolment_id) is str and 0 < len(enrolment_id) <= 256,
             "PG_TRANSFER_ENROLMENT_ID_REQUIRED")
        need(type(expected_enrolment_sha256) is str and len(expected_enrolment_sha256) == 64,
             "PG_TRANSFER_INDEPENDENT_ENROLMENT_PIN_REQUIRED")
        self.witness = witness
        self.enrolment_id = enrolment_id
        self.expected_enrolment_sha256 = expected_enrolment_sha256

    @contextlib.contextmanager
    def connection(self):
        connection = self.witness.connect()
        primary = None
        try:
            transaction_mode(connection)
            self.witness.verify(connection)
            yield connection
        except BaseException as exc:
            primary = exc
            try:
                connection.rollback()
            except BaseException:
                exc.add_note("PG_TRANSFER_STORE_ROLLBACK_UNKNOWN_NO_RETRY")
            raise
        finally:
            try:
                connection.close()
            except BaseException:
                if primary is not None:
                    primary.add_note("PG_TRANSFER_STORE_CLOSE_UNKNOWN_NO_RETRY")
                else:
                    raise DurableTransferRefused("PG_TRANSFER_STORE_CLOSE_UNKNOWN_NO_RETRY")

    @staticmethod
    def query(connection, statement, params):
        with contextlib.closing(connection.cursor()) as cursor:
            cursor.execute(statement, params)
            return cursor.fetchone()

    def enrolment(self):
        with self.connection() as connection:
            row = self.query(connection,
                "SELECT original, active FROM cerebro_pm_2872_enrolments WHERE enrolment_id=%s",
                (self.enrolment_id,))
        need(row is not None and row[1] is True, "PG_TRANSFER_ENROLMENT_ABSENT_OR_REVOKED")
        original = bytes(row[0])
        need(0 < len(original) <= 1048576 and sha(original) == self.expected_enrolment_sha256,
             "PG_TRANSFER_ENROLMENT_ORIGINAL_CHANGED")
        def pairs(items):
            result = dict(items)
            need(len(result) == len(items), "PG_TRANSFER_DUPLICATE_ENROLMENT_FIELD")
            return result
        record = json.loads(original, object_pairs_hook=pairs)
        need(set(record) == {"schema", "config", "operation_id", "nonce", "payload_sha256",
                            "origin_disposition_sha256", "independent_origin_receipt_sha256"},
             "PG_TRANSFER_EXACT_ENROLMENT_REQUIRED")
        need(record["schema"] == "cerebro-p24-2872-independent-enrolment/v1"
             and record["origin_disposition_sha256"] == ORIGIN_DISPOSITION,
             "PG_TRANSFER_ORIGIN_DISPOSITION_REQUIRED")
        for field in ("payload_sha256", "independent_origin_receipt_sha256"):
            need(type(record[field]) is str and len(record[field]) == 64
                 and all(c in "0123456789abcdef" for c in record[field]),
                 "PG_TRANSFER_ORIGIN_OR_ACTION_PIN_REQUIRED")
        return record

    def operation(self, operation_id):
        with self.connection() as connection:
            row = self.query(connection,
                "SELECT token,state,result FROM cerebro_pm_2872_accepted WHERE operation_id=%s AND enrolment_id=%s",
                (operation_id, self.enrolment_id))
        if row is None:
            return None
        return (bytes(row[0]), row[1], None if row[2] is None else bytes(row[2]))

    @contextlib.contextmanager
    def core_protocol(self):
        # A new checked connection for every core transaction group. No worker
        # supplied DSN, credentials, database directory or replacement store.
        with self.connection() as connection:
            protocol = _CoreProtocol(self, connection)
            primary = None
            try:
                yield protocol
            except BaseException as exc:
                primary = exc
                raise
            finally:
                if protocol.in_transaction:
                    try:
                        connection.rollback()
                    except BaseException:
                        if primary is not None:
                            primary.add_note("PG_TRANSFER_STORE_ROLLBACK_UNKNOWN_NO_RETRY")
                        else:
                            raise DurableTransferRefused("PG_TRANSFER_STORE_ROLLBACK_UNKNOWN_NO_RETRY")


class _CoreProtocol:
    def __init__(self, store, connection):
        self.store = store
        self.connection = connection
        self.in_transaction = False
        self.expected = None

    def execute(self, statement, params=()):
        if statement == "BEGIN IMMEDIATE":
            need(not self.in_transaction, "PG_TRANSFER_NESTED_TRANSACTION")
            transaction_mode(self.connection)
            # The enrolment trigger uses the same lock for any revocation.
            self.store.query(self.connection, "SELECT pg_advisory_xact_lock(hashtext(%s))",
                             ("cerebro_pm_2872_enrolment:" + self.store.enrolment_id,))
            transaction_mode(self.connection, 2)
            self.in_transaction = True
            return _Rows(None)
        if statement == "ROLLBACK":
            self.connection.rollback()
            self.in_transaction = False
            self.expected = None
            return _Rows(None)
        if statement == "COMMIT":
            need(self.in_transaction and self.expected is not None, "PG_TRANSFER_COMMIT_WITHOUT_WRITE")
            operation_id, expected = self.expected
            # Unknown commit/readback leaves the operation unresolved; the core
            # never proceeds to backend effect without this independent read.
            try:
                transaction_mode(self.connection, 2)
                self.connection.commit()
                transaction_mode(self.connection, 0)
            except BaseException as exc:
                exc.add_note("PG_TRANSFER_COMMIT_UNKNOWN_NO_RETRY")
                raise
            self.in_transaction = False
            actual = self.store.operation(operation_id)
            need(actual == expected, "PG_TRANSFER_COMMIT_READBACK_UNKNOWN_NO_RETRY")
            self.expected = None
            return _Rows(None)
        need(self.in_transaction, "PG_TRANSFER_TRANSACTION_REQUIRED")
        if statement == "INSERT INTO accepted VALUES(?,?,?,'ACCEPTED',NULL)":
            operation_id, nonce, token = params
            try:
                self.store.query(self.connection,
                    "INSERT INTO cerebro_pm_2872_accepted(operation_id,nonce,enrolment_id,token,state,result) VALUES(%s,%s,%s,%s,'ACCEPTED',NULL) RETURNING operation_id",
                    (operation_id, nonce, self.store.enrolment_id, token))
            except Exception as exc:
                if getattr(exc, "sqlstate", None) == "23505" or getattr(exc, "pgcode", None) == "23505":
                    raise sqlite3.IntegrityError("PG_TRANSFER_OPERATION_OR_NONCE_ALREADY_EXISTS") from exc
                raise
            self.expected = operation_id, (token, "ACCEPTED", None)
            return _Rows(None)
        if statement == "SELECT token,state FROM accepted WHERE operation_id=?":
            row = self.store.query(self.connection,
                "SELECT token,state FROM cerebro_pm_2872_accepted WHERE operation_id=%s AND enrolment_id=%s FOR UPDATE",
                (params[0], self.store.enrolment_id))
            return _Rows(None if row is None else (bytes(row[0]), row[1]))
        if statement in ("UPDATE accepted SET state='STARTED_UNKNOWN_NO_RETRY' WHERE operation_id=?",
                         "UPDATE accepted SET state='COMPLETED',result=? WHERE operation_id=?"):
            starting = "STARTED_UNKNOWN" in statement
            operation_id = params[0] if starting else params[1]
            state = "STARTED_UNKNOWN_NO_RETRY" if starting else "COMPLETED"
            previous = "ACCEPTED" if starting else "STARTED_UNKNOWN_NO_RETRY"
            result = None if starting else params[0]
            row = self.store.query(self.connection,
                "UPDATE cerebro_pm_2872_accepted SET state=%s,result=%s WHERE operation_id=%s AND enrolment_id=%s AND state=%s RETURNING token,state,result",
                (state, result, operation_id, self.store.enrolment_id, previous))
            need(row is not None, "PG_TRANSFER_NOT_ACCEPTED_OR_SPENT")
            self.expected = operation_id, (bytes(row[0]), row[1], None if row[2] is None else bytes(row[2]))
            return _Rows(None)
        raise DurableTransferRefused("PG_TRANSFER_UNSUPPORTED_CORE_STORE_STATEMENT")


def compose_postgres_transfer(*, core_path, store, clock, isolated_test=False):
    """Protected composition; owner must adopt this adapter separately from the
    signature's unchanged core pin. No automatic enrolment or installation.
    """
    need(type(store) is PostgresAcceptedStore, "PG_TRANSFER_EXACT_STORE_REQUIRED")
    core = load_core(core_path)
    record = store.enrolment()
    config = record["config"]
    need(set(config) == {"schema", "issuer", "public_key", "target", "backend_sha256"}
         and config["schema"] == "cerebro-p24-bounded-transfer-custody/v1" and config["issuer"] == "P24"
         and config["backend_sha256"] == store.witness.expected, "PG_TRANSFER_ENROLMENT_CONFIG_INVALID")
    core.Ed25519PublicKey.from_public_bytes(bytes.fromhex(config["public_key"]))

    class Port(core.P24BoundedTransferPort):
        def _db(self):
            return store.core_protocol()

        def _signed(self, raw):
            current = store.enrolment()
            need(current == record, "PG_TRANSFER_ENROLMENT_CHANGED")
            payload = super()._signed(raw)
            need(payload["operation_id"] == record["operation_id"] and payload["nonce"] == record["nonce"]
                 and sha(core.canonical(payload)) == record["payload_sha256"],
                 "PG_TRANSFER_ENROLMENT_ACTION_MISMATCH")
            return payload

    port = object.__new__(Port)
    port.config = core.copy.deepcopy(config)
    port.clock = clock
    port.test_only = isolated_test is True
    port._pg_composition = _COMPOSED
    port._pg_store = store
    return port


def run_postgres_transfer(port, token, *, witness, **material):
    """Actual protected entry owns witness closure; no production test flag."""
    from pm_protected_owner_entry import BackendWitness
    need(type(witness) is BackendWitness, "PG_TRANSFER_REAL_BACKEND_WITNESS_REQUIRED")
    try:
        need(getattr(port, "_pg_composition", None) is _COMPOSED
             and port._pg_store.witness is witness, "PG_TRANSFER_PROTECTED_COMPOSITION_REQUIRED")
        need(port.test_only is False, "PG_TRANSFER_FIXTURE_NOT_PRODUCTION")
        result = port.execute(token, witness=witness, **material)
    finally:
        primary = sys.exception()
        try:
            witness.close()
        except BaseException:
            if primary is not None:
                primary.add_note("PG_TRANSFER_OWN_BACKEND_CLOSURE_UNKNOWN_NO_RETRY")
            else:
                raise DurableTransferRefused("PG_TRANSFER_OWN_BACKEND_CLOSURE_UNKNOWN_NO_RETRY")
    return {**result, "own_backend_closure_verified": True}
