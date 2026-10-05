#!/usr/bin/env python3
"""BK04-CLAUDE-V1 -- offline, read-only classifier for bounded historical PM episode evidence.

Usage:  python3 bk04_verify.py PATH_TO_PINNED_SNAPSHOT.json
        python3 bk04_verify.py --binding OWNER_PUBLICATION_BINDING.json OWNER_FACTS.json

Reads local input (and optional explicit publication binding), verifies input SHA-256 BEFORE parsing, and writes one
UTF-8 JSON object ``bk04-result-v1`` to stdout.  Standard library only.  No network, no filesystem write
(only stdout/stderr), no process spawning, no ambient process state.

Exit codes: 0 valid classification (PASS/CONFLICT/UNKNOWN alike) | 2 malformed input, digest mismatch or
usage error (machine-readable JSON on stderr) | 3 unexpected tool failure (machine-readable JSON on stderr).

This is a diagnostic prototype.  It decides no PM authority, changes no Cerebro state and claims no production
readiness. Normal fact inputs retain pending actions and UNKNOWN qualification; bindings prove integrity only.
``case_id`` is a label and never a branch key; the snapshot's review-oracle prose (expected outcomes,
the common negative-case text), the referent text and other descriptive prose are never read as input.
"""
import hashlib
import json
import re
import sys

WORK_ORDER_ID = "BK04-CLAUDE-V1"
SCHEMA_VERSION = "bk04-result-v1"
EXPECTED_INPUT_BYTES = 4447
EXPECTED_INPUT_SHA256 = "c4211172ef7406256bc840e1d5baf30a7537eb2a7a0f5243ddc13946118bb6f3"
# PM P1776 designates these exact X2 bytes for consumption, not owner approval.
REV3_INPUT_SHA256 = "f6f6fcdf1ed5c1cb9de4b6852a2fa28c8fb4ee8f81070e2dc4278ee6f4625d8b"

PASS, CONFLICT, UNKNOWN = "PASS", "CONFLICT", "UNKNOWN"

# Normalisation choice (disclosed in BK04_README.md): rev2 carries several decisive facts only as short
# structured strings.  They are read with the closed grammars below; anything outside the grammar is UNKNOWN.
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_DIGITS = re.compile(r"^[0-9]+$")
_TASK = re.compile(r"^P[0-9]+$")
_ROW = re.compile(r"^PM([0-9]+)$")
_ROW_TOKEN = re.compile(r"^(PM[0-9]+) ([A-Z][A-Z0-9_]*)$")
_ACTOR_ROW_TOKEN = re.compile(r"^(X[0-9]+) (PM[0-9]+) ([A-Z][A-Z0-9_]*)$")
_TASK_ROWS_TOKEN = re.compile(r"^(P[0-9]+) (PM[0-9]+)(?:/([0-9]+))? ([A-Z][A-Z0-9_]*)$")
_ADMISSION = re.compile(r"^(PM[0-9]+) (admitted/released|admission/release) (\S+)$")
_EXACT_COUNT = re.compile(r"^exact([0-9]+) is ([0-9]+) create plus ([0-9]+) replace$")
_START = re.compile(r"^(PM[0-9]+)(?:\s+(.*))?$", re.S)
_CUT = re.compile(r"(?:^|;)\s*PM_PRINCIPAL_CHANNEL:([0-9]+)\s*(?:;|$)")
_CHECK_RATIO = re.compile(r"^(PASS|FAIL) ([0-9]+)/([0-9]+)$")
_CHECK_COUNT = re.compile(r"^(PASS|FAIL) ([0-9]+)$")
_EFFECT_WORDS = re.compile(r"admit|releas|effect|\blive\b|deploy|execut|publish", re.I)
_COUNT_WORDS = {"once": 1, "twice": 2, "thrice": 3}
# Token segments that assert live / active / completed / production state.  Evidence in this snapshot is
# local-only historical projection, so such a segment is a scope promotion (CONFLICT), never a PASS.
_PROMOTION_SEGMENTS = frozenset({"LIVE", "ACTIVE", "COMPLETE", "COMPLETED", "DEPLOYED", "PRODUCTION"})


