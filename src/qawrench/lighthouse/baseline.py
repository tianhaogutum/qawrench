"""Self-contained, policy-bound local baseline snapshots; no file writes."""

from copy import deepcopy
import hashlib
from pathlib import Path

from .core import _keys, _metric_result, _project_run, canonical, load_json, validate_policy

KIND = "lighthouse-noise-gate-baseline"


def _digest(lhr: dict) -> str:
    return hashlib.sha256(canonical(lhr).encode("utf-8")).hexdigest()


def _validated_runs(manifest: dict, policy: dict) -> list[dict]:
    _keys(manifest, {"kind", "schema_version", "policy", "runs"}, "baseline manifest")
    if manifest["kind"] != KIND or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported baseline manifest kind or schema_version")
    validate_policy(manifest["policy"])
    if canonical(manifest["policy"]) != canonical(policy):
        raise ValueError("baseline manifest policy does not match comparison policy; save a new baseline explicitly")
    if not isinstance(manifest["runs"], list) or not manifest["runs"]:
        raise ValueError("baseline manifest runs must be a nonempty array")
    names, runs = set(), []
    for entry in manifest["runs"]:
        _keys(entry, {"file", "sha256", "lhr"}, "baseline manifest run")
        name = entry["file"]
        if (not isinstance(name, str) or not name.endswith(".json")
                or any(c in name for c in ("/", "\\", "\0")) or name in names):
            raise ValueError("baseline manifest run file must be a unique *.json filename without path components")
        names.add(name)
        if not isinstance(entry["lhr"], dict):
            raise ValueError(f"baseline manifest {name}: lhr must be an object")
        if entry["sha256"] != _digest(entry["lhr"]):
            raise ValueError(f"baseline manifest {name}: checksum mismatch")
        run = _project_run(entry["lhr"], name, policy["metrics"])
        if run["errors"]:
            raise ValueError(f"baseline {name}: {'; '.join(run['errors'])}")
        if any(m["status"] != "valid" for m in run["metrics"].values()):
            raise ValueError(f"baseline {name}: every selected metric must have a complete valid numericValue")
        runs.append(run)
    groups: dict[str, list[dict]] = {}
    for run in runs:
        groups.setdefault(run["group_id"], []).append(run)
    for group_id, members in groups.items():
        if len(members) < policy["min_samples"]:
            raise ValueError(f"baseline group {group_id}: at least {policy['min_samples']} valid samples required")
        for metric, rule in policy["metrics"].items():
            result = _metric_result(members, members, metric, rule, policy)
            if result["decision"] == "invalid":
                raise ValueError(f"baseline group {group_id}/{metric}: {'; '.join(result['reasons'])}")
    return sorted(runs, key=lambda run: run["file"])


def create_baseline(baseline_dir: str | Path, policy: dict) -> dict:
    """Return a validated manifest; require complete, sufficiently sampled groups.

    Full parsed LHRs are embedded to keep the manifest portable and revalidatable.
    Checksums detect accidental edits, not authenticated measurement provenance.
    """
    validate_policy(policy)
    directory = Path(baseline_dir)
    if not directory.is_dir():
        raise ValueError(f"baseline directory does not exist or is not a directory: {directory}")
    snapshots = []
    for path in sorted(directory.glob("*.json")):
        lhr = load_json(path)
        snapshots.append({"file": path.name, "sha256": _digest(lhr), "lhr": lhr})
    manifest = {"kind": KIND, "schema_version": 1, "policy": deepcopy(policy), "runs": snapshots}
    _validated_runs(manifest, policy)
    return manifest


def load_baseline(path: str | Path, policy: dict) -> list[dict]:
    """Verify manifest structure, policy, checksums, LHRs and per-group samples."""
    return _validated_runs(load_json(path), policy)
