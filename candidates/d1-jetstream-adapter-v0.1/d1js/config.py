"""Opt-in D1 configuration. Default OFF. One literal subject, one stream, one durable pull consumer.

Fixture defaults are the assignment's bounded envelope; they are NOT approved production limits.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .pointer import SUBJECT

STREAM = "X2_D1_TEST"
CONSUMER = "X2_D1_RECEIVER"


class D1ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class D1Config:
    enabled: bool = False                 # default OFF: nothing connects, publishes or pulls
    server_url: str = ""                  # loopback fixture only in this candidate
    subject: str = SUBJECT
    stream: str = STREAM
    consumer: str = CONSUMER
    # stream envelope (fixture defaults)
    storage: str = "file"
    replicas: int = 1
    retention: str = "limits"
    discard: str = "new"
    max_age_s: float = 600.0
    max_msgs: int = 128
    max_bytes: int = 262_144
    max_msg_size: int = 4096
    duplicate_window_s: float = 120.0
    # consumer envelope
    ack_policy: str = "explicit"
    deliver_policy: str = "all"
    max_ack_pending: int = 1
    ack_wait_s: float = 1.0
    max_deliver: int = 3
    pull_batch: int = 1
    pull_timeout_s: float = 1.0
    hold_nak_delay_s: float = 0.2         # NAK delay for HOLD (bounded; counts as a delivery)
    # pointers and drain
    pointer_ttl_s: int = 120
    drain_max_items: int = 8              # bounded callable drain, no resident loop
    lookup_bound: int = 256               # max stream span AND max get_msg calls per reconciliation call
    lookup_time_budget_s: float = 10.0    # max elapsed lookup work per reconciliation call
    publish_timeout_s: float = 2.0
    claim_stale_s: float = 5.0            # SEND_CLAIMED younger than this may still be in flight (> timeout+2s)

    def validate(self) -> "D1Config":
        if self.subject != SUBJECT or self.stream != STREAM or self.consumer != CONSUMER:
            raise D1ConfigError("LITERAL_SUBJECT_STREAM_CONSUMER_REQUIRED")
        if any(c in self.subject for c in "*>"):
            raise D1ConfigError("NO_WILDCARD_SUBJECT")
        if (self.storage, self.retention, self.discard, self.ack_policy, self.deliver_policy) != (
                "file", "limits", "new", "explicit", "all"):
            raise D1ConfigError("FIXED_POLICIES: file/limits/new/explicit/all")
        if self.replicas != 1 or self.max_ack_pending != 1 or self.pull_batch != 1:
            raise D1ConfigError("ONE_REPLICA_ONE_PENDING_BATCH_ONE")
        if not (0 < self.pointer_ttl_s <= 120 and self.pointer_ttl_s <= self.max_age_s):
            raise D1ConfigError("POINTER_TTL_MUST_BE_<=120s_AND_<=MAX_AGE")
        if not (0 < self.duplicate_window_s <= self.max_age_s):
            raise D1ConfigError("DUPLICATE_WINDOW_MUST_BE_<=MAX_AGE")
        for name in ("max_msgs", "max_bytes", "max_msg_size", "max_deliver", "drain_max_items", "lookup_bound"):
            if not 1 <= getattr(self, name) <= 1_000_000:
                raise D1ConfigError(f"BOUND:{name}")
        if not (0 < self.ack_wait_s <= 60 and 0 < self.pull_timeout_s <= 30 and 0 <= self.hold_nak_delay_s <= 60):
            raise D1ConfigError("BOUNDED_TIMEOUTS")
        # a claimed send can stay in flight for publish_timeout_s + the transport call margin (2 s, nats_transport)
        if not (0 < self.publish_timeout_s and self.publish_timeout_s + 2.0 < self.claim_stale_s <= 600
                and 0 < self.lookup_time_budget_s <= 60):
            raise D1ConfigError("CLAIM_STALE_MUST_EXCEED_PUBLISH_TIMEOUT_PLUS_2S; BOUNDED_LOOKUP_TIME")
        if self.enabled and not self.server_url.startswith(("nats://127.0.0.1:", "nats://localhost:")):
            raise D1ConfigError("FIXTURE_IS_LOOPBACK_ONLY")
        return self

    def with_overrides(self, **over: Any) -> "D1Config":
        return replace(self, **over).validate()

    def overrides_vs_defaults(self) -> dict[str, Any]:
        base = asdict(D1Config())
        return {k: v for k, v in asdict(self).items() if base.get(k) != v and k not in ("enabled", "server_url")}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
