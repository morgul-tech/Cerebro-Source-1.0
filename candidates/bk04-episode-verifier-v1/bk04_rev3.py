"""Pure rev3 typed-episode consistency check; never grants PM or live authority."""

import re

PASS, CONFLICT, UNKNOWN = "PASS", "CONFLICT", "UNKNOWN"
ROLES = {"publication", "local_review", "builder", "distinct_verifier", "author"}
RELATIONS = {"TASK_UNDER_CLAIM", "PACKET_FOR_TASK", "QUEUE_FOR_PACKET",
             "TERMINAL_FOR_TASK", "ADMISSION_FOR_TERMINAL", "REVIEWED_ARTIFACT"}
PREDICATES = {"CALLABLE_VERIFIER_PORT", "PRODUCTION_CAPABILITY", "DEPLOYED_EFFECT",
              "LIVE_EFFECT", "OWNER_PORT", "PUBLICATION_ORDER", "PROOF_CARRY"}
NEGATIVE = {"HOLD", "NOT_PROVEN", "NO_EFFECT"}
POSITIVE = {"PROVEN", "DEPLOYED", "LIVE"}
VALUES = NEGATIVE | POSITIVE | {"UNKNOWN"}
SHEETS = {"TASK_UNDER_CLAIM": "WORK_CLAIMS", "PACKET_FOR_TASK": "WORK_PACKETS",
          "QUEUE_FOR_PACKET": "READY_QUEUE", "TERMINAL_FOR_TASK": "PM_PRINCIPAL_CHANNEL",
          "ADMISSION_FOR_TERMINAL": "PM_PRINCIPAL_CHANNEL"}
_CUT = re.compile(r"(?:^|;)\s*PM_PRINCIPAL_CHANNEL:([0-9]+)\s*(?:;|$)")


class Check:
    def __init__(self, base, global_cut):
        self.base = base
        self.global_cut = global_cut
        self.missing = []
        self.conflicts = []
        self.refs = []

    def unknown(self, path, reason):
        if path not in self.missing:
            self.missing.append(path)
        self.refs.append("%s: %s" % (path, reason))

    def conflict(self, path, reason):
        self.conflicts.append("%s: %s" % (path, reason))

    def status(self):
        return CONFLICT if self.conflicts else UNKNOWN if self.missing else PASS

    def source(self, obj, path, *, sheet=None, row=None, cut=None):
        if not isinstance(obj, dict) or not {"sheet", "row_hint", "message_id", "source_cut"} <= set(obj):
            self.unknown(path, "typed source_ref absent")
            return
        if (not isinstance(obj["sheet"], str) or not isinstance(obj["message_id"], str)
                or not obj["message_id"].strip() or not isinstance(obj["source_cut"], str)
                or type(obj["row_hint"]) is not int or obj["row_hint"] < 1):
            self.unknown(path, "typed source_ref malformed")
            return
        if sheet and obj["sheet"] != sheet:
            self.conflict(path, "source sheet differs from typed relation")
        if row is not None and obj["row_hint"] != row:
            self.conflict(path, "source row differs from episode row")
        if cut and obj["source_cut"] != cut:
            self.conflict(path, "source cut differs from episode cut")
        m = _CUT.search(obj["source_cut"])
        if not m:
            self.unknown(path + ".source_cut", "PM cut unparseable")
        elif self.global_cut is not None and int(m.group(1)) > self.global_cut:
            self.conflict(path, "source cut exceeds document cut")
        if obj["sheet"] == "PM_PRINCIPAL_CHANNEL" and m and obj["row_hint"] > int(m.group(1)):
            self.conflict(path, "PM row follows its own source cut")
        self.refs.append("%s:%s:%s@%s" % (obj["sheet"], obj["row_hint"],
                                         obj["message_id"], obj["source_cut"]))


