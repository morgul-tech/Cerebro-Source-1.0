"""Real nats-py JetStream binding (publisher with PubAck + Nats-Msg-Id, one durable pull consumer, explicit ACK).

The only module that imports nats/asyncio. One private event-loop thread; every call is a bounded blocking
``run_coroutine_threadsafe(...).result(timeout)``. The Signalvev Core NATS client is not touched or widened: this is
a separate opt-in D1 binding with its own literal subject, stream and consumer.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from typing import Any

from .transport import AckUnconfirmed, CapacityRejected, LookupUnavailable, PubAckInfo, PublishUnknown, RawDelivery

MAX_DELIVERIES_ADVISORY = "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.{stream}.{consumer}"
_ROLE_INBOX_PREFIX = {'publisher': b'_INBOX.CEREBRO_BK11_PUBLISHER',
                      'receiver': b'_INBOX.CEREBRO_BK11_RECEIVER'}
_INSTALLER_INBOX_PREFIX = b'_INBOX.CEREBRO_BK11_INSTALLER'
_CAPACITY_CODES = {10077, 10078, 10054}    # max messages / max bytes / message size exceeds maximum


class NatsJetStreamTransport:
    def __init__(self, server_url: str, *, connect_timeout: float = 3.0, call_timeout: float = 10.0,
                 name: str = "d1js", credentials_path: str | None = None,
                 runtime_role: str | None = None, installer_inbox: bool = False) -> None:
        if runtime_role not in (None, "publisher", "receiver") or (runtime_role is not None and not credentials_path):
            raise ValueError("PRODUCTION_ROLE_AND_CUSTODY_CREDENTIAL_REQUIRED")
        if type(installer_inbox) is not bool or (installer_inbox and (runtime_role is not None or not credentials_path)):
            raise ValueError('EXPLICIT_SEPARATE_INSTALLER_CUSTODY_REQUIRED')
        self._credentials_path, self._runtime_role = credentials_path, runtime_role
        # Existing trusted factory role; no caller-supplied prefix or identity inference.
        self._inbox_prefix = (_ROLE_INBOX_PREFIX[runtime_role] if runtime_role is not None
                              else _INSTALLER_INBOX_PREFIX if installer_inbox else None)
        self.server_url, self.connect_timeout, self.call_timeout, self.name = server_url, connect_timeout, call_timeout, name
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="d1js-nats-loop", daemon=False)
        self._thread.start()
        self.loop_thread_id: int | None = None
        self._nc: Any = None
        self._js: Any = None
        self._psub: Any = None
        self._cfg: Any = None
        self._verified = False
        self._advisories: list[dict[str, Any]] = []
        self.reconnects = 0
        self.disconnects = 0

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self.loop_thread_id = threading.get_ident()
        self._loop.run_forever()

    def _call(self, coro: Any, timeout: float | None = None) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout if timeout is not None else self.call_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise

    # ------------------------------------------------------------------ connect / ensure
    def connect(self) -> "NatsJetStreamTransport":
        import nats

        async def _connect():
            async def disconnected_cb():
                self.disconnects += 1

            async def reconnected_cb():
                self.reconnects += 1

            async def error_cb(exc):
                return None

            private_credentials = {} if self._credentials_path is None else {"user_credentials": self._credentials_path}
            if self._inbox_prefix is not None:
                private_credentials['inbox_prefix'] = self._inbox_prefix
            self._nc = await nats.connect(servers=[self.server_url], name=self.name,
                                          connect_timeout=self.connect_timeout, allow_reconnect=True,
                                          max_reconnect_attempts=60, reconnect_time_wait=0.25,
                                          disconnected_cb=disconnected_cb, reconnected_cb=reconnected_cb,
                                          error_cb=error_cb, **private_credentials)
            self._js = self._nc.jetstream()
        self._call(_connect(), self.connect_timeout + 5)
        return self

    def ensure(self, config: Any) -> dict[str, Any]:
        """Create (or verify) exactly the bounded stream and the one durable pull consumer."""
        if self._runtime_role is not None:
            raise ValueError("INSTALLATION_CREATE_RESOURCE_DENIED_TO_RUNTIME")
        from nats.js.api import (AckPolicy, ConsumerConfig, DeliverPolicy, DiscardPolicy, RetentionPolicy,
                                 StorageType, StreamConfig)
        from nats.js.errors import NotFoundError
        self._cfg = config
        self._psub = None
        self._verified = False

        def verify(actual: Any, expected: Any, fields: tuple[str, ...], kind: str) -> None:
            for field in fields:
                got, want = getattr(actual, field, None), getattr(expected, field, None)
                if field in ("max_age", "duplicate_window", "ack_wait"):
                    got = None if got is None else round(float(got) * 1_000_000_000)
                    want = None if want is None else round(float(want) * 1_000_000_000)
                if got != want:
                    raise ValueError(f"{kind}_CONFIG_MISMATCH:{field}")

        async def _ensure():
            scfg = StreamConfig(name=config.stream, subjects=[config.subject], storage=StorageType.FILE,
                                num_replicas=config.replicas, retention=RetentionPolicy.LIMITS,
                                discard=DiscardPolicy.NEW, max_age=config.max_age_s, max_msgs=config.max_msgs,
                                max_bytes=config.max_bytes, max_msg_size=config.max_msg_size,
                                duplicate_window=config.duplicate_window_s)
            try:
                info = await self._js.stream_info(config.stream)
            except NotFoundError:
                info = await self._js.add_stream(scfg)
            verify(info.config, scfg,
                   ("name", "subjects", "storage", "num_replicas", "retention", "discard",
                    "max_age", "max_msgs", "max_bytes", "max_msg_size", "duplicate_window"),
                   "STREAM")
            ccfg = ConsumerConfig(durable_name=config.consumer, ack_policy=AckPolicy.EXPLICIT,
                                  deliver_policy=DeliverPolicy.ALL, max_ack_pending=config.max_ack_pending,
                                  ack_wait=config.ack_wait_s, max_deliver=config.max_deliver,
                                  filter_subject=config.subject)
            try:
                cinfo = await self._js.consumer_info(config.stream, config.consumer)
            except NotFoundError:
                cinfo = await self._js.add_consumer(config.stream, ccfg)
            verify(cinfo.config, ccfg,
                   ("durable_name", "ack_policy", "deliver_policy", "max_ack_pending",
                    "ack_wait", "max_deliver", "filter_subject"), "CONSUMER")
            if getattr(cinfo.config, "deliver_subject", None) or getattr(cinfo.config, "deliver_group", None):
                raise ValueError("CONSUMER_CONFIG_MISMATCH:push_delivery")
            self._psub = await self._js.pull_subscribe_bind(config.consumer, config.stream)

            async def advisory(msg):
                import json as _json
                try:
                    self._advisories.append(_json.loads(msg.data))
                except ValueError:
                    pass
            await self._nc.subscribe(MAX_DELIVERIES_ADVISORY.format(stream=config.stream, consumer=config.consumer),
                                     cb=advisory)
            c = info.config
            cc = cinfo.config
            return {"stream": {"name": c.name, "subjects": c.subjects, "storage": str(c.storage),
                               "retention": str(c.retention), "discard": str(c.discard), "max_age": c.max_age,
                               "max_msgs": c.max_msgs, "max_bytes": c.max_bytes, "max_msg_size": c.max_msg_size,
                               "duplicate_window": c.duplicate_window, "num_replicas": c.num_replicas},
                    "consumer": {"durable_name": cc.durable_name, "ack_policy": str(cc.ack_policy),
                                 "deliver_policy": str(cc.deliver_policy), "max_ack_pending": cc.max_ack_pending,
                                 "ack_wait": cc.ack_wait, "max_deliver": cc.max_deliver,
                                 "filter_subject": cc.filter_subject},
                    "server_version": getattr(self._nc, "connected_server_version", None) and
                    str(self._nc.connected_server_version)}
        result = self._call(_ensure())
        self._verified = True
        return result

    # ------------------------------------------------------------------ publish
    def verify_runtime(self, config: Any) -> dict[str, Any]:
        """Verify existing resources using only this runtime role's exact INFO rights.

        No ADD/UPDATE/subscribe-advisory calls; a missing resource fails closed.
        Receiver uses consumer INFO/NEXT/ACK only, not publisher stream MSG.GET.
        """
        if self._runtime_role not in ("publisher", "receiver"):
            raise ValueError("EXPLICIT_RUNTIME_ROLE_REQUIRED")
        config.validate()
        self._cfg, self._verified, self._psub = config, False, None
        async def verify():
            if self._runtime_role == "publisher":
                info = await self._js.stream_info(config.stream)
                expected = {"name":config.stream,"subjects":[config.subject],"storage":"file",
                            "num_replicas":config.replicas,"retention":"limits","discard":"new",
                            "max_age":config.max_age_s,"max_msgs":config.max_msgs,"max_bytes":config.max_bytes,
                            "max_msg_size":config.max_msg_size,"duplicate_window":config.duplicate_window_s}
            else:
                info = await self._js.consumer_info(config.stream,config.consumer)
                expected = {"durable_name":config.consumer,"ack_policy":"explicit","deliver_policy":"all",
                            "max_ack_pending":config.max_ack_pending,"ack_wait":config.ack_wait_s,
                            "max_deliver":config.max_deliver,"filter_subject":config.subject}
                if getattr(info.config,"deliver_subject",None) or getattr(info.config,"deliver_group",None):
                    raise ValueError("RUNTIME_PUSH_CONSUMER_REFUSED")
            for name,want in expected.items():
                got = getattr(info.config,name,None)
                if hasattr(got,"value"): got = got.value
                if got != want: raise ValueError("RUNTIME_RESOURCE_CONFIG_DRIFT:"+name)
            if self._runtime_role == "receiver":
                self._psub = await self._js.pull_subscribe_bind(config.consumer,config.stream)
            return {"role":self._runtime_role,"resource_verified":True,"resource_created":False}
        result = self._call(verify())
        self._verified = True
        return result

    def publish(self, subject: str, payload: bytes, msg_id: str, timeout: float) -> PubAckInfo:
        if self._runtime_role == "receiver":
            raise PublishUnknown("RECEIVER_APP_PUBLISH_DENIED")
        if not self._verified or subject != self._cfg.subject:
            raise PublishUnknown("UNVERIFIED_STREAM_CONTRACT")
        from nats.js.errors import APIError

        async def _pub():
            return await self._js.publish(subject, payload, timeout=timeout, headers={"Nats-Msg-Id": msg_id})
        try:
            ack = self._call(_pub(), timeout + 2)
        except APIError as exc:
            if getattr(exc, "err_code", None) in _CAPACITY_CODES:
                raise CapacityRejected(f"{exc.err_code}:{exc.description}") from None
            raise PublishUnknown(f"api:{getattr(exc, 'err_code', None)}") from None
        except Exception as exc:  # timeout / no responders / connection: outcome unknown
            raise PublishUnknown(type(exc).__name__) from None
        return PubAckInfo(ack.stream, ack.seq, bool(ack.duplicate))

    # ------------------------------------------------------------------ pull consumer
    def fetch_one(self, timeout: float) -> RawDelivery | None:
        if self._runtime_role == "publisher":
            raise LookupUnavailable("PUBLISHER_RECEIVE_DENIED")
        if not self._verified or self._psub is None:
            raise LookupUnavailable("UNVERIFIED_CONSUMER_CONTRACT")
        async def _fetch():
            try:
                msgs = await self._psub.fetch(1, timeout=timeout)
            except asyncio.TimeoutError:
                return None
            except Exception as exc:
                if type(exc).__name__ == "TimeoutError":
                    return None
                raise
            return msgs[0] if msgs else None
        msg = self._call(_fetch(), timeout + 5)
        if msg is None:
            return None
        md = msg.metadata
        published = md.timestamp.timestamp() if getattr(md, "timestamp", None) else time.time()
        return RawDelivery(bytes(msg.data), dict(msg.headers or {}), md.stream, md.sequence.stream,
                           md.sequence.consumer, md.num_delivered, published, handle=msg)

    def ack(self, delivery: RawDelivery) -> None:
        if self._runtime_role == "publisher":
            raise AckUnconfirmed("PUBLISHER_ACK_DENIED")
        try:
            self._call(delivery.handle.ack_sync(timeout=2.0), 5)
        except Exception as exc:
            raise AckUnconfirmed(type(exc).__name__) from None

    def nak(self, delivery: RawDelivery, delay: float) -> None:
        if self._runtime_role == "publisher":
            raise AckUnconfirmed("PUBLISHER_NAK_DENIED")
        self._call(delivery.handle.nak(delay=delay if delay > 0 else None), 5)

    # ------------------------------------------------------------------ bounded lookup / state
    def stream_state(self, *, timeout: float | None = None) -> dict[str, int]:
        if self._runtime_role == "receiver":
            raise LookupUnavailable("RECEIVER_STREAM_LOOKUP_DENIED")
        try:
            limit = 5.0 if timeout is None else min(5.0, timeout)
            if limit <= 0:
                raise LookupUnavailable("elapsed budget exhausted")
            info = self._call(asyncio.wait_for(self._js.stream_info(self._cfg.stream), limit), limit)
        except Exception as exc:
            raise LookupUnavailable(type(exc).__name__) from None
        st = info.state
        return {"messages": st.messages, "first_seq": st.first_seq, "last_seq": st.last_seq, "bytes": st.bytes}

    def get_msg(self, seq: int, *, timeout: float | None = None) -> tuple[bytes, dict[str, str]] | None:
        if self._runtime_role == "receiver":
            raise LookupUnavailable("RECEIVER_STREAM_LOOKUP_DENIED")
        from nats.js.errors import NotFoundError

        async def _get():
            try:
                return await self._js.get_msg(self._cfg.stream, seq)
            except NotFoundError:
                return None
        try:
            limit = 5.0 if timeout is None else min(5.0, timeout)
            if limit <= 0:
                raise LookupUnavailable("elapsed budget exhausted")
            m = self._call(asyncio.wait_for(_get(), limit), limit)
        except Exception as exc:
            raise LookupUnavailable(type(exc).__name__) from None
        return None if m is None else (bytes(m.data or b""), dict(m.headers or {}))

    def consumer_state(self) -> dict[str, int]:
        if self._runtime_role == "publisher":
            raise LookupUnavailable("PUBLISHER_CONSUMER_LOOKUP_DENIED")
        info = self._call(self._js.consumer_info(self._cfg.stream, self._cfg.consumer), 5)
        return {"num_pending": info.num_pending, "num_ack_pending": info.num_ack_pending,
                "num_redelivered": info.num_redelivered}

    def poll_advisories(self) -> list[dict[str, Any]]:
        async def _take():
            out, self._advisories = self._advisories, []
            return out
        return self._call(_take(), 5)

    def fixture_reset_stream(self, stream: str) -> None:
        if self._runtime_role is not None:
            raise ValueError("RUNTIME_RESOURCE_DELETE_DENIED")
        """FIXTURE ONLY: delete the disposable test stream so a test can recreate it with its own bounded limits."""
        async def _del():
            try:
                await self._js.delete_stream(stream)
            except Exception:
                pass
        self._call(_del(), 10)

    def server_version(self) -> str | None:
        v = getattr(self._nc, "connected_server_version", None)
        return str(v) if v is not None else None

    def close(self) -> None:
        async def _close():
            if self._nc is not None and not self._nc.is_closed:
                await self._nc.close()
        try:
            self._call(_close(), 10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
