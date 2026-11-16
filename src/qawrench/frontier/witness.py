"""Pure supplied-content verification of strict malformed-rule provenance."""

from __future__ import annotations

import hashlib
import re

from .core import AuditError
from .robots import RobotsPolicy, _parse, origin_for, strict_rule_diagnostics


def verify_malformed_rule_witness(witness: object, manifest: object, *,
                                  verify_reason: bool = False) -> dict:
    """Verify a saved strict witness against a version-1 manifest dict.

    Return ``{"verified": True, "failure": None}``, or ``verified=False`` with
    ``failure`` containing ``code``, ``field`` and a diagnostic ``message``.
    Validate the boolean option, entire manifest, witness schema, origin availability,
    source, snapshot digest, full-record byte range, exact text and line number.
    Offsets are zero-based, half-open UTF-8 positions; lines are one-based and
    CR/LF/CRLF delimit records. An identical record elsewhere cannot repair a
    location mismatch. Inputs are neither mutated nor loaded from files/URLs.

    Default verification checks provenance only. With ``verify_reason=True``,
    also use whole-snapshot parser/group context and strict diagnostics to
    confirm the exact selected record's claimed blocking classification.
    Neither mode proves authenticity, freshness or server correspondence.
    See README for the closed witness schema and failure-code contract.
    """
    def failure(code: str, field: str, message: str) -> dict:
        return {"verified": False, "failure": {"code": code, "field": field, "message": message}}

    if type(verify_reason) is not bool:
        return failure("invalid_option", "verify_reason", "must be a boolean")

    try:
        policy = RobotsPolicy.from_dict(manifest)
    except AuditError as exc:
        return failure("invalid_manifest", "manifest", str(exc))

    string_fields = ("origin", "source", "text", "reason", "snapshot_sha256")
    fields = {*string_fields, "line", "byte_start", "byte_end"}
    if not isinstance(witness, dict) or set(witness) != fields:
        return failure("invalid_witness", "witness", "requires exactly the eight strict witness fields")
    for field in string_fields:
        if not isinstance(witness[field], str):
            return failure("invalid_witness", field, "must be a string")
        try:
            witness[field].encode("utf-8")
        except UnicodeError:
            return failure("invalid_witness", field, "must contain valid UTF-8 Unicode")
    if type(witness["line"]) is not int or witness["line"] < 1:
        return failure("invalid_witness", "line", "must be a positive integer, excluding booleans")
    try:
        canonical = origin_for(witness["origin"])
    except AuditError as exc:
        return failure("invalid_witness", "origin", str(exc))
    if canonical != witness["origin"]:
        return failure("invalid_witness", "origin", "must use the emitted canonical origin spelling")
    if not witness["source"].strip():
        return failure("invalid_witness", "source", "must be nonblank")
    if witness["reason"] not in ("invalid path pattern", "invalid user-agent token",
                                 "rule outside a valid group", "missing colon after core directive"):
        return failure("invalid_witness", "reason", "must name a strict malformed-rule category")
    if not re.fullmatch(r"[0-9a-f]{64}", witness["snapshot_sha256"]):
        return failure("invalid_witness", "snapshot_sha256", "must be a lowercase SHA-256 hex digest")

    entry = policy.origins.get(canonical)
    if entry is None:
        return failure("missing_origin", "origin", "no snapshot entry for witness origin")
    if entry["status"] != "available":
        return failure("unavailable_snapshot", "origin", "snapshot entry is unavailable")
    if entry["source"] != witness["source"]:
        return failure("source_mismatch", "source", "source label differs from supplied manifest")
    snapshot = entry["text"].encode("utf-8")
    if hashlib.sha256(snapshot).hexdigest() != witness["snapshot_sha256"]:
        return failure("digest_mismatch", "snapshot_sha256", "whole supplied snapshot digest differs")
    start, end = witness["byte_start"], witness["byte_end"]
    if (type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(snapshot)):
        return failure("invalid_span", "byte_start/byte_end",
                       "requires integer offsets with 0 <= byte_start < byte_end <= snapshot byte length")

    # Select by exact span, never by text. The same record separators are used
    # for emitted strict witnesses; BOM and Unicode line separators stay data.
    for line, record in enumerate(re.finditer(rb"([^\r\n]*)(?:\r\n|\r|\n|$)", snapshot), 1):
        if record.start(1) == start:
            if record.end(1) != end:
                break
            if record.group(1).decode("utf-8") != witness["text"]:
                return failure("text_mismatch", "text", "text differs from the exact selected record")
            if line != witness["line"]:
                return failure("line_mismatch", "line", "line number differs from the selected record")
            if verify_reason:
                # Reuse current classification on the whole bound snapshot:
                # rules depend on preceding groups, including nonselected ones.
                # No request audit or user-agent selection is needed.
                _, diagnostics = _parse(entry["text"])
                origin = {"origin": canonical, "source": entry["source"],
                          "sha256": witness["snapshot_sha256"], "diagnostics": diagnostics}
                classifications = strict_rule_diagnostics([origin], policy)
                selected = next((item for item in classifications
                                 if (item["byte_start"], item["byte_end"], item["line"])
                                 == (start, end, line)), None)
                if selected is None:
                    return failure("not_malformed_rule", "reason",
                                   "selected record has no blocking strict classification")
                if selected["reason"] != witness["reason"]:
                    return failure("reason_mismatch", "reason",
                                   f"selected record is classified as {selected['reason']!r}")
            return {"verified": True, "failure": None}
        if record.start(1) > start:
            break
    return failure("record_mismatch", "byte_start/byte_end",
                   "span must select exactly one complete record, excluding CR/LF separators")


