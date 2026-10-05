"""BK05 same-arc continuation capsule: build / validate / reconstruct / measure.

Standard library only.  Pure functions over bytes: nothing here touches the network, the clock, ambient process state or the
filesystem (``cli.py`` does the file I/O).  The result is a LOCAL, STRUCTURAL-ONLY candidate under caller-asserted,
unauthenticated review refs.  It decides no policy, mints no authority and claims no semantic completeness.
"""
import datetime
import hashlib
import json
import re
import unicodedata

SCHEMA_CAPSULE = "bk05-capsule-v1"
SCHEMA_DECISION = "bk05-decision-v1"
SCHEMA_MEASUREMENT = "bk05-measurement-v1"
SCHEMA_RECONSTRUCTION = "bk05-reconstruction-v1"

LOCAL_CAPSULE_CANDIDATE = "LOCAL_CAPSULE_CANDIDATE"
FULL_TASK_REQUIRED = "FULL_TASK_REQUIRED"
HOLD_STALE = "HOLD_STALE"
INVALID_INPUT = "INVALID_INPUT"
RECONSTRUCTED = "RECONSTRUCTED_HASH_VERIFIED"

# Most severe first.  Precedence rationale: a malformed/unverifiable input cannot be classified at all; a stale basis
# must be refreshed before even the full task is relied on; scope/authority/review gaps need the full task.
_SEVERITY = {INVALID_INPUT: 0, HOLD_STALE: 1, FULL_TASK_REQUIRED: 2}

AUTHORITY = "NONE"
STATUS_LABEL = "LOCAL_CANDIDATE_ONLY"
VALIDATION_LABEL = "STRUCTURAL_ONLY"
REVIEW_LABEL = "CALLER_ASSERTED_UNAUTHENTICATED"
CURRENTNESS_LABEL = "CALLER_SUPPLIED_NOT_LIVE_PROOF"
NOT_CLAIMED = ["BIND", "CONSUMPTION", "DEPLOYMENT", "OWNER_APPROVAL", "PRODUCTION", "SEND", "START"]
LIMITS = ["NOT_AUTHENTICATED_OWNER_OR_VERIFIER_APPROVAL", "NOT_LIVE_CURRENTNESS_PROOF", "NOT_SEMANTIC_COMPLETENESS"]

REUSE_ASSUMPTIONS = ("REREAD_ALL", "PARENT_ALREADY_HELD", "BOTH_ALREADY_HELD")
UNKNOWN = "UNKNOWN"

_SHA = re.compile(r"^[0-9a-f]{64}$")
_PCT = re.compile(r"%(?:2e|2f|5c)", re.I)
_DRIVE = re.compile(r"^[A-Za-z]:")


# ----------------------------------------------------------------------------------------------- helpers
def canonical_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(raw):
    return hashlib.sha256(raw).hexdigest()


def sizes(raw):
    """UTF-8 bytes and Unicode characters (code points of the decoded text).  Characters are NOT tokens."""
    return {"bytes": len(raw), "characters": len(raw.decode("utf-8"))}


class _Collector(object):
    def __init__(self):
        self.items = []

    def add(self, klass, code, field, detail):
        self.items.append({"class": klass, "code": code, "field": field, "detail": detail})

    def bad(self, code, field, detail):
        self.add(INVALID_INPUT, code, field, detail)

    def classes(self):
        return {i["class"] for i in self.items}

    def decision(self):
        present = [c for c in self.classes() if c in _SEVERITY]
        return min(present, key=lambda c: _SEVERITY[c]) if present else None


class _JsonProblem(Exception):
    def __init__(self, code, detail):
        Exception.__init__(self, detail)
        self.code = code
        self.detail = detail


