"""Read-only compatibility of declared, verified-export or checkpoint schemas."""
from pathlib import Path

from .core import ExportError, MAX_SCHEMA_BYTES, Schema, _parse, _read, verify_export


def _load_schema(path, label):
    try:
        return Schema.parse(_parse(_read(Path(path), MAX_SCHEMA_BYTES), label))
    except (ExportError, OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ExportError(f"{label}: {exc}") from exc


def check_export_schema_compatibility(previous_export, new_schema):
    """Verify PREVIOUS and its sidecar, then compare its declared schema -> NEW.

    Uses the manifest returned by full verify_export, never a sidecar alone.
    Requires quiescent artifacts and PyArrow; creates no extracted schema or output.
    Compatibility rules and reports match check_schema_compatibility exactly.
    """
    manifest = verify_export(previous_export)
    return _compare(Schema.parse(manifest["schema"]), _load_schema(new_schema, "new schema"))


def check_exports_schema_compatibility(previous_export, candidate_export):
    """Fully verify both exports, then compare PREVIOUS schema -> CANDIDATE.

    Only the schemas returned by verify_export are consumed. Original source and
    schema files are unnecessary; both artifact pairs must remain quiescent.
    Compatibility describes declarations, not equivalence of the exported rows.
    """
    previous = verify_export(previous_export)
    candidate = verify_export(candidate_export)
    return _compare(Schema.parse(previous["schema"]), Schema.parse(candidate["schema"]))


def check_resume_schema_compatibility(directory, new_schema):
    """Fully inspect a checkpoint, then compare its verified schema -> NEW.

    No original schema file or published destination is needed. This comparison
    does not authorize changing the binding of an existing resumable export.
    """
    from .resume import _inspect_resume
    _, spec = _inspect_resume(directory)
    return _compare(spec, _load_schema(new_schema, "new schema"))


def check_export_resume_schema_compatibility(previous_export, directory):
    """Compare a fully verified export schema -> fully inspected checkpoint schema.

    Consume only the verification result and the exact parsed schema returned by
    the locked checkpoint inspection. No schema files or published checkpoint
    destination are needed; the existing resume binding remains unchanged.
    """
    from .resume import _inspect_resume
    previous = verify_export(previous_export)
    _, candidate = _inspect_resume(directory)
    return _compare(Schema.parse(previous["schema"]), candidate)


def check_resume_export_schema_compatibility(directory, candidate_export):
    """Compare a fully inspected checkpoint schema -> verified candidate export.

    Use the exact parsed baseline schema returned by locked inspection and only
    the schema returned by full export verification. Original schema files and
    checkpoint destination publication are unnecessary; bindings stay unchanged.
    """
    from .resume import _inspect_resume
    _, previous = _inspect_resume(directory)
    candidate = verify_export(candidate_export)
    return _compare(previous, Schema.parse(candidate["schema"]))


def check_schema_compatibility(old_schema, new_schema):
    """Compare two schema files OLD -> NEW without reading rows or loading Arrow.

    Existing logical types/names must survive; nullability may only widen. Added
    fields must be nullable under nullable-union, and that policy cannot tighten
    to strict. Field order and version-specific provenance are outside this check.
    Invalid schemas raise ExportError; incompatibilities return a complete report.
    """
    return _compare(_load_schema(old_schema, "old schema"), _load_schema(new_schema, "new schema"))


def _compare(old, new):
    old_fields = {f["name"]: f for f in old.fields}
    new_fields = {f["name"]: f for f in new.fields}
    issues, added, relaxed = [], [], []
    for before in old.fields:
        name = before["name"]
        after = new_fields.get(name)
        if after is None:
            issues.append(dict(code="field_removed", field=name))
            continue
        if before["type"] != after["type"]:
            issues.append(dict(code="type_changed", field=name,
                               old=before["type"], new=after["type"]))
        if before["nullable"] and not after["nullable"]:
            issues.append(dict(code="nullability_tightened", field=name, old=True, new=False))
        elif not before["nullable"] and after["nullable"]:
            relaxed.append(name)
    for after in new.fields:
        name = after["name"]
        if name in old_fields:
            continue
        if not after["nullable"]:
            issues.append(dict(code="required_field_added", field=name))
        else:
            added.append(name)
            if new.evolution != "nullable-union":
                issues.append(dict(code="nullable_addition_requires_union", field=name))
    if old.evolution == "nullable-union" and new.evolution == "strict":
        issues.append(dict(code="evolution_tightened", old=old.evolution, new=new.evolution))
    return dict(compatible=not issues, added_nullable_fields=added,
                relaxed_nullable_fields=relaxed, issues=issues)
