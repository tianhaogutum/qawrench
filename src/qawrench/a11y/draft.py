"""Copy an exact current delta identity into a bounded, caller-authored review."""
from copy import deepcopy
from datetime import date
import re

from .core import (BridgeError, POLICY, canonical, compare, make_baseline, obj,
                   text, validate_exceptions)


ENTRY_FIELDS = ("engine", "page", "rule", "target", "owner", "reason", "expires", "maxOccurrences")
_NO_REVIEWS = object()


def _replay_delta(document):
    """Recompute the entire delta from retained sources, including historical reviews.

    Counts and copied identities must agree with the native reports. This is a
    consistency check, not authentication of caller-provided saved reports.
    """
    obj(document, "delta")
    if (type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1
            or document.get("kind") != "a11y-delta" or document.get("identityPolicy") != POLICY):
        raise BridgeError("draft requires an a11y-delta schema 1 with the supported identity policy")
    sources = obj(document.get("sources"), "delta.sources")
    snapshots = []
    for side in ("baseline", "current"):
        source = obj(sources.get(side), f"delta.sources.{side}")
        snapshots.append(make_baseline(source.get("report"), source.get("format"),
                                       url=source.get("fallbackUrl"), input_version=source.get("inputVersion")))
    exceptions, evaluated_on = None, None
    if "exceptions" in document:
        audit = obj(document["exceptions"], "delta.exceptions")
        try:
            evaluated_on = date.fromisoformat(text(audit.get("evaluatedOn"), "delta.exceptions.evaluatedOn"))
        except ValueError as exc:
            raise BridgeError("delta.exceptions.evaluatedOn: expected a valid YYYY-MM-DD date") from exc
        entries = audit.get("entries")
        if not isinstance(entries, list):
            raise BridgeError("delta.exceptions.entries: expected an array")
        reviews = []
        for entry in entries:
            obj(entry, "delta exception audit entry")
            if not all(field in entry for field in ENTRY_FIELDS):
                raise BridgeError("delta exception audit entry is missing review fields")
            reviews.append({field: entry[field] for field in ENTRY_FIELDS})
        exceptions = {"schemaVersion": 1, "kind": "a11y-reviewed-exceptions",
                      "identityPolicy": POLICY, "exceptions": reviews}
    fail_on = obj(document.get("gate"), "delta.gate").get("failOn")
    rebuilt = compare(*snapshots, fail_on=fail_on, exceptions=exceptions, evaluation_date=evaluated_on)
    if canonical(document) != canonical(rebuilt):
        raise BridgeError("delta contents/counts/fingerprints do not match its native sources; regenerate delta")
    return rebuilt


def draft_exception(delta, fingerprint, *, owner, reason, expires, existing_reviews=_NO_REVIEWS):
    """Add an exact identity and total current budget to a new review document.

    The fingerprint must be complete and present among new or unchanged current
    occurrences. Explicit review text/dates follow validate_exceptions semantics;
    an expired draft is valid but will waive nothing when applied. Optional existing
    entries retain every field value, even noncanonical URL spelling; normalized
    identities are checked for duplicates, never renewed or expanded. Inputs are
    not mutated. Explicit None is invalid; omit existing_reviews for an empty file.
    """
    entries = []
    if existing_reviews is not _NO_REVIEWS:
        validate_exceptions(existing_reviews)
        entries = deepcopy(existing_reviews["exceptions"])
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise BridgeError("fingerprint: expected a full lowercase SHA256 identity from the delta")
    delta = _replay_delta(delta)
    current = [*delta["new"], *(pair["current"] for pair in delta["unchanged"])]
    selected = [f for f in current if f["fingerprint"] == fingerprint]
    if not selected:
        raise BridgeError("fingerprint has no current occurrences in this delta (unknown or fixed-only identity)")
    entry = {field: deepcopy(selected[0][field]) for field in ("engine", "page", "rule", "target")}
    entry.update(owner=owner, reason=reason, expires=expires, maxOccurrences=len(selected))
    output = {"schemaVersion": 1, "kind": "a11y-reviewed-exceptions", "identityPolicy": POLICY,
              "exceptions": [*entries, entry]}
    # Validate the union (including normalized identity collisions), but retain
    # existing entries verbatim rather than replacing them with normalized copies.
    validate_exceptions(output)
    output["exceptions"].sort(key=canonical)
    return output
