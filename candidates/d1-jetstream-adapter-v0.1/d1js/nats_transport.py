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
_CAPACITY_CODES = {10077, 10078, 10054}    # max messages / max bytes / message size exceeds maximum


class NatsJetStreamTransport:
    def __init__(self, server_url: str, *, connect_timeout: float = 3.0, call_timeout: float = 10.0,
                 name: str = "d1js") -> None:
        self.server_url, self.connect_timeout, self.call_timeout, self.name = server_url, connect_timeout, call_timeout, name
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="d1js-nats-loop", daemon=False)
        self._thread.start()
        self.loop_thread_id: int | None = None
        self._nc: Any = None
        self._js: Any = None
        self._psub: Any = None
        self._cfg: Any = None
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

            self._nc = await nats.connect(servers=[self.server_url], name=self.name,
                                          connect_timeout=self.connect_timeout, allow_reconnect=True,
                                          max_reconnect_attempts=60, reconnect_time_wait=0.25,
                                          disconnected_cb=disconnected_cb, reconnected_cb=reconnected_cb,
                                          error_cb=error_cb)
            self._js = self._nc.jetstream()
        self._call(_connect(), self.connect_timeout + 5)
        return self

    def ensure(self, config: Any) -> dict[str, Any]:
        """Create (or verify) exactly the bounded stream and the one durable pull consumer."""
        from nats.js.api import (AckPolicy, ConsumerConfig, DeliverPolicy, DiscardPolicy, RetentionPolicy,
                                 StorageType, StreamConfig)
        self._cfg = config

        async def _ensure():
            scfg = StreamConfig(name=config.stream, subjects=[config.subject], storage=StorageType.FILE,
                                num_replicas=config.replicas, retention=RetentionPolicy.LIMITS,
                                discard=DiscardPolicy.NEW, max_age=config.max_age_s, max_msgs=config.max_msgs,
                                max_bytes=config.max_bytes, max_msg_size=config.max_msg_size,
                                duplicate_window=config.duplicate_window_s)
            try:
                info = await self._js.stream_info(config.stream)
            except Exception:
                info = None
            if info is None:
                info = await self._js.add_stream(scfg)
            ccfg = ConsumerConfig(durable_name=config.consumer, ack_policy=AckPolicy.EXPLICIT,
                                  deliver_policy=DeliverPolicy.ALL, max_ack_pending=config.max_ack_pending,
                                  ack_wait=config.ack_wait_s, max_deliver=config.max_deliver,
                                  filter_subject=config.subject)
            try:
                cinfo = await self._js.consumer_info(config.stream, config.consumer)
            except Exception:
                cinfo = await self._js.add_consumer(config.stream, ccfg)
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
        return self._call(_ensure())

    # ------------------------------------------------------------------ publish
    def publish(self, subject: str, payload: bytes, msg_id: str, timeout: float) -> PubAckInfo:
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
        try:
            self._call(delivery.handle.ack_sync(timeout=2.0), 5)
        except Exception as exc:
            raise AckUnconfirmed(type(exc).__name__) from None

    def nak(self, delivery: RawDelivery, delay: float) -> None:
        self._call(delivery.handle.nak(delay=delay if delay > 0 else None), 5)

    # ------------------------------------------------------------------ bounded lookup / state
    def stream_state(self, timeout: float | None = None) -> dict[str, int]:
        wait = 5 if timeout is None else min(5, timeout)
        if wait <= 0:
            raise LookupUnavailable("deadline exhausted before stream state")
        try:
            info = self._call(self._js.stream_info(self._cfg.stream), wait)
        except Exception as exc:
            raise LookupUnavailable(type(exc).__name__) from None
        st = info.state
        return {"messages": st.messages, "first_seq": st.first_seq, "last_seq": st.last_seq, "bytes": st.bytes}

    def get_msg(self, seq: int, timeout: float | None = None) -> tuple[bytes, dict[str, str]] | None:
        from nats.js.errors import NotFoundError

        wait = 5 if timeout is None else min(5, timeout)
        if wait <= 0:
            raise LookupUnavailable("deadline exhausted before get_msg")

        async def _get():
            try:
                return await self._js.get_msg(self._cfg.stream, seq)
            except NotFoundError:
                return None
        try:
            m = self._call(_get(), wait)
        except Exception as exc:
            raise LookupUnavailable(type(exc).__name__) from None
        return None if m is None else (bytes(m.data or b""), dict(m.headers or {}))

    def consumer_state(self) -> dict[str, int]:
        info = self._call(self._js.consumer_info(self._cfg.stream, self._cfg.consumer), 5)
        return {"num_pending": info.num_pending, "num_ack_pending": info.num_ack_pending,
                "num_redelivered": info.num_redelivered}

    def poll_advisories(self) -> list[dict[str, Any]]:
        async def _take():
            out, self._advisories = self._advisories, []
            return out
        return self._call(_take(), 5)

    def fixture_reset_stream(self, stream: str) -> None:
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