def parse_json_object(raw):
    """Strict JSON object parse: UTF-8, no BOM, no duplicate keys at any level, no NaN/Infinity."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _JsonProblem("NON_UTF8_INPUT", str(exc))
    if text.startswith("﻿"):
        raise _JsonProblem("MALFORMED_JSON", "byte-order mark is not allowed")

    def hook(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise _JsonProblem("DUPLICATE_JSON_KEY", "duplicate key %r" % k)
            out[k] = v
        return out

    def const(name):
        raise _JsonProblem("MALFORMED_JSON", "non-finite number %s" % name)

    try:
        obj = json.loads(text, object_pairs_hook=hook, parse_constant=const)
    except _JsonProblem:
        raise
    except (ValueError, RecursionError) as exc:
        raise _JsonProblem("MALFORMED_JSON", str(exc))
    if not isinstance(obj, dict):
        raise _JsonProblem("JSON_NOT_OBJECT", "top-level JSON value is not an object")
    return obj


def check_path(p):
    """-> None if the path string is a canonical repo-relative path, else a reason code."""
    if p.startswith("/") or p.startswith("~") or _DRIVE.match(p):
        return "PATH_TRAVERSAL"
    if "\\" in p or _PCT.search(p) or p != p.strip() or unicodedata.normalize("NFC", p) != p:
        return "PATH_AMBIGUOUS_NORMALIZATION"
    if any(ord(c) < 32 or ord(c) == 127 for c in p):
        return "PATH_AMBIGUOUS_NORMALIZATION"
    segs = p.split("/")
    if any(s == ".." for s in segs):
        return "PATH_TRAVERSAL"
    if any(s in ("", ".") for s in segs):
        return "PATH_AMBIGUOUS_NORMALIZATION"
    return None


def _fold(p):
    return unicodedata.normalize("NFKC", p).casefold()


# ----------------------------------------------------------------------------------------------- field contract
# kinds: "str" nonblank string | "rcpt" string, may be empty | "sha" lowercase hex sha256 | "int" non-negative int |
#        "dt" ISO-8601 with UTC offset | "strs" non-empty list of unique nonblank strings | "paths" non-empty list of
#        canonical repo-relative paths | ("enum", values) | ("const", value) | ("obj", spec)
_REVIEW_PARENT = {"status": ("enum", ("OWNER_REVIEWED", "UNREVIEWED")), "receipt_ref": "rcpt"}
_REVIEW_VERIFIER = {"status": ("enum", ("VERIFIER_REVIEWED", "UNREVIEWED")), "receipt_ref": "rcpt"}

PARENT_MANIFEST_SPEC = {
    "task_ref": "str", "parent_revision": "str", "parent_sha256": "sha", "actor_ref": "str", "effect_class": "str",
    "privacy_class": "str", "live_scope": "str", "authority_class": "str", "allowed_paths": "paths",
    "required_invariants": "strs", "stop_edges": "strs", "return_target": "str", "way_home": "str",
    "source_head": "str", "semantic_review": ("obj", _REVIEW_PARENT),
}
VERIFIER_DELTA_SPEC = {
    "verifier_ref": "str", "verifier_sha256": "sha", "finding": "str", "repair_paths": "paths",
    "requested_actor_ref": "str", "requested_effect_class": "str", "requested_privacy_class": "str",
    "requested_live_scope": "str", "requested_authority_class": "str", "requested_return_target": "str",
    "required_test_delta": "strs", "evidence_refs": "strs", "idempotency_key": "str",
    "semantic_review": ("obj", _REVIEW_VERIFIER),
}
CURRENTNESS_SPEC = {"observed_source_head": "str", "observed_parent_revision": "str", "observed_at": "dt", "provenance": "str"}
PRIOR_RECORD_SPEC = {"idempotency_key": "str", "payload_sha256": "sha"}

_CAPSULE_REVIEW_P = {"status": ("const", "OWNER_REVIEWED"), "receipt_ref": "str", "assertion": ("const", REVIEW_LABEL)}
_CAPSULE_REVIEW_V = {"status": ("const", "VERIFIER_REVIEWED"), "receipt_ref": "str", "assertion": ("const", REVIEW_LABEL)}
CAPSULE_SPEC = {
    "schema_version": ("const", SCHEMA_CAPSULE), "authority": ("const", AUTHORITY), "status": ("const", STATUS_LABEL),
    "validation": ("const", VALIDATION_LABEL), "not_claimed": ("const", NOT_CLAIMED),
    "parent": ("obj", {"task_ref": "str", "revision": "str", "sha256": "sha", "bytes": "int"}),
    "verifier": ("obj", {"ref": "str", "sha256": "sha", "bytes": "int"}),
    "actor_ref": "str", "effect_class": "str", "privacy_class": "str", "live_scope": "str", "authority_class": "str",
    "source_head": "str", "allowed_repair_paths": "paths", "finding": "str", "required_test_delta": "strs",
    "inherited_proof_refs": "strs", "required_invariants": "strs", "stop_edges": "strs", "return_target": "str",
    "way_home": "str", "idempotency_key": "str",
    "reviews": ("obj", {"parent": ("obj", _CAPSULE_REVIEW_P), "verifier": ("obj", _CAPSULE_REVIEW_V)}),
    "currentness_basis": ("obj", {"observed_source_head": "str", "observed_parent_revision": "str", "observed_at": "dt",
                                  "provenance": "str", "assertion": ("const", CURRENTNESS_LABEL)}),
    "payload_sha256": "sha",
}
# Absent / empty / wrongly typed values for these manifest fields have their own typed code (work-order stop rule).
_MISSING_CODE = {"required_invariants": "MISSING_REQUIRED_INVARIANT", "stop_edges": "MISSING_STOP_EDGE",
                 "return_target": "MISSING_RETURN_TARGET", "way_home": "MISSING_WAY_HOME"}


def _has_surrogate(s):
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def _check_str(v, allow_empty=False):
    if not isinstance(v, str):
        return "not a string"
    if _has_surrogate(v):
        return "contains a lone surrogate (not representable as UTF-8)"
    if not allow_empty and v.strip() == "":
        return "empty or blank"
    return None


def _validate(obj, spec, label, col, special=None):
    """Exact-field validation: unknown fields, missing fields and type problems are INVALID_INPUT."""
    special = special or {}
    ok = True
    out = {}
    for key in obj:
        if key not in spec:
            col.bad("UNKNOWN_FIELD", "%s.%s" % (label, key), "field is not part of the contract")
            ok = False
    for key, kind in spec.items():
        path = "%s.%s" % (label, key)

        def fail(code, detail, path=path, key=key):
            col.bad(special.get(key, code), path, detail)
            return False

        if key not in obj:
            ok = fail("MISSING_FIELD", "required field is absent") and ok
            continue
        v = obj[key]
        if isinstance(kind, tuple) and kind[0] == "obj":
            if not isinstance(v, dict):
                ok = fail("WRONG_TYPE", "expected an object") and ok
                continue
            sub = _validate(v, kind[1], path, col)
            if sub is None:
                ok = False
            else:
                out[key] = sub
        elif isinstance(kind, tuple) and kind[0] == "enum":
            if not isinstance(v, str) or v not in kind[1]:
                ok = fail("INVALID_ENUM_VALUE", "must be one of %s" % ", ".join(kind[1])) and ok
            else:
                out[key] = v
        elif isinstance(kind, tuple) and kind[0] == "const":
            if v != kind[1] or type(v) is not type(kind[1]):
                ok = fail("CONSTANT_MISMATCH", "must be exactly %s" % json.dumps(kind[1])) and ok
            else:
                out[key] = v
        elif kind in ("str", "rcpt"):
            problem = _check_str(v, allow_empty=(kind == "rcpt"))
            if problem:
                ok = fail("INVALID_STRING", problem) and ok
            else:
                out[key] = v
        elif kind == "sha":
            if not isinstance(v, str) or not _SHA.match(v):
                ok = fail("INVALID_SHA256", "must be 64 lowercase hex characters") and ok
            else:
                out[key] = v
        elif kind == "int":
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                ok = fail("WRONG_TYPE", "expected a non-negative integer") and ok
            else:
                out[key] = v
        elif kind == "dt":
            problem = _check_str(v)
            parsed = None
            if not problem:
                try:
                    parsed = datetime.datetime.fromisoformat(v)
                except ValueError:
                    parsed = None
            if problem or parsed is None or parsed.utcoffset() is None:
                ok = fail("OBSERVED_AT_UNPARSEABLE", "must be an ISO-8601 timestamp with an explicit UTC offset") and ok
            else:
                out[key] = v
        elif kind in ("strs", "paths"):
            if not isinstance(v, list) or not v:
                ok = fail("EMPTY_OR_WRONG_LIST", "expected a non-empty list") and ok
                continue
            good = True
            for i, item in enumerate(v):
                problem = _check_str(item)
                if problem:
                    ok = fail("INVALID_STRING", "item %d: %s" % (i, problem)) and ok
                    good = False
            if not good:
                continue
            if len(set(v)) != len(v):
                ok = fail("DUPLICATE_LIST_ITEM", "list contains duplicate items") and ok
                continue
            if kind == "paths":
                for i, item in enumerate(v):
                    code = check_path(item)
                    if code:
                        col.bad(code, "%s[%d]" % (path, i), "%r is not a canonical repo-relative path" % item)
                        good = False
                if good and len({_fold(p) for p in v}) != len(v):
                    col.bad("PATH_AMBIGUOUS_NORMALIZATION", path, "two entries are equal after case/Unicode folding")
                    good = False
                if not good:
                    ok = False
                    continue
            out[key] = list(v)
        else:  # pragma: no cover - contract bug
            raise AssertionError(kind)
    return out if ok else None


def _load(raw, label, spec, col, special=None):
    if raw is None:
        col.bad("ABSENT_INPUT_FILE", label, "required input file is absent")
        return None
    if len(raw) == 0:
        col.bad("EMPTY_INPUT", label, "input file is empty")
        return None
    try:
        obj = parse_json_object(raw)
    except _JsonProblem as exc:
        col.bad(exc.code, label, exc.detail)
        return None
    return _validate(obj, spec, label, col, special)


def _load_text(raw, label, col):
    if raw is None:
        col.bad("ABSENT_REFERENCED_BYTES", label, "referenced bytes are absent")
        return False
    if len(raw) == 0:
        col.bad("EMPTY_INPUT", label, "input file is empty")
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        col.bad("NON_UTF8_INPUT", label, str(exc))
        return False
    return True


# ----------------------------------------------------------------------------------------------- capsule
_SCOPE_PAIRS = (
    ("actor_ref", "requested_actor_ref"), ("effect_class", "requested_effect_class"),
    ("privacy_class", "requested_privacy_class"), ("live_scope", "requested_live_scope"),
    ("authority_class", "requested_authority_class"), ("return_target", "requested_return_target"),
)


def payload_hash(capsule):
    body = {k: v for k, v in capsule.items() if k != "payload_sha256"}
    return sha256_hex(canonical_bytes(body))


def _build_capsule(m, d, c, parent_raw, verifier_raw):
    capsule = {
        "schema_version": SCHEMA_CAPSULE, "authority": AUTHORITY, "status": STATUS_LABEL,
        "validation": VALIDATION_LABEL, "not_claimed": list(NOT_CLAIMED),
        "parent": {"task_ref": m["task_ref"], "revision": m["parent_revision"], "sha256": m["parent_sha256"],
                   "bytes": len(parent_raw)},
        "verifier": {"ref": d["verifier_ref"], "sha256": d["verifier_sha256"], "bytes": len(verifier_raw)},
        "actor_ref": m["actor_ref"], "effect_class": m["effect_class"], "privacy_class": m["privacy_class"],
        "live_scope": m["live_scope"], "authority_class": m["authority_class"], "source_head": m["source_head"],
        "allowed_repair_paths": list(d["repair_paths"]), "finding": d["finding"],
        "required_test_delta": list(d["required_test_delta"]), "inherited_proof_refs": list(d["evidence_refs"]),
        "required_invariants": list(m["required_invariants"]), "stop_edges": list(m["stop_edges"]),
        "return_target": m["return_target"], "way_home": m["way_home"], "idempotency_key": d["idempotency_key"],
        "reviews": {
            "parent": {"status": m["semantic_review"]["status"], "receipt_ref": m["semantic_review"]["receipt_ref"],
                       "assertion": REVIEW_LABEL},
            "verifier": {"status": d["semantic_review"]["status"], "receipt_ref": d["semantic_review"]["receipt_ref"],
                         "assertion": REVIEW_LABEL},
        },
        "currentness_basis": {"observed_source_head": c["observed_source_head"],
                              "observed_parent_revision": c["observed_parent_revision"],
                              "observed_at": c["observed_at"], "provenance": c["provenance"],
                              "assertion": CURRENTNESS_LABEL},
    }
    capsule["payload_sha256"] = payload_hash(capsule)
    return capsule


def capsule_file_bytes(capsule):
    return canonical_bytes(capsule) + b"\n"


def measure(capsule_file, parent_raw, verifier_raw, full_raw, reuse):
    """Size measurement.  Characters are not tokens; token/spend/latency/effort/net saving stay UNKNOWN."""
    s_cap, s_par, s_ver = sizes(capsule_file), sizes(parent_raw), sizes(verifier_raw)
    s_full = sizes(full_raw) if full_raw is not None else None

    def total(*parts):
        return {"bytes": sum(p["bytes"] for p in parts), "characters": sum(p["characters"] for p in parts)}

    plans = {
        "REREAD_ALL": ("capsule.json + parent.txt + verifier.txt are all read in full", [s_cap, s_par, s_ver], 3),
        "PARENT_ALREADY_HELD": ("parent.txt is assumed already held unchanged and is not reread; capsule.json + "
                                "verifier.txt are read", [s_cap, s_ver], 2),
        "BOTH_ALREADY_HELD": ("parent.txt and verifier.txt are assumed already held unchanged; only capsule.json is "
                              "read (hash recovery only)", [s_cap], 1),
    }
    scenarios = {}
    for name in REUSE_ASSUMPTIONS:
        text, parts, reads = plans[name]
        scenarios[name] = {"assumption": text, "recipient_files_read": reads, "combined": total(*parts),
                           "reread_parent_and_verifier": total(*[p for p in parts if p is not s_cap])
                           if len(parts) > 1 else {"bytes": 0, "characters": 0}}
    chosen = scenarios[reuse]["combined"]
    if s_full is None:
        comparison = {"selected_assumption": reuse, "result": "NOT_MEASURED_BASELINE_ABSENT",
                      "capsule_burden": chosen, "baseline": None, "delta_bytes": None, "delta_characters": None}
    else:
        better = chosen["bytes"] < s_full["bytes"] and chosen["characters"] < s_full["characters"]
        comparison = {"selected_assumption": reuse,
                      "result": "OBSERVED_SIZE_ADVANTAGE" if better else "NO_OBSERVED_SIZE_ADVANTAGE",
                      "capsule_burden": chosen, "baseline": s_full,
                      "delta_bytes": chosen["bytes"] - s_full["bytes"],
                      "delta_characters": chosen["characters"] - s_full["characters"]}
    return {
        "schema_version": SCHEMA_MEASUREMENT,
        "unit_note": "bytes are UTF-8 bytes; characters are Unicode code points of the decoded text; "
                     "neither is a token count",
        "sizes": {"capsule_json": s_cap, "parent_txt": s_par, "verifier_txt": s_ver, "full_continuation_txt": s_full},
        "reuse_assumption_selected": reuse,
        "scenarios": scenarios,
        "baseline": None if s_full is None else {"recipient_files_read": 1, "size": s_full},
        "comparison": comparison,
        "missing_inputs": [] if s_full is not None else ["full_continuation.txt"],
        "unknown_until_externally_measured": {k: UNKNOWN for k in
                                              ("token_count", "provider_spend", "latency", "human_effort", "net_saving")},
        "note": "A size comparison never changes the structural validation result.",
    }


# ----------------------------------------------------------------------------------------------- evaluate
class Evaluation(object):
    def __init__(self, decision, reasons, idempotency, capsule=None, measurement=None):
        self.decision = decision
        self.reasons = reasons
        self.idempotency = idempotency
        self.capsule = capsule
        self.measurement = measurement

    def decision_document(self):
        reasons = self.reasons
        if self.decision == LOCAL_CAPSULE_CANDIDATE:
            reasons = [{"class": LOCAL_CAPSULE_CANDIDATE, "code": "STRUCTURALLY_CONSISTENT", "field": "*",
                        "detail": "typed structural consistency under caller-asserted, unauthenticated review refs"}]
        doc = {
            "schema_version": SCHEMA_DECISION, "decision": self.decision, "reasons": reasons,
            "authority": AUTHORITY, "status": STATUS_LABEL, "validation": VALIDATION_LABEL,
            "review_basis": REVIEW_LABEL, "currentness_basis": CURRENTNESS_LABEL, "limits": list(LIMITS),
            "idempotency": self.idempotency,
            "payload_sha256": self.capsule["payload_sha256"] if self.capsule else None,
            "measurement": "PRODUCED" if self.measurement else "NOT_PRODUCED",
        }
        return doc


def evaluate(parent, manifest, verifier, delta, currentness, full=None, prior=None, reuse="REREAD_ALL",
             named_but_absent=()):
    """All inputs are raw bytes or None (absent).  ``named_but_absent`` lists optional inputs the caller explicitly
    named but that could not be read ("full_continuation.txt" / "prior_record.json"): INVALID_INPUT, never skipped."""
    if reuse not in REUSE_ASSUMPTIONS:
        raise ValueError("unknown reuse assumption %r" % (reuse,))
    col = _Collector()
    for name in named_but_absent:
        col.bad("ABSENT_INPUT_FILE", name, "explicitly named optional input is absent or unreadable")
    parent_ok = _load_text(parent, "parent.txt", col)
    verifier_ok = _load_text(verifier, "verifier.txt", col)
    m = _load(manifest, "parent_manifest.json", PARENT_MANIFEST_SPEC, col, _MISSING_CODE)
    d = _load(delta, "verifier_delta.json", VERIFIER_DELTA_SPEC, col)
    c = _load(currentness, "currentness.json", CURRENTNESS_SPEC, col)
    p = None
    if prior is not None:
        p = _load(prior, "prior_record.json", PRIOR_RECORD_SPEC, col)
    if full is not None:
        _load_text(full, "full_continuation.txt", col)

    # Referenced-bytes integrity.
    if parent_ok and m is not None and sha256_hex(parent) != m["parent_sha256"]:
        col.bad("PARENT_HASH_MISMATCH", "parent_manifest.json.parent_sha256", "sha256(parent.txt) is %s" % sha256_hex(parent))
    if verifier_ok and d is not None and sha256_hex(verifier) != d["verifier_sha256"]:
        col.bad("VERIFIER_HASH_MISMATCH", "verifier_delta.json.verifier_sha256",
                "sha256(verifier.txt) is %s" % sha256_hex(verifier))

    if m is not None and d is not None:
        # Ambiguity first: a repair path that equals an allowed path only after folding is not silently accepted.
        allowed = set(m["allowed_paths"])
        folded = {_fold(a) for a in allowed}
        for i, rp in enumerate(d["repair_paths"]):
            if rp not in allowed and _fold(rp) in folded:
                col.bad("PATH_AMBIGUOUS_NORMALIZATION", "verifier_delta.json.repair_paths[%d]" % i,
                        "%r matches an allowed path only after case/Unicode folding" % rp)
        outside = [rp for rp in d["repair_paths"] if rp not in allowed and _fold(rp) not in folded]
        if outside:
            col.add(FULL_TASK_REQUIRED, "REPAIR_PATH_NOT_ALLOWED", "verifier_delta.json.repair_paths",
                    "paths outside parent allowed_paths: %s" % json.dumps(outside, ensure_ascii=False))
        for pk, dk in _SCOPE_PAIRS:
            if m[pk] != d[dk]:
                col.add(FULL_TASK_REQUIRED, "REQUESTED_%s_DIFFERS" % pk.upper(), "verifier_delta.json." + dk,
                        "parent has %s, verifier requests %s (any change needs the full task)"
                        % (json.dumps(m[pk], ensure_ascii=False), json.dumps(d[dk], ensure_ascii=False)))
        for who, rev, label, want in (("PARENT", m["semantic_review"], "parent_manifest.json.semantic_review", "OWNER_REVIEWED"),
                                      ("VERIFIER", d["semantic_review"], "verifier_delta.json.semantic_review",
                                       "VERIFIER_REVIEWED")):
            if rev["status"] != want or rev["receipt_ref"].strip() == "":
                col.add(FULL_TASK_REQUIRED, "%s_REVIEW_MISSING" % who, label,
                        "needs status %s with a nonempty receipt_ref; got status %s, receipt_ref %s"
                        % (want, rev["status"], "empty" if rev["receipt_ref"].strip() == "" else "present"))
    if m is not None and c is not None:
        if c["observed_parent_revision"] != m["parent_revision"]:
            col.add(HOLD_STALE, "STALE_PARENT_REVISION", "currentness.json.observed_parent_revision",
                    "caller-supplied observation %s differs from manifest %s"
                    % (json.dumps(c["observed_parent_revision"]), json.dumps(m["parent_revision"])))
        if c["observed_source_head"] != m["source_head"]:
            col.add(HOLD_STALE, "STALE_SOURCE_HEAD", "currentness.json.observed_source_head",
                    "caller-supplied observation %s differs from manifest %s"
                    % (json.dumps(c["observed_source_head"]), json.dumps(m["source_head"])))

    decision = col.decision()
    capsule = None
    idem = {"status": "NO_PRIOR_RECORD_CHECKED"}
    if decision is None:
        capsule = _build_capsule(m, d, c, parent, verifier)
        idem["idempotency_key"] = capsule["idempotency_key"]
        if p is not None:
            if p["idempotency_key"] != capsule["idempotency_key"]:
                idem["status"] = "PRIOR_RECORD_DIFFERENT_KEY"
            elif p["payload_sha256"] == capsule["payload_sha256"]:
                idem["status"] = "PRIOR_RECORD_MATCH"
            else:
                col.bad("IDEMPOTENCY_COLLISION", "prior_record.json",
                        "same idempotency_key with a different payload_sha256 (prior %s, now %s)"
                        % (p["payload_sha256"], capsule["payload_sha256"]))
                idem["status"] = "IDEMPOTENCY_COLLISION"
                decision, capsule = INVALID_INPUT, None
    elif prior is not None:
        idem["status"] = "NOT_CHECKED_NO_CAPSULE"
    measurement = None
    if decision is None:
        decision = LOCAL_CAPSULE_CANDIDATE
        measurement = measure(capsule_file_bytes(capsule), parent, verifier, full, reuse)
    return Evaluation(decision, col.items, idem, capsule, measurement)


# ----------------------------------------------------------------------------------------------- reconstruct
def reconstruct(capsule_raw, parent, verifier):
    """Verify a capsule and the exact parent/verifier bytes it references.  -> (decision, reasons, payload|None)."""
    col = _Collector()
    cap = _load(capsule_raw, "capsule.json", CAPSULE_SPEC, col)
    if parent is None:
        col.bad("MISSING_PARENT_BYTES", "parent.txt", "referenced parent bytes are absent; nothing is reconstructed")
    if verifier is None:
        col.bad("MISSING_VERIFIER_BYTES", "verifier.txt", "referenced verifier bytes are absent; nothing is reconstructed")
    if cap is not None:
        if payload_hash(cap) != cap["payload_sha256"]:
            col.bad("CAPSULE_PAYLOAD_HASH_MISMATCH", "capsule.json.payload_sha256",
                    "recomputed %s" % payload_hash(cap))
        if parent is not None:
            if sha256_hex(parent) != cap["parent"]["sha256"] or len(parent) != cap["parent"]["bytes"]:
                col.bad("PARENT_HASH_MISMATCH", "capsule.json.parent", "parent bytes do not match the capsule reference")
        if verifier is not None:
            if sha256_hex(verifier) != cap["verifier"]["sha256"] or len(verifier) != cap["verifier"]["bytes"]:
                col.bad("VERIFIER_HASH_MISMATCH", "capsule.json.verifier", "verifier bytes do not match the capsule reference")
    if col.items:
        return INVALID_INPUT, col.items, None
    payload = {
        "schema_version": SCHEMA_RECONSTRUCTION, "decision": RECONSTRUCTED, "validation": VALIDATION_LABEL,
        "authority": AUTHORITY, "semantic_completeness": "NOT_ASSERTED_OWNER_REVIEW_REQUIRED",
        "parent": {"sha256": cap["parent"]["sha256"], "bytes": len(parent)},
        "verifier": {"sha256": cap["verifier"]["sha256"], "bytes": len(verifier)},
        "capsule": cap,
    }
    return RECONSTRUCTED, [], payload