class InputError(Exception):
    """Malformed input / digest mismatch / usage error -> exit 2."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------------------------- input gate
def _reject_duplicate_keys(pairs):
    out = {}
    for k, val in pairs:
        if k in out:
            raise InputError("MALFORMED_INPUT", "duplicate JSON object key: %r" % k)
        out[k] = val
    return out


def _reject_constant(name):
    raise InputError("MALFORMED_INPUT", "non-finite JSON number: %s" % name)


def load_verified(raw, expected_sha256=EXPECTED_INPUT_SHA256, expected_bytes=EXPECTED_INPUT_BYTES):
    """Verify raw-byte size + SHA-256 first, then parse.  Returns (document, sha256_lowercase_hex)."""
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != expected_bytes or digest != expected_sha256:
        raise InputError(
            "DIGEST_MISMATCH",
            "input differs from the designated byte basis: got %d bytes sha256=%s; expected %d bytes sha256=%s"
            % (len(raw), digest, expected_bytes, expected_sha256))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError("MALFORMED_INPUT", "input is not valid UTF-8: %s" % exc)
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except InputError:
        raise
    except ValueError as exc:
        raise InputError("MALFORMED_INPUT", "input is not valid JSON: %s" % exc)
    return doc, digest


class _HashBoundDocument(dict):
    """Internal raw-gate evidence; an input field or digest argument is insufficient."""
    def __init__(self, doc, digest):
        super().__init__(doc)
        self.digest = digest
        self.parsed_bytes = json.dumps(doc, sort_keys=True, ensure_ascii=False)


def load_consumer_input(raw, binding=None):
    """CLI pins: historical rev2 or PM-designated current rev3. No caller digest flag.

    Acceptance proves byte identity only. The X2 rev3 draft is not owner-approved;
    typed structural consistency and qualified usability are separate below.
    """
    if binding is not None:
        return load_owner_input(raw, binding)
    digest = hashlib.sha256(raw).hexdigest()
    if digest == REV3_INPUT_SHA256:
        doc, digest = load_verified(raw, REV3_INPUT_SHA256, len(raw))
        if doc.get("revision") != "3.0":
            raise InputError("REVISION_MISMATCH", "designated rev3 input must declare revision 3.0")
        return _HashBoundDocument(doc, digest), digest
    return load_verified(raw)


# Normal owner input: the separately supplied publication binding is an integrity
# expectation, never an authentication, approval or live currentness oracle.
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OWNER_SCHEMA = "x2-owner-carry-basis-v1"
_RECORD_KEYS = ("task_id", "claim_id", "actor", "original_sha256", "original_bytes",
                "original_source", "actor_start", "terminal", "admission",
                "owner_corroboration", "record_status", "typed_carry")


def _require(condition, message):
    if not condition:
        raise InputError("OWNER_BINDING_MISMATCH", message)


def _sha(value):
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _positive(value):
    return type(value) is int and value > 0


def _source_ref(ref):
    _require(isinstance(ref, dict) and ref.get("sheet") == "PM_PRINCIPAL_CHANNEL"
             and _positive(ref.get("row")) and _text(ref.get("message_id"))
             and _sha(ref.get("record_sha256")), "invalid typed source reference")
    return ref["row"]


def owner_projection(doc):
    """Only actual typed facts are adapted; descriptive prose never grants proof."""
    _require(isinstance(doc, dict), "owner input must be an object")
    records = doc.get("records")
    _require(isinstance(records, list) and bool(records), "nonempty records required")
    _require(all(isinstance(r, dict) and all(k in r for k in _RECORD_KEYS)
                 for r in records), "incomplete typed record")
    prior = doc.get("closed_prior_basis")
    _require(isinstance(prior, dict) and _sha(prior.get("immutable_sha256")),
             "immutable prior basis required")
    return {"owner_source_cut": doc.get("owner_source_cut"),
            "integration_source": doc.get("integration_source"),
            "prior_basis_sha256": prior["immutable_sha256"],
            "records": [{k: r[k] for k in _RECORD_KEYS} for r in records]}


def _validate_owner_facts(doc):
    projection = owner_projection(doc)
    cut = _source_ref(projection["owner_source_cut"])
    _require(_source_ref(projection["integration_source"]) >= cut,
             "integration reference precedes owner cut")
    seen_tasks, seen_claims = set(), set()
    for record in projection["records"]:
        task, claim = record["task_id"], record["claim_id"]
        _require(_task(task) and isinstance(claim, str) and re.fullmatch(r"C[0-9]+", claim)
                 and _text(record["actor"]) and _sha(record["original_sha256"])
                 and _positive(record["original_bytes"]), "invalid original identity")
        _require(task not in seen_tasks and claim not in seen_claims, "duplicate task or claim")
        seen_tasks.add(task); seen_claims.add(claim)
        original = _source_ref(record["original_source"])
        start = _source_ref(record["actor_start"])
        _require(original < start <= cut and record["owner_corroboration"] == projection["owner_source_cut"],
                 "original/start/corroboration inconsistent with owner cut")
        carry = record["typed_carry"]
        _require(isinstance(carry, dict) and carry.get("provider_effect") == UNKNOWN
                 and carry.get("publication_status") == "NOT_EXECUTED_AT_OWNER_CUT"
                 and carry.get("global_publication_completeness") == UNKNOWN,
                 "unsupported provider effect/publication assertion")
        if record["record_status"] == "ACTIVE_SAME_CLAIM_SAME_ORIGINAL":
            _require(record["terminal"] is None and record["admission"] is None
                     and carry.get("kind") == "CONTINUE_SAME_TASK_PENDING_OAUTH_AND_X1_TERMINAL"
                     and carry.get("causal_equality") == "SAME_CLAIM_TASK_AND_ORIGINAL_HASH_CORROBORATED_BY_OWNER",
                     "active carry cannot close or change original claim")
        elif record["record_status"] == "READ_COMPLETE_ADMITTED_NO_EFFECT":
            terminal, admission = _source_ref(record["terminal"]), _source_ref(record["admission"])
            _require(start < terminal < admission <= cut
                     and carry.get("kind") == "SEPARATE_PROVIDER_ACTION_PENDING"
                     and carry.get("causal_equality") == "READ_RECEIPT_IS_NOT_PROVIDER_EXECUTION",
                     "read receipt order/carry inconsistent")
        else:
            raise InputError("OWNER_BINDING_MISMATCH", "unsupported record status")
        action = carry.get("provider_action_source")
        _require(isinstance(action, dict), "provider action reference required")
        if "corroborated_at" in action:
            _require(action.get("sheet") == "PM_PRINCIPAL_CHANNEL"
                     and _text(action.get("message_id"))
                     and action["corroborated_at"] == projection["owner_source_cut"]
                     and "row" not in action and "record_sha256" not in action,
                     "corroborated action must retain its unresolved exact row/hash")
        else:
            _require(_source_ref(action) <= cut, "provider action after owner cut")
    coverage = doc.get("coverage")
    _require(isinstance(coverage, dict)
             and coverage.get("semantic_source_approval") == "NOT_GRANTED"
             and coverage.get("complete_for_global_publication_scope") == UNKNOWN,
             "coverage cannot promote semantic approval or global completeness")
    return projection


class _OwnerBoundDocument(_HashBoundDocument):
    def __init__(self, doc, digest, binding):
        super().__init__(doc, digest)
        # Detach from caller mutation. No capability/authority is derived here.
        self.binding = json.loads(json.dumps(binding))
        self.binding_bytes = json.dumps(self.binding, sort_keys=True, ensure_ascii=False)


def load_owner_input(raw, binding):
    """Verify external expectations before parsing input; qualify nothing.

    Binding schema bk04-owner-input-binding-v1: publication_ref (typed PM ref),
    source {file_id, provider_revision}, expected {schema, artifact_id, revision,
    raw_sha256, raw_bytes}, currentness {owner_source_cut, integration_source},
    provenance (owner_projection). The designated publication supplies these
    expectations. Matching them does not authenticate that publication or prove
    the provider revision remains latest; the owner must fresh-read it externally.
    """
    _require(isinstance(binding, dict) and binding.get("schema") == "bk04-owner-input-binding-v1",
             "explicit typed owner publication binding required")
    _source_ref(binding.get("publication_ref"))
    source, expected = binding.get("source"), binding.get("expected")
    _require(isinstance(source, dict) and _text(source.get("file_id"))
             and _text(source.get("provider_revision")), "provider file/revision identity required")
    _require(isinstance(expected, dict) and expected.get("schema") == _OWNER_SCHEMA
             and _text(expected.get("artifact_id")) and _text(expected.get("revision"))
             and _sha(expected.get("raw_sha256")) and _positive(expected.get("raw_bytes")),
             "typed raw identity expectation required")
    current, provenance = binding.get("currentness"), binding.get("provenance")
    _require(isinstance(current, dict) and isinstance(provenance, dict)
             and current.get("owner_source_cut") == provenance.get("owner_source_cut")
             and current.get("integration_source") == provenance.get("integration_source"),
             "binding currentness/provenance correlation required")
    _require(_source_ref(current.get("owner_source_cut")) <= _source_ref(current.get("integration_source"))
             <= _source_ref(binding["publication_ref"]), "binding publication/cut order invalid")
    doc, digest = load_verified(raw, expected["raw_sha256"], expected["raw_bytes"])
    _require(isinstance(doc, dict) and all(doc.get(k) == expected[k]
             for k in ("schema", "artifact_id", "revision")), "input revision/artifact/schema mismatch")
    _require(_validate_owner_facts(doc) == provenance, "immutable original/carry/provenance mismatch")
    return _OwnerBoundDocument(doc, digest, binding), digest


def _classify_owner_document(doc, digest):
    _require(_text(doc.get("artifact_id")) and _text(doc.get("revision")), "owner artifact/revision required")
    projection = _validate_owner_facts(doc)
    bound = (type(doc) is _OwnerBoundDocument and doc.digest == digest
             and doc.parsed_bytes == json.dumps(doc, sort_keys=True, ensure_ascii=False)
             and doc.binding_bytes == json.dumps(doc.binding, sort_keys=True, ensure_ascii=False)
             and projection == doc.binding["provenance"])
    # No episode is synthesized from a fact projection; an admitted read is not
    # the completion of its separately pending provider action.
    return {"schema_version": SCHEMA_VERSION, "work_order_id": WORK_ORDER_ID,
            "input_snapshot_id": doc["artifact_id"], "input_revision": doc["revision"],
            "input_sha256": digest, "input_acceptance": "HASH_BOUND_OWNER_FACTS" if bound else "UNQUALIFIED_INPUT",
            "structural_overall_status": PASS if bound else UNKNOWN,
            "overall_status": UNKNOWN, "qualification": "OWNER_BASIS_NOT_QUALIFIED", "authority": "NONE",
            "binding_trust": "INTEGRITY_ONLY_NOT_AUTHENTICATED",
            "currentness": "BOUND_SOURCE_CUT_ONLY_PROVIDER_LATEST_NOT_VERIFIED",
            "source_binding": doc.binding["source"] if bound else None,
            "publication_ref": doc.binding["publication_ref"] if bound else None,
            "records": [{**record, "structural_status": PASS if bound else UNKNOWN,
                         "status": UNKNOWN, "provider_action_status": "OPEN",
                         "publication_completeness": UNKNOWN}
                        for record in projection["records"]]}


def _rev3_completeness(case, result):
    """Do not let unlinked carry flags or empty publication lists look complete.

    This adds structural checks only; source references still need independent
    qualified owner evidence. No flag in this document can supply that evidence.
    """
    if case.get("shape") != "CI_PLUS_LOCAL_LIMIT":
        return
    ci = case.get("ci_evidence")
    carry = ci.get("carried_to") if isinstance(ci, dict) else None
    orders = case.get("publication_orders")
    coverage = case.get("coverage_assertions")
    def missing(path):
        if path not in result["missing_fields"]:
            result["missing_fields"].append(path)
        if result["status"] == PASS:
            result["status"] = UNKNOWN
            result["reason"] = "typed carry/publication linkage incomplete"
    if isinstance(carry, list):
        episodes = case.get("episodes")
        targets = {ep["episode_id"] for ep in episodes if isinstance(ep, dict)
                   and isinstance(ep.get("episode_id"), str)} if isinstance(episodes, list) else set()
        for item in carry:
            if (not isinstance(item, dict) or not isinstance(item.get("target"), str)
                    or item["target"] not in targets):
                missing("$.typed_carry.target_episode")
    if isinstance(orders, list):
        for item in orders:
            if not isinstance(item, dict) or _head(item.get("head")) is None:
                missing("$.publication_orders.exact_head")
    if isinstance(coverage, list):
        for item in coverage:
            if isinstance(item, dict) and item.get("complete_for_scope") is True:
                ref = item.get("source_ref")
                episodes = case.get("episodes")
                cuts = {ep.get("source_cut") for ep in episodes if isinstance(ep, dict)
                        and isinstance(ep.get("source_cut"), str)} if isinstance(episodes, list) else set()
                if item.get("cut") not in cuts:
                    missing("$.coverage_assertions.episode_cut")
                if not isinstance(ref, dict) or ref.get("source_cut") != item.get("cut"):
                    missing("$.coverage_assertions.owner_source_cut")


# --------------------------------------------------------------------------------------------- parsers
def _text(x):
    return x if isinstance(x, str) and x.strip() != "" else None


def _head(x):
    return x if isinstance(x, str) and _HEX40.match(x) else None


def _ident(x):
    if isinstance(x, str) and _DIGITS.match(x):
        return x
    return None


def _task(x):
    return x if isinstance(x, str) and _TASK.match(x) else None


def _row_no(s):
    m = _ROW.match(s) if isinstance(s, str) else None
    return int(m.group(1)) if m else None


def _row_token(x):
    m = _ROW_TOKEN.match(x) if isinstance(x, str) else None
    return (int(m.group(1)[2:]), m.group(2)) if m else None


def _admission(x):
    """-> (row, count) ; count None when the count word is outside the closed vocabulary."""
    m = _ADMISSION.match(x) if isinstance(x, str) else None
    if not m:
        return None
    word = m.group(3)
    count = _COUNT_WORDS.get(word)
    if count is None and _DIGITS.match(word):
        count = int(word)
    return (int(m.group(1)[2:]), count)


def token_family(token):
    segs = token.split("_")
    if any(s in _PROMOTION_SEGMENTS for s in segs):
        return "PROMOTION"
    if segs[0] == "PASS" and "LOCAL" in segs:
        return "LOCAL_PASS"
    if "VERIFIED" in segs and "LOCAL" in segs:
        return "LOCAL_VERIFIED"
    if segs[0] == "REFINE":
        return "REFINE"
    if segs[0] == "HOLD":
        return "HOLD"
    return "OTHER"


# --------------------------------------------------------------------------------------------- verdicts
class _Verdict(object):
    def __init__(self, base):
        self.base = base
        self.refs = []
        self.rows = set()
        self.missing = []
        self.why = []
        self.conflicts = []

    def path(self, *parts):
        out = self.base
        for p in parts:
            out += "[%d]" % p if isinstance(p, int) else "." + p
        return out

    def ref(self, path, value):
        self.refs.append("%s=%s" % (path, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)))

    def miss(self, path, why):
        if path not in self.missing:
            self.missing.append(path)
            self.why.append("%s: %s" % (path, why))

    def conf(self, path, claimed, counter):
        self.conflicts.append("%s, claimed %s, counterevidence: %s" % (path, claimed, counter))

    def req(self, obj, key, parent, parser):
        """Required typed field: absent or unparseable -> missing path.  Returns parsed value or None."""
        path = parent + "." + key
        if not isinstance(obj, dict) or key not in obj or obj[key] is None:
            self.miss(path, "absent")
            return None
        parsed = parser(obj[key])
        if parsed is None:
            self.miss(path, "present but unparseable")
            return None
        self.ref(path, obj[key])
        return parsed

    def status(self):
        return CONFLICT if self.conflicts else (UNKNOWN if self.missing else PASS)


class _Ctx(object):
    def __init__(self, cut_row):
        self.cut_row = cut_row


def _source_rows(case, v):
    path = v.path("source_rows")
    rows = case.get("source_rows")
    if not isinstance(rows, list) or not rows:
        v.miss(path, "absent or not a non-empty list")
        return None
    out = set()
    for k, item in enumerate(rows):
        n = _row_no(item)
        if n is None:
            v.miss(v.path("source_rows", k), "present but unparseable")
        else:
            out.add(n)
    return out or None


def _use_row(v, path, row, rows, ctx):
    """A row cited by an evidence field must be listed in the case's source_rows and lie within the source cut."""
    v.rows.add(row)
    if rows is not None and row not in rows:
        v.conf(path, "PM%d" % row, "row is not among the case's cited source_rows")
    if ctx.cut_row is not None and row > ctx.cut_row:
        v.conf(path, "PM%d" % row, "row lies after the declared source cut PM%d" % ctx.cut_row)


