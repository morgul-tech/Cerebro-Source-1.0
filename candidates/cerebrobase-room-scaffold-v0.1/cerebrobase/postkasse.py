"""Fail-closed, private Postkassa HTTP adapter. The service, never this UI, owns message state.

The wire keys below are a v0.1 candidate pending X6 service readback. Nothing is
enabled by default; each CerebroBase room needs an explicit private credential and
an explicit correspondent mapping before a request can leave this process.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Config, resolve_secret

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TYPES = {"TEXT", "LIST", "STRUCTURED_NOTE", "REQUEST", "REPLY"}


class PostkasseError(Exception):
    def __init__(self, code: str, status: int = 503) -> None:
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class Binding:
    room_id: str
    credential: str
    contacts: dict[str, str]


class Postkasse:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.spec = cfg.integrations.get("postkasse", {"enabled": False})

    def configured(self, room_id: str) -> bool:
        try:
            self._binding(room_id)
            self._address()
            return True
        except PostkasseError:
            return False

    def _address(self) -> tuple[str, str, int]:
        if self.spec.get("enabled") is not True:
            raise PostkasseError("POSTKASSE_DISABLED")
        raw = self.spec.get("endpoint")
        if not isinstance(raw, str):
            raise PostkasseError("POSTKASSE_ENDPOINT_UNQUALIFIED")
        try:
            u = urlsplit(raw)
            ip = ipaddress.ip_address(u.hostname or "")
            port = u.port
        except (ValueError, TypeError):
            raise PostkasseError("POSTKASSE_ENDPOINT_UNQUALIFIED") from None
        private = ip.is_loopback or (ip.is_private and not (ip.is_link_local or ip.is_reserved or ip.is_unspecified))
        if (u.scheme not in ("http", "https") or not private or (u.scheme == "http" and not ip.is_loopback) or
                not port or u.path or u.query or u.fragment or u.username or u.password):
            raise PostkasseError("POSTKASSE_ENDPOINT_UNQUALIFIED")
        return u.scheme, str(ip), port

    def _binding(self, cb_room: str) -> Binding:
        if self.spec.get("enabled") is not True:
            raise PostkasseError("POSTKASSE_DISABLED")
        rooms = self.spec.get("rooms")
        row = rooms.get(cb_room) if isinstance(rooms, dict) else None
        if not isinstance(row, dict) or set(row) != {"server_room_id", "credential_ref", "contacts"}:
            raise PostkasseError("POSTKASSE_ROOM_UNPAIRED")
        sid, ref, contacts = row["server_room_id"], row["credential_ref"], row["contacts"]
        if (not isinstance(sid, str) or not _ID.fullmatch(sid) or not isinstance(ref, str) or
                ref not in self.cfg.secret_refs or not isinstance(contacts, dict) or
                any(not isinstance(k, str) or not _ID.fullmatch(k) or not isinstance(v, str) or
                    not _ID.fullmatch(v) or v == sid for k, v in contacts.items())):
            raise PostkasseError("POSTKASSE_ROOM_UNPAIRED")
        credential = resolve_secret(self.cfg.secret_refs[ref])
        if not credential or "\r" in credential or "\n" in credential:
            raise PostkasseError("POSTKASSE_CREDENTIAL_UNRESOLVED")
        return Binding(sid, credential, contacts)

    def _request(self, binding: Binding, method: str, path: str, body: dict | None = None) -> dict:
        scheme, host, port = self._address()
        conn = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(
            host, port, timeout=3)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else None
        try:
            conn.request(method, path, body=payload,
                         headers={"Authorization": "Bearer " + binding.credential,
                                  "Accept": "application/json", "Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status in (401, 403):
                raise PostkasseError("POSTKASSE_CREDENTIAL_REJECTED")
            if response.status == 404:
                raise PostkasseError("NOT_FOUND", 404)
            if response.status == 409:
                raise PostkasseError("POSTKASSE_CONFLICT", 409)
            if response.status not in (200, 201, 202):
                raise PostkasseError("POSTKASSE_UPSTREAM_REJECTED")
            raw = response.read(262145)
            if len(raw) > 262144:
                raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("expected object")
            return obj
        except PostkasseError:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError, TimeoutError, socket.timeout):
            # A mutating request may already have committed. Never retry blindly.
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN" if method == "POST" else
                                 "POSTKASSE_UNAVAILABLE") from None
        finally:
            conn.close()

    def _me(self, binding: Binding) -> None:
        me = self._request(binding, "GET", "/v1/me")
        if me.get("room_id") != binding.room_id:
            raise PostkasseError("POSTKASSE_ROOM_MISMATCH")

    def _contacts(self, binding: Binding) -> dict[str, str]:
        obj = self._request(binding, "GET", "/v1/contacts")
        rows = obj.get("contacts")
        if not isinstance(rows, list):
            raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
        actual = {r.get("room_id") for r in rows if isinstance(r, dict) and isinstance(r.get("room_id"), str)}
        return {alias: rid for alias, rid in binding.contacts.items() if rid in actual}

    def mailbox(self, cb_room: str) -> tuple[list[dict], dict[str, str]]:
        b = self._binding(cb_room)
        self._me(b)
        contacts = self._contacts(b)
        obj = self._request(b, "GET", "/v1/postkassa?limit=50")
        rows = obj.get("messages")
        if not isinstance(rows, list):
            raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
        # Never expose a body from an unverified list. Detail fetch authorizes again.
        safe = []
        for r in rows:
            if (isinstance(r, dict) and r.get("recipient_room_id") == b.room_id and
                    isinstance(r.get("message_id"), str) and _ID.fullmatch(r["message_id"])):
                safe.append({"message_id": r["message_id"], "sender_room_id": r.get("sender_room_id", ""),
                             "delivery_state": r.get("delivery_state", "")})
        return safe, contacts

    def message(self, cb_room: str, message_id: str) -> dict:
        if not _ID.fullmatch(message_id):
            raise PostkasseError("NOT_FOUND", 404)
        b = self._binding(cb_room)
        self._me(b)
        obj = self._request(b, "GET", "/v1/postkassa/" + message_id)
        if (obj.get("message_id") != message_id or obj.get("recipient_room_id") != b.room_id or
                not isinstance(obj.get("sender_room_id"), str) or
                not isinstance(obj.get("payload"), str)):
            raise PostkasseError("NOT_FOUND", 404)
        return {k: obj.get(k) for k in ("message_id", "sender_room_id", "payload", "delivery_state")}

    def send(self, cb_room: str, alias: str, payload: str, dedupe_key: str) -> str:
        b = self._binding(cb_room)
        self._me(b)
        contacts = self._contacts(b)
        recipient = contacts.get(alias)
        if (recipient is None or not payload or len(payload) > 4096 or not _ID.fullmatch(dedupe_key)):
            raise PostkasseError("POSTKASSE_SEND_REJECTED", 400)
        body = {"recipient": recipient, "type": "TEXT", "payload": payload, "dedupe_key": dedupe_key}
        obj = self._request(b, "POST", "/v1/postkassa", body)
        mid = obj.get("message_id")
        if (not isinstance(mid, str) or not _ID.fullmatch(mid) or obj.get("sender_room_id") != b.room_id or
                obj.get("recipient_room_id") != recipient):
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN")
        return mid

    def ack(self, cb_room: str, message_id: str) -> None:
        b = self._binding(cb_room)
        self.message(cb_room, message_id)  # recipient-only ownership before mutation
        obj = self._request(b, "POST", "/v1/postkassa/" + message_id + "/ack", {})
        if obj.get("message_id") != message_id:
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN")

    def reply(self, cb_room: str, message_id: str, payload: str, dedupe_key: str) -> str:
        original = self.message(cb_room, message_id)
        b = self._binding(cb_room)
        contacts = self._contacts(b)
        alias = next((k for k, v in contacts.items() if v == original["sender_room_id"]), None)
        if (alias is None or not payload or len(payload) > 4096 or not _ID.fullmatch(dedupe_key)):
            raise PostkasseError("POSTKASSE_SEND_REJECTED", 400)
        obj = self._request(b, "POST", "/v1/postkassa/" + message_id + "/reply",
                            {"type": "REPLY", "payload": payload, "dedupe_key": dedupe_key})
        mid = obj.get("message_id")
        if (not isinstance(mid, str) or not _ID.fullmatch(mid) or obj.get("sender_room_id") != b.room_id or
                obj.get("recipient_room_id") != contacts[alias]):
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN")
        return mid
