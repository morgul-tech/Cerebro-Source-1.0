"""Explicit D1-selected production R1; fixture D1Config is unchanged.

Configuration is policy data, never issuer/assignment/lease authority.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from .config import D1Config, D1ConfigError

PROFILE = "BK11_CONTEXT_EDGE_BACKEND_PRODUCTION/v1"
POLICY = {"subject":"cerebro.v1.pm.owner.d1.pointer","stream":"CEREBRO_BK11_PM_OWNER_D1_V1",
 "consumer":"CEREBRO_BK11_PM_X9_BACKEND_V1","storage":"file","replicas":1,"retention":"limits","discard":"new",
 "max_age_s":86400.0,"max_msgs":4096,"max_bytes":16777216,"max_msg_size":4096,"duplicate_window_s":120.0,
 "ack_policy":"explicit","deliver_policy":"all","max_ack_pending":1,"max_deliver":5,"ack_wait_s":30.0,
 "pull_batch":1,"pull_timeout_s":1.0,"hold_nak_delay_s":5.0,"pointer_ttl_s":120,"drain_max_items":8,
 "lookup_bound":256,"lookup_time_budget_s":10.0,"publish_timeout_s":2.0,"claim_stale_s":10.0}

@dataclass(frozen=True)
class ProductionD1Config(D1Config):
    profile: str = PROFILE
    subject: str = POLICY["subject"]
    stream: str = POLICY["stream"]
    consumer: str = POLICY["consumer"]
    max_age_s: float = 86400.0
    max_msgs: int = 4096
    max_bytes: int = 16777216
    max_deliver: int = 5
    ack_wait_s: float = 30.0
    hold_nak_delay_s: float = 5.0
    claim_stale_s: float = 10.0

    def validate(self) -> "ProductionD1Config":
        if self.profile != PROFILE or type(self.enabled) is not bool:
            raise D1ConfigError("EXPLICIT_PRODUCTION_PROFILE_REQUIRED")
        for name, expected in POLICY.items():
            got = getattr(self, name)
            if type(got) is bool or got != expected:
                raise D1ConfigError("PRODUCTION_SELECTED_POLICY_MISMATCH:"+name)
        if self.server_url:
            try:
                url = urlsplit(self.server_url)
                if url.scheme not in ("nats","tls") or not url.hostname or not url.port or url.username or url.password \
                        or url.path or url.query or url.fragment:
                    raise ValueError()
            except (ValueError,TypeError):
                raise D1ConfigError("NONSECRET_EXPLICIT_BROKER_ENDPOINT_REQUIRED") from None
        elif self.enabled:
            raise D1ConfigError("PRODUCTION_BROKER_ENDPOINT_REQUIRED")
        return self

    def public_policy(self) -> dict:
        return {"profile":PROFILE,"enabled":self.enabled,"transport":dict(POLICY),
                "machine_recovery_cycle_seconds":60,"server_file_budget_bytes":67108864,
                "account_file_budget_bytes":33554432,"max_streams":1,"max_consumers":1,
                "authority":"SELECTED_POLICY_NOT_SERVICE_ADMISSION","measured_SLA":False}