def _use_token(v, path, token, allowed):
    fam = token_family(token)
    if fam == "PROMOTION":
        v.conf(path, token, "token asserts a live/active/complete/production state; evidence is bounded local-only")
    elif fam not in allowed:
        v.miss(path, "token %s (family %s) is outside the closed vocabulary for this role" % (token, fam))
    return fam


def _claim_triplet(case, v):
    cpq = case.get("claim_packet_queue")
    base = v.path("claim_packet_queue")
    if not isinstance(cpq, dict):
        v.miss(base, "absent or not an object")
        return
    for key in ("claim", "packet", "queue"):
        v.req(cpq, key, base, _ident)


def _free_text_remainder(raw, head_re):
    """Split 'HEAD; free text' on the first ';'.  Returns (head_match, remainder) or (None, None)."""
    if not isinstance(raw, str):
        return None, None
    head, sep, rest = raw.partition(";")
    m = head_re.match(head.strip())
    if not m:
        return None, None
    return m, rest.strip()


# --------------------------------------------------------------------------------------------- shape adapters
def _ci_plus_local_limit(case, v, ctx):
    rows = _source_rows(case, v)
    head = v.req(case, "exact_head", v.base, _head)
    _claim_triplet(case, v)
    ci = case.get("ci_evidence")
    cip = v.path("ci_evidence")
    ci_head = None
    run = job = None
    if not isinstance(ci, dict):
        v.miss(cip, "absent or not an object")
    else:
        run = v.req(ci, "run", cip, _ident)
        job = v.req(ci, "job", cip, _ident)
        ci_head = v.req(ci, "checked_out_head", cip, _head)
        if head and ci_head and head != ci_head:
            v.conf(cip + ".checked_out_head", ci_head, "exact_head is %s; CI proof belongs to different bytes" % head)
        sp = cip + ".governed_sdk_selftest"
        r = v.req(ci, "governed_sdk_selftest", cip, lambda x: _CHECK_RATIO.match(x) if isinstance(x, str) else None)
        if r:
            verdict, n, m = r.group(1), int(r.group(2)), int(r.group(3))
            if verdict == "FAIL":
                v.conf(sp, ci["governed_sdk_selftest"], "CI evidence reports FAIL; it cannot serve as proof")
            elif n != m:
                v.conf(sp, ci["governed_sdk_selftest"], "PASS with %d of %d is self-contradictory" % (n, m))
            elif m == 0:
                v.miss(sp, "vacuous proof (0/0)")
        hp = cip + ".host_checks"
        h = v.req(ci, "host_checks", cip, lambda x: _CHECK_COUNT.match(x) if isinstance(x, str) else None)
        if h:
            if h.group(1) == "FAIL":
                v.conf(hp, ci["host_checks"], "CI evidence reports FAIL; it cannot serve as proof")
            elif int(h.group(2)) == 0:
                v.miss(hp, "vacuous proof (0 host checks)")
        # Proposed field (absent in rev2): explicit record of where the component proof is carried.
        carried = ci.get("carried_to")
        cp = cip + ".carried_to"
        if not isinstance(carried, list):
            v.miss(cp, "absent or not a list (rev2 has no typed record of where the proof is carried; PASS needs one)")
        else:
            for k, ent in enumerate(carried):
                ep = v.path("ci_evidence", "carried_to", k)
                if not isinstance(ent, dict) or _text(ent.get("target")) is None:
                    v.miss(ep + ".target", "absent or unparseable")
                    continue
                eq = ent.get("causal_basis_equal")
                if not isinstance(eq, bool):
                    v.miss(ep + ".causal_basis_equal", "absent or not a boolean")
                elif not eq:
                    v.conf(ep, ent["target"], "component proof for %s promoted without matching causal bytes/contract/"
                           "result boundary/dependencies" % (head or "exact_head"))
    # Later local result: structured head + verbatim limitation clause.
    lp = v.path("later_local_result")
    later = case.get("later_local_result")
    limitation = None
    if later is None:
        v.miss(lp, "absent")
    else:
        m, limitation = _free_text_remainder(later, _TASK_ROWS_TOKEN)
        if not m:
            v.miss(lp, "present but does not match 'P<task> PM<row>[/<row>] TOKEN; <limitation>'")
        else:
            v.ref(lp, later)
            _use_token(v, lp, m.group(4), ("LOCAL_VERIFIED", "LOCAL_PASS", "REFINE", "HOLD"))
            for r in [m.group(2)] + (["PM" + m.group(3)] if m.group(3) else []):
                _use_row(v, lp, int(r[2:]), rows, ctx)
            if not limitation:
                v.miss(lp, "no limitation clause after ';' (the later local limitation must stay visible)")
                limitation = None
    # Proposed field (absent in rev2): publication orders recorded for the exact head.
    pp = v.path("publication_orders")
    orders = case.get("publication_orders")
    if not isinstance(orders, list):
        v.miss(pp, "absent or not a list (rev2 records no publication orders, so 'no duplicate publication' is unprovable)")
    else:
        same = 0
        for k, ent in enumerate(orders):
            eh = _head(ent.get("head")) if isinstance(ent, dict) else None
            er = _row_no(ent.get("row")) if isinstance(ent, dict) else None
            if eh is None:
                v.miss(v.path("publication_orders", k, "head"), "absent or unparseable")
            if er is None:
                v.miss(v.path("publication_orders", k, "row"), "absent or unparseable")
            else:
                _use_row(v, v.path("publication_orders", k, "row"), er, rows, ctx)
            if eh is not None and head is not None and eh == head:
                same += 1
        if same > 1:
            v.conf(pp, "%d publication orders for %s" % (same, head), "a single exact-head publication is the most the evidence allows")
    scope = ("Component proof only: CI run %s / job %s checked out %s; it carries to no other component or whole gate "
             "without matching causal bytes, contract, result boundary and dependencies. The later local result is "
             "local-only and does not erase it%s." % (
                 run or "?", job or "?", ci_head or head or "?",
                 ("; limitation as recorded: " + limitation) if limitation else ""))
    return "CI_PLUS_LOCAL_LIMIT", scope, "exact-head CI proof and later local limitation are both visible, consistent, and not promoted"