def verify_malformed_rule_collection(witnesses: object, manifest: object, *,
                                     origins: object) -> dict:
    """Check exact coverage of current strict records in explicit snapshots.

    ``origins`` must be a nonempty list of unique canonical origins, each with
    an available snapshot in the fully validated version-1 manifest. Require a
    witness list (empty allowed); every entry uses provenance AND strict reason
    verification. Return ``complete``, envelope ``failure``, indexed ``entries``
    and exact ``missing`` witnesses, ordered by scope then snapshot record.

    Entries are invalid before unexpected before duplicate before verified;
    only verified in-scope records cover expected diagnostics. Duplicate entries
    name the first verified index. Envelope failures stop before evaluation and
    leave both lists empty. See README for the closed schema/failure ordering.
    Pure in-memory supplied-content coverage only: no report-wide completeness,
    authenticity, freshness, requests, audit replay, I/O or input mutation.
    """
    def failure(code: str, field: str, message: str) -> dict:
        return {"complete": False, "failure": {"code": code, "field": field, "message": message},
                "entries": [], "missing": []}

    if not isinstance(origins, list) or not origins:
        return failure("invalid_scope", "origins", "requires a nonempty list of canonical origins")
    scope = set()
    for index, origin in enumerate(origins):
        field = f"origins[{index}]"
        if not isinstance(origin, str):
            return failure("invalid_scope", field, "must be a canonical origin string")
        try:
            canonical = origin_for(origin)
        except AuditError as exc:
            return failure("invalid_scope", field, str(exc))
        if origin != canonical:
            return failure("invalid_scope", field, "must use canonical origin spelling")
        if origin in scope:
            return failure("invalid_scope", field, "duplicate scope origin")
        scope.add(origin)

    try:
        policy = RobotsPolicy.from_dict(manifest)
    except AuditError as exc:
        return failure("invalid_manifest", "manifest", str(exc))
    for index, origin in enumerate(origins):
        entry = policy.origins.get(origin)
        if entry is None:
            return failure("missing_origin", f"origins[{index}]", "no snapshot entry for scope origin")
        if entry["status"] != "available":
            return failure("unavailable_snapshot", f"origins[{index}]", "scope snapshot is unavailable")
    if not isinstance(witnesses, list):
        return failure("invalid_collection", "witnesses", "requires an explicit witness list")

    # Enumerate expectations independently of the submitted list. Include all
    # groups, even if a request/user-agent audit would never encounter them.
    expected = []
    for origin in origins:
        entry = policy.origins[origin]
        _, diagnostics = _parse(entry["text"])
        expected.extend(strict_rule_diagnostics([{
            "origin": origin, "source": entry["source"], "diagnostics": diagnostics,
            "sha256": hashlib.sha256(entry["text"].encode("utf-8")).hexdigest()}], policy))

    def binding(witness: dict) -> tuple:
        # Semantic/provenance verification binds all other fields. Equal text
        # at another line/range never covers this record.
        return (witness["origin"], witness["line"], witness["byte_start"], witness["byte_end"])

    seen, entries = {}, []
    for index, witness in enumerate(witnesses):
        checked = verify_malformed_rule_witness(witness, manifest, verify_reason=True)
        result = {"index": index, "status": "invalid", "failure": checked["failure"],
                  "duplicate_of": None}
        if checked["verified"]:
            if witness["origin"] not in scope:
                result.update(status="unexpected", failure={"code": "unexpected_origin", "field": "origin",
                              "message": "verified witness origin is outside the explicit scope"})
            elif binding(witness) in seen:
                result.update(status="duplicate", duplicate_of=seen[binding(witness)],
                              failure={"code": "duplicate_witness", "field": "witness",
                                       "message": "record already covered by an earlier verified entry"})
            else:
                seen[binding(witness)] = index
                result.update(status="verified")
        entries.append(result)
    missing = [witness for witness in expected if binding(witness) not in seen]
    return {"complete": not missing and all(item["status"] == "verified" for item in entries),
            "failure": None, "entries": entries, "missing": missing}
