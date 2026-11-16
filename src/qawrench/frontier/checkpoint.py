"""Versioned, self-contained batch history for conservative frontier merging."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import re

from .core import AuditError, Policy, Request, load_request_text


def validate_batch_id(batch_id: str | None) -> None:
    if batch_id is not None and (not isinstance(batch_id, str) or
                                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", batch_id)):
        raise AuditError("batch_id must be 1..128 ASCII characters: [A-Za-z0-9][A-Za-z0-9._:-]*")


class BatchConflict(AuditError):
    """An existing ID cannot identify a different format or exact text."""

    def __init__(self, saved: Batch, dialect: str, text: str):
        self.conflict = {"batch_id": saved.batch_id,
                         "checkpoint": {"format": saved.format, "text_sha256": saved.text_sha256},
                         "requested": {"format": dialect,
                                       "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}}
        super().__init__(f"checkpoint batch ID conflict: {saved.batch_id}")


@dataclass(frozen=True)
class Batch:
    format: str
    text: str
    batch_id: str | None = None

    def __post_init__(self) -> None:
        validate_batch_id(self.batch_id)
        if self.format not in ("jsonl", "crawlee") or not isinstance(self.text, str):
            raise AuditError("checkpoint batch requires format jsonl/crawlee and string text")
        try:
            self.text.encode("utf-8")
        except UnicodeError as exc:
            raise AuditError("checkpoint batch text must be valid Unicode") from exc
        # Broken array envelopes cannot be skipped; JSONL issues can be audited.
        self.load_requests()

    def load_requests(self) -> tuple[list[Request], list[dict], int]:
        # Match file loading's universal newline parsing without changing the
        # exact snapshot used for batch identity or serialization.
        return load_request_text(self.text.replace("\r\n", "\n").replace("\r", "\n"), self.format)

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    policy: Policy
    batches: tuple[Batch, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, Policy) or not isinstance(self.batches, (tuple, list)) or any(
                not isinstance(batch, Batch) for batch in self.batches):
            raise AuditError("checkpoint requires a Policy and Batch history")
        object.__setattr__(self, "batches", tuple(self.batches))
        ids = [batch.batch_id for batch in self.batches if batch.batch_id is not None]
        if len(ids) != len(set(ids)):
            raise AuditError("checkpoint contains duplicate batch IDs")

    @property
    def version(self) -> int:
        return 2 if any(batch.batch_id is not None for batch in self.batches) else 1

    @classmethod
    def from_dict(cls, data: object) -> Checkpoint:
        if not isinstance(data, dict) or data.keys() != {"version", "policy", "batches"}:
            raise AuditError("checkpoint requires exactly version, policy and batches")
        if type(data["version"]) is not int or data["version"] not in (1, 2):
            raise AuditError("checkpoint version must be 1 or 2")
        if not isinstance(data["policy"], dict) or data["policy"].keys() != Policy.__dataclass_fields__.keys():
            raise AuditError("checkpoint must retain the complete dedupe policy")
        policy = Policy.from_dict(data["policy"])
        if not isinstance(data["batches"], list) or not data["batches"]:
            raise AuditError("checkpoint batches must be a nonempty array")
        batches = []
        fields = {"format", "text"} | ({"batch_id"} if data["version"] == 2 else set())
        for batch in data["batches"]:
            if not isinstance(batch, dict) or batch.keys() != fields:
                raise AuditError("checkpoint batch requires exactly " + ", ".join(sorted(fields)))
            batches.append(Batch(**batch))
        return cls(policy, tuple(batches))

    def to_dict(self) -> dict:
        version = self.version
        return {"version": version, "policy": asdict(self.policy),
                "batches": [{key: value for key, value in asdict(batch).items()
                             if version == 2 or key != "batch_id"} for batch in self.batches]}

    def append(self, text: str, dialect: str = "jsonl", *, batch_id: str | None = None) -> Checkpoint:
        validate_batch_id(batch_id)
        if batch_id is not None:
            for saved in self.batches:
                if saved.batch_id == batch_id:
                    if saved.format == dialect and saved.text == text:
                        return self
                    # Validate scalar types/Unicode, but report conflicting content even
                    # when the new text has a broken Crawlee array envelope.
                    if dialect not in ("jsonl", "crawlee") or not isinstance(text, str):
                        raise AuditError("checkpoint batch requires format jsonl/crawlee and string text")
                    try:
                        text.encode("utf-8")
                    except UnicodeError as exc:
                        raise AuditError("checkpoint batch text must be valid Unicode") from exc
                    raise BatchConflict(saved, dialect, text)
        return Checkpoint(self.policy, self.batches + (Batch(dialect, text, batch_id),))

    def policy_conflicts(self, requested: Policy) -> list[dict]:
        old, new = asdict(self.policy), asdict(requested)
        conflicts = []
        for field in old:
            # Removal-key ordering has no effect on identity or merge permission.
            left, right = old[field], new[field]
            if field == "remove_query_keys":
                left, right = sorted(left), sorted(right)
            if left != right:
                conflicts.append({"field": field, "checkpoint": old[field], "requested": new[field]})
        return conflicts

    def load_requests(self) -> tuple[list[Request], list[dict], int]:
        """Re-audit originals, including prior nonrepresentatives and issues."""
        requests, issues, total = [], [], 0
        for number, batch in enumerate(self.batches, 1):
            records, errors, count = batch.load_requests()
            requests.extend(replace(r, index=r.index + total, location=f"batch {number} {r.location}")
                            for r in records)
            issues.extend({**e, "index": e["index"] + total, "location": f"batch {number} {e['location']}"}
                          for e in errors)
            total += count
        return requests, issues, total