def _local_work_plus_owner_hold(case, v, ctx):
    rows = _source_rows(case, v)
    v.req(case, "exact_head", v.base, _head)
    _claim_triplet(case, v)
    lp = v.path("local_evidence")
    raw = case.get("local_evidence")
    seen = {"terminal": False, "admission": False, "count": False}
    if not isinstance(raw, str) or not raw.strip():
        v.miss(lp, "absent or not a non-empty string")
    else:
        v.ref(lp, raw)
        for clause in [c.strip() for c in raw.split(";")]:
            m1, m2, m3 = _ACTOR_ROW_TOKEN.match(clause), _admission(clause), _EXACT_COUNT.match(clause)
            if m1 and not seen["terminal"]:
                seen["terminal"] = True
                _use_row(v, lp, int(m1.group(2)[2:]), rows, ctx)
                _use_token(v, lp, m1.group(3), ("LOCAL_PASS",))  # a non-PASS terminal => local completion not evidenced
            elif m2 and not seen["admission"]:
                seen["admission"] = True
                row, count = m2
                _use_row(v, lp, row, rows, ctx)
                if count is None or count < 1:
                    v.miss(lp, "admission/release count word is outside the closed vocabulary")
                elif count > 1:
                    v.conf(lp, "admission/release recorded %d times" % count, "release evidence allows exactly one")
            elif m3 and not seen["count"]:
                seen["count"] = True
                total, a, b = int(m3.group(1)), int(m3.group(2)), int(m3.group(3))
                if total != a + b:
                    v.conf(lp, "exact%d is %d create plus %d replace" % (total, a, b), "%d + %d = %d, not %d" % (a, b, a + b, total))
            else:
                v.miss(lp, "unrecognised or repeated clause %r (fail closed on prose)" % clause)
        for kind, label in (("terminal", "'X<n> PM<row> TOKEN' terminal clause"),
                            ("admission", "'PM<row> admission/release once' clause"),
                            ("count", "'exact<N> is <a> create plus <b> replace' clause")):
            if not seen[kind]:
                v.miss(lp, "no %s" % label)
    bp = v.path("remaining_boundary")
    braw = case.get("remaining_boundary")
    boundary = None
    if braw is None:
        v.miss(bp, "absent")
    else:
        m, boundary = _free_text_remainder(braw, _ROW_TOKEN)
        if not m:
            v.miss(bp, "present but does not match 'PM<row> TOKEN; <boundary>'")
        else:
            v.ref(bp, braw)
            _use_row(v, bp, int(m.group(1)[2:]), rows, ctx)
            _use_token(v, bp, m.group(2), ("HOLD",))
            if not boundary:
                v.miss(bp, "no boundary clause after ';' (the remaining boundary must stay visible)")
                boundary = None
    scope = ("Local work only. The callable capability stays on the recorded HOLD; no live, production, authority or "
             "effect state is established%s." % (("; boundary as recorded: " + boundary) if boundary else ""))
    return "LOCAL_WORK_PLUS_OWNER_HOLD", scope, "local work is passed and released once while the callable capability remains on HOLD"


