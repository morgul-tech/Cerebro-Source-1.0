"""Bounded hand-off from the NATS event-loop thread to the (blocking, synchronous) existing receiver.

The receiver may wait on an owner reread, so it must never run on the event loop. Frames are queued (bounded) and
handled in arrival order by ONE worker thread. When the queue is full the frame is DROPPED and counted: D0 hints are
ephemeral and reconstructible, loss is lawful, and a dropped frame is never reported as delivered or read.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable

Handler = Callable[[bytes], Any]
_STOP = object()


class Dispatcher:
    def __init__(self, *, max_queue: int, on_result: Callable[[Any], None] | None = None) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._on_result = on_result
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._accepting = True
        self.counters = {"received": 0, "dispatched": 0, "dropped_overflow": 0, "handler_errors": 0}
        self.worker_thread_id: int | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="signalvev-dispatch", daemon=False)
            self._thread.start()

    def submit(self, handler: Handler, data: bytes) -> bool:
        """Non-blocking. Called from the event-loop thread."""
        with self._lock:
            self.counters["received"] += 1
            if not self._accepting:
                self.counters["dropped_overflow"] += 1
                return False
            try:
                self._q.put_nowait((handler, data))
            except queue.Full:
                self.counters["dropped_overflow"] += 1
                return False
        return True

    def _run(self) -> None:
        self.worker_thread_id = threading.get_ident()
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            handler, data = item
            try:
                result = handler(data)
            except Exception:  # noqa: BLE001 - the receiver contract is "never raises"; count if it ever does
                with self._lock:
                    self.counters["handler_errors"] += 1
                result = None
            with self._lock:
                self.counters["dispatched"] += 1
            if self._on_result is not None and result is not None:
                try:
                    self._on_result(result)
                except Exception:  # noqa: BLE001 - output problems must not kill the worker
                    with self._lock:
                        self.counters["handler_errors"] += 1

    def pending(self) -> int:
        return self._q.qsize()

    def stop(self, *, timeout: float = 10.0) -> bool:
        """Stop accepting, let queued frames finish, join. True when the worker ended within the timeout."""
        with self._lock:
            self._accepting = False
        if self._thread is None:
            return True
        try:
            self._q.put(_STOP, timeout=timeout)    # waits only for room while the worker drains
        except queue.Full:
            return False                           # worker stuck inside a handler (e.g. a hung owner reread)
        self._thread.join(timeout)
        return not self._thread.is_alive()
