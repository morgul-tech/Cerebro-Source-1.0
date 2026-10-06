"""Default-off private Postkassa adapter; service DB remains the only mailbox authority.

The selected STAGING synthetic Tailscale carrier is an exact, bounded exception to
loopback HTTP. This module neither installs that carrier nor stores messages.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config, resolve_secret

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PROTOCOL = "proto-postkassa/0.1"
_SCOPES = {"postkassa:read", "postkassa:send", "contacts:read"}
_SELECTED_PRIVATE_HTTP = "http://100.77.125.87:18788"


class PostkasseError(Exception):
    def __init__(self, code: str, status: int = 503) -> None:
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class Binding:
    cb_room: str
    room_id: str
    credential: str
    contacts: dict[str, str]


class Postkasse:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.spec = cfg.integrations.get("postkasse", {"enabled": False})

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
        if (u.scheme not in ("http", "https") or not port or u.path or u.query or u.fragment or
                u.username or u.password or ip.is_unspecified or ip.is_multicast or ip.is_link_local):
            raise PostkasseError("POSTKASSE_ENDPOINT_UNQUALIFIED")
        selected = (raw == _SELECTED_PRIVATE_HTTP and self.cfg.environment == "STAGING" and
                    self.cfg.auth_mode == "SYNTHETIC")
        if u.scheme == "http" and not (ip.is_loopback or selected):
            raise PostkasseError("POSTKASSE_ENDPOINT_UNQUALIFIED")
        if u.scheme == "https" and not (ip.is_loopback or ip.is_private and not ip.is_reserved):
            raise PostkasseError("POSTKASSE_ENDPOINT_UNQUALIFIED")
        return u.scheme, str(ip), port

    def _bindings(self) -> dict[str, Binding]:
        if self.spec.get("enabled") is not True:
            raise PostkasseError("POSTKASSE_DISABLED")
        rooms = self.spec.get("rooms")
        ref = self.spec.get("credential_reader_ref")
        if (set(self.spec) != {"enabled", "endpoint", "credential_reader_ref", "rooms"} or
                not isinstance(rooms, dict) or not rooms or not isinstance(ref, str)):
            raise PostkasseError("POSTKASSE_ROOM_UNPAIRED")
        # One protected reader file holds {server_room_id: bearer_token}; selection is by
        # the server room id, never by a browser field or an untrusted local alias.
        source = self.cfg.secret_refs.get(ref, ref)
        if (not isinstance(source, str) or not source.startswith("file:") or
                not Path(source[5:]).is_absolute()):
            raise PostkasseError("POSTKASSE_CREDENTIAL_UNRESOLVED")
        raw = resolve_secret(source)
        try:
            tokens = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            tokens = None
        if not isinstance(tokens, dict):
            raise PostkasseError("POSTKASSE_CREDENTIAL_UNRESOLVED")
        ids, out = set(), {}
        for cb_room, row in rooms.items():
            if (not isinstance(cb_room, str) or not _ID.fullmatch(cb_room) or not isinstance(row, dict) or
                    set(row) != {"server_room_id", "contacts"}):
                raise PostkasseError("POSTKASSE_ROOM_UNPAIRED")
            sid, contacts = row["server_room_id"], row["contacts"]
            if (not isinstance(sid, str) or not _ID.fullmatch(sid) or sid in ids or
                    not isinstance(contacts, dict) or not contacts or
                    any(not isinstance(a, str) or not _ID.fullmatch(a) or not isinstance(v, str) or
                        not _ID.fullmatch(v) or v == sid for a, v in contacts.items()) or
                    len(set(contacts.values())) != len(contacts)):
                raise PostkasseError("POSTKASSE_ROOM_UNPAIRED")
            token = tokens.get(sid)
            if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
                raise PostkasseError("POSTKASSE_CREDENTIAL_UNRESOLVED")
            ids.add(sid)
            out[cb_room] = Binding(cb_room, sid, token, contacts)
        if set(tokens) != ids or len({b.credential for b in out.values()}) != len(out):
            raise PostkasseError("POSTKASSE_CREDENTIAL_UNRESOLVED")
        by_server_id = {b.room_id: b for b in out.values()}
        for b in out.values():
            if any(peer not in by_server_id or b.room_id not in by_server_id[peer].contacts.values()
                   for peer in b.contacts.values()):
                raise PostkasseError("POSTKASSE_ROOM_UNPAIRED")
        return out

    def _binding(self, cb_room: str) -> Binding:
        b = self._bindings().get(cb_room)
        if b is None:
            raise PostkasseError("POSTKASSE_ROOM_UNPAIRED")
        self._address()
        return b

    def configured(self, cb_room: str) -> bool:
        try:
            self._binding(cb_room)
            return True
        except PostkasseError:
            return False

    def _request(self, b: Binding, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        scheme, host, port = self._address()
        conn = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(host, port, timeout=3)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else None
        try:
            conn.request(method, path, body=payload,
                         headers={"Authorization": "Bearer " + b.credential, "Accept": "application/json",
                                  "Content-Type": "application/json"})
            response = conn.getresponse()
            status = response.status
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError("oversize response")
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("expected object")
            if status == 409 and obj.get("code") == "DEDUPE_CONFLICT":
                raise PostkasseError("POSTKASSE_CONFLICT", 409)
            if method == "POST" and status not in (200, 201):
                raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN")
            if status == 404:
                raise PostkasseError("NOT_FOUND", 404)
            if status in (401, 403):
                raise PostkasseError("POSTKASSE_CREDENTIAL_REJECTED")
            if status not in ((200, 201) if method == "POST" else (200,)):
                raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN" if method == "POST" else
                                     "POSTKASSE_UPSTREAM_REJECTED")
            return status, obj
        except PostkasseError:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError, TimeoutError, socket.timeout):
            # A POST can have committed even when transport/response failed. No blind retry.
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN" if method == "POST" else
                                 "POSTKASSE_UNAVAILABLE") from None
        finally:
            conn.close()

    def _me(self, b: Binding) -> None:
        _, me = self._request(b, "GET", "/v1/me")
        scope_set = me.get("scope_set")
        scopes = [part.strip() for part in scope_set.split(",")] if isinstance(scope_set, str) else []
        if (me.get("room_id") != b.room_id or len(scopes) != len(_SCOPES) or
                set(scopes) != _SCOPES):
            raise PostkasseError("POSTKASSE_ROOM_MISMATCH")

    def _contacts(self, b: Binding) -> dict[str, str]:
        _, obj = self._request(b, "GET", "/v1/contacts")
        rows = obj.get("contacts")
        if not isinstance(rows, list):
            raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
        allowed = set()
        for row in rows:
            if not isinstance(row, dict):
                raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
            if row.get("state") == "ALLOWED":
                alias = row.get("local_alias")
                if not isinstance(alias, str) or alias in allowed:
                    raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
                allowed.add(alias)
        if allowed != set(b.contacts):
            raise PostkasseError("POSTKASSE_CONTACT_MISMATCH")
        return b.contacts

    def _message(self, b: Binding, obj: dict, expected_id: str | None = None) -> dict:
        mid, sender, recipient = obj.get("message_id"), obj.get("sender_room_id"), obj.get("recipient_room_id")
        if (not isinstance(mid, str) or not _ID.fullmatch(mid) or expected_id is not None and mid != expected_id or
                obj.get("protocol_version") != _PROTOCOL or obj.get("payload_type") not in ("TEXT", "REPLY") or
                not isinstance(obj.get("payload"), dict) or not isinstance(obj["payload"].get("text"), str)):
            raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
        peers = set(b.contacts.values())
        if recipient == b.room_id and sender in peers:
            role = "incoming"
        elif sender == b.room_id and recipient in peers:
            role = "sent"
        else:
            raise PostkasseError("NOT_FOUND", 404)
        return {"message_id": mid, "sender_room_id": sender, "recipient_room_id": recipient,
                "payload": obj["payload"]["text"], "delivery_state": obj.get("state", ""),
                "reply_to": obj.get("reply_to"), "thread_id": obj.get("thread_id"), "role": role}

    def mailbox(self, cb_room: str) -> tuple[list[dict], dict[str, str]]:
        b = self._binding(cb_room)
        self._me(b)
        contacts = self._contacts(b)
        _, obj = self._request(b, "GET", "/v1/postkassa?limit=50")
        rows = obj.get("messages")
        if not isinstance(rows, list):
            raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
        safe = []
        for row in rows:
            if not isinstance(row, dict):
                raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
            try:
                msg = self._message(b, row)
            except PostkasseError as exc:
                if exc.status == 404:  # foreign/unpaired: never show body or existence
                    continue
                raise
            safe.append({k: msg[k] for k in ("message_id", "sender_room_id", "delivery_state", "role")})
        return safe, contacts

    def message(self, cb_room: str, message_id: str) -> dict:
        if not _ID.fullmatch(message_id):
            raise PostkasseError("NOT_FOUND", 404)
        b = self._binding(cb_room)
        self._me(b)
        self._contacts(b)
        _, obj = self._request(b, "GET", "/v1/postkassa/" + message_id)
        envelope = obj.get("message")
        if not isinstance(envelope, dict):
            raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
        return self._message(b, envelope, message_id)

    def _write_receipt(self, b: Binding, status: int, obj: dict, recipient: str,
                       *, reply_to: str | None = None) -> str:
        deduped, envelope = obj.get("deduped"), obj.get("message")
        if (not isinstance(deduped, bool) or status != (200 if deduped else 201) or
                not isinstance(envelope, dict)):
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN")
        msg = self._message(b, envelope)
        if (msg["role"] != "sent" or msg["sender_room_id"] != b.room_id or
                msg["recipient_room_id"] != recipient or msg["reply_to"] != reply_to):
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN")
        # Confirm service-visible durable identity. Failure remains UNKNOWN, never a retry.
        try:
            _, detail = self._request(b, "GET", "/v1/postkassa/" + msg["message_id"])
            actual = self._message(b, detail["message"], msg["message_id"])
        except (PostkasseError, KeyError, TypeError):
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN") from None
        if (actual["sender_room_id"], actual["recipient_room_id"], actual["reply_to"]) != (
                b.room_id, recipient, reply_to):
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN")
        return msg["message_id"]

    def send(self, cb_room: str, alias: str, payload: str, dedupe_key: str) -> str:
        b = self._binding(cb_room)
        self._me(b)
        contacts = self._contacts(b)
        if (alias not in contacts or not payload or len(payload) > 4096 or not _ID.fullmatch(dedupe_key)):
            raise PostkasseError("POSTKASSE_SEND_REJECTED", 400)
        status, obj = self._request(b, "POST", "/v1/postkassa",
                                    {"recipient": alias, "type": "TEXT", "payload": {"text": payload},
                                     "dedupe_key": dedupe_key})
        return self._write_receipt(b, status, obj, contacts[alias])

    def ack(self, cb_room: str, message_id: str) -> None:
        msg = self.message(cb_room, message_id)
        if msg["role"] != "incoming":
            raise PostkasseError("NOT_FOUND", 404)
        b = self._binding(cb_room)
        _, obj = self._request(b, "POST", "/v1/postkassa/" + message_id + "/ack", {})
        if (obj.get("state") != "ACKED_BY_RECIPIENT_PROTO" or not isinstance(obj.get("deduped"), bool) or
                not isinstance(obj.get("guards"), dict)):
            raise PostkasseError("POSTKASSE_OUTCOME_UNKNOWN")

    def reply(self, cb_room: str, message_id: str, payload: str, dedupe_key: str) -> str:
        original = self.message(cb_room, message_id)
        if original["role"] != "incoming" or not payload or len(payload) > 4096 or not _ID.fullmatch(dedupe_key):
            raise PostkasseError("POSTKASSE_SEND_REJECTED", 400)
        b = self._binding(cb_room)
        self._contacts(b)
        status, obj = self._request(b, "POST", "/v1/postkassa/" + message_id + "/reply",
                                    {"type": "REPLY", "payload": {"text": payload}, "dedupe_key": dedupe_key})
        return self._write_receipt(b, status, obj, original["sender_room_id"], reply_to=message_id)

    def qualify(self) -> None:
        """Read-only pre-start attestation against the *current* service and local memberships."""
        if self.cfg.environment != "STAGING" or self.cfg.auth_mode != "SYNTHETIC":
            raise PostkasseError("POSTKASSE_SCOPE_UNQUALIFIED")
        self._address()
        bindings = self._bindings()
        try:
            con = sqlite3.connect(self.cfg.db_path)
            for b in bindings.values():
                row = con.execute("SELECT 1 FROM rooms r JOIN memberships m ON m.room_id=r.id "
                                  "JOIN accounts a ON a.id=m.account_id WHERE r.id=? AND m.status='ACTIVE' "
                                  "AND a.status='ACTIVE' AND a.synthetic=1 LIMIT 1", (b.cb_room,)).fetchone()
                if row is None:
                    raise PostkasseError("POSTKASSE_MEMBERSHIP_UNQUALIFIED")
        except sqlite3.Error:
            raise PostkasseError("POSTKASSE_MEMBERSHIP_UNQUALIFIED") from None
        finally:
            if "con" in locals():
                con.close()
        for b in bindings.values():
            self._me(b)
            self._contacts(b)
            _, inbox = self._request(b, "GET", "/v1/postkassa?limit=1")
            if not isinstance(inbox.get("messages"), list):
                raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
            for row in inbox["messages"]:
                if not isinstance(row, dict):
                    raise PostkasseError("POSTKASSE_RESPONSE_INVALID")
                self._message(b, row)
