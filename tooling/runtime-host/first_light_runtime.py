#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "cerebro-first-light-runtime/v0.1"
RECEIPT_SCHEMA = "cerebro-first-light-receipt/v0.1"
MODE_REAL = "REAL_FIRST_LIGHT"
MODE_SIM = "SIM_TEMPORALIS"
ALLOWED_MODES = {MODE_REAL, MODE_SIM}
ALLOWED_EVENT_TYPES = {"NOOP", "SET", "INCREMENT", "MODEL_STUB", "MODEL_RECORDED"}
ALLOWED_CAPABILITIES = {"LOCAL_EVIDENCE", "MODEL_STUB", "MODEL_RECORDED"}
DENIED_CAPABILITIES = {
    "FILESYSTEM_EXTERNAL", "PROCESS_SPAWN", "NETWORK", "SOURCE_MUTATION",
    "SHARED_MUTATION", "MATERIAL_EFFECT", "PM_MUTATION", "SCHEDULER_MUTATION",
}
MAX_CANONICAL_EVENT_BYTES = 1024 * 1024


class FirstLightError(RuntimeError):
    def __init__(self, classification: str, detail: str = ""):
        super().__init__(detail or classification)
        self.classification = classification
        self.detail = detail or classification


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def semantic_fingerprint(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def _event_for_fingerprint(event: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in event.items() if k not in {"event_fingerprint", "observation_metadata"}}


def event_fingerprint(event: dict[str, Any]) -> str:
    return semantic_fingerprint(_event_for_fingerprint(event))


def _ensure_relative_subject(value: str) -> str:
    text = value.strip()
    if not text or len(text) > 512:
        raise FirstLightError("SUBJECT_REF_INVALID", text)
    return text


def validate_event(event: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode not in ALLOWED_MODES:
        raise FirstLightError("MODE_INVALID", mode)
    required = ("event_id", "event_type", "subject_ref", "intended_consequence_class", "payload")
    missing = [name for name in required if name not in event]
    if missing:
        raise FirstLightError("EVENT_FIELD_MISSING", ",".join(missing))
    event_id = str(event["event_id"]).strip()
    if not event_id or len(event_id) > 256:
        raise FirstLightError("EVENT_ID_INVALID", event_id)
    event_type = str(event["event_type"]).upper()
    if event_type not in ALLOWED_EVENT_TYPES:
        raise FirstLightError("EVENT_TYPE_UNDECLARED", event_type)
    _ensure_relative_subject(str(event["subject_ref"]))
    consequence = str(event["intended_consequence_class"]).strip()
    if not consequence:
        raise FirstLightError("INTENDED_CONSEQUENCE_CLASS_MISSING")
    capabilities = [str(item).upper() for item in event.get("capabilities", ["LOCAL_EVIDENCE"])]
    for capability in capabilities:
        if capability in DENIED_CAPABILITIES:
            raise FirstLightError("CAPABILITY_DENIED", capability)
        if capability not in ALLOWED_CAPABILITIES:
            raise FirstLightError("CAPABILITY_UNDECLARED", capability)
    if mode == MODE_SIM and any(cap not in {"LOCAL_EVIDENCE", "MODEL_STUB", "MODEL_RECORDED"} for cap in capabilities):
        raise FirstLightError("TEMPORALIS_SIDE_EFFECT_DENY")
    if event_type == "MODEL_STUB" and "MODEL_STUB" not in capabilities:
        raise FirstLightError("MODEL_CAPABILITY_MISSING", "MODEL_STUB")
    if event_type == "MODEL_RECORDED" and "MODEL_RECORDED" not in capabilities:
        raise FirstLightError("MODEL_CAPABILITY_MISSING", "MODEL_RECORDED")
    encoded = canonical_bytes(_event_for_fingerprint(event))
    if len(encoded) > MAX_CANONICAL_EVENT_BYTES:
        raise FirstLightError("EVENT_TOO_LARGE", str(len(encoded)))
    expected = str(event.get("event_fingerprint") or "").strip().lower()
    observed = event_fingerprint(event)
    if expected and expected != observed:
        raise FirstLightError("EVENT_FINGERPRINT_MISMATCH", f"expected={expected};observed={observed}")
    return {
        "event_id": event_id,
        "event_type": event_type,
        "subject_ref": str(event["subject_ref"]),
        "intended_consequence_class": consequence,
        "capabilities": capabilities,
        "event_fingerprint": observed,
    }


def _state_from_payload(event_type: str, payload: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    current = json.loads(json.dumps(previous))
    if event_type == "NOOP":
        return current
    if event_type == "SET":
        key = str(payload.get("key") or "")
        if not key:
            raise FirstLightError("SET_KEY_MISSING")
        current[key] = payload.get("value")
        return current
    if event_type == "INCREMENT":
        key = str(payload.get("key") or "")
        if not key:
            raise FirstLightError("INCREMENT_KEY_MISSING")
        amount = payload.get("amount", 1)
        if not isinstance(amount, int):
            raise FirstLightError("INCREMENT_AMOUNT_INVALID")
        before = current.get(key, 0)
        if not isinstance(before, int):
            raise FirstLightError("INCREMENT_TARGET_NOT_INTEGER")
        current[key] = before + amount
        return current
    if event_type == "MODEL_STUB":
        current["model_result"] = {
            "mode": "STUB",
            "request_fingerprint": semantic_fingerprint(payload.get("request")),
            "response": payload.get("stub_response"),
        }
        return current
    if event_type == "MODEL_RECORDED":
        if "recorded_response" not in payload:
            raise FirstLightError("RECORDED_RESPONSE_MISSING")
        current["model_result"] = {
            "mode": "RECORDED",
            "request_fingerprint": semantic_fingerprint(payload.get("request")),
            "response_fingerprint": semantic_fingerprint(payload["recorded_response"]),
            "response": payload["recorded_response"],
        }
        return current
    raise FirstLightError("EVENT_TYPE_UNDECLARED", event_type)


class FirstLightStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(db_path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.connection.close()

    def _schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events(
                event_id TEXT PRIMARY KEY,
                event_fingerprint TEXT NOT NULL,
                event_type TEXT NOT NULL,
                subject_ref TEXT NOT NULL,
                intended_consequence_class TEXT NOT NULL,
                mode TEXT NOT NULL,
                payload_canonical TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_state(
                subject_ref TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                state_revision INTEGER NOT NULL,
                currentness_cursor TEXT NOT NULL,
                state_canonical TEXT NOT NULL,
                state_fingerprint TEXT NOT NULL,
                terminal_state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts(
                invocation_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                execution_truth TEXT NOT NULL,
                verification_truth TEXT NOT NULL,
                poststate_truth TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts(
                receipt_id TEXT PRIMARY KEY,
                event_id TEXT UNIQUE NOT NULL,
                receipt_fingerprint TEXT NOT NULL,
                result_class TEXT NOT NULL,
                producer_scope_state TEXT NOT NULL,
                consequence_state TEXT NOT NULL,
                receipt_canonical TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def _existing_receipt(self, event_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT receipt_canonical FROM receipts WHERE event_id=?", (event_id,)
        ).fetchone()
        return json.loads(row["receipt_canonical"]) if row else None

    def process(
        self,
        event: dict[str, Any],
        *,
        mode: str,
        currentness_cursor: str = "LOCAL_EVIDENCE_ONLY",
        fault_injection: str | None = None,
    ) -> dict[str, Any]:
        validated = validate_event(event, mode)
        event_id = validated["event_id"]
        fingerprint = validated["event_fingerprint"]
        payload = event["payload"]
        if not isinstance(payload, dict):
            raise FirstLightError("PAYLOAD_NOT_OBJECT")

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            # The identity check and the state transition share one write lock.
            # Concurrent invocations therefore cannot both execute an event.
            existing = self.connection.execute(
                "SELECT event_fingerprint FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing:
                if existing["event_fingerprint"] != fingerprint:
                    raise FirstLightError("EVENT_ID_FINGERPRINT_COLLISION", event_id)
                receipt = self._existing_receipt(event_id)
                if receipt is None:
                    raise FirstLightError("RECOVERY_REQUIRED", "finalized event missing receipt")
                self.connection.commit()
                replay = dict(receipt)
                replay["replay"] = "IDEMPOTENT_NO_EXECUTION"
                return replay

            invocation_id = semantic_fingerprint(
                {"event_id": event_id, "event_fingerprint": fingerprint, "mode": mode}
            )[:24]
            subject_ref = validated["subject_ref"]
            prior_row = self.connection.execute(
                "SELECT state_revision,state_canonical FROM event_state WHERE subject_ref=?",
                (subject_ref,),
            ).fetchone()
            prior_state = json.loads(prior_row["state_canonical"]) if prior_row else {}
            revision = int(prior_row["state_revision"]) + 1 if prior_row else 1
            next_state = _state_from_payload(validated["event_type"], payload, prior_state)
            state_fingerprint = semantic_fingerprint(next_state)
            event_payload = canonical_bytes(_event_for_fingerprint(event)).decode("utf-8")

            consequence_state = "SIMULATION_ONLY" if mode == MODE_SIM else "LOCAL_EVIDENCE_ONLY"
            receipt_core = {
                "schema": RECEIPT_SCHEMA,
                "event_id": event_id,
                "event_fingerprint": fingerprint,
                "mode": mode,
                "subject_ref": subject_ref,
                "state_revision": revision,
                "state_fingerprint": state_fingerprint,
                "currentness_cursor": currentness_cursor,
                "intended_consequence_class": validated["intended_consequence_class"],
                "result_class": "PASS",
                "producer_scope_state": "SCOPE_CLOSED",
                "consequence_state": consequence_state,
                "execution_truth": "EXECUTED_LOCAL_REDUCER",
                "verification_truth": "DETERMINISTIC_LOCAL_COMMIT_VERIFIED",
                "poststate_truth": "LOCAL_DERIVED_STATE_ONLY",
                "authority": "EVIDENCE_ONLY",
                "next_owner": "EXTERNAL_GOVERNING_CONSUMER",
                "material_effect": False,
            }
            receipt_fingerprint = semantic_fingerprint(receipt_core)
            receipt = dict(receipt_core)
            receipt["receipt_id"] = "FLR-" + receipt_fingerprint[:20].upper()
            receipt["receipt_fingerprint"] = receipt_fingerprint

            self.connection.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?)",
                (invocation_id, event_id, "EXECUTED_LOCAL_REDUCER",
                 "DETERMINISTIC_LOCAL_COMMIT_VERIFIED", "LOCAL_DERIVED_STATE_ONLY"),
            )
            self.connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_id, fingerprint, validated["event_type"], subject_ref,
                    validated["intended_consequence_class"], mode, event_payload, "FINALIZED",
                ),
            )
            if fault_injection == "AFTER_EVENT_INSERT":
                raise RuntimeError("FIRST_LIGHT_TEST_FAULT_AFTER_EVENT_INSERT")
            if fault_injection not in (None, ""):
                raise FirstLightError("FAULT_INJECTION_UNDECLARED", fault_injection)
            self.connection.execute(
                """
                INSERT INTO event_state(subject_ref,event_id,state_revision,currentness_cursor,
                    state_canonical,state_fingerprint,terminal_state)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(subject_ref) DO UPDATE SET
                    event_id=excluded.event_id,
                    state_revision=excluded.state_revision,
                    currentness_cursor=excluded.currentness_cursor,
                    state_canonical=excluded.state_canonical,
                    state_fingerprint=excluded.state_fingerprint,
                    terminal_state=excluded.terminal_state
                """,
                (
                    subject_ref, event_id, revision, currentness_cursor,
                    canonical_bytes(next_state).decode("utf-8"), state_fingerprint, "FINALIZED",
                ),
            )
            self.connection.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?,?,?)",
                (
                    receipt["receipt_id"], event_id, receipt_fingerprint, "PASS",
                    "SCOPE_CLOSED", consequence_state,
                    canonical_bytes(receipt).decode("utf-8"),
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return receipt


def run_event(event: dict[str, Any], db_path: Path, mode: str) -> dict[str, Any]:
    store = FirstLightStore(db_path)
    try:
        return store.process(event, mode=mode)
    finally:
        store.close()


def _load_json(path: str | None) -> dict[str, Any]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.load(sys.stdin)


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro First Light — one bounded event per invocation")
    parser.add_argument("--event-file")
    parser.add_argument("--db", required=True)
    parser.add_argument("--mode", choices=sorted(ALLOWED_MODES), default=MODE_REAL)
    args = parser.parse_args()
    try:
        event = _load_json(args.event_file)
        result = run_event(event, Path(args.db), args.mode)
        print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))
        return 0
    except FirstLightError as exc:
        print(json.dumps({
            "schema": RECEIPT_SCHEMA,
            "result_class": "BLOCK",
            "classification": exc.classification,
            "detail": exc.detail,
            "material_effect": False,
        }, sort_keys=True, indent=2))
        return 2
    except Exception as exc:
        print(json.dumps({
            "schema": RECEIPT_SCHEMA,
            "result_class": "UNKNOWN",
            "classification": "RECOVERY_REQUIRED",
            "detail": type(exc).__name__,
            "material_effect": False,
        }, sort_keys=True, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
