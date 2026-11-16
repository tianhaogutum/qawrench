"""Local snapshot audit using RFC 9309 groups and path matching; no fetching."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit

from .core import AuditError, Request, validate_url

_TOKEN = re.compile(r"[A-Za-z_-]+\Z")
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def origin_for(url: str) -> str:
    """Canonical policy scope, independent of request identity normalization."""
    parts = urlsplit(validate_url(url))
    host = parts.hostname
    if ":" in host:
        host = "[" + ipaddress.IPv6Address(host).compressed + "]"
    else:
        host = host.encode("idna").decode("ascii").lower()
    port = parts.port
    suffix = "" if port is None or port == {"http": 80, "https": 443}[parts.scheme] else f":{port}"
    return f"{parts.scheme}://{host}{suffix}"


def _encoded(value: str) -> str:
    """Normalize escaped unreserved octets, retaining escaped reserved octets."""
    result = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "%":
            byte = int(value[i + 1:i + 3], 16)
            result.append(chr(byte) if byte in _UNRESERVED else f"%{byte:02X}")
            i += 3
            continue
        for byte in char.encode("utf-8"):
            # * and $ in a URL are literals, distinct from pattern operators.
            result.append(f"%{byte:02X}" if byte >= 128 or byte in (42, 36) else chr(byte))
        i += 1
    return "".join(result)


@dataclass(frozen=True)
class Rule:
    line: int
    directive: str
    pattern: str
    parts: tuple[str, ...]
    anchored: bool
    specificity: int

    def matches(self, target: str) -> bool:
        # Ordered literal searches avoid regex backtracking on untrusted input.
        if not target.startswith(self.parts[0]):
            return False
        position = len(self.parts[0])
        if len(self.parts) == 1:
            return not self.anchored or position == len(target)
        for part in self.parts[1:-1]:
            found = target.find(part, position)
            if found < 0:
                return False
            position = found + len(part)
        last = self.parts[-1]
        if self.anchored:
            return target.endswith(last) and len(target) - len(last) >= position
        return target.find(last, position) >= 0

    def witness(self) -> dict:
        return {"line": self.line, "directive": self.directive, "pattern": self.pattern,
                "specificity": self.specificity}


def _parse(text: str) -> tuple[list[dict], list[dict]]:
    groups, diagnostics = [], []
    group = None
    has_rules = False
    # Only CR/LF are record separators; Unicode line separators are path data.
    text = text.removeprefix("\ufeff")
    for number, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        content = raw.partition("#")[0].strip(" \t")
        if not content:
            continue
        field, colon, value = content.partition(":")
        field, value = field.strip(" \t").lower(), value.strip(" \t")
        if not colon or field not in ("user-agent", "allow", "disallow"):
            diagnostics.append({"line": number, "text": raw, "reason": "ignored non-rule record"})
            continue
        if field == "user-agent":
            if group is None or has_rules:
                group = {"agents": [], "rules": []}
                groups.append(group)
                has_rules = False
            if value == "*" or _TOKEN.fullmatch(value):
                group["agents"].append({"line": number, "value": value})
            else:
                diagnostics.append({"line": number, "text": raw, "reason": "invalid user-agent token"})
            continue
        has_rules = True
        if group is None or not group["agents"]:
            diagnostics.append({"line": number, "text": raw, "reason": "rule outside a valid group"})
            continue
        if not value:  # empty rules impose no restriction
            continue
        if (not value.startswith("/") or re.search(r"%(?![0-9a-fA-F]{2})", value)
                or re.search(r"[\x00-\x20\x7f]", value)):
            diagnostics.append({"line": number, "text": raw, "reason": "invalid path pattern"})
            continue
        anchored = value.endswith("$")
        pattern = value[:-1] if anchored else value
        parts = tuple(_encoded(part) for part in pattern.split("*"))
        specificity = len("*".join(parts)) + int(anchored)
        group["rules"].append(Rule(number, field, value, parts, anchored, specificity))
    return groups, diagnostics


def strict_rule_diagnostics(origins: list[dict], policy: RobotsPolicy) -> list[dict]:
    """Scope blocking witnesses without changing tolerant parser diagnostics."""
    malformed = []
    for origin in origins:
        record_spans = None
        for diagnostic in origin["diagnostics"]:
            reason = diagnostic["reason"]
            if reason == "ignored non-rule record":
                content = diagnostic["text"].partition("#")[0].strip(" \t")
                # Exact ASCII field, then SP/HTAB or end. A colon in the value
                # cannot supply the delimiter immediately after the field.
                match = re.match(r"(?:user-agent|allow|disallow)(?=[ \t]|$)", content,
                                 re.IGNORECASE | re.ASCII)
                if not match or content[match.end():].lstrip(" \t").startswith(":"):
                    continue
                reason = "missing colon after core directive"
            elif reason not in ("invalid path pattern", "invalid user-agent token",
                                "rule outside a valid group"):
                continue
            # Tolerant diagnostics historically omit the single initial BOM.
            # Recognize using that same text, but restore the signature only in
            # strict original-record witnesses; other BOMs are already intact.
            text = diagnostic["text"]
            if diagnostic["line"] == 1 and policy.origins[origin["origin"]]["text"].startswith("\ufeff"):
                text = "\ufeff" + text
            if record_spans is None:
                snapshot = policy.origins[origin["origin"]]["text"].encode("utf-8")
                # Byte positions in the original snapshot, before BOM removal
                # or newline normalization. CRLF is one separator; Unicode
                # line separators are ordinary data, just as in _parse.
                record_spans = [match.span(1) for match in re.finditer(
                    rb"([^\r\n]*)(?:\r\n|\r|\n|$)", snapshot)]
            start, end = record_spans[diagnostic["line"] - 1]
            malformed.append({"origin": origin["origin"], "source": origin["source"],
                              **diagnostic, "text": text, "reason": reason,
                              "snapshot_sha256": origin["sha256"],
                              "byte_start": start, "byte_end": end})
    return malformed


@dataclass(frozen=True)
class RobotsPolicy:
    """Validated version-1 manifest containing inline snapshots, never URLs to fetch."""

    origins: dict[str, dict]

    @classmethod
    def from_dict(cls, value: object, *, max_robots_bytes: int | None = None) -> RobotsPolicy:
        if max_robots_bytes is not None and (type(max_robots_bytes) is not int or max_robots_bytes < 1):
            raise AuditError("max_robots_bytes must be a positive integer or None")
        if (not isinstance(value, dict) or set(value) != {"version", "origins"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["origins"], dict)):
            raise AuditError("robots manifest requires version 1 and an origins object")
        origins = {}
        for origin, entry in value["origins"].items():
            canonical = origin_for(origin)
            parts = urlsplit(origin)
            if parts.path not in ("", "/") or "?" in origin or "#" in origin:
                raise AuditError(f"robots scope must be an origin, not a URL path: {origin}")
            if canonical in origins:
                raise AuditError(f"duplicate normalized robots origin: {canonical}")
            if not isinstance(entry, dict) or entry.get("status") not in ("available", "unavailable"):
                raise AuditError(f"robots origin {origin}: status must be available or unavailable")
            field = "text" if entry["status"] == "available" else "reason"
            if set(entry) != {"status", "source", field} or any(not isinstance(v, str) for v in entry.values()):
                raise AuditError(f"robots origin {origin}: requires only status, source and string {field}")
            if not entry["source"].strip() or (field == "reason" and not entry[field].strip()):
                raise AuditError(f"robots origin {origin}: source/reason must be nonempty")
            try:
                for item in entry.values():
                    item.encode("utf-8")
            except UnicodeError as exc:
                raise AuditError(f"robots origin {origin}: invalid Unicode") from exc
            if field == "text" and max_robots_bytes is not None:
                size = len(entry["text"].encode("utf-8"))
                if size > max_robots_bytes:
                    raise AuditError(f"robots snapshot exceeds byte limit: origin={canonical} "
                                     f"source={entry['source']!r} actual_bytes={size} "
                                     f"limit_bytes={max_robots_bytes}")
            origins[canonical] = dict(entry)
        return cls(origins)

    def audit(self, requests: list[Request], user_agent: str) -> dict:
        if not isinstance(user_agent, str) or not _TOKEN.fullmatch(user_agent):
            raise AuditError("robots user-agent must be an explicit product token (ASCII letters, '_' or '-')")
        origins = {}
        counts = {"allow": 0, "disallow": 0, "unknown": 0}
        for request in requests:
            origin = origin_for(request.url)
            if origin not in origins:
                entry = self.origins.get(origin, {"status": "unknown", "reason": "no local snapshot supplied"})
                output = {k: v for k, v in entry.items() if k != "text"}
                output.update(origin=origin, robots_url=origin + "/robots.txt", requests=[],
                              selected_agents=[], diagnostics=[])
                rules = []
                if entry["status"] == "available":
                    text = entry["text"]
                    output["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    groups, output["diagnostics"] = _parse(text)
                    selected = [g for g in groups if any(a["value"].lower() == user_agent.lower() for a in g["agents"])]
                    if not selected:
                        selected = [g for g in groups if any(a["value"] == "*" for a in g["agents"])]
                    output["selected_agents"] = [a for g in selected for a in g["agents"]]
                    rules = [rule for g in selected for rule in g["rules"]]
                origins[origin] = output, rules
            output, rules = origins[origin]
            parts = urlsplit(request.url)
            path = parts.path or "/"
            # Fragment never reaches an HTTP server; keep even an empty query marker.
            target = _encoded(path + ("?" + parts.query if "?" in request.url.partition("#")[0] else ""))
            matches = [rule for rule in rules if rule.matches(target)]
            winner = max(matches, key=lambda r: (r.specificity, r.directive == "allow", -r.line), default=None)
            if output["status"] != "available":
                decision, reason = "unknown", output["reason"]
            elif _encoded(path) == "/robots.txt":
                decision, reason = "allow", "robots.txt is implicitly allowed"
                winner = None
            elif winner:
                decision, reason = winner.directive, "most specific matching rule; allow wins ties"
            else:
                decision, reason = "allow", "no matching rule in selected groups"
            counts[decision] += 1
            output["requests"].append({"index": request.index, "location": request.location,
                "url": request.url, "target": target, "decision": decision, "reason": reason,
                "matched_rules": [r.witness() for r in matches],
                "winning_rule": winner.witness() if winner else None})
        return {"user_agent": user_agent, "mode": "offline-informational", "counts": counts,
                "origins": [output for output, _ in origins.values()]}
