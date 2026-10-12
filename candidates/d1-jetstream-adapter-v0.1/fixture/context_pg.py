"""Own ONE disposable loopback PostgreSQL cluster carrying the ACTUAL selected Context schema and code path.

DEV/FIXTURE ONLY. Nothing here is a production install, credential, grant or role binding:
  * initdb a private cluster under <work>/pg (scram-sha-256, 127.0.0.1, free port); passwords are generated per run,
    kept only under <work>/state and removed with it;
  * the selected Context migration manifest is applied unchanged through Context's own ``apply_postgres_migrations``
    with a distinct migration role, and the runtime role is provisioned with Context's own
    ``deployment/provision_database._provision_runtime_role`` (NOSUPERUSER/NOBYPASSRLS);
  * a separate SELECT-only role models the existing dedicated read-only runtime-notice connection;
  * SYNTHETIC project/session/X9-role rows are seeded with Context's own registry functions (labelled fixture rows,
    standing in for an installation authority) -- never real actors;
  * JWTs are signed by a per-run RSA key and verified by the UNCHANGED ``OAuthBearerAuthenticator`` through a
    static-key ``BearerTokenVerifier`` (the deployed verifier fetches a JWKS over HTTPS instead).
Only this run's postmaster PID and paths under <work> are touched.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PG_BIN_CANDIDATES = ("/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/17/bin", "/usr/local/pgsql/bin")
TENANT, WORKSPACE, PROJECT = "tenant:syn-1", "workspace:syn-1", "project:syn-1"
CONSUMER = "CHATGPT_REMOTE_MCP"
ISSUER = "https://issuer.fixture.invalid"
RESOURCE = "https://context.fixture.invalid"
PRINCIPALS = {"pm": "principal:syn-pm", "x9": "principal:syn-x9", "d1pub": "principal:syn-d1-publisher",
              "other": "principal:syn-other"}
# persisted session refs as the Context transport namespaces them ("openai/session" X -> "chatgpt:X")
SESSIONS = {"pm": "chatgpt:syn-pm", "x9": "chatgpt:syn-x9", "d1pub": "chatgpt:syn-d1pub",
            "other": "chatgpt:syn-other"}


class PgUnavailable(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pg_bin(explicit: str | None = None) -> Path:
    for cand in ((explicit,) if explicit else PG_BIN_CANDIDATES):
        if cand and (Path(cand) / ("initdb.exe" if os.name == "nt" else "initdb")).is_file():
            return Path(cand)
    raise PgUnavailable("POSTGRES_BINARIES_NOT_FOUND")


def context_imports(context_root: Path) -> None:
    for p in (context_root, context_root / "mcp", context_root / "tooling" / "context", context_root / "deployment"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


@dataclass
class Roles:
    admin: str
    migrator: str
    runtime: str
    readonly: str


class ContextPostgres:
    """initdb + start + Context migrations/provisioning + synthetic seed on 127.0.0.1."""

    def __init__(self, work: Path, context_root: Path, *, bin_dir: Path, run_as: str | None) -> None:
        self.work, self.context_root, self.bin = Path(work), Path(context_root), Path(bin_dir)
        self.dir = self.work / "pg"
        self.data = self.dir / "data"
        self.log = self.work / "logs" / "postgres.log"
        self.port = _free_port()
        self.run_as = run_as
        self.secret_dir = self.work / "state" / "pg-secrets"
        self.pw = {k: secrets.token_urlsafe(24) for k in ("admin", "migrator", "runtime", "readonly")}
        self.roles = Roles("postgres", "cerebro_migrator", "cerebro_runtime", "cerebro_notice_ro")
        self.started = False
        self.dbname = "cerebro"

    # ------------------------------------------------------------------ process
    def _cmd(self, args: list[str], **kw) -> subprocess.CompletedProcess:
        if self.run_as:
            args = ["runuser", "-u", self.run_as, "--"] + args
        if os.name == "nt" and Path(args[0]).name.lower() == "pg_ctl.exe" and args[-1] == "start":
            # Windows postgres inherits pg_ctl's stdout pipe; use a file so start can return.
            with (self.work / "logs" / "pg_ctl.log").open("ab") as log:
                return subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=120, **kw)
        return subprocess.run(args, capture_output=True, text=True, timeout=120, **kw)

    def start(self) -> None:
        self.dir.mkdir(parents=True)
        self.secret_dir.mkdir(parents=True, mode=0o700)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        pwfile = self.dir / "pwfile"
        pwfile.write_text(self.pw["admin"] + "\n")
        if self.run_as:                  # the cluster user only needs to traverse <work> and <work>/logs
            for d in (self.work, self.log.parent):
                os.chmod(d, os.stat(d).st_mode | 0o011)
            shutil.chown(self.dir, user=self.run_as)
            shutil.chown(pwfile, user=self.run_as)
            self.log.touch()
            shutil.chown(self.log, user=self.run_as)
        r = self._cmd([str(self.bin / ("initdb.exe" if os.name == "nt" else "initdb")), "-D", str(self.data), "-U", "postgres", "--auth=scram-sha-256",
                       f"--pwfile={pwfile}", "-E", "UTF8", "--locale=C" if os.name == "nt" else "--locale=C.UTF-8"])
        pwfile.unlink()
        if r.returncode != 0:
            raise PgUnavailable("INITDB_FAILED: " + r.stderr.strip()[-300:])
        r = self._cmd([str(self.bin / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")), "-D", str(self.data), "-l", str(self.log), "-w", "-t", "30",
                       "-o", (f"-c listen_addresses=127.0.0.1 -p {self.port} "
                              + ("" if os.name == "nt" else "-c unix_socket_directories='' ")
                              + "-c fsync=on -c max_connections=60"), "start"])
        if r.returncode != 0:
            raise PgUnavailable("PG_START_FAILED: " + ((r.stderr or r.stdout) or "").strip()[-300:])
        self.started = True

    def stop(self) -> None:
        if self.started:
            self._cmd([str(self.bin / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")), "-D", str(self.data), "-m", "fast", "-w", "stop"])
            self.started = False

    def version(self) -> str:
        r = self._cmd([str(self.bin / ("postgres.exe" if os.name == "nt" else "postgres")), "--version"])
        return r.stdout.strip()

    # ------------------------------------------------------------------ connections
    def dsn(self, who: str, dbname: str | None = None) -> str:
        user = getattr(self.roles, who)
        return (f"host=127.0.0.1 port={self.port} dbname={dbname or self.dbname} user={user} password={self.pw[who]} "
                f"connect_timeout=5 application_name=d1-owner-outbox-fixture-{who}")

    def connect(self, who: str, *, autocommit: bool = False):
        import psycopg
        return psycopg.connect(self.dsn(who), autocommit=autocommit)

    def factory(self, who: str):
        import psycopg

        def make():
            return psycopg.connect(self.dsn(who), autocommit=False)
        return make

    # ------------------------------------------------------------------ Context schema (unchanged code path)
    def install_context(self, *, dbname: str = "cerebro", manifest: Path | None = None) -> dict[str, Any]:
        """One fresh database per call (per test): migrations via the migration role, then runtime provisioning."""
        import psycopg
        context_imports(self.context_root)
        self.dbname = dbname
        admin = psycopg.connect(self.dsn("admin", "postgres"), autocommit=True)
        with admin:
            if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname='cerebro_migrator'").fetchone():
                admin.execute("CREATE ROLE cerebro_migrator LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %s".replace(
                    "%s", "'" + self.pw["migrator"] + "'"))
            admin.execute(f"CREATE DATABASE {dbname} OWNER cerebro_migrator")
        with psycopg.connect(self.dsn("admin"), autocommit=True) as db:
            db.execute("ALTER SCHEMA public OWNER TO cerebro_migrator")
        from control_context_state_postgres import apply_postgres_migrations, DEFAULT_MANIFEST
        result = apply_postgres_migrations(self.factory("migrator"), manifest or DEFAULT_MANIFEST)
        self.provision_runtime()
        return result

    def provision_runtime(self) -> None:
        import psycopg
        context_imports(self.context_root)
        from provision_database import _provision_runtime_role
        _provision_runtime_role(self.dsn("admin"), self.roles.runtime, self.pw["runtime"])
        with psycopg.connect(self.dsn("admin"), autocommit=True) as db:
            exists = db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (self.roles.readonly,)).fetchone()
            if not exists:
                db.execute(f"CREATE ROLE {self.roles.readonly} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD "
                           f"'{self.pw['readonly']}'")
            db.execute(f"GRANT USAGE ON SCHEMA public TO {self.roles.readonly}")
            db.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {self.roles.readonly}")

    # ------------------------------------------------------------------ synthetic seed (labelled)
    def seed(self) -> dict[str, Any]:
        """SYNTHETIC project + 4 session bindings + X9 role tuple rows, via Context's own registry functions."""
        import psycopg
        context_imports(self.context_root)
        from control_context_registry import bind_control_session, bootstrap_project_state
        project, _ = bootstrap_project_state(
            aggregate_id="AGG-SYN-D1-OUTBOX", tenant_ref=TENANT, workspace_ref=WORKSPACE, project_ref=PROJECT,
            source_revision="fixture-synthetic", event_id="EVENT-SYN-BOOT", decision_ref="MCPD-SYN-BOOT",
            root={"context_id": "CTX-SYN-ROOT", "human_label": "Synthetic fixture root",
                  "objective_ref": "OBJ-SYN", "scope_ref": "SCOPE-SYN", "basis_refs": ["SYN"],
                  "project_basis_ref": "PB-SYN", "quality_trace_ref": "QT-SYN", "completion_criteria_refs": ["SYN"]})
        sessions = {k: bind_control_session(project, session_binding_id=f"binding:syn-{k}", principal_ref=p,
                                            consumer_ref=CONSUMER, session_ref=SESSIONS[k])
                    for k, p in PRINCIPALS.items()}
        with psycopg.connect(self.dsn("admin"), autocommit=False) as db:
            db.execute("""INSERT INTO cerebro_project_instances (tenant_ref,workspace_ref,project_ref,aggregate_id,
                source_revision,project_status,default_context_ref,aggregate_revision,aggregate_fingerprint,next_sequence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                       (TENANT, WORKSPACE, PROJECT, project["aggregate_id"], project["source_revision"],
                        project["project_status"], project["default_context_ref"], project["revision"],
                        project["fingerprint"], project["next_sequence"]))
            for c in project["contexts"]:
                db.execute("""INSERT INTO cerebro_control_contexts (tenant_ref,workspace_ref,project_ref,context_id,
                    parent_context_ref,derived_from_context_ref,lifecycle,control_condition,disposition,sequence,
                    context_payload,context_fingerprint) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                           (TENANT, WORKSPACE, PROJECT, c["context_id"], c["parent_context_ref"],
                            c["derived_from_context_ref"], c["lifecycle"], c["control_condition"], c["disposition"],
                            c["sequence"], json.dumps(c), c["context_fingerprint"]))
            for k, s in sessions.items():
                db.execute("""INSERT INTO cerebro_control_session_bindings (tenant_ref,workspace_ref,principal_ref,
                    consumer_ref,session_ref,session_binding_id,project_ref,active_context_ref,project_revision,
                    session_revision,session_fingerprint) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                           (TENANT, WORKSPACE, s["principal_ref"], CONSUMER, s["session_ref"],
                            s["session_binding_id"], PROJECT, s["active_context_ref"], s["project_revision"],
                            s["session_revision"], s["fingerprint"]))
            for role, key, assignment in (("producer", "pm", "assignment:syn-producer"),
                                          ("receiver", "x9", "assignment:syn-receiver")):
                s = sessions[key]
                db.execute("""INSERT INTO cerebro_x9_channel_role_assignments (tenant_ref,workspace_ref,project_ref,
                    channel,role,config_ref,assignment_ref,source_ref,principal_ref,consumer_ref,session_ref,
                    project_revision,session_binding_id,session_revision,session_fingerprint,active)
                    VALUES (%s,%s,%s,'PROSJEKTMANN_CHANNEL',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)""",
                           (TENANT, WORKSPACE, PROJECT, role, "config:syn-x9", assignment, "fixture:synthetic",
                            s["principal_ref"], CONSUMER, s["session_ref"], s["project_revision"],
                            s["session_binding_id"], s["session_revision"], s["fingerprint"]))
            db.commit()
        self.project, self.sessions = project, sessions
        return {"project_revision": project["revision"], "sessions": {k: s["session_binding_id"]
                                                                     for k, s in sessions.items()}}

    def export_info(self) -> dict[str, Any]:
        """Nonsecret-to-the-repo connection facts for a child process of THIS run (state dir only)."""
        return {"port": self.port, "dbname": self.dbname, "pw": self.pw, "context_root": str(self.context_root),
                "sessions": self.sessions, "project": self.project}

    def x9_configuration_mapping(self) -> dict[str, Any]:
        def role(name, key, assignment):
            s = self.sessions[key]
            return {"role": name, "assignment_ref": assignment, "source_ref": "fixture:synthetic",
                    "identity": {"tenant_ref": TENANT, "workspace_ref": WORKSPACE, "principal_ref": s["principal_ref"],
                                 "consumer_ref": CONSUMER, "session_ref": s["session_ref"], "project_ref": PROJECT,
                                 "project_revision": s["project_revision"],
                                 "session_binding_id": s["session_binding_id"],
                                 "session_revision": s["session_revision"],
                                 "session_fingerprint": s["fingerprint"]}}
        return {"config_ref": "config:syn-x9", "channel": "PROSJEKTMANN_CHANNEL",
                "producer": role("producer", "pm", "assignment:syn-producer"),
                "receiver": role("receiver", "x9", "assignment:syn-receiver")}

    def pm_assignment_mapping(self) -> dict[str, str]:
        s = self.sessions["pm"]
        return {"owner_ref": "owner:syn-pm", "tenant_ref": TENANT, "workspace_ref": WORKSPACE,
                "project_ref": PROJECT, "principal_ref": s["principal_ref"], "consumer_ref": CONSUMER,
                "session_ref": s["session_ref"], "session_binding_id": s["session_binding_id"]}

    def publisher_assignment_mapping(self) -> dict[str, str]:
        s = self.sessions["d1pub"]
        return {"owner_ref": "owner:syn-pm", "route_ref": "route:syn-d1-x9", "tenant_ref": TENANT,
                "workspace_ref": WORKSPACE, "project_ref": PROJECT, "principal_ref": s["principal_ref"],
                "consumer_ref": CONSUMER, "session_ref": s["session_ref"],
                "session_binding_id": s["session_binding_id"]}


class FixtureTokens:
    """Per-run RSA key; JWTs verified by the unchanged OAuthBearerAuthenticator via a static-key verifier."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public = self._key.public_key()

    def token(self, principal: str, scopes: str, *, ttl: int = 600, extra: dict | None = None,
              now: float | None = None) -> str:
        import jwt
        now = time.time() if now is None else now
        claims = {"iss": ISSUER, "aud": RESOURCE, "sub": principal, "exp": int(now + ttl), "iat": int(now),
                  "scope": scopes, "cerebro_tenant": TENANT, "cerebro_workspace": WORKSPACE,
                  "cerebro_project": PROJECT}
        claims.update(extra or {})
        return jwt.encode(claims, self._key, algorithm="RS256")

    def verifier(self):
        import jwt
        from control_context_remote_service import VerifiedBearerToken
        public = self.public

        class StaticKeyVerifier:
            """FIXTURE: same decode options as deployment/app.JwksBearerTokenVerifier, static per-run key."""

            def verify(self, token: str):
                claims = jwt.decode(token, key=public, algorithms=["RS256"],
                                    options={"verify_aud": False, "verify_exp": False, "verify_nbf": False,
                                             "verify_iat": False, "verify_iss": False})
                return VerifiedBearerToken(claims=claims, signature_verified=True)
        return StaticKeyVerifier()


ROUTE_REF, RECEIVER_REF = "route:syn-d1-x9", "x9-receiver:syn-1"


def install_route(pg: ContextPostgres, *, enqueue: bool = True, drain: bool = True, ttl: int = 120,
                  subject: str = "cerebro.test.d1.pointer", stream: str = "X2_D1_TEST") -> None:
    """SYNTHETIC installation-authority act (migration/owner role, not the runtime role): one route row."""
    s = pg.sessions["d1pub"]
    with pg.connect("migrator") as db:
        db.execute("""INSERT INTO cerebro_pm_owner_d1_routes (tenant_ref,workspace_ref,project_ref,owner_ref,route_ref,
            receiver_ref,delivery_class,subject,stream,x9_config_ref,receiver_assignment_ref,publisher_principal_ref,
            publisher_consumer_ref,publisher_session_ref,publisher_session_binding_id,pointer_ttl_seconds,
            binding_revision,enqueue_enabled,drain_enabled,source_ref)
            VALUES (%s,%s,%s,'owner:syn-pm',%s,%s,'D1_POINTER_HINT',%s,%s,'config:syn-x9','assignment:syn-receiver',
                    %s,%s,%s,%s,%s,1,%s,%s,'fixture:synthetic-installation')
            ON CONFLICT (tenant_ref,workspace_ref,owner_ref,route_ref) DO UPDATE
            SET enqueue_enabled=EXCLUDED.enqueue_enabled, drain_enabled=EXCLUDED.drain_enabled""",
                   (TENANT, WORKSPACE, PROJECT, ROUTE_REF, RECEIVER_REF, subject, stream, s["principal_ref"],
                    CONSUMER, s["session_ref"], s["session_binding_id"], ttl, enqueue, drain))
        db.commit()


class PgInfo(ContextPostgres):
    """Child-process view of the parent's cluster (no process control)."""

    def __init__(self, info: dict[str, Any]) -> None:   # noqa: D401 - deliberately skips ContextPostgres.__init__
        self.port, self.dbname, self.pw = info["port"], info["dbname"], info["pw"]
        self.context_root = Path(info["context_root"])
        self.sessions, self.project = info["sessions"], info["project"]
        self.roles = Roles("postgres", "cerebro_migrator", "cerebro_runtime", "cerebro_notice_ro")
        self.started = False
