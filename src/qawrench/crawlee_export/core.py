"""Validation, bounded row batches, local Parquet publication and verification."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterator

PROVENANCE = ("_cpe_source", "_cpe_item_id", "_cpe_line")
MISSING_FIELDS = "_cpe_missing_fields"
TYPES = {"string", "int64", "float64", "bool", "json-string"}
METADATA = {"__metadata__.json"}
MAX_SCHEMA_BYTES = 1024 * 1024


class ExportError(Exception):
    """An input, validation, IO or verification error; no rows are committed."""

    def __init__(self, message: str, *, rows_validated: int = 0):
        super().__init__(message)
        self.rows_validated = rows_validated


def _arrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ExportError(
            "qawrench crawlee-export needs pyarrow: pipx install 'qawrench[parquet]' "
            "(or pip install 'qawrench[parquet]')"
        ) from exc
    return pa, pq


def canonical(value: Any) -> bytes:
    """Project canonical JSON: sorted keys, compact UTF-8, finite numbers, LF framing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"non-finite JSON number {value}")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"float64 overflow/non-finite number {value}")
    return result


def _parse(raw: bytes, label: str):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=_constant, parse_float=_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ExportError(f"{label}: invalid UTF-8 JSON: {exc}") from exc


def _regular(path: Path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ExportError(f"{path}: expected a regular file (symlinks are unsupported)")


def _read(path: Path, limit: int) -> bytes:
    _regular(path)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ExportError(f"{path}: exceeds byte limit {limit}")
    return raw


@dataclass(frozen=True)
class Schema:
    fields: tuple[dict, ...]
    version: int = 1
    evolution: str = "strict"

    @classmethod
    def parse(cls, obj):
        if not isinstance(obj, dict) or type(obj.get("version")) is not int:
            raise ExportError("schema needs an integer version")
        version = obj["version"]
        if version not in (1, 2):
            raise ExportError("unsupported schema version; expected integer 1 or 2")
        keys = {"version", "fields"} | ({"evolution"} if version == 2 else set())
        if set(obj) != keys:
            raise ExportError(f"schema version {version} must contain exactly {sorted(keys)}")
        evolution = obj.get("evolution", "strict")
        if not isinstance(evolution, str) or evolution not in ("strict", "nullable-union"):
            raise ExportError("schema evolution must be strict or nullable-union")
        if not isinstance(obj["fields"], list) or not obj["fields"]:
            raise ExportError("schema fields must be a nonempty list")
        fields, names = [], set()
        for field in obj["fields"]:
            if (not isinstance(field, dict) or not {"name", "type"} <= set(field)
                    or set(field) - {"name", "type", "nullable"}):
                raise ExportError("each field needs name/type and optional nullable only")
            name, kind = field["name"], field["type"]
            if (not isinstance(name, str) or not name or name.startswith("_cpe_")
                    or name in names):
                raise ExportError(f"invalid, duplicate or reserved field name {name!r}")
            if not isinstance(kind, str) or kind not in TYPES:
                raise ExportError(f"{name}: unsupported field type {kind!r}")
            nullable = field.get("nullable", False)
            if type(nullable) is not bool:
                raise ExportError(f"{name}: nullable must be a boolean")
            fields.append({"name": name, "type": kind, "nullable": nullable})
            names.add(name)
        result = cls(tuple(fields), version, evolution)
        try:
            canonical(result.as_dict())  # Also rejects unpaired Unicode surrogates.
        except UnicodeError as exc:
            raise ExportError("schema contains invalid Unicode") from exc
        return result

    def as_dict(self):
        result = {"version": self.version, "fields": list(self.fields)}
        if self.version == 2:
            result["evolution"] = self.evolution
        return result

    @property
    def provenance(self):
        return PROVENANCE + ((MISSING_FIELDS,) if self.version == 2 else ())

    def arrow(self, pa):
        types = {"string": pa.string(), "json-string": pa.string(),
                 "int64": pa.int64(), "float64": pa.float64(), "bool": pa.bool_()}
        fields = [pa.field(f["name"], types[f["type"]], nullable=f["nullable"],
                           metadata={b"cpe.logical_type": f["type"].encode()})
                  for f in self.fields]
        fields += [pa.field(PROVENANCE[0], pa.string(), nullable=False),
                   pa.field(PROVENANCE[1], pa.string()),
                   pa.field(PROVENANCE[2], pa.int64())]
        if self.version == 2:
            fields.append(pa.field(MISSING_FIELDS, pa.list_(
                pa.field("element", pa.string(), nullable=False)), nullable=False))
        return pa.schema(fields, metadata={b"cpe.schema": canonical(self.as_dict()),
                                          b"cpe.version": str(self.version).encode()})

    def validate(self, obj, label: str):
        if not isinstance(obj, dict):
            raise ExportError(f"{label}: item must be a JSON object")
        names = {f["name"] for f in self.fields}
        missing, extra = names - obj.keys(), obj.keys() - names
        forbidden_missing = missing
        if self.evolution == "nullable-union":
            forbidden_missing = missing - {f["name"] for f in self.fields if f["nullable"]}
        if forbidden_missing or extra:
            raise ExportError(f"{label}: missing fields {sorted(forbidden_missing)}; "
                              f"undeclared fields {sorted(extra)}")
        row = {}
        for field in self.fields:
            name, kind = field["name"], field["type"]
            value = obj.get(name)
            if value is None:
                if not field["nullable"]:
                    raise ExportError(f"{label}: {name}: null in non-nullable field")
            elif kind == "json-string":
                try:
                    value = canonical(value).decode("utf-8")
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ExportError(f"{label}: {name}: invalid nested JSON: {exc}") from exc
            elif kind == "int64":
                if type(value) is not int or not -(2**63) <= value < 2**63:
                    raise ExportError(f"{label}: {name}: expected exact int64, no overflow")
            elif kind == "float64":
                if type(value) is not float or not math.isfinite(value):
                    raise ExportError(f"{label}: {name}: expected finite float64 "
                                      "(JSON decimal/exponent token; integers are not coerced)")
            elif kind == "bool":
                if type(value) is not bool:
                    raise ExportError(f"{label}: {name}: expected bool")
            elif type(value) is not str:
                raise ExportError(f"{label}: {name}: expected string")
            row[name] = value
        if self.version == 2:
            row[MISSING_FIELDS] = [f["name"] for f in self.fields if f["name"] in missing]
        return row


class _FieldProvenance:
    """O(fields) accounting from output rows, including replayed checkpoint parts."""

    def __init__(self, spec):
        self.spec, self.count = spec, 0
        self.summary = {f["name"]: {"present": 0, "missing": 0, "explicit_null": 0,
                                     "first_present_row": None} for f in spec.fields}

    def observe(self, row):
        self.count += 1
        missing = row[MISSING_FIELDS]
        if not isinstance(missing, list) or any(not isinstance(n, str) for n in missing):
            raise ExportError("invalid field provenance: expected missing-field name list")
        absent = set(missing)
        if (missing != [f["name"] for f in self.spec.fields if f["name"] in absent]
                or (missing and self.spec.evolution == "strict")):
            raise ExportError("invalid field provenance: unknown, duplicate or unordered missing fields")
        for field in self.spec.fields:
            name = field["name"]
            stats = self.summary[name]
            if name in absent:
                if not field["nullable"] or row[name] is not None:
                    raise ExportError(f"invalid field provenance: {name} missing but not nullable/null")
                stats["missing"] += 1
            else:
                stats["present"] += 1
                if stats["first_present_row"] is None:
                    stats["first_present_row"] = self.count
                if row[name] is None:
                    if not field["nullable"]:
                        raise ExportError(f"invalid field provenance: {name} non-nullable null")
                    stats["explicit_null"] += 1


@dataclass(frozen=True)
class Input:
    path: Path
    mode: str
    items: tuple[tuple[str, Path], ...]
    ignored: tuple[str, ...]

    @classmethod
    def inspect(cls, path: Path, mode: str):
        if mode not in {"dataset", "jsonl"}:
            raise ExportError("input format must be dataset or jsonl")
        if mode == "jsonl":
            _regular(path)
            return cls(path, mode, (), ())
        if not path.is_dir() or path.is_symlink():
            raise ExportError(f"{path}: expected a dataset directory, not a symlink")
        items, seen, ignored = [], set(), []
        for item in path.iterdir():
            _regular(item)
            if item.name in METADATA:
                ignored.append(item.name)
                continue
            if not re.fullmatch(r"[0-9]{1,128}\.json", item.name):
                raise ExportError(f"{item}: unexpected dataset filename")
            key = str(int(item.stem))
            if key in seen:
                raise ExportError(f"{item}: duplicate numeric item id {key}")
            seen.add(key)
            items.append((key, item))
        items.sort(key=lambda item: int(item[0]))
        return cls(path, mode, tuple(items), tuple(sorted(ignored)))

    def rows(self, max_item_bytes: int, start: int = 0,
             jsonl_offset: int = 0) -> Iterator[tuple[Any, dict, str, int | None]]:
        if self.mode == "dataset":
            for key, item in self.items[start:]:
                yield (_parse(_read(item, max_item_bytes), item.name),
                       dict(zip(PROVENANCE, (item.name, key, None))), item.name, None)
        else:
            with self.path.open("rb") as stream:
                # Resume validates the offset against the full-source hash pass.
                stream.seek(jsonl_offset)
                line = start
                while raw := stream.readline(max_item_bytes + 2):
                    line += 1
                    next_offset = stream.tell()
                    # Count bytes excluding LF (and CR in CRLF), not decoded characters.
                    raw = raw.removesuffix(b"\n").removesuffix(b"\r")
                    label = f"{self.path.name}:{line}"
                    if len(raw) > max_item_bytes:
                        raise ExportError(f"{label}: exceeds byte limit {max_item_bytes}")
                    yield (_parse(raw, label), dict(zip(PROVENANCE,
                           (self.path.name, None, line))), label, next_offset)


def _sha256_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readback(path, schema, expected_count, expected_digest, batch_size, pa, pq,
              *, expected_field_provenance=None):
    digest, count = hashlib.sha256(), 0
    fields = _FieldProvenance(schema) if schema.version == 2 else None
    with pq.ParquetFile(path) as parquet:
        if not parquet.schema_arrow.equals(schema.arrow(pa), check_metadata=True):
            raise ExportError("read-back schema mismatch")
        if parquet.metadata.num_rows != expected_count:
            raise ExportError("read-back footer row count mismatch")
        # Stay within a writer row group at a time, including for large strings.
        for group in range(parquet.num_row_groups):
            for batch in parquet.iter_batches(batch_size=batch_size, row_groups=[group],
                                               use_threads=False):
                for row in batch.to_pylist():
                    digest.update(canonical(row) + b"\n")
                    if fields is not None:
                        fields.observe(row)
                    count += 1
        groups = parquet.num_row_groups
    if count != expected_count or digest.hexdigest() != expected_digest:
        raise ExportError("read-back row count/value digest mismatch")
    if (expected_field_provenance is not None
            and canonical(fields.summary) != canonical(expected_field_provenance)):
        raise ExportError("read-back field provenance mismatch")
    return groups


def _paths(source: Path, schema_path: Path, out: Path, mode: str, *, resume=False):
    # Resolve parent aliases, but never follow a destination symlink.
    out = out.parent.resolve(strict=True) / out.name
    manifest = out.with_name(out.name + ".manifest.json")
    for target in (out, manifest):
        if not resume and (target.exists() or target.is_symlink()):
            raise ExportError(f"destination already exists: {target}; choose a new output")
        if mode == "dataset" and target.is_relative_to(source.resolve()):
            raise ExportError("output/manifest inside dataset is forbidden")
        if target == source.resolve() or target == schema_path.resolve():
            raise ExportError("output/input/schema collision")
    return out, manifest


def _temp(parent: Path):
    fd, name = tempfile.mkstemp(prefix=".cpe-", suffix=".tmp", dir=parent)
    os.close(fd)
    return Path(name)


class _Batch(list):
    """Rows plus the source position after their last row, excluding read-ahead."""

    next_jsonl_offset: int | None = None


def _batches(inputs, spec, batch_size, batch_bytes, max_item_bytes, progress, start=0,
             jsonl_offset=0):
    batch, size = _Batch(), 0
    for obj, provenance, label, next_offset in inputs.rows(max_item_bytes, start, jsonl_offset):
        row = spec.validate(obj, label)
        row.update(provenance)
        try:
            encoded = canonical(row) + b"\n"
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ExportError(f"{label}: invalid canonical row: {exc}") from exc
        if len(encoded) > batch_bytes:
            raise ExportError(f"{label}: canonical row exceeds batch_bytes {batch_bytes}")
        if batch and (len(batch) >= batch_size or size + len(encoded) > batch_bytes):
            yield batch
            batch, size = _Batch(), 0
        batch.append(row)
        batch.next_jsonl_offset = next_offset
        size += len(encoded)
        progress(1)
    if batch:
        yield batch


def export_dataset(source, *, schema, out, input_format="dataset", batch_size=1000,
                   batch_bytes=16 * 1024 * 1024, max_item_bytes=8 * 1024 * 1024, resume_dir=None):
    """Export to a new local destination and sidecar; return the verified manifest.

    resume_dir opts into durable parts bound to this input/schema/destination.
    Repeat identical arguments to recover; matching published artifacts are accepted.
    Input must be a quiescent local snapshot. No overwrite or skipped bad rows.
    """
    count, temps, published = 0, [], []
    resume_state = None
    def progress(amount):
        nonlocal count
        count += amount
    try:
        for name, value in (("batch_size", batch_size), ("batch_bytes", batch_bytes),
                            ("max_item_bytes", max_item_bytes)):
            if type(value) is not int or value <= 0:
                raise ExportError(f"{name} must be a positive integer")
        pa, pq = _arrow()
        source, schema_path, out = Path(source), Path(schema), Path(out)
        out, manifest_path = _paths(source, schema_path, out, input_format,
                                    resume=resume_dir is not None)
        spec = Schema.parse(_parse(_read(schema_path, MAX_SCHEMA_BYTES), "schema"))
        inputs = Input.inspect(source, input_format)
        batches = _batches(inputs, spec, batch_size, batch_bytes, max_item_bytes, progress)
        if resume_dir is not None:
            from .resume import Resume
            resume_state = Resume(resume_dir, inputs, spec, schema_path, out,
                                  batch_size, batch_bytes, max_item_bytes, pa, pq)
            resume_state.open()
            batches = resume_state.batches(progress)
        staged = _temp(out.parent)
        temps.append(staged)
        digest = hashlib.sha256()
        fields = _FieldProvenance(spec) if spec.version == 2 else None
        arrow_schema = spec.arrow(pa)
        with pq.ParquetWriter(staged, arrow_schema, compression="NONE", version="2.6",
                              use_dictionary=False, write_statistics=True) as writer:
            for batch in batches:
                writer.write_table(pa.Table.from_pylist(batch, schema=arrow_schema),
                                   row_group_size=len(batch))
                for row in batch:
                    digest.update(canonical(row) + b"\n")
                    if fields is not None:
                        fields.observe(row)
        if resume_state is not None:
            resume_state.check_source()
        rows_digest = digest.hexdigest()
        field_summary = fields.summary if fields is not None else None
        groups = _readback(staged, spec, count, rows_digest, batch_size, pa, pq,
                           expected_field_provenance=field_summary)
        manifest = {
            "format_version": 1, "tool_version": "0.1.0", "pyarrow_version": pa.__version__,
            "input_format": input_format, "row_count": count, "row_groups": groups,
            "schema": spec.as_dict(), "provenance_columns": list(spec.provenance),
            "rows_sha256": rows_digest, "parquet_sha256": _sha256_file(staged),
            "parquet_bytes": staged.stat().st_size, "verified": True,
            "ignored_metadata": list(inputs.ignored), "rejected_rows": 0,
            "batch_size": batch_size, "batch_bytes": batch_bytes,
            "max_item_bytes": max_item_bytes,
        }
        if fields is not None:
            manifest["field_provenance"] = field_summary
        staged_manifest = _temp(out.parent)
        temps.append(staged_manifest)
        manifest_bytes = canonical(manifest) + b"\n"
        if len(manifest_bytes) > MAX_SCHEMA_BYTES:
            raise ExportError("manifest exceeds supported 1 MiB limit; reduce schema size")
        staged_manifest.write_bytes(manifest_bytes)
        for path in temps:
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        # Atomic no-clobber hard links on the same filesystem. Parquet appears last.
        # Two directory entries cannot be committed atomically: see README crash limits.
        for staged_path, final_path in ((staged_manifest, manifest_path), (staged, out)):
            if resume_state is not None and (final_path.exists() or final_path.is_symlink()):
                _regular(final_path)
                if (final_path.stat().st_size != staged_path.stat().st_size
                        or _sha256_file(final_path) != _sha256_file(staged_path)):
                    raise ExportError(f"existing destination differs from resumed export: {final_path}")
            else:
                os.link(staged_path, final_path)
                published.append(final_path)
        directory_fd = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        published.clear()
        return manifest
    except ExportError as exc:
        exc.rows_validated = count
        raise
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ExportError(str(exc), rows_validated=count) from exc
    finally:
        if resume_state is not None:
            resume_state.close()
        for path in reversed(published):
            path.unlink(missing_ok=True)
        for path in temps:
            path.unlink(missing_ok=True)


def verify_export(out, *, batch_size=1000):
    """Read Parquet independently and validate its sidecar's schema, counts and digests."""
    try:
        if type(batch_size) is not int or batch_size <= 0:
            raise ExportError("batch_size must be a positive integer")
        pa, pq = _arrow()
        out = Path(out)
        _regular(out)
        manifest = _parse(_read(Path(str(out) + ".manifest.json"), MAX_SCHEMA_BYTES), "manifest")
        if (not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int
                or manifest["format_version"] != 1):
            raise ExportError("unsupported manifest format_version")
        for key in ("row_count", "row_groups", "parquet_bytes"):
            if type(manifest.get(key)) is not int or manifest[key] < 0:
                raise ExportError(f"manifest {key} must be a nonnegative integer")
        for key in ("rows_sha256", "parquet_sha256"):
            if not isinstance(manifest.get(key), str) or not re.fullmatch("[0-9a-f]{64}", manifest[key]):
                raise ExportError(f"manifest {key} must be a SHA-256 hex digest")
        spec = Schema.parse(manifest.get("schema"))
        if manifest.get("provenance_columns") != list(spec.provenance):
            raise ExportError("manifest provenance_columns mismatch")
        field_summary = None
        if spec.version == 2:
            field_summary = manifest.get("field_provenance")
            if not isinstance(field_summary, dict):
                raise ExportError("manifest field provenance must be an object")
        if (out.stat().st_size != manifest["parquet_bytes"]
                or _sha256_file(out) != manifest["parquet_sha256"]):
            raise ExportError("Parquet file size/digest mismatch")
        groups = _readback(out, spec, manifest["row_count"], manifest["rows_sha256"],
                           batch_size, pa, pq, expected_field_provenance=field_summary)
        if groups != manifest["row_groups"]:
            raise ExportError("manifest row_groups mismatch")
        return manifest
    except ExportError:
        raise
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ExportError(f"verification failed: {exc}") from exc