def _episode(ep, index, check):
    path = check.base + ".episodes[%d]" % index
    required = {"episode_id", "role", "task_id", "claim_id", "claim_row", "packet_id",
                "packet_row", "queue_id", "queue_row", "task_sha256", "source_cut",
                "terminal", "admission"}
    if not isinstance(ep, dict) or not required <= set(ep):
        check.unknown(path, "typed episode fields absent")
        return None
    valid = True
    for field in ("episode_id", "task_id", "claim_id", "packet_id", "queue_id", "source_cut"):
        if not isinstance(ep[field], str) or not ep[field].strip():
            check.unknown(path + "." + field, "identifier absent")
            valid = False
    if not isinstance(ep["role"], str) or ep["role"] not in ROLES:
        check.unknown(path + ".role", "role outside closed vocabulary")
        valid = False
    for field in ("claim_row", "packet_row", "queue_row"):
        if type(ep[field]) is not int or ep[field] < 1:
            check.unknown(path + "." + field, "positive row required")
            valid = False
    if not isinstance(ep["task_sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", ep["task_sha256"]):
        check.unknown(path + ".task_sha256", "SHA-256 required")
        valid = False
    complete = True
    for field in ("terminal", "admission"):
        item = ep[field]
        if not isinstance(item, dict) or not {"source_ref", "status", "scope"} <= set(item):
            check.unknown(path + "." + field, "typed outcome absent")
            complete = False
            continue
        check.source(item["source_ref"], path + "." + field + ".source_ref",
                     sheet="PM_PRINCIPAL_CHANNEL", cut=ep["source_cut"])
        if not isinstance(item["status"], str) or not item["status"].strip():
            check.unknown(path + "." + field + ".status", "status absent")
        if not isinstance(item["scope"], str) or not item["scope"].strip():
            check.unknown(path + "." + field + ".scope", "scope absent")
    if isinstance(ep["admission"], dict) and (
            type(ep["admission"].get("release_count")) is not int
            or ep["admission"]["release_count"] != 1):
        check.conflict(path + ".admission.release_count", "exactly one release required")
    return ep if complete and valid else None


def _row_hint(item):
    ref = item.get("source_ref") if isinstance(item, dict) else None
    row = ref.get("row_hint") if isinstance(ref, dict) else None
    return row if type(row) is int and row > 0 else None


def _links(case, episodes, check):
    links = case.get("evidence_links")
    if not isinstance(links, list):
        check.unknown(check.base + ".evidence_links", "typed links absent")
        return
    seen = set()
    for i, link in enumerate(links):
        path = check.base + ".evidence_links[%d]" % i
        if not isinstance(link, dict) or not {"relation", "subject_episode", "object_episode_or_artifact", "source_ref"} <= set(link):
            check.unknown(path, "link fields absent")
            continue
        relation, episode_id = link["relation"], link["subject_episode"]
        if not isinstance(relation, str) or relation not in RELATIONS:
            check.unknown(path + ".relation", "relation outside closed vocabulary")
            continue
        if not isinstance(episode_id, str):
            check.unknown(path + ".subject_episode", "episode ID required")
            continue
        ep = episodes.get(episode_id)
        if ep is None:
            check.conflict(path + ".subject_episode", "link points outside typed episodes")
            continue
        seen.add((episode_id, relation))
        expected = {"TASK_UNDER_CLAIM": (ep["claim_id"], ep["claim_row"]),
                    "PACKET_FOR_TASK": (ep["packet_id"], ep["packet_row"]),
                    "QUEUE_FOR_PACKET": (ep["queue_id"], ep["queue_row"]),
                    "TERMINAL_FOR_TASK": (ep["task_id"], _row_hint(ep["terminal"])),
                    "ADMISSION_FOR_TERMINAL": (ep["task_id"], _row_hint(ep["admission"]))}
        if relation in expected:
            target, row = expected[relation]
            if link["object_episode_or_artifact"] != target:
                check.conflict(path + ".object_episode_or_artifact", "target differs from typed episode ID")
            check.source(link["source_ref"], path + ".source_ref", sheet=SHEETS[relation],
                         row=row, cut=ep["source_cut"])
            source_ref = link["source_ref"]
            message_id = source_ref.get("message_id") if isinstance(source_ref, dict) else None
            if isinstance(message_id, str):
                if relation == "TASK_UNDER_CLAIM" and (
                        ep["task_id"] not in message_id or ep["claim_id"] not in message_id):
                    check.conflict(path + ".source_ref.message_id", "claim-row identity does not tie task and claim")
                elif relation == "PACKET_FOR_TASK" and ep["task_id"] not in message_id:
                    check.conflict(path + ".source_ref.message_id", "packet-row identity does not tie task")
                elif relation == "QUEUE_FOR_PACKET" and message_id != ep["queue_id"].removeprefix("Q"):
                    check.conflict(path + ".source_ref.message_id", "queue-row identity does not tie queue")
        else:
            check.source(link["source_ref"], path + ".source_ref",
                         sheet="PM_PRINCIPAL_CHANNEL", cut=ep["source_cut"])
    for episode_id in episodes:
        for relation in SHEETS:
            if (episode_id, relation) not in seen:
                check.unknown(check.base + ".evidence_links", "%s missing for %s" % (relation, episode_id))


def _boundaries(case, check):
    assertions = case.get("boundary_assertions")
    if not isinstance(assertions, list) or not assertions:
        check.unknown(check.base + ".boundary_assertions", "typed boundary absent")
        return
    grouped = {}
    for i, assertion in enumerate(assertions):
        path = check.base + ".boundary_assertions[%d]" % i
        if not isinstance(assertion, dict) or not {"predicate", "value", "scope", "as_of_cut", "source_ref"} <= set(assertion):
            check.unknown(path, "assertion fields absent")
            continue
        if (not isinstance(assertion["predicate"], str)
                or not isinstance(assertion["value"], str)
                or assertion["predicate"] not in PREDICATES or assertion["value"] not in VALUES):
            check.unknown(path, "predicate/value outside closed vocabulary")
            continue
        if not isinstance(assertion["scope"], str) or not assertion["scope"].strip():
            check.unknown(path + ".scope", "scope absent")
            continue
        if not isinstance(assertion["as_of_cut"], str):
            check.unknown(path + ".as_of_cut", "typed cut required")
            continue
        check.source(assertion["source_ref"], path + ".source_ref",
                     sheet="PM_PRINCIPAL_CHANNEL", cut=assertion["as_of_cut"])
        key = (assertion["predicate"], assertion["scope"], assertion["as_of_cut"])
        source_ref = assertion["source_ref"]
        grouped.setdefault(key, []).append((assertion["value"], path,
                                             source_ref.get("message_id", "?")
                                             if isinstance(source_ref, dict) else "?"))
    for key, entries in grouped.items():
        negatives = [e for e in entries if e[0] in NEGATIVE]
        positives = [e for e in entries if e[0] in POSITIVE]
        if negatives and positives:
            check.conflict(check.base + ".boundary_assertions",
                           "%s scope/cut contradiction: %s versus %s" %
                           (key[0], negatives[0][2], positives[0][2]))


def classify_rev3_case(case, index, global_cut):
    base = "$.cases[%d]" % index
    check = Check(base, global_cut)
    if not isinstance(case, dict):
        check.unknown(base, "case object required")
        case = {}
    shape = case.get("shape")
    if not isinstance(shape, str) or shape not in {
            "CI_PLUS_LOCAL_LIMIT", "LOCAL_WORK_PLUS_OWNER_HOLD", "DISTINCT_AUTHOR_VERIFIER"}:
        check.unknown(base + ".shape", "typed shape required")
    episodes = {}
    raw_episodes = case.get("episodes")
    if not isinstance(raw_episodes, list) or not raw_episodes:
        check.unknown(base + ".episodes", "typed episodes absent")
    else:
        for i, raw in enumerate(raw_episodes):
            ep = _episode(raw, i, check)
            if ep:
                if ep["episode_id"] in episodes:
                    check.conflict(base + ".episodes[%d].episode_id" % i, "duplicate episode ID")
                episodes[ep["episode_id"]] = ep
    if len(episodes) > 1:
        ids = list(episodes.values())
        for field in ("task_id", "packet_id", "queue_id"):
            if len({ep[field] for ep in ids}) != len(ids):
                check.conflict(base + ".episodes", "distinct episodes share %s" % field)
        claims = [ep["claim_id"] for ep in ids]
        if len(set(claims)) != len(claims) and case.get("claim_scope") != "standing":
            check.conflict(base + ".episodes", "distinct episodes share claim without standing scope")
        rows = [_row_hint(ep[field]) for ep in ids for field in ("terminal", "admission")]
        rows = [row for row in rows if row is not None]
        if len(set(rows)) != len(rows):
            check.conflict(base + ".episodes", "author/verifier terminal or admission row collapsed")
    expected_roles = ({"builder", "distinct_verifier"} if shape == "LOCAL_WORK_PLUS_OWNER_HOLD"
                      else {"author", "distinct_verifier"} if shape == "DISTINCT_AUTHOR_VERIFIER"
                      else None)
    if expected_roles is not None and {ep["role"] for ep in episodes.values()} != expected_roles:
        check.unknown(base + ".episodes", "required separate roles not established")
    _links(case, episodes, check)
    _boundaries(case, check)
    for field in ("remaining_boundary", "referent"):
        if case.get(field):
            check.unknown(base + "." + field, "unmapped legacy prose cannot establish a typed boundary or referent")
    if shape in {"LOCAL_WORK_PLUS_OWNER_HOLD", "DISTINCT_AUTHOR_VERIFIER"}:
        relations = case.get("artifact_relations")
        if not isinstance(relations, list) or not relations:
            check.unknown(base + ".artifact_relations", "typed reviewed-artifact relation absent")
        else:
            for i, relation in enumerate(relations):
                path = base + ".artifact_relations[%d]" % i
                if not isinstance(relation, dict) or not {"relation", "source_episode", "target_episode",
                       "artifact_kind", "exact_head_or_digest", "evidence_ref", "scope"} <= set(relation):
                    check.unknown(path, "artifact relation fields absent")
                    continue
                expected_relation = ("REVIEWED_ARTIFACT" if shape == "LOCAL_WORK_PLUS_OWNER_HOLD"
                                     else "REVIEWED_ORIGINAL_AUTHOR_ARTIFACT")
                if relation["relation"] != expected_relation:
                    check.unknown(path + ".relation", "wrong scoped review relation")
                source_id, target_id = relation["source_episode"], relation["target_episode"]
                if (not isinstance(source_id, str) or not isinstance(target_id, str)
                        or source_id not in episodes or target_id not in episodes):
                    check.conflict(path, "artifact relation points outside typed episodes")
                elif (episodes[source_id]["role"] != "distinct_verifier"
                      or episodes[target_id]["role"] !=
                      ("builder" if shape == "LOCAL_WORK_PLUS_OWNER_HOLD" else "author")):
                    check.conflict(path, "review must link distinct verifier to builder/author")
                digest = relation["exact_head_or_digest"]
                if not isinstance(digest, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", digest):
                    check.unknown(path + ".exact_head_or_digest", "exact 40/64 hex identity required")
                check.source(relation["evidence_ref"], path + ".evidence_ref",
                             sheet="PM_PRINCIPAL_CHANNEL")
    if shape == "CI_PLUS_LOCAL_LIMIT":
        ci = case.get("ci_evidence", {})
        carried = ci.get("carried_to") if isinstance(ci, dict) else None
        if not isinstance(carried, list) or not carried:
            check.unknown(base + ".ci_evidence.carried_to", "typed carry evidence absent")
        else:
            for i, item in enumerate(carried):
                path = base + ".ci_evidence.carried_to[%d]" % i
                if not isinstance(item, dict) or not {"target", "causal_basis_equal", "source_ref"} <= set(item):
                    check.unknown(path, "typed carry relation absent")
                    continue
                if item["causal_basis_equal"] is False:
                    check.conflict(path, "component proof promoted without equal causal basis")
                elif item["causal_basis_equal"] is not True:
                    check.unknown(path + ".causal_basis_equal", "causal equality unproven")
                check.source(item["source_ref"], path + ".source_ref")
        orders = case.get("publication_orders")
        if not isinstance(orders, list):
            check.unknown(base + ".publication_orders", "owner publication evidence absent")
        else:
            for i, item in enumerate(orders):
                path = base + ".publication_orders[%d]" % i
                if not isinstance(item, dict) or not {"head", "source_ref"} <= set(item):
                    check.unknown(path, "typed publication order absent")
                    continue
                check.source(item["source_ref"], path + ".source_ref")
        coverage = case.get("coverage_assertions")
        if not isinstance(coverage, list) or not coverage:
            check.unknown(base + ".coverage_assertions", "owner completeness evidence absent")
        else:
            covered = False
            for i, item in enumerate(coverage):
                path = base + ".coverage_assertions[%d]" % i
                if not isinstance(item, dict) or not {"predicate", "complete_for_scope",
                        "query_or_owner_read_ref", "cut", "source_ref"} <= set(item):
                    check.unknown(path, "typed completeness record absent")
                    continue
                if item["predicate"] == "PUBLICATION_ORDER" and item["complete_for_scope"] is True:
                    covered = True
                if not isinstance(item["query_or_owner_read_ref"], str) or not item["query_or_owner_read_ref"].strip():
                    check.unknown(path + ".query_or_owner_read_ref", "owner read reference absent")
                check.source(item["source_ref"], path + ".source_ref")
            if not covered:
                check.unknown(base + ".coverage_assertions", "publication-order completeness unproven")
    status = check.status()
    return {"case_id": case.get("case_id", "case[%d]" % index), "status": status,
            "rule_family": shape or "SHAPE_UNRECOGNIZED", "evidence_refs": check.refs,
            "reason": "typed contradiction" if status == CONFLICT else
                      "typed evidence incomplete" if status == UNKNOWN else "typed local evidence consistent",
            "missing_fields": check.missing, "conflicts": check.conflicts,
            "scope_limit": "Offline structural consistency only; authority NONE. No PM admission, provider, deploy or live effect."}
