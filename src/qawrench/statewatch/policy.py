"""Small, strict JSON policy; no expression evaluation or implicit module imports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path

DEFAULT_REDACT = ("env:*TOKEN*", "env:*SECRET*", "env:*PASSWORD*", "env:*KEY*")


def valid_variable(target: str) -> bool:
    if target.count(":") != 1:
        return False
    module, attributes = target.split(":")
    return all(part.isidentifier() for path in (module, attributes) for part in path.split("."))


class PolicyError(ValueError):
    """An actionable invalid-policy error."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


@dataclass(frozen=True)
class Policy:
    env: tuple[str, ...] = ()
    cwd: bool = False
    variables: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    redact: tuple[str, ...] = DEFAULT_REDACT
    max_value_bytes: int = 65536
    max_snapshot_bytes: int = 1048576

    @classmethod
    def load(cls, path: Path) -> Policy:
        try:
            if path.stat().st_size > 1024 * 1024:
                raise PolicyError("policy exceeds 1 MiB")
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise PolicyError(f"cannot read policy {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PolicyError("policy must be a JSON object")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise PolicyError(f"unknown policy keys: {', '.join(sorted(unknown))}")
        for key in ("env", "variables", "allow", "redact"):
            value = data.get(key, [])
            if not isinstance(value, list) or any(type(x) is not str or not x for x in value):
                raise PolicyError(f"{key} must be a list of nonempty strings")
            data[key] = tuple(dict.fromkeys(value))
        for name in data["variables"]:
            if not valid_variable(name):
                raise PolicyError(f"invalid variable {name!r}; use module:attribute.path")
        if type(data.get("cwd", False)) is not bool:
            raise PolicyError("cwd must be true or false")
        limit = data.get("max_value_bytes", 65536)
        if type(limit) is not int or not 128 <= limit <= 1048576:
            raise PolicyError("max_value_bytes must be an integer between 128 and 1048576")
        total = data.get("max_snapshot_bytes", 1048576)
        if type(total) is not int or not 1024 <= total <= 8388608:
            raise PolicyError("max_snapshot_bytes must be an integer between 1024 and 8388608")
        data["redact"] = tuple(dict.fromkeys((*DEFAULT_REDACT, *data["redact"])))
        policy = cls(**data)
        if not policy.env and not policy.cwd and not policy.variables:
            raise PolicyError("select at least one env pattern, cwd, or variable")
        return policy

    def matches(self, field: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatchcase(field, pattern) for pattern in patterns)

    def allowed(self, field: str) -> bool:
        return self.matches(field, self.allow)

    def redacted(self, field: str) -> bool:
        return self.matches(field.upper(), tuple(x.upper() for x in self.redact))

    def to_dict(self) -> dict:
        return asdict(self)
