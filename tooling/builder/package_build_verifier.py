"""Owner-configured HTTPS binder; request JSON never chooses a verifier or grants authority.

Operator environment: CEREBRO_PACKAGE_VERIFIER_URL (qualified HTTPS route),
CEREBRO_PACKAGE_VERIFIER_TOKEN (OAuth package_build:verify),
CEREBRO_PACKAGE_VERIFIER_PROVIDER, CEREBRO_PACKAGE_VERIFIER_GRANT,
CEREBRO_PACKAGE_VERIFIER_REVISION. All are required; absence disables builds.
The host must read its real owner grant/currentness backend on every call.
"""
import hashlib
import json
import math
import os
import secrets
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler


class VerifierError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise VerifierError("VERIFIER_REDIRECT_REFUSED")


class PackageBuildVerifier:
    def __init__(self, config, *, clock=time.time, transport=None):
        self.config = config
        self.clock = clock
        self.transport = transport or self._post
        parsed = urlsplit(config.get("URL", ""))
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/control/package-build/verify"):
            raise VerifierError("VERIFIER_QUALIFIED_HTTPS_URL_REQUIRED")
        if any(not isinstance(config.get(k), str) or not config[k].strip()
               for k in ("TOKEN", "PROVIDER", "GRANT", "REVISION")):
            raise VerifierError("VERIFIER_OWNER_CONFIGURATION_REQUIRED")
        if any(ord(c) < 33 or ord(c) > 126 for c in config["TOKEN"]):
            raise VerifierError("VERIFIER_TOKEN_INVALID")

    @classmethod
    def from_environment(cls):
        return cls({k: os.environ.get("CEREBRO_PACKAGE_VERIFIER_" + k, "")
                    for k in ("URL", "TOKEN", "PROVIDER", "GRANT", "REVISION")})

    def _post(self, request):
        wire = Request(self.config["URL"], data=canonical(request), method="POST",
                       headers={"Authorization": "Bearer " + self.config["TOKEN"],
                                "Content-Type": "application/json"})
        try:
            with build_opener(NoRedirect(), HTTPSHandler()).open(wire, timeout=10) as response:
                if response.status != 200 or response.headers.get_content_type() != "application/json":
                    raise VerifierError("VERIFIER_RESPONSE_INVALID")
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise VerifierError("VERIFIER_RESPONSE_TOO_LARGE")
                return json.loads(raw)
        except Exception as exc:
            raise VerifierError("VERIFIER_TRANSPORT_REFUSED") from exc

    def __call__(self, declared):
        auth, source, identity = (declared[k] for k in ("authorization", "source", "identity"))
        binding = {"effect": "RUN_ONLY_PACKAGE_BUILD",
                   **{k: auth.get(k) for k in ("claim", "packet", "queue", "actor")},
                   "source_base": source.get("base_commit"),
                   "current_main_commit": source.get("current_main_commit"),
                   "candidate_commit": source.get("candidate_commit"),
                   "candidate_tree": source.get("candidate_tree"),
                   "target_bytes_sha256": identity.get("target_bytes_sha256"),
                   "qualification_report_sha256": auth.get("qualification_report_sha256")}
        if any(not isinstance(v, str) or not v.strip() for v in binding.values()):
            raise VerifierError("VERIFIER_EXACT_BINDING_REQUIRED")
        request = {"schema": "cerebro-package-build-verification/v1",
                   "grant_ref": self.config["GRANT"], "grant_revision": self.config["REVISION"],
                   "nonce": secrets.token_hex(24), "binding": binding,
                   "request_sha256": hashlib.sha256(canonical(declared)).hexdigest()}
        before = self.clock()
        receipt = self.transport(request)
        now = self.clock()
        if not isinstance(receipt, dict):
            raise VerifierError("VERIFIER_RECEIPT_INVALID")
        for key, value in request.items():
            if receipt.get(key) != value:
                raise VerifierError("VERIFIER_REQUEST_BINDING_MISMATCH")
        issued, expires = receipt.get("issued_at"), receipt.get("expires_at")
        if (receipt.get("result") != "PASS" or receipt.get("provider") != self.config["PROVIDER"]
                or receipt.get("currentness") != "CURRENT" or receipt.get("revoked") is not False
                or type(issued) not in (int, float) or type(expires) not in (int, float)
                or not all(math.isfinite(v) for v in (before, now, issued, expires))
                or not before - 1 <= issued <= now + 1 or not now < expires <= issued + 15):
            raise VerifierError("VERIFIER_GRANT_STALE_OR_REVOKED")
        return {**receipt, **binding}