def _distinct_author_verifier(case, v, ctx):
    rows = _source_rows(case, v)
    boundary = v.req(case, "remaining_boundary", v.base, _text)
    eps = {}
    for role, extra in (("author", "local_head"), ("distinct_verifier", "start")):
        ep = case.get(role)
        ep_path = v.path(role)
        if not isinstance(ep, dict):
            v.miss(ep_path, "absent or not an object")
            eps[role] = None
            continue
        info = {
            "task": v.req(ep, "task", ep_path, _task),
            "claim": v.req(ep, "claim", ep_path, _ident),
            "packet": v.req(ep, "packet", ep_path, _ident),
            "queue": v.req(ep, "queue", ep_path, _ident),
            "rows": [],
        }
        if role == "author":
            v.req(ep, "local_head", ep_path, _head)
        else:
            sp = ep_path + ".start"
            sraw = ep.get("start")
            if sraw is None:
                v.miss(sp, "absent")
            else:
                m = _START.match(sraw) if isinstance(sraw, str) else None
                if not m:
                    v.miss(sp, "present but unparseable")
                else:
                    v.ref(sp, sraw)
                    if m.group(2) and _EFFECT_WORDS.search(m.group(2)):
                        v.conf(sp, sraw, "a verifier START is a start marker, not an admission, release or live effect")
                    elif m.group(2):
                        v.miss(sp, "start carries unrecognised extra text (fail closed on prose)")
                    info["start"] = int(m.group(1)[2:])
                    info["rows"].append(("start", sp, info["start"]))
        term = v.req(ep, "terminal", ep_path, _row_token)
        adm = v.req(ep, "admission", ep_path, _admission)
        if term:
            info["terminal"] = term[0]
            info["rows"].append(("terminal", ep_path + ".terminal", term[0]))
            _use_token(v, ep_path + ".terminal", term[1], ("LOCAL_PASS", "REFINE", "HOLD", "LOCAL_VERIFIED"))
        if adm:
            if adm[1] is None or adm[1] < 1:
                v.miss(ep_path + ".admission", "admission/release count word is outside the closed vocabulary")
            elif adm[1] > 1:
                v.conf(ep_path + ".admission", "admission/release recorded %d times" % adm[1], "release evidence allows exactly one")
            info["admission"] = adm[0]
            info["rows"].append(("admission", ep_path + ".admission", adm[0]))
        for _, rp, rn in info["rows"]:
            _use_row(v, rp, rn, rows, ctx)
        # Intra-episode order: start < terminal < admission (an admission cannot precede the terminal it admits).
        order = [(name, rp, rn) for (name, rp, rn) in info["rows"]]
        order.sort(key=lambda t: ("start", "terminal", "admission").index(t[0]))
        for (n1, p1, r1), (n2, p2, r2) in zip(order, order[1:]):
            if r1 >= r2:
                v.conf(p2, "PM%d (%s)" % (r2, n2), "%s row PM%d does not precede it" % (n1, r1))
        eps[role] = info
    a, b = eps.get("author"), eps.get("distinct_verifier")
    if a and b:
        for key in ("task", "claim", "packet", "queue"):
            if a[key] is not None and a[key] == b[key]:
                v.conf(v.path("distinct_verifier", key), b[key], "author episode carries the same %s; episodes are not distinct" % key)
        seen_rows = {}
        for role, info in (("author", a), ("distinct_verifier", b)):
            for name, rp, rn in info["rows"]:
                if rn in seen_rows:
                    v.conf(rp, "PM%d" % rn, "same row already used at %s; a shared row collapses the two episodes" % seen_rows[rn])
                else:
                    seen_rows[rn] = rp
    cuts = ["author cut PM%d" % max(r for _, _, r in a["rows"]) if a and a["rows"] else None,
            "verifier cut PM%d" % max(r for _, _, r in b["rows"]) if b and b["rows"] else None]
    scope = ("Two separate episodes (author %s/claim %s; verifier %s/claim %s) each read at their own cut (%s); the "
             "verifier PASS is local-only and no typed field binds it to the author's local_head; boundary as recorded: %s"
             % (a and a.get("task") or "?", a and a.get("claim") or "?", b and b.get("task") or "?",
                b and b.get("claim") or "?", ", ".join(c for c in cuts if c) or "unknown", boundary or "UNKNOWN"))
    return "DISTINCT_AUTHOR_VERIFIER", scope, "author and verifier episodes keep separate IDs, rows, terminals and admissions; START is not an effect"


