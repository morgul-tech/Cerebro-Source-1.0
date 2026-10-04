"""THE narrow real-client boundary: nats-py (asyncio) behind the SYNCHRONOUS facade the existing CoreNatsAdapter expects.

Execution model: one private event-loop thread per connection runs nats-py; every facade call is a blocking
`run_coroutine_threadsafe(...).result(timeout)` from the caller's thread. Received frames never run the (blocking)
receiver on the loop: they are handed to the bounded Dispatcher. This is the only module that imports nats, asyncio
or ssl. The offline core and its guards are untouched.

UNKNOWN_SEND safety (what the library is NOT allowed to do for us):
  * SEND/PROBE connections use allow_reconnect=False and pending_size=0. Nothing is buffered across a reconnect and
    there is no reconnect path that could re-write an earlier, ambiguous publish. A dropped connection stays dropped.
  * publish() refuses (NotSentError = PROVEN not written) when the connection is not currently CONNECTED, and maps the
    client library's own pre-write refusals (closed/draining/buffer-limit/max-payload/bad-subject) to the same proof.
    Any other failure, a publish timeout, or a failed flush is NOT a proof => the adapter reports UNKNOWN_SEND and the
    ledger forbids a second publish of that event.
  * Only the two registered literal subjects may be published or subscribed. No wildcard, no request/reply, no JetStream.
LISTEN connections may reconnect (they only re-subscribe) and can never publish.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import ssl
import threading
from importlib import metadata
from typing import Any, Callable

from . import _bootstrap  # noqa: F401
from signalvev_sensing.model import SUBJECT_MESSAGE_TYPE
from signalvev_sensing.transport import NotSentError

from .config import ClientConfig
from .dispatch import Dispatcher, Handler
from .errors import BindingError, ConnectError

SEND, LISTEN, PROBE = "SEND", "LISTEN", "PROBE"
ROLES = frozenset({SEND, LISTEN, PROBE})
ALLOWED_SUBJECTS = frozenset(SUBJECT_MESSAGE_TYPE)
_PRE_SEND_ERROR_NAMES = frozenset({"ConnectionClosedError", "ConnectionDrainingError", "OutboundBufferLimitError",
                                   "MaxPayloadError", "BadSubjectError"})


def _is_pre_send_error(exc: BaseException) -> bool:
    return type(exc).__name__ in _PRE_SEND_ERROR_NAMES and type(exc).__module__.startswith("nats")


def nats_py_version() -> str | None:
    try:
        return metadata.version("nats-py")
    except metadata.PackageNotFoundError:
        return None


def _import_nats_connect() -> Callable[..., Any]:
    try:
        import nats
    except ImportError:
        raise BindingError("NATS_PY_NOT_INSTALLED", "the real binding needs the 'nats-py' package") from None
    return nats.connect


def build_tls_context(cfg: ClientConfig) -> ssl.SSLContext | None:
    if not cfg.tls_requested:
        return None
    ctx = ssl.create_default_context(cafile=str(cfg.tls_ca_file) if cfg.tls_ca_file else None)
    if cfg.tls_cert_file is not None and cfg.tls_key_file is not None:
        ctx.load_cert_chain(str(cfg.tls_cert_file), str(cfg.tls_key_file))
    return ctx


def build_connect_kwargs(cfg: ClientConfig, role: str, callbacks: dict[str, Callable[..., Any]] | None = None) -> dict[str, Any]:
    """Pure mapping config+role -> nats.connect(**kwargs). Secrets appear only as FILE PATHS handed to the library."""
    if role not in ROLES:
        raise BindingError("ROLE_INVALID", role)
    kwargs: dict[str, Any] = {
        "servers": [cfg.server],
        "name": f"signalvev-client:{cfg.node_id}:{role.lower()}",
        "connect_timeout": cfg.connect_timeout,
        "allow_reconnect": role == LISTEN,
        "max_reconnect_attempts": cfg.max_reconnect_attempts if role == LISTEN else 0,
        "reconnect_time_wait": cfg.reconnect_wait,
        "pending_size": 0,                     # no application publish is ever buffered by the library
        "drain_timeout": max(1, int(cfg.flush_timeout) + 3),
    }
    tls = build_tls_context(cfg)
    if tls is not None:
        kwargs["tls"] = tls
        if cfg.tls_hostname:
            kwargs["tls_hostname"] = cfg.tls_hostname
    if cfg.credentials_file is not None:
        kwargs["user_credentials"] = str(cfg.credentials_file)
    kwargs.update(callbacks or {})
    return kwargs


class NatsPyConnection:
    """Synchronous facade over one nats-py connection. Satisfies the client protocol of CoreNatsAdapter
    (publish / flush / subscribe) and adds connect / close / status."""

    def __init__(self, cfg: ClientConfig, *, role: str, dispatcher: Dispatcher | None = None,
                 connect_fn: Callable[..., Any] | None = None) -> None:
        if role not in ROLES:
            raise BindingError("ROLE_INVALID", role)
        self._cfg, self._role, self._dispatcher, self._connect_fn = cfg, role, dispatcher, connect_fn
        self._nc: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._opened = False
        self._closed = False
        self._counters = {"publish_calls": 0, "publish_refused_not_written": 0, "publish_errors": 0, "flush_errors": 0,
                          "disconnects": 0, "reconnects": 0, "async_errors": 0}
        self._last_error: str | None = None
        self.loop_thread_id: int | None = None

    # ------------------------------------------------------------------ loop plumbing
    def _loop_main(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        self.loop_thread_id = threading.get_ident()
        loop.run_forever()
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def _start_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop_main, name="signalvev-nats-loop", daemon=False)
        self._thread.start()

    def _stop_loop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(10.0)
        self._loop = self._thread = None

    def _run(self, coro: Any, timeout: float) -> Any:
        if self._loop is None:
            coro.close()
            raise BindingError("NOT_CONNECTED")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise

    # ------------------------------------------------------------------ redaction / bookkeeping
    def _redact(self, exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}"[:200]
        for secret_path in (self._cfg.credentials_file, self._cfg.tls_ca_file, self._cfg.tls_cert_file, self._cfg.tls_key_file):
            if secret_path is not None:
                text = text.replace(str(secret_path), "<path>")
        return text

    def _note_error(self, exc: BaseException) -> None:
        with self._lock:
            self._last_error = self._redact(exc)

    def _bump(self, key: str) -> None:
        with self._lock:
            self._counters[key] += 1

    def _callbacks(self) -> dict[str, Callable[..., Any]]:
        async def error_cb(exc: BaseException) -> None:
            self._bump("async_errors")
            self._note_error(exc)

        async def disconnected_cb() -> None:
            self._bump("disconnects")

        async def reconnected_cb() -> None:
            self._bump("reconnects")

        async def closed_cb() -> None:
            return None

        return {"error_cb": error_cb, "disconnected_cb": disconnected_cb, "reconnected_cb": reconnected_cb,
                "closed_cb": closed_cb}

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        if self._opened:
            raise BindingError("ALREADY_OPENED", "a connection object is single-use; create a new one")
        self._opened = True
        connect_fn = self._connect_fn or _import_nats_connect()
        if self._connect_fn is None and self._cfg.credentials_file is not None and importlib.util.find_spec("nkeys") is None:
            self._closed = True
            raise BindingError("NKEYS_NOT_INSTALLED",
                               "credentials_file needs the 'nkeys' package: pip install \"signalvev-client[credentials]\"")
        kwargs = build_connect_kwargs(self._cfg, self._role, self._callbacks())
        self._start_loop()
        try:
            self._nc = self._run(connect_fn(**kwargs), self._cfg.connect_timeout + 2.0)
        except BaseException as exc:  # noqa: BLE001
            self._note_error(exc)
            self._stop_loop()
            self._closed = True
            if not isinstance(exc, Exception):       # KeyboardInterrupt / SystemExit: clean up, then propagate
                raise
            raise ConnectError("CONNECT_FAILED", self._redact(exc)) from None

    def close(self) -> None:
        """Idempotent. LISTEN drains (finishes already-received frames), others close. Never raises."""
        if self._closed:
            return
        self._closed = True
        nc = self._nc
        if nc is not None and self._loop is not None:
            try:
                closer = nc.drain() if self._role == LISTEN and not nc.is_closed else nc.close()
                self._run(closer, self._cfg.flush_timeout + 8.0)
            except BaseException as exc:  # noqa: BLE001 - shutting down: record, never raise
                self._note_error(exc)
        self._stop_loop()

    # ------------------------------------------------------------------ facade used by CoreNatsAdapter
    def publish(self, subject: str, data: bytes) -> None:
        if self._role != SEND:
            raise NotSentError("ROLE_NOT_SEND")
        if subject not in ALLOWED_SUBJECTS or type(data) is not bytes:
            raise NotSentError("SUBJECT_OR_PAYLOAD_NOT_ALLOWED")
        nc = self._nc
        if nc is None or self._closed or nc.is_closed or not nc.is_connected:
            self._bump("publish_refused_not_written")
            raise NotSentError("NOT_CONNECTED")
        self._bump("publish_calls")
        try:
            self._run(nc.publish(subject, data), self._cfg.flush_timeout + 1.0)
        except NotSentError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if _is_pre_send_error(exc):
                self._bump("publish_refused_not_written")
                raise NotSentError("CLIENT_REFUSED_BEFORE_WRITE") from None
            self._bump("publish_errors")
            self._note_error(exc)
            raise                                   # not provably unsent => CoreNatsAdapter reports UNKNOWN_SEND

    def flush(self, timeout: float) -> None:
        nc = self._nc
        if nc is None or self._closed:
            raise BindingError("NOT_CONNECTED")
        try:
            self._run(nc.flush(timeout), timeout + 1.0)
        except BaseException as exc:  # noqa: BLE001
            self._bump("flush_errors")
            self._note_error(exc)
            raise

    def subscribe(self, subject: str, handler: Handler) -> None:
        if self._role != LISTEN:
            raise BindingError("ROLE_NOT_LISTEN")
        if subject not in ALLOWED_SUBJECTS:
            raise BindingError("SUBJECT_NOT_ALLOWED", "only the two registered literal subjects")
        if self._nc is None or self._dispatcher is None:
            raise BindingError("NOT_CONNECTED")
        dispatcher = self._dispatcher

        async def _on_message(msg: Any) -> None:
            dispatcher.submit(handler, bytes(msg.data))      # never runs the receiver on the event loop

        self._run(self._nc.subscribe(subject, cb=_on_message), self._cfg.connect_timeout)

    # ------------------------------------------------------------------ honest status
    def state(self) -> str:
        nc = self._nc
        if nc is None:
            return "CLOSED" if self._closed else "NEW"
        if self._closed or nc.is_closed:
            return "CLOSED"
        if getattr(nc, "is_reconnecting", False):
            return "RECONNECTING"
        if nc.is_connected:
            return "CONNECTED"
        return "DISCONNECTED"

    def status(self) -> dict[str, Any]:
        nc = self._nc
        url = getattr(nc, "connected_url", None) if nc is not None else None
        version = getattr(nc, "connected_server_version", None) if nc is not None else None
        with self._lock:
            counters, last_error = dict(self._counters), self._last_error
        return {"role": self._role, "state": self.state(), "server": getattr(url, "netloc", None) or None,
                "server_version": str(version) if version is not None else None, "last_error": last_error,
                "counters": counters, "nats_py_version": nats_py_version()}
