"""Default-off original Drive resolver for the existing module:callable(cfg) port.

Factory: signalvev_client.drive_owner_resolver:create_resolver. A1's trusted
host bootstrap must first configure_host(HostBinding(...)). No environment
lookup, assistant connector, fixture fallback or owner sequence in config.
Google OAuth authorized-user/service-account credentials stay in a host-owned
file; only Google token/API origins are allowed. google-auth + requests are
required on the host, not installed by this module. OwnerConfirmationPort is a
separate existing owner-custody port: implementing it is NOT this resolver's
authority. Missing live credential/proof binding refuses.

API basis: developers.google.com/workspace/drive/api/reference/rest/v3/
files/get, revisions/get, about/get; google-auth.readthedocs.io/en/latest/
reference/google.auth.transport.requests.html. This binary Markdown file uses
headRevisionId, not native-Doc export. Drive version is never an owner sequence.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from . import _bootstrap  # noqa: F401 -- installed resource verification
from .config import ClientConfig, RESOLVER_FACTORY
from signalvev_sensing.resolver import ResolveRequest, ResolverResult, ResolverUnavailable

API = "https://www.googleapis.com/drive/v3"
TOKEN_URI = "https://oauth2.googleapis.com/token"
READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FILE_ID = "1h9LsJjulTABrN90s5OLqsiymvNXDDYZh"
REVISION = "0B7z2Cqc7tiqdMmwrWitob0lHbFdXOU9FUC9iT2pUU3NVU3N3PQ"
SOURCE_SHA256 = "45b4ff212e42cf231c965a6aee883a51327b93d51f826c10c37dbad48bc8f5b6"
MAX_SOURCE_BYTES = 65536
MAX_GROUND_CHARS = 4096


def _need(ok: bool, code: str) -> None:
    if not ok:
        raise ResolverUnavailable(code)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DriveScope:
    owner_ref: str
    referent_type: str
    referent_id: str
    pointer_ref: str
    file_id: str = FILE_ID
    revision: str = REVISION
    sha256: str = SOURCE_SHA256
    first_line: int = 1
    line_count: int = 12

    def validate(self) -> None:
        _need(all(isinstance(v, str) and v.strip() and "\n" not in v for v in
                  (self.owner_ref, self.referent_type, self.referent_id, self.pointer_ref)), "SOURCE_SCOPE_INVALID")
        _need(self.file_id == FILE_ID and self.revision == REVISION
              and self.sha256 == SOURCE_SHA256, "ORIGINAL_SOURCE_SCOPE_MISMATCH")
        _need(type(self.first_line) is int and self.first_line >= 1
              and type(self.line_count) is int and 1 <= self.line_count <= 64, "SELECTION_INVALID")


@dataclass(frozen=True)
class SourceObservation:
    file_id: str
    revision: str
    sha256: str
    provider_version: str
    principal_permission_id: str


@dataclass(frozen=True)
class OwnerConfirmation:
    nonce: str
    request_sha256: str
    observation_sha256: str
    owner_ref: str
    proof_ref: str
    receipt_ref: str
    relation: str
    owner_seq: int
    issued_at: float
    expires_at: float


class OwnerConfirmationPort(Protocol):
    """Trusted owner-issued confirmation/revocation read, never caller config.

    Must freshly authenticate owner custody and issue a nonce-bound confirmation
    of this exact request + observed principal/source revision/hash/version.
    No production implementation or fake static sequence is supplied here.
    """

    def confirm_current(self, request: ResolveRequest, observation: SourceObservation,
                        *, nonce: str) -> OwnerConfirmation: ...


@dataclass(frozen=True)
class HostBinding:
    scope: DriveScope
    credential_ref: Path
    principal_permission_id: str
    owner_proof_ref: str
    owner_confirmation: OwnerConfirmationPort | None = None
    enabled: bool = False


_host: HostBinding | None = None
_host_lock = threading.Lock()


def configure_host(binding: HostBinding | None) -> None:
    """A1 trusted in-process bootstrap only; never accepts request fields/secrets."""
    global _host
    _need(binding is None or type(binding) is HostBinding, "HOST_BINDING_INVALID")
    with _host_lock:
        _host = binding


class GoogleDriveReadProvider:
    """Bounded GET-only v3 reads via host OAuth AuthorizedSession, no redirects."""

    def __init__(self, session: Any, principal_permission_id: str) -> None:
        self._session = session
        self._principal = principal_permission_id

    def _get(self, path: str, params: dict[str, str], limit: int) -> bytes:
        response = None
        deadline = time.monotonic() + 15
        try:
            response = self._session.get(API + path, params=params, timeout=(5, 10),
                                         stream=True, allow_redirects=False, headers={"Cache-Control": "no-cache"})
            _need(response.status_code == 200, "DRIVE_AUTH_OR_ACCESS_UNAVAILABLE")
            data = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                _need(time.monotonic() <= deadline, "DRIVE_READ_DEADLINE_EXCEEDED")
                _need(len(data) + len(chunk) <= limit, "DRIVE_READ_BOUND_EXCEEDED")
                data.extend(chunk)
            return bytes(data)
        except ResolverUnavailable:
            raise
        except Exception:
            raise ResolverUnavailable("DRIVE_READ_UNAVAILABLE") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            value = json.loads(self._get(path, params, 16384))
            _need(type(value) is dict, "DRIVE_RESPONSE_INVALID")
            return value
        except ResolverUnavailable:
            raise
        except Exception:
            raise ResolverUnavailable("DRIVE_RESPONSE_INVALID") from None

    def current(self, scope: DriveScope) -> tuple[str, str]:
        scope.validate()
        user = self._json("/about", {"fields": "user(permissionId)"}).get("user", {})
        _need(isinstance(user, dict) and user.get("permissionId") == self._principal
              and bool(self._principal), "DRIVE_PRINCIPAL_MISMATCH")
        meta = self._json(f"/files/{scope.file_id}", {
            "fields": "id,mimeType,headRevisionId,version,trashed,capabilities(canDownload)",
            "supportsAllDrives": "true"})
        capabilities = meta.get("capabilities")
        _need(meta.get("id") == scope.file_id and meta.get("mimeType") == "text/markdown"
              and meta.get("trashed") is False
              and isinstance(capabilities, dict) and capabilities.get("canDownload") is True,
              "DRIVE_SOURCE_ACCESS_DENIED")
        _need(meta.get("headRevisionId") == scope.revision, "ORIGINAL_REVISION_NO_LONGER_CURRENT")
        version = meta.get("version")
        _need(isinstance(version, str) and version.isdigit(), "DRIVE_VERSION_UNPROVEN")
        return scope.revision, version

    def read_original(self, scope: DriveScope) -> tuple[SourceObservation, str]:
        before = self.current(scope)
        body = self._get(f"/files/{scope.file_id}/revisions/{scope.revision}",
                         {"alt": "media"}, MAX_SOURCE_BYTES)
        _need(hashlib.sha256(body).hexdigest() == scope.sha256, "ORIGINAL_HASH_MISMATCH")
        try:
            text = body.decode("utf-8")
        except UnicodeError:
            raise ResolverUnavailable("ORIGINAL_TEXT_INVALID") from None
        _need(self.current(scope) == before, "DRIVE_CHANGED_DURING_READ")
        lines = text.splitlines()
        _need(scope.first_line <= len(lines), "SELECTION_MISSING")
        selected = "\n".join(lines[scope.first_line - 1:scope.first_line - 1 + scope.line_count])
        _need(bool(selected) and len(selected) <= MAX_GROUND_CHARS, "GROUNDING_BOUND_EXCEEDED")
        return SourceObservation(scope.file_id, before[0], scope.sha256, before[1], self._principal), selected


def _authorized_session(credential_ref: Path):
    """Only explicit host file custody; never ADC/env or external token origins."""
    try:
        _need(isinstance(credential_ref, Path) and credential_ref.is_absolute()
              and credential_ref.is_file() and not credential_ref.is_symlink()
              and credential_ref.stat().st_size <= 32768, "DRIVE_CREDENTIAL_REFERENCE_UNAVAILABLE")
        info = json.loads(credential_ref.read_bytes())
        _need(type(info) is dict and info.get("token_uri") == TOKEN_URI
              and info.get("universe_domain", "googleapis.com") == "googleapis.com", "DRIVE_CREDENTIAL_ORIGIN_REFUSED")
        if info.get("type") == "authorized_user":
            from google.oauth2.credentials import Credentials
            credentials = Credentials.from_authorized_user_info(info, scopes=[READ_SCOPE])
        elif info.get("type") == "service_account":
            from google.oauth2.service_account import Credentials
            credentials = Credentials.from_service_account_info(info, scopes=[READ_SCOPE])
        else:
            raise ResolverUnavailable("DRIVE_CREDENTIAL_TYPE_REFUSED")
        from google.auth.transport.requests import AuthorizedSession
        return AuthorizedSession(credentials, max_refresh_attempts=0)
    except ResolverUnavailable:
        raise
    except Exception:
        raise ResolverUnavailable("DRIVE_CREDENTIAL_OR_SDK_UNAVAILABLE") from None


class DriveOwnerResolver:
    description = "ORIGINAL_DRIVE_OWNER_READ_DEFAULT_OFF_UNTIL_HOST_QUALIFIED"

    def __init__(self, binding: HostBinding, provider: GoogleDriveReadProvider, *, clock=time.time) -> None:
        self._binding, self._provider, self._clock = binding, provider, clock

    def resolve(self, request: ResolveRequest) -> ResolverResult:
        b, s = self._binding, self._binding.scope
        s.validate()
        _need(b.enabled is True and b.owner_confirmation is not None, "OWNER_CONFIRMATION_PORT_UNBOUND")
        _need(type(request) is ResolveRequest and request.depth == "POINTER_GROUND"
              and (request.owner_ref, request.referent_type, request.referent_id, request.pointer_ref,
                   request.expected_revision, request.expected_sha256)
              == (s.owner_ref, s.referent_type, s.referent_id, s.pointer_ref, s.revision, s.sha256)
              and type(request.owner_seq) is int and request.owner_seq > 0, "EXACT_REFERENT_BINDING_MISMATCH")
        observation, selected = self._provider.read_original(s)
        nonce = secrets.token_hex(24)
        try:
            proof = b.owner_confirmation.confirm_current(request, observation, nonce=nonce)
        except Exception:
            raise ResolverUnavailable("OWNER_CONFIRMATION_UNAVAILABLE") from None
        now = self._clock()
        _need(type(proof) is OwnerConfirmation and proof.nonce == nonce
              and proof.request_sha256 == _fingerprint(asdict(request))
              and proof.observation_sha256 == _fingerprint(asdict(observation))
              and proof.owner_ref == s.owner_ref and proof.proof_ref == b.owner_proof_ref
              and isinstance(proof.receipt_ref, str) and bool(proof.receipt_ref.strip())
              and proof.relation == "SAME" and type(proof.owner_seq) is int
              and proof.owner_seq == request.owner_seq
              and type(proof.issued_at) in (int, float) and type(proof.expires_at) in (int, float)
              and now - 30 <= proof.issued_at <= now < proof.expires_at <= proof.issued_at + 30,
              "OWNER_CONFIRMATION_STALE_OR_MISMATCHED")
        _need(self._provider.current(s) == (observation.revision, observation.provider_version),
              "DRIVE_CHANGED_AFTER_OWNER_CONFIRMATION")
        _need(self._clock() < proof.expires_at, "OWNER_CONFIRMATION_EXPIRED")
        return ResolverResult(s.owner_ref, s.referent_type, s.referent_id, observation.revision,
            proof.relation, observation.sha256, grounding={"file_id": s.file_id, "revision": observation.revision,
            "principal_permission_id": observation.principal_permission_id, "owner_receipt_ref": proof.receipt_ref,
            "first_line": s.first_line, "line_count": s.line_count, "text": selected}, owner_seq=proof.owner_seq)


def create_resolver(cfg) -> DriveOwnerResolver:
    """Existing load_resolver factory. A1 registers one real binding before call."""
    with _host_lock:
        binding = _host
    _need(binding is not None and binding.enabled is True, "DRIVE_HOST_BINDING_DEFAULT_OFF")
    binding.scope.validate()
    _need(isinstance(cfg, ClientConfig) and cfg.resolver_kind == RESOLVER_FACTORY
          and cfg.resolver_factory == "signalvev_client.drive_owner_resolver:create_resolver"
          and any((i.owner_ref, i.referent_type, i.referent_id)
                  == (binding.scope.owner_ref, binding.scope.referent_type, binding.scope.referent_id)
                  for i in cfg.interests), "HOST_CONFIG_SCOPE_MISMATCH")
        _need(binding.owner_confirmation is not None
          and callable(getattr(binding.owner_confirmation, "confirm_current", None))
          and getattr(binding.owner_confirmation, "SYNTHETIC_TEST_ONLY", False) is not True,
          "OWNER_CONFIRMATION_PORT_UNBOUND")
    _need(bool(binding.principal_permission_id) and bool(binding.owner_proof_ref), "HOST_CUSTODY_REFERENCE_UNBOUND")
    session = _authorized_session(binding.credential_ref)
    return DriveOwnerResolver(binding, GoogleDriveReadProvider(session, binding.principal_permission_id))