def _detect_shape(case):
    sigs = {"CI": ("ci_evidence", "later_local_result"), "EPI": ("author", "distinct_verifier"), "LOC": ("local_evidence",)}
    matched = [g for g, keys in sigs.items() if any(k in case for k in keys)]
    if not matched and "remaining_boundary" in case:
        matched = ["LOC"]
    return matched


def _classify_case(case, index, ctx):
    base = "$.cases[%d]" % index
    v = _Verdict(base)
    label = case.get("case_id") if isinstance(case, dict) and isinstance(case.get("case_id"), str) else "case[%d]" % index
    family, scope, ok_reason = "SHAPE_UNRECOGNIZED", "No shape adapter applied; nothing is established.", ""
    if not isinstance(case, dict):
        v.miss(base, "case is not an object")
    else:
        matched = _detect_shape(case)
        if ctx.cut_row is None:
            v.miss("$.source_cut", "no 'PM_PRINCIPAL_CHANNEL:<row>' cut found; rows cannot be checked against the cut")
        if len(matched) == 0:
            v.miss(base, "no evidence shape recognised (needs ci_evidence/later_local_result, author/distinct_verifier, or local_evidence)")
        elif len(matched) > 1:
            family = "SHAPE_AMBIGUOUS"
            v.miss(base, "evidence fields of more than one shape are present (%s)" % "+".join(matched))
        else:
            fn = {"CI": _ci_plus_local_limit, "EPI": _distinct_author_verifier, "LOC": _local_work_plus_owner_hold}[matched[0]]
            family, scope, ok_reason = fn(case, v, ctx)
    status = v.status()
    if status == CONFLICT:
        reason = "%d demonstrated contradiction(s)/scope promotion(s); first at %s" % (len(v.conflicts), v.conflicts[0].split(",")[0])
        if v.missing:
            reason += "; %d further path(s) missing" % len(v.missing)
    elif status == UNKNOWN:
        reason = "required proof missing or unparseable at %d path(s); first: %s" % (len(v.missing), v.why[0])
    else:
        reason = ok_reason
    refs = ["PM%d" % r for r in sorted(v.rows)] + v.refs
    return {
        "case_id": label,
        "status": status,
        "rule_family": family,
        "evidence_refs": refs,
        "reason": reason,
        "missing_fields": list(v.missing),
        "conflicts": list(v.conflicts),
        "scope_limit": scope,
    }


