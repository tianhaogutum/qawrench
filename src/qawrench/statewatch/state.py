"""Bounded snapshots and established DeepDiff comparisons."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from fnmatch import fnmatchcase
from typing import Any

from deepdiff import DeepDiff

from .policy import Policy

MISSING = {"status": "missing"}


def _validate(value: Any, seen: set[int], budget: list[int], depth: int = 0) -> None:
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("value has too many nodes for the serialized byte limit")
    if depth > 50:
        raise ValueError("value nesting exceeds 50 levels")
    if type(value) in (str, int, float, bool, type(None)):
        return
    if type(value) not in (list, dict):
        raise TypeError(f"unsupported {type(value).__name__}; use JSON-native values")
    if id(value) in seen:
        raise ValueError("cyclic value")
    seen.add(id(value))
    try:
        if type(value) is dict:
            if any(type(k) is not str for k in value):
                raise TypeError("dictionary keys must be strings")
            children = value.values()
        else:
            children = value
        for child in children:
            _validate(child, seen, budget, depth + 1)
    finally:
        seen.remove(id(value))


def freeze_json(value: Any, limit: int) -> tuple[Any, bytes, int]:
    if type(value) in (str, list, dict) and len(value) > limit:
        raise ValueError(f"value exceeds {limit} serialized bytes")
    _validate(value, set(), [limit])
    chunks = []
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False, sort_keys=True).iterencode(
        value
    ):
        size += len(chunk)
        if size > limit:
            raise ValueError(f"value exceeds {limit} serialized bytes")
        chunks.append(chunk)
    encoded = "".join(chunks)
    return json.loads(encoded), hashlib.sha256(encoded.encode("ascii")).digest(), size


def clone_json(value: Any, limit: int) -> Any:
    return freeze_json(value, limit)[0]


@dataclass
class Snapshot:
    values: dict[str, dict]
    errors: dict[str, str]
    signatures: dict[str, tuple[str, bytes]] = dataclass_field(default_factory=dict)

    def get(self, field: str) -> dict:
        return self.values.get(field, MISSING)

    def signature(self, field: str) -> tuple[str, bytes]:
        if field not in self.values:
            return ("missing", b"")
        if field not in self.signatures:
            sample = self.values[field]
            payload = json.dumps(sample, sort_keys=True, ensure_ascii=True).encode("ascii")
            self.signatures[field] = (sample["status"], hashlib.sha256(payload).digest())
        return self.signatures[field]

    def same(self, other: Snapshot, field: str) -> bool:
        return self.signature(field) == other.signature(field)


def capture(policy: Policy) -> Snapshot:
    values = {}
    errors = {}
    signatures = {}
    remaining = policy.max_snapshot_bytes

    def observe(field: str, callback) -> None:
        nonlocal remaining
        try:
            if remaining <= 0:
                raise ValueError("snapshot serialized value budget exceeded")
            limit = min(policy.max_value_bytes, remaining)
            try:
                value, signature, size = freeze_json(callback(), limit)
            except ValueError as exc:
                if limit < policy.max_value_bytes:
                    raise ValueError("snapshot serialized value budget exceeded") from exc
                raise
            remaining -= size
            values[field] = {"status": "value", "value": value}
            signatures[field] = ("value", signature)
        except Exception as exc:
            # Error messages from user objects may contain secrets. Only messages
            # from our own validation exceptions are safe to persist.
            reason = str(exc) if type(exc) in (ValueError, TypeError) else type(exc).__name__
            errors[field] = reason

    for key in sorted(os.environ):
        if key == "PYTEST_CURRENT_TEST":
            continue  # pytest's own phase bookkeeping, never application state
        if any(fnmatchcase(key, pattern) for pattern in policy.env):
            observe(f"env:{key}", lambda key=key: os.environ[key])
    for pattern in policy.env:
        if not any(char in pattern for char in "*?[") and pattern != "PYTEST_CURRENT_TEST":
            values.setdefault(f"env:{pattern}", dict(MISSING))
            signatures.setdefault(f"env:{pattern}", ("missing", b""))
    if policy.cwd:
        observe("cwd", os.getcwd)
    for target in policy.variables:
        field = f"var:{target}"
        module_name, attrs = target.split(":")
        module = sys.modules.get(module_name)
        if module is None:
            values[field] = {"status": "unloaded"}
            signatures[field] = ("unloaded", b"")
            continue
        try:
            value = module
            for attr in attrs.split("."):
                value = inspect.getattr_static(value, attr)
        except AttributeError:
            values[field] = dict(MISSING)
            signatures[field] = ("missing", b"")
        except Exception as exc:
            errors[field] = type(exc).__name__
        else:
            observe(field, lambda value=value: value)
    return Snapshot(values, errors, signatures)


def equivalent(a: dict, b: dict) -> bool:
    return not DeepDiff(a, b, verbose_level=2, zip_ordered_iterables=True)


def visible(policy: Policy, field: str, sample: dict) -> dict:
    if policy.redacted(field) and sample.get("status") == "value":
        return {"status": "value", "value": "<redacted>"}
    return sample


def changes(policy: Policy, before: Snapshot, after: Snapshot) -> list[dict]:
    result = []
    for field in sorted(before.values.keys() | after.values.keys()):
        if field in before.errors or field in after.errors:
            continue
        if before.same(after, field):
            continue
        old, new = before.get(field), after.get(field)
        diff = DeepDiff(old, new, verbose_level=2, zip_ordered_iterables=True)
        if not diff:
            continue
        redacted = policy.redacted(field)
        result.append(
            {
                "field": field,
                "before": visible(policy, field, old),
                "after": visible(policy, field, new),
                "diff": {"changed": True, "redacted": True}
                if redacted
                else json.loads(diff.to_json()),
                "allowed": policy.allowed(field),
            }
        )
    return result
