"""Default-off read-only package verifier in the existing control host.

The constructor-bound owner reader must freshly read the real authoritative
grant and revocation store. No request field or local digest authenticates it.
The port decides permission for these exact bytes only; it performs no build.
"""
import math
import re
import time


class PackageBuildPortError(ValueError):
    pass


class PackageBuildVerifierPort:
    def __init__(self, *, provider, grant_reader, clock=time.time):
        if not provider or not callable(grant_reader):
            raise PackageBuildPortError("OWNER_GRANT_READER_REQUIRED")
        self.provider, self.reader, self.clock = provider, grant_reader, clock

    def verify(self, identity, request):
        identity.validate()
        if not isinstance(request, dict) or set(request) != {
                "schema", "grant_ref", "grant_revision", "nonce", "binding", "request_sha256"}:
            raise PackageBuildPortError("EXACT_REQUEST_REQUIRED")
        if (request["schema"] != "cerebro-package-build-verification/v1"
                or not isinstance(request["binding"], dict)
                or request["binding"].get("effect") != "RUN_ONLY_PACKAGE_BUILD"
                or any(not isinstance(request[k], str) or not request[k].strip()
                       for k in ("grant_ref", "grant_revision", "nonce", "request_sha256"))):
            raise PackageBuildPortError("REQUEST_INVALID")
        fields = {"effect", "claim", "packet", "queue", "actor", "source_base", "current_main_commit",
                  "candidate_commit", "candidate_tree", "target_bytes_sha256", "qualification_report_sha256"}
        if (set(request["binding"]) != fields
                or any(not isinstance(v, str) or not v.strip() for v in request["binding"].values())
                or not re.fullmatch(r"[0-9a-f]{64}", request["request_sha256"])
                or not re.fullmatch(r"[0-9a-f]{48}", request["nonce"])):
            raise PackageBuildPortError("EXACT_EXECUTION_BINDING_REQUIRED")
        grant = self.reader(request["grant_ref"], identity)
        now = self.clock()
        if not isinstance(grant, dict):
            raise PackageBuildPortError("OWNER_GRANT_ABSENT")
        expiry = grant.get("expires_at")
        if (grant.get("currentness") != "CURRENT" or grant.get("revoked") is not False
                or grant.get("result") != "PASS" or grant.get("revision") != request["grant_revision"]
                or type(expiry) not in (int, float) or not math.isfinite(expiry)
                or not math.isfinite(now) or expiry <= now):
            raise PackageBuildPortError("OWNER_GRANT_STALE_OR_REVOKED")
        expected = {"tenant_ref": identity.tenant_ref, "workspace_ref": identity.workspace_ref,
                    "principal_ref": identity.principal_ref, "binding": request["binding"],
                    "request_sha256": request["request_sha256"]}
        if any(grant.get(k) != v for k, v in expected.items()):
            raise PackageBuildPortError("OWNER_GRANT_SCOPE_MISMATCH")
        return {**request, "result": "PASS", "provider": self.provider,
                "currentness": "CURRENT", "revoked": False,
                "issued_at": now, "expires_at": min(expiry, now + 15)}