def classify_document(doc, input_sha256):
    """Classify an already digest-verified (or test-supplied) document.  Pure function; raises InputError."""
    if not isinstance(doc, dict):
        raise InputError("MALFORMED_INPUT", "top-level JSON value is not an object")
    if doc.get("schema") == _OWNER_SCHEMA:
        return _classify_owner_document(doc, input_sha256)
    meta = {}
    for key in ("snapshot_id", "revision", "source_cut"):
        if _text(doc.get(key)) is None:
            raise InputError("MALFORMED_INPUT", "required top-level field %r is absent or not a non-empty string" % key)
        meta[key] = doc[key]
    cases = doc.get("cases")
    if not isinstance(cases, list) or not cases:
        raise InputError("MALFORMED_INPUT", "'cases' is absent or not a non-empty list")
    m = _CUT.search(meta["source_cut"])
    ctx = _Ctx(int(m.group(1)) if m else None)
    if meta["revision"] == "3.0":
        from bk04_rev3 import classify_rev3_case
        results = [classify_rev3_case(c, i, ctx.cut_row) for i, c in enumerate(cases)]
        for case, result in zip(cases, results):
            if isinstance(case, dict):
                _rev3_completeness(case, result)
        for field in ("owner_projection_cut", "supersedes"):
            if _text(doc.get(field)) is None:
                for result in results:
                    result["missing_fields"].append("$." + field)
                    if result["status"] == PASS:
                        result["status"] = UNKNOWN
                        result["reason"] = "rev3 provenance metadata incomplete"
        for result in results:
            result["structural_status"] = result["status"]
            if result["status"] == PASS:
                result["status"] = UNKNOWN
                result["missing_fields"].append("$.owner_approved_rev3_digest")
                result["reason"] = "owner-approved rev3 byte basis absent"
    else:
        results = [_classify_case(c, i, ctx) for i, c in enumerate(cases)]
    statuses = [r["status"] for r in results]
    overall = CONFLICT if CONFLICT in statuses else (UNKNOWN if UNKNOWN in statuses else PASS)
    result = {
        "schema_version": SCHEMA_VERSION,
        "work_order_id": WORK_ORDER_ID,
        "input_snapshot_id": meta["snapshot_id"],
        "input_revision": meta["revision"],
        "input_sha256": input_sha256,
        "source_cut": meta["source_cut"],
        "cases": results,
        "overall_status": overall,
    }
    if meta["revision"] == "3.0":
        result["authority"] = "NONE"
        bound = (type(doc) is _HashBoundDocument and doc.digest == input_sha256 == REV3_INPUT_SHA256
                 and doc.parsed_bytes == json.dumps(doc, sort_keys=True, ensure_ascii=False))
        result["input_acceptance"] = "HASH_BOUND_X2_DRAFT" if bound else "UNQUALIFIED_INPUT"
        result["qualification"] = "OWNER_BASIS_NOT_QUALIFIED"
        result["structural_overall_status"] = (CONFLICT if any(r["structural_status"] == CONFLICT for r in results)
                                                else UNKNOWN if any(r["structural_status"] == UNKNOWN for r in results)
                                                else PASS)
    return result


