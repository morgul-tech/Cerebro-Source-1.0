"""Disposable-PostgreSQL harness for the V0.2 candidate tests (test tooling; not part of the installed wheel).

Rules this module enforces:
* Nothing runs unless the caller passes an explicit disposable-test confirmation (see run_pg_tests.py).
* It NEVER discovers an endpoint or credential: every ``PG*`` / ``DATABASE_URL`` / ``*_DSN`` variable is scrubbed from
  the process environment before anything starts, and every connection is fully explicit (host/port/dbname/user/
  password), so libpq has nothing ambient to fill in.
* The default endpoint is a throw-away cluster that this module initialises in its own mkdtemp directory with a
  generated password, bound to a Unix socket in that directory only (``listen_addresses=''``): no TCP listener.
* An externally supplied endpoint (``CEE_V02_TEST_CONN`` JSON, set by the operator) must name a database starting with
  ``cee_v02_test``; schemas are created fresh as ``cee_v02_test_<hex>`` and only those are dropped.
* Cleanup removes only what this module created.
Secrets (the generated password) are never printed, logged or written outside the cluster directory.
"""
from __future__ import annotations

import glob
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

ACTIVE_CLUSTER = None  # set by run_pg_tests.py when IT provisioned the cluster (restart tests need it)
ENV_CONN = "CEE_V02_TEST_CONN"  # JSON of explicit connection params, set by the runner for test code and workers
DB_PREFIX = "cee_v02_test"
SCHEMA_PREFIX = "cee_v02_test_"


def scrub_ambient_environment() -> list:
    """Remove every variable through which libpq (or a tool) could discover an existing endpoint/credential."""
    removed = []
    for key in list(os.environ):
        up = key.upper()
        if up.startswith("PG") or up in ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL") or up.endswith("_DSN"):
            removed.append(key)
            del os.environ[key]
    return removed


def _bin_dir() -> str:
    found = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))
    if not found:
        raise RuntimeError("postgresql-server-binaries-not-found (initdb)")
    return os.path.dirname(found[-1])


class DisposableCluster:
    """A throw-away local PostgreSQL cluster. ``start()`` / ``stop()`` / ``restart()`` / ``cleanup()``."""

    def __init__(self) -> None:
        self.root: "str | None" = None
        self._password = secrets.token_urlsafe(24)
        self._port = 54000 + secrets.randbelow(1000)
        self._bin = _bin_dir()
        self.admin_user = "cee_test_admin"
        self.dbname = DB_PREFIX + "_db"
        self.version = ""

    # -- helpers --------------------------------------------------------------------------------------------------------
    def _wrap(self, argv: list) -> list:
        if os.geteuid() == 0:  # PostgreSQL refuses to run as root; use its own OS user for the server processes only
            return [shutil.which("runuser", path="/usr/sbin:/sbin:/usr/bin:/bin") or "/usr/sbin/runuser", "-u", "postgres", "--"] + argv
        return argv

    def _run(self, argv: list, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(self._wrap(argv), check=True, capture_output=True, text=True,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}, **kw)

    @property
    def data(self) -> str:
        return os.path.join(self.root or "", "data")

    @property
    def sockdir(self) -> str:
        return os.path.join(self.root or "", "sock")

    def conn_params(self, *, dbname: "str | None" = None, application_name: str = "cee_test",
                    connect_timeout: int = 10) -> dict:
        return {"host": self.sockdir, "port": str(self._port), "dbname": dbname or self.dbname,
                "user": self.admin_user, "password": self._password, "application_name": application_name,
                "connect_timeout": str(connect_timeout)}

    # -- lifecycle --------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        self.root = tempfile.mkdtemp(prefix="cee_v02_pg_", dir=tempfile.gettempdir())
        os.makedirs(self.sockdir)
        if os.geteuid() == 0:
            pg = pwd.getpwnam("postgres")
            for path in (self.root, self.sockdir):
                os.chown(path, pg.pw_uid, pg.pw_gid)
        os.chmod(self.root, 0o700)
        pwfile = os.path.join(self.root, "pw")
        with open(pwfile, "w", encoding="utf-8") as fh:
            fh.write(self._password + "\n")
        if os.geteuid() == 0:
            os.chown(pwfile, pg.pw_uid, pg.pw_gid)
        os.chmod(pwfile, 0o600)
        self._run([os.path.join(self._bin, "initdb"), "-D", self.data, "-U", self.admin_user, "-E", "UTF8",
                   "--locale=C.UTF-8", "--auth-local=scram-sha-256", "--auth-host=scram-sha-256",
                   f"--pwfile={pwfile}"])
        with open(os.path.join(self.data, "postgresql.conf"), "a", encoding="utf-8") as fh:
            fh.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{self.sockdir}'\nport = {self._port}\n"
                     "max_connections = 80\nlog_min_messages = warning\npassword_encryption = scram-sha-256\n")
        self._start_server()
        from controlled_effect_executor_durable.pg_libpq import connect
        conn = connect(self.conn_params(dbname="postgres"))
        try:
            conn.cursor().execute(f"CREATE DATABASE {self.dbname}")
            cur = conn.cursor()
            cur.execute("SHOW server_version")
            self.version = cur.fetchone()["server_version"]
        finally:
            conn.close()

    def _start_server(self) -> None:
        self._run([os.path.join(self._bin, "pg_ctl"), "-D", self.data, "-l", os.path.join(self.root or "", "server.log"),
                   "-w", "-t", "60", "start"])

    def stop(self, mode: str = "fast") -> None:
        self._run([os.path.join(self._bin, "pg_ctl"), "-D", self.data, "-m", mode, "-w", "-t", "60", "stop"])

    def restart(self, mode: str = "immediate") -> None:
        """Stop (default: ``immediate`` = a crash, WAL recovery on restart) and start again."""
        self.stop(mode)
        self._start_server()

    def cleanup(self) -> None:
        if self.root is None:
            return
        try:
            self.stop("immediate")
        except Exception:  # noqa: BLE001 - already stopped is fine
            pass
        shutil.rmtree(self.root, ignore_errors=True)
        self.root = None


def external_params_from_env() -> "dict | None":
    raw = os.environ.get(ENV_CONN)
    if not raw:
        return None
    params = json.loads(raw)
    if not str(params.get("dbname", "")).startswith(DB_PREFIX):
        raise RuntimeError("refusing: test database name must start with cee_v02_test")
    return params


CREATED_SCHEMAS: list = []  # every schema name this run handed out (the runner drops leftovers, only these)


def new_schema_name() -> str:
    name = SCHEMA_PREFIX + secrets.token_hex(4)
    CREATED_SCHEMAS.extend([name, name + "_prov"])
    return name


def drop_schemas(connect, names: list) -> None:
    """Drop ONLY schemas this run itself named (prefix AND registered in CREATED_SCHEMAS), validated BEFORE connecting."""
    for name in names:
        if not name.startswith(SCHEMA_PREFIX) or name not in CREATED_SCHEMAS or not name.replace("_", "").isalnum():
            raise RuntimeError("refusing to drop a schema this harness did not name")
    conn = connect()
    try:
        cur = conn.cursor()
        for name in names:
            cur.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    finally:
        conn.close()


def wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.02, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError(f"timeout waiting for {what}")


__all__ = ["ACTIVE_CLUSTER", "CREATED_SCHEMAS", "DB_PREFIX", "DisposableCluster", "ENV_CONN", "SCHEMA_PREFIX", "drop_schemas", "external_params_from_env",
           "new_schema_name", "scrub_ambient_environment", "wait_until"]
