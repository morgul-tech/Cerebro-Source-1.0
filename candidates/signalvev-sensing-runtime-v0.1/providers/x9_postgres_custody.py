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
from providers.x9_session_channel import (ProviderSessionIdentity,
    X9SessionChannelPort, ACCEPTED, NOT_SENT, UNKNOWN_SEND)
from providers.x9_role_binding import (TrustedCustodyConfigurationPort, read_trusted_configuration)

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
    Explicit receiver/producer role assignment and the full host-verified tuple pair
    come from a separate trusted configuration port. OAuth identity must match the
    configured tuple and the persisted binding must still be current on every operation.
    No actor identity or scope flag can be supplied with a read or append request.
    """
    def __init__(self, *, authenticator: OAuthBearerAuthenticator, header_provider: Callable,
                 connection_factory: Callable, configuration_provider: TrustedCustodyConfigurationPort,
                 enabled: bool = False, role: str = "receiver"):
        if not isinstance(authenticator,OAuthBearerAuthenticator):raise CustodyUnavailable("EXISTING_OAUTH_AUTHENTICATOR_REQUIRED")
        if not callable(header_provider) or not callable(connection_factory):raise CustodyUnavailable("EXISTING_HOST_BINDING_REQUIRED")
        if configuration_provider is None or not callable(getattr(configuration_provider,"read_configuration",None)):
            raise CustodyUnavailable("TRUSTED_ROLE_CONFIGURATION_REQUIRED")
        if role not in ("receiver","producer"):raise CustodyUnavailable("INVALID_ROLE")
        self._auth=authenticator;self._headers=header_provider;self._connect=connection_factory
        self._config=configuration_provider;self._enabled=enabled is True;self._role=role

    def _configuration(self):
        try:return read_trusted_configuration(self._config)
        except Exception:raise CustodyUnavailable("TRUSTED_ROLE_CONFIGURATION_UNAVAILABLE") from None

    def _binding(self,config):
        return config.receiver if self._role=="receiver" else config.producer

    @contextmanager
    def _transaction(self, *, write=False):
        if not self._enabled:raise CustodyUnavailable("CUSTODY_DISABLED")
        config=self._configuration();binding=self._binding(config);expected=binding.identity
        try:
            identity=self._auth.authenticate(self._headers(),required_scope="project_state:read")
            identity.validate()
            if (identity.tenant_ref,identity.workspace_ref,identity.principal_ref,identity.consumer_ref) != (
                    expected.tenant_ref,expected.workspace_ref,expected.principal_ref,expected.consumer_ref):
                raise CustodyUnavailable("AUTHENTICATED_IDENTITY_TUPLE_MISMATCH")
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
            for key,value in (("tenant_ref",identity.tenant_ref),("workspace_ref",identity.workspace_ref),
                              ("principal_ref",identity.principal_ref),("consumer_ref",identity.consumer_ref),
                              ("session_ref",expected.session_ref),("role",binding.role),
                              ("role_assignment_ref",binding.assignment_ref),
                              ("config_ref",config.config_ref),
                              ("project_ref",expected.project_ref),
                              ("project_revision",str(expected.project_revision)),
                              ("session_binding_id",expected.session_binding_id),
                              ("session_revision",str(expected.session_revision)),
                              ("session_fingerprint",expected.session_fingerprint)):
                cursor.execute("SELECT set_config(%s,%s,true)",("cerebro."+key,value))
            cursor.execute("""SELECT s.session_binding_id,s.session_revision,s.session_fingerprint,
                    s.project_ref,s.project_revision
              FROM cerebro_control_session_bindings s JOIN cerebro_project_instances p
              ON (s.tenant_ref,s.workspace_ref,s.project_ref)=(p.tenant_ref,p.workspace_ref,p.project_ref)
              WHERE s.tenant_ref=%s AND s.workspace_ref=%s AND s.principal_ref=%s
              AND s.consumer_ref=%s AND s.session_ref=%s
              AND s.project_ref=%s AND s.project_revision=%s
              AND s.session_binding_id=%s AND s.session_revision=%s AND s.session_fingerprint=%s
              AND s.project_revision=p.aggregate_revision AND p.project_status='ACTIVE'
              FOR SHARE OF s,p""",(identity.tenant_ref,identity.workspace_ref,identity.principal_ref,identity.consumer_ref,
                                   expected.session_ref,expected.project_ref,expected.project_revision,
                                   expected.session_binding_id,expected.session_revision,expected.session_fingerprint))
            session=_row(cursor)
            if session is None or not session['session_binding_id'] or session['session_revision']<1:
                raise CustodyUnavailable("ACTUAL_CURRENT_CONTROL_SESSION_REQUIRED")
            if (session['session_binding_id'],session['session_revision'],session['session_fingerprint'],
                session['project_ref'],session['project_revision']) != (
                expected.session_binding_id,expected.session_revision,expected.session_fingerprint,
                expected.project_ref,expected.project_revision):
                raise CustodyUnavailable("PERSISTED_SESSION_TUPLE_MISMATCH")
            yield cursor,identity,session,config,binding
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
        with self._transaction() as (_,identity,__,config,binding):
            scopes={"pointer:read","disposition:read"}
            if self._role=="receiver" and "project_state:transition" in identity.state_scopes:scopes.add("disposition:append")
            return ProviderSessionIdentity(CHANNEL,binding.identity.principal_ref,binding.identity.session_ref,
                                           True,True,frozenset(scopes))

    def _read(self,cursor,identity,kind,event_id,config):
        cursor.execute(f"SELECT record_payload,content_sha256,writer_principal,writer_consumer,writer_session,writer_role,writer_assignment_ref,peer_role,peer_assignment_ref,peer_session_ref,config_ref,provider_revision FROM {TABLE} WHERE tenant_ref=%s AND workspace_ref=%s AND project_ref=%s AND channel=%s AND record_kind=%s AND event_id=%s",
                       (identity.tenant_ref,identity.workspace_ref,config.receiver.identity.project_ref,CHANNEL,kind,event_id))
        row=_row(cursor)
        if row is None:return None
        cls=PointerRecord if kind=="POINTER" else X9Disposition
        payload=row['record_payload']
        if isinstance(payload,str):payload=json.loads(payload)
        if type(payload) is not dict or set(payload)!={f.name for f in fields(cls)}:raise CustodyUnavailable("ORIGINAL_RECORD_SHAPE_MISMATCH")
        payload=dict(payload)
        if kind=="POINTER":payload['way_home']=tuple(payload['way_home'])
        record=cls(**payload)
        writer=config.producer if kind=="POINTER" else config.receiver
        peer=config.receiver if kind=="POINTER" else config.producer
        expected=writer.identity.principal_ref
        if (record.event_id!=event_id or row['writer_principal']!=expected or record.content_sha256!=row['content_sha256']
            or row['writer_role']!=writer.role or row['writer_assignment_ref']!=writer.assignment_ref
            or row['writer_consumer']!=writer.identity.consumer_ref
            or row['writer_session']!=writer.identity.session_ref
            or row['peer_role']!=peer.role or row['peer_assignment_ref']!=peer.assignment_ref
            or row['peer_session_ref']!=peer.identity.session_ref
            or row['config_ref']!=config.config_ref
            or type(row['provider_revision']) is not int or row['provider_revision']<1):
            raise CustodyUnavailable("ORIGINAL_RECORD_HASH_OR_WRITER_MISMATCH")
        if kind=="POINTER" and (record.schema!=SCHEMA or record.producer_id!=expected
                                 or record.receiver_ref!=peer.identity.session_ref):
            raise CustodyUnavailable("ORIGINAL_POINTER_CUSTODY_MISMATCH")
        if kind=="DISPOSITION" and (record.schema!=DISPOSITION_SCHEMA
                                     or row['writer_session']!=writer.identity.session_ref):
            raise CustodyUnavailable("ORIGINAL_DISPOSITION_CUSTODY_MISMATCH")
        return Readback(record,record.content_sha256,expected,"x9pg:"+str(row['provider_revision']))

    def _public_read(self,kind,event_id):
        if not isinstance(event_id,str) or not ID.fullmatch(event_id):raise CustodyUnavailable("INVALID_EVENT_ID")
        with self._transaction() as (c,i,_,config,__):return self._read(c,i,kind,event_id,config)

    def read_pointer_by_event_id(self,event_id):return self._public_read("POINTER",event_id)
    def read_disposition_by_event_id(self,event_id):return self._public_read("DISPOSITION",event_id)

    def _append(self,kind,record):
        write_started=False
        try:
            body=_json(record)
            if len(body.encode('utf8'))>MAX_RECORD_BYTES:return NOT_SENT
            with self._transaction(write=True) as (c,i,_,config,binding):
                if kind=="DISPOSITION":
                    pointer=self._read(c,i,"POINTER",record.event_id,config)
                    if pointer is None or (pointer.record.attempt_id,pointer.content_sha256)!=(record.attempt_id,record.pointer_sha256):return NOT_SENT
                write_started=True
                peer=config.receiver if self._role=="producer" else config.producer
                c.execute(f"""INSERT INTO {TABLE}
                  (tenant_ref,workspace_ref,project_ref,channel,config_ref,record_kind,event_id,writer_principal,writer_consumer,writer_session,
                   writer_role,writer_assignment_ref,peer_role,peer_assignment_ref,peer_session_ref,content_sha256,record_payload)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                  ON CONFLICT (tenant_ref,workspace_ref,project_ref,channel,record_kind,event_id) DO NOTHING""",
                  (i.tenant_ref,i.workspace_ref,binding.identity.project_ref,CHANNEL,config.config_ref,kind,record.event_id,i.principal_ref,i.consumer_ref,
                   binding.identity.session_ref,binding.role,binding.assignment_ref,peer.role,peer.assignment_ref,
                   peer.identity.session_ref,
                   record.content_sha256,body))
                # READ COMMITTED, separate SELECT after conflict: returns the ORIGINAL durable row.
                original=self._read(c,i,kind,record.event_id,config)
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
        config=self._configuration();producer=config.producer;receiver=config.receiver
        if (record.schema!=SCHEMA or record.producer_id!=producer.identity.principal_ref
            or record.receiver_ref!=receiver.identity.session_ref
            or not all(isinstance(v,str) and ID.fullmatch(v) for v in (record.event_id,record.attempt_id,record.closure_id,record.owner_ref,record.referent_type,record.revision,record.claim_ref,record.packet_ref,record.queue_ref,record.source_cut))
            or not all(isinstance(v,str) and HEX64.fullmatch(v) for v in (record.expected_sha256,record.packet_sha256))):return NOT_SENT
        return self._append("POINTER",record)


class PostgresPMProducerChannel:
    """Producer's existing ChannelPort over its SEPARATE actual PostgreSQL credential/session API."""
    def __init__(self,api):
        if not isinstance(api,PostgresX9SessionAPI) or api._role!="producer":raise CustodyUnavailable("SEPARATE_PRODUCER_API_REQUIRED")
        self._api=api
    def identity(self):
        with self._api._transaction() as (_,i,__,config,binding):
            return ChannelIdentity(CHANNEL,binding.identity.principal_ref,True,
                                   "project_state:transition" in i.state_scopes,True)
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
    if receiver_api._configuration()!=producer_api._configuration():
        raise CustodyUnavailable("RECEIVER_PRODUCER_CONFIG_MISMATCH")
    from signalvev_client.pm_x9 import build_binding
    receiver_channel=X9SessionChannelPort(api=receiver_api,enabled=True)
    producer_channel=PostgresPMProducerChannel(producer_api)
    if not receiver_channel.identity().authenticated or not producer_channel.identity().append_allowed:
        raise CustodyUnavailable("ACTUAL_PROVIDER_REGISTRATION_UNQUALIFIED")
    qualified=replace(ports,x9_channel=receiver_channel,
                      producer_channel=producer_channel)
    return build_binding(settings,qualified)