# --------------------------------------------------------------------------------------------- CLI
def _err(stream, exit_code, code, message):
    obj = {"schema_version": "bk04-error-v1", "work_order_id": WORK_ORDER_ID, "exit_code": exit_code,
           "error_code": code, "message": message}
    stream.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    stream.flush()


def main(argv=None, stdout=None, stderr=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    out = stdout if stdout is not None else sys.stdout.buffer
    err = stderr if stderr is not None else sys.stderr.buffer
    try:
        binding = None
        if len(argv) == 3 and argv[0] == "--binding":
            try:
                with open(argv[1], "rb") as fh:
                    binding_raw = fh.read()
            except OSError as exc:
                raise InputError("INPUT_UNREADABLE", "cannot read binding: %s" % exc)
            # Parsing an external expectation does not authenticate it.
            binding, _ = load_verified(binding_raw, hashlib.sha256(binding_raw).hexdigest(), len(binding_raw))
            argv = [argv[2]]
        if len(argv) != 1:
            raise InputError("USAGE", "expected snapshot.json or --binding owner-binding.json owner-facts.json")
        try:
            with open(argv[0], "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise InputError("INPUT_UNREADABLE", "cannot read %r: %s" % (argv[0], exc.strerror or exc))
        doc, digest = load_consumer_input(raw, binding) if binding is not None else load_consumer_input(raw)
        result = classify_document(doc, digest)
        payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    except InputError as exc:
        _err(err, 2, exc.code, exc.message)
        return 2
    except Exception as exc:  # noqa: BLE001 - contract: unexpected tool failure -> exit 3
        _err(err, 3, "TOOL_FAILURE", "%s: %s" % (type(exc).__name__, exc))
        return 3
    out.write(payload)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
