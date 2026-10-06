"""Scoped PostgreSQL X9 receipt custody inside the existing control provider.
No listener, token issuance, schema install, model call, wake or send.
Fresh existing OAuth verification + persisted current control-session binding on EVERY operation.
The three X9 capability labels describe authorized operations; OAuth grants remain project_state read/transition.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
import json
from typing import Callable
from control_context_remote_service import OAuthBearerAuthenticator
from adapters.x9_channel_ingress import (CHANNEL, SCHEMA, DISPOSITION_SCHEMA, ID, HEX64, STALE, NO_DELTA,
    MATERIAL, PointerRecord, X9Disposition, Readback, ChannelIdentity)
from providers.x9_session_channel import (X9_PRINCIPAL, X9_SESSION_REF, ProviderSessionIdentity,
    X9SessionChannelPort, ACCEPTED, NOT_SENT, UNKNOWN_SEND)

PM_PRINCIPAL = "CURRENT_PM_PROJECT_MANAGER_C1A05B39"
TABLE = "cerebro_x9_channel_records"
MAX_RECORD_BYTES = 8192

class CustodyUnavailable(RuntimeError):
    """Fixed safe code only; do not serialize underlying credential/database exceptions."""


def _json(record):
    return json.dumps(asdict(record),sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)


def _row(cursor):
    row=cursor.fetchone()
    if row is None:return None
    if isinstance(row,dict):return row
    return dict(zip((d[0] for d in cursor.description),row))


class PostgresX9SessionAPI:
    """Actual DB implementation. Host supplies existing verifier/secret getter/DB connection factory.
    Receiver role is pinned; producer role uses a separate authenticator/secret getter/session.
    No actor identity or scope flag can be supplied with a read or append request.
    """
    def __init__(self, *, authenticator: OAuthBearerAuthenticator, header_provider: Callable,
                 connection_factory: Callable, enabled: bool = False, role: str = "receiver",
                 producer_session_ref: str | None = None):
        if not isinstance(authenticator,OAuthBearerAuthenticator):raise CustodyUnavailable("EXISTING_OAUTH_AUTHENTICATOR_REQUIRED")
        if not callable(header_provider) or not callable(connection_factory):raise CustodyUnavailable("EXISTING_HOST_BINDING_REQUIRED")
        if role not in ("receiver","producer"):raise CustodyUnavailable("INVALID_ROLE")
        if role=="producer" and (not isinstance(producer_session_ref,str) or not ID.fullmatch(producer_session_ref)):
            raise CustodyUnavailable("PRODUCER_REGISTERED_SESSION_REQUIRED")
        self._auth=authenticator;self._headers=header_provider;self._connect=connection_factory
        self._enabled=enabled is True;self._role=role
        self._principal=X9_PRINCIPAL if role=="receiver" else PM_PRINCIPAL
        self._session=X9_SESSION_REF if role=="receiver" else producer_session_ref

    @contextmanager
    def _transaction(self, *, write=False):
        if not self._enabled:raise CustodyUnavailable("CUSTODY_DISABLED")
        try:
            identity=self._auth.authenticate(self._headers(),required_scope="project_state:read")
            identity.validate()
            if identity.principal_ref!=self._principal:raise CustodyUnavailable("AUTHENTICATED_PRINCIPAL_MISMATCH")
            if write and "project_state:transition" not in identity.state_scopes:raise CustodyUnavailable("EXISTING_TRANSITION_GRANT_REQUIRED")
        except CustodyUnavailable:raise
        except Exception:raise CustodyUnavailable("EXISTING_AUTHENTICATION_UNAVAILABLE") from None
        conn=cursor=None
        try:
            conn=self._connect()
            if getattr(conn,"autocommit",False) is not False:raise CustodyUnavailable("TRANSACTIONAL_CONNECTION_REQUIRED")
            cursor=conn.cursor()
            cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            cursor.execute("SET LOCAL statement_timeout = '3000ms'")
            cursor.execute("SET LOCAL lock_timeout = '1000ms'")
            for key,value in (("tenant_ref",identity.tenant_ref),("workspace_ref",identity.workspace_ref),("principal_ref",identity.principal_ref)):
                cursor.execute("SELECT set_config(%s,%s,true)",("cerebro."+key,value))
            cursor.execute("""SELECT s.session_binding_id,s.session_revision,s.session_fingerprint
              FROM cerebro_control_session_bindings s JOIN cerebro_project_instances p
              ON (s.tenant_ref,s.workspace_ref,s.project_ref)=(p.tenant_ref,p.workspace_ref,p.project_ref)
              WHERE s.tenant_ref=%s AND s.workspace_ref=%s AND s.principal_ref=%s
              AND s.consumer_ref=%s AND s.session_ref=%s
              AND s.project_revision=p.aggregate_revision AND p.project_status='ACTIVE'
              FOR SHARE OF s,p""",(identity.tenant_ref,identity.workspace_ref,identity.principal_ref,identity.consumer_ref,self._session))
            session=_row(cursor)
            if session is None or not session['session_binding_id'] or session['session_revision']<1:
                raise CustodyUnavailable("ACTUAL_CURRENT_CONTROL_SESSION_REQUIRED")
            yield cursor,identity,session
            conn.commit()
        except Exception as exc:
            if conn is not None:
                try:conn.rollback()
                except Exception:pass
            if isinstance(exc,CustodyUnavailable):raise
            raise CustodyUnavailable("EXISTING_PROVIDER_OPERATION_FAILED") from None
        finally:
            for obj in (cursor,conn):
                if obj is not None:
                    try:obj.close()
                    except Exception:pass

    def identity(self):
        with self._transaction() as (_,identity,__):
            scopes={"pointer:read","disposition:read"}
            if self._role=="receiver" and "project_state:transition" in identity.state_scopes:scopes.add("disposition:append")
            return ProviderSessionIdentity(CHANNEL,self._principal,self._session,True,True,frozenset(scopes))

    def _read(self,cursor,identity,kind,event_id):
        cursor.execute(f"SELECT record_payload,content_sha256,writer_principal,writer_session,provider_revision FROM {TABLE} WHERE tenant_ref=%s AND workspace_ref=%s AND channel=%s AND record_kind=%s AND event_id=%s",
                       (identity.tenant_ref,identity.workspace_ref,CHANNEL,kind,event_id))
        row=_row(cursor)
        if row is None:return None
        cls=PointerRecord if kind=="POINTER" else X9Disposition
        payload=row['record_payload']
        if isinstance(payload,str):payload=json.loads(payload)
        if type(payload) is not dict or set(payload)!={f.name for f in fields(cls)}:raise CustodyUnavailable("ORIGINAL_RECORD_SHAPE_MISMATCH")
        payload=dict(payload)
        if kind=="POINTER":payload['way_home']=tuple(payload['way_home'])
        record=cls(**payload)
        expected=PM_PRINCIPAL if kind=="POINTER" else X9_PRINCIPAL
        if (record.event_id!=event_id or row['writer_principal']!=expected or record.content_sha256!=row['content_sha256']
            or type(row['provider_revision']) is not int or row['provider_revision']<1):
            raise CustodyUnavailable("ORIGINAL_RECORD_HASH_OR_WRITER_MISMATCH")
        if kind=="POINTER" and (record.schema!=SCHEMA or record.producer_id!=expected or record.receiver_ref!=X9_SESSION_REF):
            raise CustodyUnavailable("ORIGINAL_POINTER_CUSTODY_MISMATCH")
        if kind=="DISPOSITION" and (record.schema!=DISPOSITION_SCHEMA or row['writer_session']!=X9_SESSION_REF):
            raise CustodyUnavailable("ORIGINAL_DISPOSITION_CUSTODY_MISMATCH")
        return Readback(record,record.content_sha256,expected,"x9pg:"+str(row['provider_revision']))

    def _public_read(self,kind,event_id):
        if not isinstance(event_id,str) or not ID.fullmatch(event_id):raise CustodyUnavailable("INVALID_EVENT_ID")
        with self._transaction() as (c,i,_):return self._read(c,i,kind,event_id)

    def read_pointer_by_event_id(self,event_id):return self._public_read("POINTER",event_id)
    def read_disposition_by_event_id(self,event_id):return self._public_read("DISPOSITION",event_id)

    def _append(self,kind,record):
        write_started=False
        try:
            body=_json(record)
            if len(body.encode('utf8'))>MAX_RECORD_BYTES:return NOT_SENT
            with self._transaction(write=True) as (c,i,_):
                if kind=="DISPOSITION":
                    pointer=self._read(c,i,"POINTER",record.event_id)
                    if pointer is None or (pointer.record.attempt_id,pointer.content_sha256)!=(record.attempt_id,record.pointer_sha256):return NOT_SENT
                write_started=True
                c.execute(f"""INSERT INTO {TABLE}
                  (tenant_ref,workspace_ref,channel,record_kind,event_id,writer_principal,writer_consumer,writer_session,content_sha256,record_payload)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                  ON CONFLICT (tenant_ref,workspace_ref,channel,record_kind,event_id) DO NOTHING""",
                  (i.tenant_ref,i.workspace_ref,CHANNEL,kind,record.event_id,i.principal_ref,i.consumer_ref,self._session,record.content_sha256,body))
                # READ COMMITTED, separate SELECT after conflict: returns the ORIGINAL durable row.
                original=self._read(c,i,kind,record.event_id)
                if original is None:raise CustodyUnavailable("INSERT_ORIGINAL_NOT_VISIBLE")
                if original.record!=record:return NOT_SENT
            return ACCEPTED # commit completed; independent public read is still required
        except CustodyUnavailable:return UNKNOWN_SEND if write_started else NOT_SENT
        except Exception:return UNKNOWN_SEND if write_started else NOT_SENT

    def append_disposition_once(self,event_id,content_sha256,record):
        if self._role!="receiver" or not isinstance(record,X9Disposition):return NOT_SENT
        if (record.schema!=DISPOSITION_SCHEMA or record.event_id!=event_id or record.content_sha256!=content_sha256
            or record.disposition not in (STALE,NO_DELTA,MATERIAL) or record.work_consumed is not False or record.effect!="NONE_CLAIMED"
            or not all(isinstance(v,str) and ID.fullmatch(v) for v in (record.event_id,record.attempt_id,record.owner_revision,record.reason))
            or not all(isinstance(v,str) and HEX64.fullmatch(v) for v in (record.pointer_sha256,record.owner_material_sha256))):return NOT_SENT
        return self._append("DISPOSITION",record)

    def append_pointer_once(self,record):
        if self._role!="producer" or not isinstance(record,PointerRecord):return NOT_SENT
        if (record.schema!=SCHEMA or record.producer_id!=PM_PRINCIPAL or record.receiver_ref!=X9_SESSION_REF
            or not all(isinstance(v,str) and ID.fullmatch(v) for v in (record.event_id,record.attempt_id,record.closure_id,record.owner_ref,record.referent_type,record.revision,record.claim_ref,record.packet_ref,record.queue_ref,record.source_cut))
            or not all(isinstance(v,str) and HEX64.fullmatch(v) for v in (record.expected_sha256,record.packet_sha256))):return NOT_SENT
        return self._append("POINTER",record)


class PostgresPMProducerChannel:
    """Producer's existing ChannelPort over its SEPARATE actual PostgreSQL credential/session API."""
    def __init__(self,api):
        if not isinstance(api,PostgresX9SessionAPI) or api._role!="producer":raise CustodyUnavailable("SEPARATE_PRODUCER_API_REQUIRED")
        self._api=api
    def identity(self):
        with self._api._transaction() as (_,i,__):
            return ChannelIdentity(CHANNEL,PM_PRINCIPAL,True,"project_state:transition" in i.state_scopes,True)
    def append_pointer_once(self,record):return self._api.append_pointer_once(record)
    def read_pointer_by_event_id(self,event_id):return self._api.read_pointer_by_event_id(event_id)
    def append_disposition_once(self,record):return NOT_SENT
    def read_disposition_by_event_id(self,event_id):return self._api.read_disposition_by_event_id(event_id)


def bind_existing_pm_x9(settings,ports,*,receiver_api,producer_api,enabled=False):
    """Registration only in existing build_binding. Does NOT install schema or start/send/pulse."""
    if enabled is not True:raise CustodyUnavailable("REGISTRATION_DISABLED")
    if not isinstance(receiver_api,PostgresX9SessionAPI) or not isinstance(producer_api,PostgresX9SessionAPI):
        raise CustodyUnavailable("ACTUAL_POSTGRES_APIS_REQUIRED")
    if receiver_api is producer_api or receiver_api._role!='receiver' or producer_api._role!='producer':
        raise CustodyUnavailable("SEPARATE_RECEIVER_PRODUCER_REQUIRED")
    from signalvev_client.pm_x9 import build_binding
    receiver_channel=X9SessionChannelPort(api=receiver_api,enabled=True)
    producer_channel=PostgresPMProducerChannel(producer_api)
    if not receiver_channel.identity().authenticated or not producer_channel.identity().append_allowed:
        raise CustodyUnavailable("ACTUAL_PROVIDER_REGISTRATION_UNQUALIFIED")
    qualified=replace(ports,x9_channel=receiver_channel,
                      producer_channel=producer_channel)
    return build_binding(settings,qualified)
