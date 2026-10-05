"""Minimal PostgreSQL driver over the system libpq (stdlib ``ctypes``; no third-party dependency).

WHY THIS EXISTS: the Source convention is ``psycopg[binary]>=3.2,<4`` with a connection *factory* returning a
DBAPI-like connection (see tooling/context/control_context_state_postgres.py). In the isolated environment that built
this candidate the Python package index was not reachable (egress policy), so psycopg could not be installed. To still
execute against a REAL PostgreSQL server, the adapter is written against the tiny DBAPI subset below and this module
supplies one implementation of it. A psycopg connection factory (autocommit, ``row_factory=dict_row``) satisfies the
same subset, but THAT PATH IS NOT EXECUTED by this candidate (UNRUN).

The subset the adapter uses: ``connect_params -> Connection``; ``Connection.cursor()``; ``Cursor.execute(sql, params)``
with ``%s`` placeholders; ``Cursor.fetchone()/fetchall()`` returning dict rows with TEXT values (or None);
``Cursor.rowcount``; ``Connection.close()``. Connections are autocommit; the adapter issues explicit BEGIN/COMMIT.
A connection must not be shared between threads.

HONEST LIMITS: Linux/glibc ``libpq.so.5`` only (loaded by name); no TLS options, no pooling, no async, no COPY, no
binary protocol. Values are passed and returned as text.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from typing import Any, Mapping, Sequence

_ALLOWED = ("host", "port", "dbname", "user", "password", "application_name", "connect_timeout", "options",
            "client_encoding", "sslmode")

CONNECTION_OK = 0
PGRES_EMPTY_QUERY, PGRES_COMMAND_OK, PGRES_TUPLES_OK = 0, 1, 2
PQTRANS_IDLE = 0
_PG_DIAG_SQLSTATE, _PG_DIAG_MESSAGE_PRIMARY = ord("C"), ord("M")


class PgError(Exception):
    """A server- or connection-level error. ``sqlstate`` is the 5-character code (or '' for client-side errors).
    The message never contains connection parameters or passwords."""

    def __init__(self, message: str, sqlstate: str = "") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


_lib: "ctypes.CDLL | None" = None
_NOTICE_RECEIVER = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(lambda _arg, _res: None)  # drop NOTICEs


def _load() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    name = ctypes.util.find_library("pq") or "libpq.so.5"
    try:
        lib = ctypes.CDLL(name)
    except OSError as exc:
        raise PgError(f"libpq-not-loadable:{type(exc).__name__}") from None
    vp, cp, ci = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
    sigs = {
        "PQconnectdb": (vp, [cp]), "PQstatus": (ci, [vp]), "PQerrorMessage": (cp, [vp]), "PQfinish": (None, [vp]),
        "PQexec": (vp, [vp, cp]),
        "PQexecParams": (vp, [vp, cp, ci, ctypes.c_void_p, ctypes.POINTER(cp), ctypes.c_void_p, ctypes.c_void_p, ci]),
        "PQresultStatus": (ci, [vp]), "PQresultErrorField": (cp, [vp, ci]), "PQntuples": (ci, [vp]),
        "PQnfields": (ci, [vp]), "PQfname": (cp, [vp, ci]), "PQgetvalue": (cp, [vp, ci, ci]),
        "PQgetisnull": (ci, [vp, ci, ci]), "PQcmdTuples": (cp, [vp]), "PQclear": (None, [vp]),
        "PQtransactionStatus": (ci, [vp]), "PQbackendPID": (ci, [vp]),
        "PQsetNoticeReceiver": (vp, [vp, vp, vp]),
    }
    for fn, (res, args) in sigs.items():
        f = getattr(lib, fn)
        f.restype, f.argtypes = res, args
    _lib = lib
    return lib


def _quote(value: Any) -> str:
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


_REQUIRED = ("host", "port", "dbname", "user", "password")


def conninfo(params: Mapping[str, Any]) -> str:
    """Every connection is fully explicit: libpq is never allowed to fill host/port/dbname/user/password from the
    ambient environment, a service file or a password file (no discovery of any existing endpoint or credential)."""
    unknown = set(params) - set(_ALLOWED)
    if unknown:
        raise PgError(f"connection-parameter-not-allowed:{sorted(unknown)[0]}")
    for key, value in params.items():
        if isinstance(value, str) and "\0" in value:  # a C string would be silently truncated (dropping later keys)
            raise PgError(f"connection-parameter-contains-nul:{key}")
    missing = [k for k in _REQUIRED if params.get(k) in (None, "")]
    if missing:
        raise PgError(f"connection-parameter-required:{missing[0]}")
    return " ".join(f"{k}={_quote(v)}" for k, v in params.items() if v is not None)


def _translate(sql: str) -> str:
    """``%s`` -> ``$n``; ``%%`` -> ``%``. Any other ``%`` is a programming error."""
    out: list[str] = []
    n = 0
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "%":
            nxt = sql[i + 1] if i + 1 < len(sql) else ""
            if nxt == "s":
                n += 1
                out.append(f"${n}")
                i += 2
                continue
            if nxt == "%":
                out.append("%")
                i += 2
                continue
            raise PgError("sql-placeholder-invalid")
        out.append(ch)
        i += 1
    return "".join(out)


def _param_text(value: Any) -> "bytes | None":
    if value is None:
        return None
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, str) and "\0" in value:
        raise PgError("sql-parameter-contains-nul")
    if isinstance(value, (int, str)):
        return str(value).encode("utf-8")
    raise PgError(f"sql-parameter-type:{type(value).__name__}")


class Cursor:
    def __init__(self, conn: "Connection") -> None:
        self._conn = conn
        self._rows: list[dict] = []
        self._pos = 0
        self.rowcount = -1

    def execute(self, sql: str, params: Sequence[Any] = ()) -> "Cursor":
        lib, handle = _load(), self._conn._handle()
        text = _translate(sql).encode("utf-8")
        if params:
            arr = (ctypes.c_char_p * len(params))(*[_param_text(p) for p in params])
            res = lib.PQexecParams(handle, text, len(params), None, arr, None, None, 0)
        else:
            res = lib.PQexec(handle, text)
        if not res:
            raise PgError("libpq-no-result:" + _clean(lib.PQerrorMessage(handle)))
        try:
            status = lib.PQresultStatus(res)
            if status not in (PGRES_EMPTY_QUERY, PGRES_COMMAND_OK, PGRES_TUPLES_OK):
                state = (lib.PQresultErrorField(res, _PG_DIAG_SQLSTATE) or b"").decode("ascii", "replace")
                msg = (lib.PQresultErrorField(res, _PG_DIAG_MESSAGE_PRIMARY) or b"").decode("utf-8", "replace")
                raise PgError(msg or "pg-error", state)
            rows: list[dict] = []
            if status == PGRES_TUPLES_OK:
                nf = lib.PQnfields(res)
                names = [lib.PQfname(res, c).decode("utf-8") for c in range(nf)]
                for r in range(lib.PQntuples(res)):
                    rows.append({names[c]: (None if lib.PQgetisnull(res, r, c)
                                            else lib.PQgetvalue(res, r, c).decode("utf-8")) for c in range(nf)})
            tuples = lib.PQcmdTuples(res)
            self.rowcount = int(tuples) if tuples else (len(rows) if status == PGRES_TUPLES_OK else 0)
            self._rows, self._pos = rows, 0
        finally:
            lib.PQclear(res)
        return self

    def fetchone(self) -> "dict | None":
        if self._pos >= len(self._rows):
            return None
        self._pos += 1
        return self._rows[self._pos - 1]

    def fetchall(self) -> list:
        rest = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rest


def _clean(raw: "bytes | None") -> str:
    return (raw or b"").decode("utf-8", "replace").strip().splitlines()[0] if raw else ""


class Connection:
    def __init__(self, raw: int) -> None:
        self._raw: "int | None" = raw

    def _handle(self) -> int:
        if self._raw is None:
            raise PgError("connection-closed")
        return self._raw

    def cursor(self) -> Cursor:
        return Cursor(self)

    @property
    def backend_pid(self) -> int:
        return int(_load().PQbackendPID(self._handle()))

    @property
    def in_transaction(self) -> bool:
        return _load().PQtransactionStatus(self._handle()) != PQTRANS_IDLE

    def close(self) -> None:
        if self._raw is not None:
            _load().PQfinish(self._raw)
            self._raw = None

    def __repr__(self) -> str:  # never shows connection parameters
        return f"<libpq Connection {'closed' if self._raw is None else 'open'}>"


def connect(params: Mapping[str, Any]) -> Connection:
    lib = _load()
    raw = lib.PQconnectdb(conninfo(params).encode("utf-8"))
    if not raw:
        raise PgError("libpq-connect-failed")
    if lib.PQstatus(raw) != CONNECTION_OK:
        message = _clean(lib.PQerrorMessage(raw))
        lib.PQfinish(raw)
        raise PgError("connect-failed:" + message[:160])
    lib.PQsetNoticeReceiver(raw, ctypes.cast(_NOTICE_RECEIVER, ctypes.c_void_p), None)
    return Connection(raw)


def make_connection_factory(params: Mapping[str, Any]):
    """Factory with the same shape as the Source ``make_psycopg_connection_factory``: a zero-argument callable."""
    frozen = dict(params)
    conninfo(frozen)  # validate keys early

    def factory() -> Connection:
        return connect(frozen)

    factory.__qualname__ = "libpq_connection_factory"
    return factory
