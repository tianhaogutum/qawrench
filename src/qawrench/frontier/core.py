"""Loss-aware request loading, explicit URL policy, and deterministic auditing."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import unquote_to_bytes, urlsplit

from w3lib.url import canonicalize_url

if TYPE_CHECKING:
    from .robots import RobotsPolicy


class AuditError(ValueError):
    """An input or policy cannot be audited safely."""


class Unsupported(AuditError):
    """A well-formed feature lies outside the supported offline dialect."""


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(text: str) -> object:
    def invalid_constant(value: str) -> None:
        raise AuditError(f"non-JSON number: {value}")
    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise AuditError("JSON number exceeds finite float range")
        return parsed
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, RecursionError) as exc:
        raise AuditError(f"invalid JSON: {exc}") from exc


@dataclass(frozen=True)
class Policy:
    version: int = 1
    remove_query_keys: tuple[str, ...] = ()
    fragment: str = "preserve"
    w3lib_sort_query: bool = False
    idna: str = "preserve"
    allow_ambiguous_merges: bool = False

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise AuditError("policy version must be 1")
        if self.fragment not in ("preserve", "drop"):
            raise AuditError("policy fragment must be preserve or drop")
        if self.idna not in ("preserve", "ascii"):
            raise AuditError("policy idna must be preserve or ascii")
        for name in ("w3lib_sort_query", "allow_ambiguous_merges"):
            if type(getattr(self, name)) is not bool:
                raise AuditError(f"policy {name} must be boolean")
        if not isinstance(self.remove_query_keys, (tuple, list)) or any(
            not isinstance(k, str) or not k or not k.isascii() or
            any(c in k for c in "&=%+#") or any(c.isspace() for c in k)
            for k in self.remove_query_keys
        ):
            raise AuditError("remove_query_keys must list nonempty ASCII decoded key names (no &= %+# or whitespace)")
        if len(set(self.remove_query_keys)) != len(self.remove_query_keys):
            raise AuditError("remove_query_keys contains duplicate names")
        object.__setattr__(self, "remove_query_keys", tuple(self.remove_query_keys))

    @classmethod
    def from_dict(cls, data: object) -> Policy:
        if not isinstance(data, dict):
            raise AuditError("policy must be a JSON object")
        unknown = data.keys() - cls.__dataclass_fields__.keys()
        if unknown:
            raise AuditError(f"unknown policy fields: {', '.join(sorted(unknown))}")
        return cls(**data)


def validate_url(url: object) -> str:
    if not isinstance(url, str) or not url:
        raise AuditError("url must be a nonempty string")
    if any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 or
           0xD800 <= ord(c) <= 0xDFFF for c in url):
        raise AuditError("URL contains whitespace, controls, or unpaired surrogate")
    if any(c in url for c in '\\<>"{}|^`'):
        raise AuditError("URL contains an unescaped forbidden character")
    if re.search(r"%(?![0-9a-fA-F]{2})", url):
        raise AuditError("URL contains malformed percent escape")
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            raise Unsupported("unsupported URL scheme; only HTTP(S) is supported")
        if not re.match(r"(?i)^https?://", url) or not parts.hostname:
            raise AuditError("HTTP(S) URL requires // and a host")
        if parts.username is not None or parts.password is not None:
            raise Unsupported("URL userinfo is unsupported")
        if parts.netloc.startswith("["):
            if not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parts.netloc):
                raise AuditError("invalid bracketed IPv6 authority")
        elif parts.netloc.count(":") > 1:
            raise AuditError("IPv6 host must use brackets")
        port = parts.port  # forces validation, including range
        if parts.netloc.endswith(":") or port == 0:
            raise AuditError("port must be an integer from 1 to 65535")
        host = parts.hostname
        if ":" in host:
            if "%" in host:
                raise Unsupported("IPv6 zone identifiers are unsupported")
            ipaddress.IPv6Address(host)
        else:
            ascii_host = host.encode("idna").decode("ascii")
            if len(ascii_host.rstrip(".")) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in ascii_host.rstrip(".").split(".")
            ):
                raise AuditError("invalid DNS/IDNA host")
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, AuditError):
            raise
        raise AuditError(f"invalid URL: {exc}") from exc
    return url


@dataclass(frozen=True)
class Request:
    index: int
    location: str
    url: str
    method: str
    body: str
    original: object
    metadata: dict

    def exact_key(self) -> tuple[str, str, str]:
        return self.method, self.url, self.body


JSONL_FIELDS = {"url", "method", "body", "source", "metadata"}
# Deliberately narrow subset of Crawlee JS 3.18 RequestOptions JSON sources.
CRAWLEE_FIELDS = {"url", "method", "payload", "userData", "label"}


def parse_request(value: object, index: int, location: str, dialect: str) -> Request:
    original = value
    if dialect == "crawlee" and isinstance(value, str):
        value = {"url": value}
    if not isinstance(value, dict):
        raise AuditError("request must be a JSON object (or URL string in crawlee format)")
    fields = JSONL_FIELDS if dialect == "jsonl" else CRAWLEE_FIELDS
    if value.keys() - fields:
        raise Unsupported("unsupported request fields: " + ", ".join(sorted(value.keys() - fields)))
    url = validate_url(value.get("url"))
    method = value.get("method", "GET")
    if not isinstance(method, str) or not re.fullmatch(r"[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}", method):
        raise AuditError("method must be an uppercase HTTP token, 1..32 characters")
    body = value.get("body" if dialect == "jsonl" else "payload", "")
    if not isinstance(body, str):
        raise AuditError("body/payload must be a string; null and binary/object payloads are unsupported")
    try:
        body.encode("utf-8")
    except UnicodeError as exc:
        raise AuditError("body/payload must be valid Unicode") from exc
    metadata = {k: v for k, v in value.items() if k not in {"url", "method", "body", "payload"}}
    if "source" in metadata and not isinstance(metadata["source"], str):
        raise AuditError("source must be a string")
    if "userData" in metadata and not isinstance(metadata["userData"], dict):
        raise AuditError("Crawlee userData must be an object")
    if "label" in metadata and not isinstance(metadata["label"], str):
        raise AuditError("Crawlee label must be a string")
    if "metadata" in metadata and not isinstance(metadata["metadata"], dict):
        raise AuditError("metadata must be an object")
    if dialect == "crawlee" and metadata:
        metadata = {"metadata": metadata}
    return Request(index, location, url, method, body, original, metadata)


def load_requests(path: Path, dialect: str = "jsonl") -> tuple[list[Request], list[dict], int]:
    """Load every record, preserving invalid/unsupported input as report issues.

    Bad JSONL lines are per-record issues. Invalid UTF-8 or broken Crawlee array
    JSON is a file-level failure: record boundaries cannot be recovered safely.
    """
    return load_request_text(path.read_text(encoding="utf-8"), dialect)


def load_request_text(text: str, dialect: str = "jsonl") -> tuple[list[Request], list[dict], int]:
    """Parse a local batch snapshot with the same rules as file input."""
    if dialect not in ("jsonl", "crawlee"):
        raise AuditError("format must be jsonl or crawlee")
    if dialect == "crawlee":
        values = strict_json(text)
        if not isinstance(values, list):
            raise AuditError("crawlee input must be a JSON array of local URL/request sources")
        entries = [(f"item {i}", value, False) for i, value in enumerate(values, 1)]
    else:
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()  # ordinary terminal newline is not an extra record
        entries = [(f"line {i}", line, True) for i, line in enumerate(lines, 1)]
    requests, issues = [], []
    for index, (location, value, needs_parse) in enumerate(entries, 1):
        try:
            record = strict_json(value) if needs_parse else value
            requests.append(parse_request(record, index, location, dialect))
        except AuditError as exc:
            issues.append({"index": index, "location": location,
                           "kind": "unsupported" if isinstance(exc, Unsupported) else "invalid",
                           "error": str(exc), "original": value})
    return requests, issues, len(entries)


def proposed_url(url: str, policy: Policy) -> str:
    """Transform only explicit components, retaining raw delimiters by default."""
    base, fragment_mark, fragment = url.partition("#")
    path, query_mark, query = base.partition("?")
    if policy.remove_query_keys and query_mark:
        keys = {k.encode("ascii") for k in policy.remove_query_keys}
        fields = query.split("&")
        kept = [field for field in fields if
                unquote_to_bytes(field.partition("=")[0].replace("+", " ")) not in keys]
        if kept != fields:
            query = "&".join(kept)
            query_mark = "?" if kept else ""
    if policy.w3lib_sort_query and query_mark:
        # Use the real upstream query canonicalizer in a fixed dummy URL.
        # Applying it to the whole input would also change host/path/IDNA.
        query = urlsplit(canonicalize_url("https://query.invalid/?" + query,
                                         keep_blank_values=True, keep_fragments=True)).query
        query_mark = "?" if query else ""
    if policy.idna == "ascii":
        prefix, rest = path.split("://", 1)
        authority, slash, suffix = rest.partition("/")
        if not authority.startswith("["):
            host, colon, port = authority.partition(":")
            authority = host.encode("idna").decode("ascii") + colon + port
        path = prefix + "://" + authority + slash + suffix
    if policy.fragment == "drop":
        fragment_mark, fragment = "", ""
    return path + query_mark + query + fragment_mark + fragment


def fingerprint(key: tuple[str, str, str]) -> str:
    """Digest is display-only; dedup itself compares full tuples, not hashes."""
    return hashlib.sha256(json_text(key).encode("ascii")).hexdigest()


def audit(requests: list[Request], policy: Policy, *, issues: list[dict] | None = None,
          total: int | None = None, on_invalid: str = "fail", robots: RobotsPolicy | None = None,
          user_agent: str | None = None, enforce_robots: bool = False,
          strict_robots: bool = False) -> tuple[list[dict], dict]:
    """Return the simulated candidate and report; rejected candidates must not be used."""
    if type(enforce_robots) is not bool:
        raise AuditError("enforce_robots must be boolean")
    if type(strict_robots) is not bool:
        raise AuditError("strict_robots must be boolean")
    if strict_robots and not enforce_robots:
        raise AuditError("strict_robots requires enforce_robots=True")
    if (robots is None) != (user_agent is None):
        raise AuditError("robots manifest and explicit user_agent must be supplied together")
    if enforce_robots and robots is None:
        raise AuditError("robots enforcement requires robots manifest and explicit user_agent")
    if on_invalid not in ("fail", "skip"):
        raise AuditError("on_invalid must be fail or skip")
    issues = [] if issues is None else issues
    exact_groups: dict[tuple, list[Request]] = defaultdict(list)
    proposed_groups: dict[tuple, list[Request]] = defaultdict(list)
    url_groups: dict[str, list[Request]] = defaultdict(list)
    changed, frontier = [], []
    for request in requests:
        target = proposed_url(request.url, policy)
        exact_groups[request.exact_key()].append(request)
        key = request.method, target, request.body
        proposed_groups[key].append(request)
        url_groups[target].append(request)
        if target != request.url:
            changed.append({"index": request.index, "location": request.location,
                            "original": request.original, "exact_url": request.url,
                            "proposed_url": target, "exact_fingerprint": fingerprint(request.exact_key()),
                            "proposed_fingerprint": fingerprint(key)})
    def witness(request: Request) -> dict:
        return {"index": request.index, "location": request.location, "original": request.original,
                "method": request.method, "body": request.body, "url": request.url}
    def collision(key: tuple, group: list[Request]) -> dict:
        distinct = len({r.exact_key() for r in group})
        return {"fingerprint": fingerprint(key), "canonical_url": key[1], "method": key[0],
                "body": key[2], "representative_index": group[0].index,
                "distinct_exact_keys": distinct, "policy_created": distinct > 1,
                "members": [witness(r) for r in group]}
    exact_collisions = [collision(k, g) for k, g in exact_groups.items() if len(g) > 1]
    collisions = [collision(k, g) for k, g in proposed_groups.items() if len(g) > 1]
    protected = []
    for url, group in url_groups.items():
        if len({(r.method, r.body) for r in group}) > 1:
            protected.append({"canonical_url": url,
                              "policy_created": len({r.url for r in group}) > 1,
                              "members": [witness(r) for r in group]})
    for key, group in proposed_groups.items():
        rep = group[0]
        item = {"url": rep.url, "method": rep.method, "body": rep.body, **rep.metadata}
        frontier.append(item)
    merge_count = sum(c["policy_created"] for c in collisions)
    protected_created = sum(c["policy_created"] for c in protected)
    errors, warnings = [], []
    if issues and on_invalid == "fail":
        errors.append(f"{len(issues)} invalid/unsupported records; use --on-invalid skip to audit the rest")
    if merge_count or protected_created:
        message = (f"policy creates {merge_count} request collision groups and {protected_created} "
                   "URL collision groups with different methods/bodies (kept separate)")
        if policy.allow_ambiguous_merges:
            warnings.append(message + "; explicitly permitted, semantic equivalence is unverified")
        else:
            errors.append(message + "; set allow_ambiguous_merges=true only after reviewing witnesses")
    if protected:
        warnings.append(f"{len(protected)} URL groups have distinct methods/bodies; fingerprints preserve them")
    # Metadata is retained in witnesses and the first representative; differences
    # are visible even when the request tuple is an exact duplicate.
    metadata_differences = [
        {"representative_index": group[0].index, "member_indices": [r.index for r in group]}
        for group in proposed_groups.values() if len({json_text(r.metadata) for r in group}) > 1
    ]
    if metadata_differences:
        warnings.append(f"{len(metadata_differences)} duplicate groups have different provenance metadata; review originals")
    counts = {"raw_records": len(requests) + len(issues) if total is None else total,
              "valid_requests": len(requests), "invalid": sum(i["kind"] == "invalid" for i in issues),
              "unsupported": sum(i["kind"] == "unsupported" for i in issues),
              "raw_url_unique": len({r.url for r in requests}),
              "exact_unique": len(exact_groups), "conservative_unique": len(exact_groups),
              "proposed_unique": len(proposed_groups), "accepted": len(frontier),
              "duplicates": len(requests) - len(frontier), "changed_keys": len(changed),
              "policy_created_collision_groups": merge_count}
    report = {"schema_version": 1, "tool_version": "0.1.0", "policy": asdict(policy),
              "status": "rejected" if errors else "ok", "on_invalid": on_invalid,
              "counts": counts, "issues": issues, "errors": errors, "warnings": warnings,
              "representatives": [{"index": group[0].index, "fingerprint": fingerprint(key),
                                   "canonical_url": key[1], "original": group[0].original}
                                  for key, group in proposed_groups.items()],
              "exact_collision_groups": exact_collisions, "collision_groups": collisions,
              "protected_url_groups": protected, "changed_key_witnesses": changed,
              "metadata_differences": metadata_differences,
              "frontier_emitted": False}
    if robots is not None:
        report["robots"] = robots.audit(requests, user_agent)
        if enforce_robots:
            report["robots"]["mode"] = "offline-enforced"
        if strict_robots:
            from .robots import strict_rule_diagnostics

            report["robots"]["strict"] = True
            malformed = strict_rule_diagnostics(report["robots"]["origins"], robots)
            report["robots"]["malformed_rules"] = malformed
            if malformed:
                report["errors"].append(
                    f"strict robots rejected: {len(malformed)} malformed-rule diagnostics; "
                    "see robots.malformed_rules for origin/source/line/reason")
                report["status"] = "rejected"
        counts = report["robots"]["counts"]
        if counts["disallow"] or counts["unknown"]:
            if enforce_robots:
                report["errors"].append(
                    f"robots enforcement rejected: {counts['disallow']} disallowed, {counts['unknown']} unknown "
                    "decisions; every valid original request must be allowed")
                report["status"] = "rejected"
            else:
                report["warnings"].append(
                    f"offline robots audit: {counts['disallow']} disallowed, {counts['unknown']} unknown; frontier unchanged")
    return frontier, report
