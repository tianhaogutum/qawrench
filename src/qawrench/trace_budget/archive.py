"""Bounded, non-extracting archive processing; no Playwright code is imported."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import BinaryIO, Iterator
import zipfile
import zlib


class ArchiveError(ValueError):
    """Invalid/unsupported archive, policy limit, or unsafe output operation."""


@dataclass(frozen=True)
class Limits:
    max_archive_bytes: int = 512 * 1024 * 1024
    max_members: int = 10_000
    max_member_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_ratio: float = 1000.0
    max_line_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            try:
                valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                raise ArchiveError(f"{key} must be finite and positive")
            if key != "max_ratio" and not isinstance(value, int):
                raise ArchiveError(f"{key} must be an integer")


CHUNK_BYTES = 128 * 1024
CATEGORIES = ("trace", "network", "resources", "other")
RESOURCE_GROUPS = ("screencast_screenshots", "network_bodies", "other_referenced", "shared", "unreferenced")
REFERENCE_SAMPLE_LIMIT = 20
REFERENCE_SAMPLE_TEXT_LIMIT = 160
KNOWN_REFERENCE_SLOTS = (
    ("screencast-frame", ("sha1",), "screencast_screenshots"),
    ("resource-snapshot", ("snapshot", "request", "postData", "_sha1"), "network_bodies"),
    ("resource-snapshot", ("snapshot", "response", "content", "_sha1"), "network_bodies"),
)
SUPPORTED_TRACE_VERSIONS = (8,)
# PKWARE APPNOTE 4.4.4: descriptors and UTF-8 for both methods; only
# DEFLATE interprets bits 1-2 as compression options. All other bits require
# unsupported features or are unused/reserved, even if both headers agree.
SUPPORTED_ZIP_FLAGS = {zipfile.ZIP_STORED: 0x0808, zipfile.ZIP_DEFLATED: 0x080E}
ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError,
              zlib.error, EOFError, UnicodeError, struct.error)


def _category(info: zipfile.ZipInfo) -> str:
    if info.is_dir():
        return "other"
    if info.filename.startswith("resources/"):
        return "resources"
    if info.filename.endswith(".trace"):
        return "trace"
    if info.filename.endswith(".network"):
        return "network"
    return "other"


def _archive_end_records(handle: BinaryIO, archive_bytes: int) -> int | None:
    """Check raw end requirements and return the resolved entry total.

    Match zipfile's classic end-record selection, with at most a 64 KiB comment
    and fixed-size ZIP64 reads. Structural validity remains zipfile's job.
    """
    tail_start = max(0, archive_bytes - 65535 - 22)
    handle.seek(tail_start)
    tail = handle.read(65535 + 22)
    if len(tail) >= 22 and tail[-22:-18] == b"PK\x05\x06" and tail[-2:] == b"\0\0":
        end = len(tail) - 22
    else:
        end = tail.rfind(b"PK\x05\x06")
    if end < 0 or len(tail) - end < 22:
        return  # The normal ZIP reader will diagnose the missing/truncated end.

    declared_comment = struct.unpack_from("<H", tail, end + 20)[0]
    available_comment = len(tail) - end - 22
    # Validate the selected record, without searching backward for an earlier
    # signature. Extra trailing bytes remain permitted; only truncation fails.
    if declared_comment > available_comment:
        raise ArchiveError(f"truncated ZIP archive comment: {str(handle.name)!r}; "
                           f"classic EOCD declared {declared_comment} bytes, available {available_comment} bytes")

    def require(field: str, declared: int, supported: int = 0) -> None:
        if declared != supported:
            raise ArchiveError(f"unsupported ZIP archive disk declaration: {str(handle.name)!r}; "
                               f"{field} {declared}, supported {supported}")

    current, start = struct.unpack_from("<HH", tail, end + 4)
    require("classic EOCD current disk", current)
    require("classic EOCD central-directory start disk", start)
    classic_counts = struct.unpack_from("<HH", tail, end + 8)
    classic_directory = struct.unpack_from("<II", tail, end + 12)
    zip64_counts = None
    zip64_directory = None
    locator_position = tail_start + end - 20
    locator = b""
    if locator_position >= 0:
        handle.seek(locator_position)
        locator = handle.read(20)
    if locator.startswith(b"PK\x06\x07"):
        _, disk, offset, disks = struct.unpack("<4sIQI", locator)
        require("ZIP64 locator end-record disk", disk)
        require("ZIP64 locator total disks", disks, 1)
        # Like zipfile, first use the locator offset; for a concatenated ZIP fall
        # back to the fixed record immediately before the locator. This also keeps
        # reads bounded when an invalid locator claims an out-of-file offset.
        fixed_position = locator_position - 56
        record = b""
        if 0 <= offset <= fixed_position:
            handle.seek(offset)
            record = handle.read(56)
        if not record.startswith(b"PK\x06\x06") and fixed_position >= 0:
            handle.seek(fixed_position)
            record = handle.read(56)
        if len(record) == 56 and record.startswith(b"PK\x06\x06"):
            # The +14 extraction requirement is independent of +12 made-by and
            # member versions. ZIP64 needs 4.5; higher requirements (including
            # any nonzero high byte) exceed this tool's supported features.
            needed = struct.unpack_from("<H", record, 14)[0]
            if needed != 45:
                raise ArchiveError(f"unsupported ZIP64 EOCD extraction-version declaration: {str(handle.name)!r}; "
                                   f"required {needed}, supported 45 (4.5)")
            current, start = struct.unpack_from("<II", record, 16)
            require("ZIP64 EOCD current disk", current)
            require("ZIP64 EOCD central-directory start disk", start)
            zip64_counts = struct.unpack_from("<QQ", record, 24)
            zip64_directory = struct.unpack_from("<QQ", record, 40)

    def inconsistent(details: str) -> None:
        raise ArchiveError(f"inconsistent ZIP end-record entry counts: {str(handle.name)!r}; {details}")

    if zip64_counts is None:
        # In classic count fields 0xffff means ZIP64, never an unresolved count.
        if 0xFFFF in classic_counts:
            inconsistent("classic EOCD count sentinel 65535 requires a readable ZIP64 EOCD")
        here, total = classic_counts
        if here != total:
            inconsistent(f"classic EOCD entries on this disk {here}, total entries {total}")
    else:
        here, total = zip64_counts
        if here != total:
            inconsistent(f"ZIP64 EOCD entries on this disk {here}, total entries {total}")
        # Classic fields may independently use a sentinel or the actual count.
        # Check literal values too: zipfile otherwise silently overwrites them.
        for field, classic, resolved in zip(("entries on this disk", "total entries"),
                                            classic_counts, zip64_counts):
            if classic != 0xFFFF and classic != resolved:
                inconsistent(f"classic EOCD {field} {classic}, ZIP64 EOCD {field} {resolved}")

    for index, field in enumerate(("size", "offset")):
        classic = classic_directory[index]
        details = None
        if zip64_directory is None:
            if classic == 0xFFFFFFFF:
                details = (f"classic EOCD central-directory {field} sentinel {classic} "
                           "requires a readable ZIP64 EOCD")
        else:
            # Each classic field independently uses a sentinel or matching
            # literal. Compare RAW offsets: any prepended-data adjustment is
            # zipfile's job, after this redundant-metadata agreement check.
            resolved = zip64_directory[index]
            if classic != 0xFFFFFFFF and classic != resolved:
                details = (f"classic EOCD central-directory {field} {classic}, "
                           f"ZIP64 EOCD central-directory {field} {resolved}")
        if details is not None:
            raise ArchiveError(f"inconsistent ZIP central-directory declarations: {str(handle.name)!r}; {details}")
    return total


def _safe_members(infos: list[zipfile.ZipInfo], limits: Limits, archive_name: str) -> None:
    if len(infos) > limits.max_members:
        raise ArchiveError("member-count limit exceeded")
    names: set[str] = set()
    files: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        # zipfile checks field data lengths but silently ignores a final 1-3
        # bytes. Require complete central TLVs for ALL members before decoding
        # any payload. Conditional ZIP64 decoding remains its job, but this
        # tool conservatively permits only one central ZIP64 block per member.
        position = 0
        seen_zip64 = False
        while position < len(info.extra):
            if len(info.extra) - position < 4:
                raise ArchiveError(f"invalid ZIP central extra: {archive_name!r}; {name!r}; truncated field header")
            tag, length = struct.unpack_from("<HH", info.extra, position)
            position += 4
            if length > len(info.extra) - position:
                raise ArchiveError(f"invalid ZIP central extra: {archive_name!r}; {name!r}; truncated field data")
            if tag == 1:
                if seen_zip64:
                    raise ArchiveError(f"invalid ZIP central ZIP64: {archive_name!r}; {name!r}; duplicate block")
                seen_zip64 = True
            position += length
        parts = name.rstrip("/").split("/")
        if (info.orig_filename != name or not name or "\\" in name or ":" in name
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or any(p in ("", ".", "..") for p in parts) or "//" in name):
            raise ArchiveError(f"unsafe member name: {name!r}")
        canonical = name.removesuffix("/")
        if canonical in names:
            raise ArchiveError(f"duplicate member name or file/directory collision: {name!r}")
        names.add(canonical)
        mode = stat.S_IFMT(info.external_attr >> 16)
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ArchiveError(f"special/symlink member is unsupported: {name!r}")
        if mode == stat.S_IFDIR and not info.is_dir():
            raise ArchiveError(f"directory mode/name mismatch: {name!r}")
        # ZipInfo.volume retains the raw central disk-start field; zipfile does
        # not resolve its ZIP64 sentinel. Support only literal disk zero.
        if info.volume != 0:
            raise ArchiveError(f"unsupported ZIP disk start: {name!r}; declared {info.volume}, supported 0")
        if info.compress_type not in SUPPORTED_ZIP_FLAGS:
            raise ArchiveError(f"unsupported ZIP compression method for {name!r}")
        supported_flags = SUPPORTED_ZIP_FLAGS[info.compress_type]
        unsupported_flags = info.flag_bits & ~supported_flags
        if unsupported_flags:
            encryption = "; encrypted member is unsupported" if info.flag_bits & (1 | 64) else ""
            raise ArchiveError(f"unsupported ZIP flags: {name!r}; unsupported mask 0x{unsupported_flags:04x}, "
                               f"supported mask 0x{supported_flags:04x}{encryption}")
        if info.file_size > limits.max_member_bytes:
            raise ArchiveError(f"member-size limit exceeded: {name!r}")
        if info.file_size / max(info.compress_size, 1) > limits.max_ratio:
            raise ArchiveError(f"compression-ratio limit exceeded: {name!r}")
        if info.is_dir() and info.file_size:
            raise ArchiveError(f"nonempty directory member: {name!r}")
        if not info.is_dir():
            files.add(name)
        total += info.file_size
    if total > limits.max_total_bytes:
        raise ArchiveError("total uncompressed-size limit exceeded")
    for name in names:
        parts = name.split("/")
        if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
            raise ArchiveError(f"file used as member parent: {name!r}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant {value}")


class _ResourceRoles:
    """Attribute existing resources and keep only capped known-slot diagnostics."""

    def __init__(self, infos: list[zipfile.ZipInfo]):
        self.roles: dict[str, set[str]] = {i.filename: set() for i in infos if _category(i) == "resources"}
        self.missing_counts = {"screencast_screenshots": 0, "network_bodies": 0}
        self.missing_samples: list[dict] = []

    def observe(self, event: dict, stream: str, line: int) -> None:
        # Count occurrences, never distinct values. In particular, no unresolved
        # identifier set can grow with stream length or with opaque extensions.
        for kind, slot, role in KNOWN_REFERENCE_SLOTS:
            if event["type"] != kind:
                continue
            value = event
            for key in slot:
                value = value.get(key) if isinstance(value, dict) else None
            if not isinstance(value, str) or not value or "resources/" + value in self.roles:
                continue
            self.missing_counts[role] += 1
            if len(self.missing_samples) < REFERENCE_SAMPLE_LIMIT:
                self.missing_samples.append({
                    "role": role, "slot": ".".join(slot), "line": line,
                    "stream_prefix": stream[:REFERENCE_SAMPLE_TEXT_LIMIT],
                    "stream_truncated": len(stream) > REFERENCE_SAMPLE_TEXT_LIMIT,
                    "reference_prefix": value[:REFERENCE_SAMPLE_TEXT_LIMIT],
                    "reference_truncated": len(value) > REFERENCE_SAMPLE_TEXT_LIMIT,
                })
        # v1.56.1 visitTraceEvent collects these keys recursively. Only the
        # pinned screencast/HAR slots give specific roles; extensions are opaque.
        pending = [(event, ())]
        while pending:
            node, path = pending.pop()
            if isinstance(node, list):
                pending.extend((value, path + (index,)) for index, value in enumerate(node)
                               if isinstance(value, (dict, list)))
            elif isinstance(node, dict):
                for key, value in node.items():
                    field = path + (key,)
                    if isinstance(value, (dict, list)):
                        pending.append((value, field))
                    if not (key in ("sha1", "_sha1") or key.endswith("Sha1")) or not isinstance(value, str):
                        continue
                    roles = self.roles.get("resources/" + value)
                    if roles is None:
                        continue
                    role = next((role for kind, slot, role in KNOWN_REFERENCE_SLOTS
                                 if event["type"] == kind and field == slot), "other_referenced")
                    roles.add(role)

    def diagnostics(self) -> dict:
        total = sum(self.missing_counts.values())
        return {"missing_occurrences": total, "missing_occurrences_by_role": self.missing_counts,
                "sample_limit": REFERENCE_SAMPLE_LIMIT, "sample_text_limit": REFERENCE_SAMPLE_TEXT_LIMIT,
                "sample_omitted_occurrences": total - len(self.missing_samples), "samples": self.missing_samples}

    def allocation(self, members: list[dict]) -> dict:
        groups = {g: {"member_count": 0, "stored_bytes": 0, "uncompressed_bytes": 0} for g in RESOURCE_GROUPS}
        for member in members:
            if member["category"] != "resources":
                continue
            roles = sorted(self.roles[member["name"]])
            group = "shared" if len(roles) > 1 else roles[0] if roles else "unreferenced"
            member["resource_roles"], member["resource_group"] = roles, group
            groups[group]["member_count"] += 1
            for metric in ("stored_bytes", "uncompressed_bytes"):
                groups[group][metric] += member[metric]
        return groups


class _NDJSON:
    """Validate line envelopes without changing or reconstructing any event."""

    def __init__(self, name: str, category: str, limit: int, resources: _ResourceRoles):
        self.name, self.category, self.limit = name, category, limit
        self.resources = resources
        self.buffer = bytearray()
        self.lines = 0
        self.types: dict[str, int] = {}
        self.type_bytes: dict[str, int] = {}

    def feed(self, data: bytes) -> None:
        # Split each bounded chunk before appending, never buffer the full stream.
        segments = data.split(b"\n")
        for i, segment in enumerate(segments):
            self.buffer.extend(segment)
            if len(self.buffer) > self.limit:
                raise ArchiveError(f"NDJSON line-size limit exceeded: {self.name!r}")
            if i < len(segments) - 1:
                self._line(bytes(self.buffer), terminated=True)
                self.buffer.clear()

    def finish(self) -> dict:
        if self.buffer:
            self._line(bytes(self.buffer))
            self.buffer.clear()
        if self.category == "trace" and not self.lines:
            raise ArchiveError(f"empty .trace stream: {self.name!r}")
        return {"event_count": self.lines, "event_types": dict(sorted(self.types.items())),
                "event_uncompressed_bytes": dict(sorted(self.type_bytes.items())),
                "trace_version": 8 if self.category == "trace" else None}

    def _line(self, raw: bytes, *, terminated: bool = False) -> None:
        self.lines += 1
        try:
            event = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str) or not event["type"]:
                raise ValueError("event must be an object with a nonempty string type")
            kind = event["type"]
            if self.category == "trace":
                if self.lines == 1 and kind != "context-options":
                    raise ValueError(".trace must begin with context-options")
                if kind == "context-options":
                    if type(event.get("version")) is not int or event["version"] not in SUPPORTED_TRACE_VERSIONS:
                        raise ValueError("only trace version 8 is supported")
                    for key in ("browserName", "platform"):
                        if not isinstance(event.get(key), str):
                            raise ValueError(f"context-options requires string {key}")
                    if event.get("origin") not in ("library", "testRunner") or not isinstance(event.get("options"), dict):
                        raise ValueError("context-options requires origin and options object")
                    for key in ("wallTime", "monotonicTime"):
                        value = event.get(key)
                        if type(value) not in (int, float) or not math.isfinite(value):
                            raise ValueError(f"context-options requires finite numeric {key}")
            elif kind != "resource-snapshot" or not isinstance(event.get("snapshot"), dict):
                raise ValueError(".network requires resource-snapshot events with snapshot objects")
            self.resources.observe(event, self.name, self.lines)
            self.types[kind] = self.types.get(kind, 0) + 1
            self.type_bytes[kind] = self.type_bytes.get(kind, 0) + len(raw) + int(terminated)
        except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
            raise ArchiveError(f"invalid NDJSON in {self.name!r}, line {self.lines}: {exc}") from exc


def _fingerprint(handle: BinaryIO) -> tuple:
    st = os.fstat(handle.fileno())
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _local_header(handle: BinaryIO, info: zipfile.ZipInfo) -> tuple[int, bool]:
    """Check redundant local metadata and return payload offset / ZIP64 usage.

    Descriptor entries may use zero placeholders or redundant matching values.
    Local ZIP64 uses both sentinels and exactly one two-size (16-byte) block;
    central-directory ZIP64 extras have a different, conditional field layout.
    """
    handle.seek(info.header_offset)
    header = handle.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise ArchiveError(f"invalid ZIP local header: {info.filename!r}")
    version = struct.unpack_from("<H", header, 4)[0]
    central_version = info.extract_version | (info.reserved << 8)
    if version != central_version:
        raise ArchiveError(f"local ZIP extraction version mismatch: {info.filename!r}; "
                           f"local {version}, central {central_version}")
    # Bound this tool's requirements independently of zipfile's broader support.
    # This also rejects nonzero high bytes in matching extraction declarations.
    if version > 45:
        raise ArchiveError(f"unsupported ZIP extraction version: {info.filename!r}; "
                           f"required {version}, maximum supported 45")
    flags, method = struct.unpack_from("<HH", header, 6)
    if flags != info.flag_bits:
        raise ArchiveError(f"local ZIP flags mismatch: {info.filename!r}")
    if method != info.compress_type:
        raise ArchiveError(f"local ZIP method mismatch: {info.filename!r}")
    crc, stored, size = struct.unpack_from("<III", header, 14)
    name_length, extra_length = struct.unpack_from("<HH", header, 26)
    handle.seek(name_length, os.SEEK_CUR)
    extra = handle.read(extra_length)
    if len(extra) != extra_length:
        raise ArchiveError(f"invalid ZIP local extra: {info.filename!r}; truncated")
    payload_start = handle.tell()
    zip64_sizes = None
    position = 0
    while position < len(extra):
        if len(extra) - position < 4:
            raise ArchiveError(f"invalid ZIP local extra: {info.filename!r}; truncated field header")
        tag, length = struct.unpack_from("<HH", extra, position)
        position += 4
        if length > len(extra) - position:
            raise ArchiveError(f"invalid ZIP local extra: {info.filename!r}; truncated field data")
        if tag == 1:
            if zip64_sizes is not None or length != 16:
                raise ArchiveError(f"invalid ZIP local ZIP64: {info.filename!r}; duplicate or invalid sizes")
            zip64_sizes = struct.unpack_from("<QQ", extra, position)
        position += length
    zip64 = stored == 0xFFFFFFFF or size == 0xFFFFFFFF
    if zip64:
        if stored != 0xFFFFFFFF or size != 0xFFFFFFFF or zip64_sizes is None:
            raise ArchiveError(f"invalid ZIP local ZIP64: {info.filename!r}; both sentinels and sizes required")
        size, stored = zip64_sizes
    elif zip64_sizes is not None:
        raise ArchiveError(f"invalid ZIP local ZIP64: {info.filename!r}; size sentinels required")
    # Central ZIP64 may be forced independently of the local representation,
    # including on a small member. zipfile has already decoded its TLV fields.
    central_zip64 = False
    position = 0
    while position + 4 <= len(info.extra):
        tag, length = struct.unpack_from("<HH", info.extra, position)
        central_zip64 |= tag == 1
        position += 4 + length
    minimum = 45 if zip64 or central_zip64 else 20 if method == zipfile.ZIP_DEFLATED or info.is_dir() else 10
    if version < minimum:
        raise ArchiveError(f"ZIP extraction version below feature minimum: {info.filename!r}; "
                           f"required {version}, minimum {minimum}")
    for label, local, central in (("CRC32", crc, info.CRC),
                                  ("compressed size", stored, info.compress_size),
                                  ("uncompressed size", size, info.file_size)):
        if local != central and not (flags & 8 and local == 0):
            raise ArchiveError(f"local ZIP {label} mismatch: {info.filename!r}; "
                               f"local {local}, central {central}")
    return payload_start, zip64


def _data_descriptor(handle: BinaryIO, info: zipfile.ZipInfo, payload_start: int, zip64: bool) -> None:
    """Require a matching descriptor at the payload end, bounded by the next record."""
    start = payload_start + info.compress_size
    # zipfile computes this boundary from the next local header / central dir,
    # including any prefix-offset correction. Never borrow their bytes for a DD.
    boundary = info._end_offset
    if boundary is None or start > boundary:
        raise ArchiveError(f"invalid ZIP data descriptor: {info.filename!r}; overlaps next record")
    handle.seek(start)
    raw = handle.read(min(24 if zip64 else 16, boundary - start))
    fmt = "<IQQ" if zip64 else "<III"
    length = struct.calcsize(fmt)
    expected = (info.CRC, info.compress_size, info.file_size)
    # Try both interpretations: an unsigned descriptor's CRC can itself equal
    # the optional signature. A signature alone must never choose the layout.
    for offset in (0, 4) if raw[:4] == b"PK\x07\x08" else (0,):
        if len(raw) >= offset + length and struct.unpack_from(fmt, raw, offset) == expected:
            return
    raise ArchiveError(f"invalid ZIP data descriptor: {info.filename!r}; "
                       "missing, truncated, or CRC32/sizes differ from central directory")


def _verified_chunks(archive: zipfile.ZipFile, handle: BinaryIO, info: zipfile.ZipInfo,
                     limits: Limits, total_remaining: int) -> Iterator[bytes]:
    """Decode the complete compressed extent, never trusting the declared output cap.

    ZipExtFile clips output to file_size and can accept an unfinished DEFLATE
    stream. Keep zipfile's local-header/name/overlap checks, but verify raw bytes
    ourselves using bounded zlib output. The shared handle is used sequentially;
    no ZipExtFile reader remains open while raw payload bytes are consumed.
    """
    try:
        payload_start, zip64 = _local_header(handle, info)
        with archive.open(info):
            pass
        if info.flag_bits & 8:
            _data_descriptor(handle, info, payload_start, zip64)
        handle.seek(payload_start)
        decoder = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
        remaining = info.compress_size
        actual = checksum = 0
        while remaining:
            encoded = handle.read(min(CHUNK_BYTES, remaining))
            if not encoded:
                raise ArchiveError(f"truncated ZIP payload: {info.filename!r}")
            remaining -= len(encoded)
            pending = encoded
            while True:
                # Decode at most one byte past a size boundary, enough to prove
                # a violation without expanding a forged-size ZIP bomb in memory.
                cap = min(CHUNK_BYTES, info.file_size - actual + 1,
                          limits.max_member_bytes - actual + 1, total_remaining - actual + 1)
                chunk = decoder.decompress(pending, cap) if decoder else pending
                actual += len(chunk)
                if actual > limits.max_member_bytes:
                    raise ArchiveError(f"streamed member-size limit exceeded: {info.filename!r}")
                if actual > total_remaining:
                    raise ArchiveError(f"streamed total uncompressed-size limit exceeded: {info.filename!r}")
                if actual > info.file_size:
                    raise ArchiveError(f"member size mismatch: {info.filename!r}; "
                                       f"decoded more than declared {info.file_size} bytes")
                checksum = zlib.crc32(chunk, checksum)
                if chunk:
                    yield chunk
                if decoder is None:
                    break
                if decoder.eof:
                    if decoder.unused_data or remaining:
                        raise ArchiveError(f"trailing compressed data: {info.filename!r}")
                    break
                pending = decoder.unconsumed_tail
                if not pending and len(chunk) < cap:
                    break
        if decoder is not None and not decoder.eof:
            raise ArchiveError(f"truncated DEFLATE stream: {info.filename!r}; end marker missing")
        if actual != info.file_size:
            raise ArchiveError(f"member size mismatch: {info.filename!r}; "
                               f"declared {info.file_size}, decoded {actual} bytes")
        if checksum != info.CRC:
            raise ArchiveError(f"CRC32 mismatch: {info.filename!r}; "
                               f"expected {info.CRC:08x}, got {checksum:08x}")
    except ZIP_ERRORS as exc:
        raise ArchiveError(f"invalid ZIP member {info.filename!r}: {exc}") from exc


def _scan(archive: zipfile.ZipFile, handle: BinaryIO, archive_bytes: int, limits: Limits, top: int,
          budget_bytes: int | None = None, budget_basis: str = "archive", *,
          declared_entries: int | None) -> dict:
    if archive_bytes > limits.max_archive_bytes:
        raise ArchiveError("archive-size limit exceeded")
    infos = archive.infolist()
    # zipfile reads the directory by its byte extent, ignoring declared counts.
    # Count all parsed records, including directories, before any member decoding.
    if declared_entries is not None and declared_entries != len(infos):
        raise ArchiveError(f"ZIP central-directory entry count mismatch: {str(handle.name)!r}; "
                           f"declared {declared_entries}, actual {len(infos)}")
    _safe_members(infos, limits, str(handle.name))
    if not any(_category(info) == "trace" for info in infos):
        raise ArchiveError("not a trace-shaped ZIP: no .trace stream")
    allocation = {c: {"member_count": 0, "stored_bytes": 0, "uncompressed_bytes": 0} for c in CATEGORIES}
    resources = _ResourceRoles(infos)
    members = []
    actual_total = 0
    for info in sorted(infos, key=lambda item: item.filename):
        category = _category(info)
        validator = _NDJSON(info.filename, category, limits.max_line_bytes, resources) if category in ("trace", "network") else None
        digest = hashlib.sha256()
        actual = 0
        for chunk in _verified_chunks(archive, handle, info, limits, limits.max_total_bytes - actual_total):
            actual += len(chunk)
            actual_total += len(chunk)
            digest.update(chunk)
            if validator:
                validator.feed(chunk)
        member = {"name": info.filename, "category": category, "stored_bytes": info.compress_size,
                  "uncompressed_bytes": actual, "sha256": digest.hexdigest(),
                  "crc32": f"{info.CRC:08x}", "compression_method": info.compress_type,
                  "is_directory": info.is_dir()}
        if validator:
            member["ndjson"] = validator.finish()
        members.append(member)
        group = allocation[category]
        group["member_count"] += 1
        group["stored_bytes"] += info.compress_size
        group["uncompressed_bytes"] += actual
    stored = sum(m["stored_bytes"] for m in members)
    values = {"archive": archive_bytes, "stored": stored, "uncompressed": actual_total}
    measured = values[budget_basis]
    return {"schema_version": 1, "trace_versions": [8], "archive_bytes": archive_bytes,
            "stored_bytes": stored, "uncompressed_bytes": actual_total,
            "zip_overhead_bytes": archive_bytes - stored, "member_count": len(members),
            "allocation": allocation, "resource_allocation": resources.allocation(members), "members": members,
            "resource_reference_diagnostics": resources.diagnostics(),
            "top_members": sorted(members, key=lambda m: (-m["stored_bytes"], m["name"]))[:top],
            "top_uncompressed_members": sorted(members, key=lambda m: (-m["uncompressed_bytes"], m["name"]))[:top],
            "budget": {"basis": budget_basis, "limit_bytes": budget_bytes, "measured_bytes": measured,
                       "exceeded": budget_bytes is not None and measured > budget_bytes},
            "limits": asdict(limits), "verified": {"crc32": True, "sha256": True}}


def _options(top: int, budget_bytes: int | None, budget_basis: str) -> None:
    if type(top) is not int or top < 0:
        raise ArchiveError("top must be a nonnegative integer")
    if budget_bytes is not None and (type(budget_bytes) is not int or budget_bytes < 0):
        raise ArchiveError("budget_bytes must be a nonnegative integer")
    if budget_basis not in ("archive", "stored", "uncompressed"):
        raise ArchiveError("budget_basis must be archive, stored, or uncompressed")


def inspect_archive(path: str | os.PathLike, *, limits: Limits = Limits(), top: int = 10,
                    budget_bytes: int | None = None, budget_basis: str = "archive") -> dict:
    """Read every member, check CRC/NDJSON/limits, return a JSON-serializable report."""
    _options(top, budget_bytes, budget_basis)
    try:
        with open(path, "rb") as handle:
            before = _fingerprint(handle)
            if before[2] > limits.max_archive_bytes:
                raise ArchiveError("archive-size limit exceeded")
            declared_entries = _archive_end_records(handle, before[2])
            with zipfile.ZipFile(handle) as archive:
                report = _scan(archive, handle, before[2], limits, top, budget_bytes, budget_basis,
                               declared_entries=declared_entries)
            if _fingerprint(handle) != before:
                raise ArchiveError("input changed during inspection")
            return report
    except ZIP_ERRORS as exc:
        raise ArchiveError(f"invalid ZIP archive: {exc}") from exc


def ensure_output(input_path: str | os.PathLike, output_path: str | os.PathLike, force: bool) -> None:
    source, target = Path(input_path), Path(output_path)
    if source.resolve() == target.resolve() or (target.exists() and os.path.samefile(source, target)):
        raise ArchiveError("input and output must be different files (including links)")
    if target.is_symlink():
        raise ArchiveError("output symlinks are unsupported")
    if os.path.lexists(target) and not force:
        raise ArchiveError("output exists; use --force to replace it")
    if target.exists() and not target.is_file():
        raise ArchiveError("output must be a regular file")


def _publish(temp: Path, output: Path, force: bool) -> None:
    if force:
        os.replace(temp, output)
    else:
        # Atomic no-clobber publication: even a concurrent creator cannot be overwritten.
        try:
            os.link(temp, output)
        except FileExistsError as exc:
            raise ArchiveError("output exists; use --force to replace it") from exc
        temp.unlink()


def write_report(report: dict, output: str | os.PathLike, *, input_path: str | os.PathLike,
                 force: bool = False, additional_inputs: tuple[str | os.PathLike, ...] = ()) -> None:
    """Write JSON atomically, protecting the source archive and existing outputs."""
    target = Path(output)
    inputs = (input_path, *additional_inputs)
    for source in inputs:
        ensure_output(source, target, force)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for source in inputs:
            ensure_output(source, target, force)
        _publish(temp, target, force)
    finally:
        temp.unlink(missing_ok=True)


def _compare_members(original: dict, verified: dict) -> None:
    def signatures(report: dict) -> dict:
        return {m["name"]: (m["uncompressed_bytes"], m["sha256"], m["crc32"], m["is_directory"])
                for m in report["members"]}
    if signatures(original) != signatures(verified):
        raise ArchiveError("repack verification failed: member names/bytes/digests differ")


def _delta(before: dict, after: dict) -> dict:
    return {"before": before, "after": after,
            "delta": {key: after[key] - before[key] for key in before}}


def _event_allocation(report: dict) -> dict:
    groups: dict[str, dict] = {"trace": {}, "network": {}}
    for member in report["members"]:
        if "ndjson" not in member:
            continue
        for kind, count in member["ndjson"]["event_types"].items():
            group = groups[member["category"]].setdefault(kind, {"event_count": 0, "uncompressed_bytes": 0})
            group["event_count"] += count
            group["uncompressed_bytes"] += member["ndjson"]["event_uncompressed_bytes"][kind]
    return groups


def _comparison(before: dict, after: dict, *, allow_removals: bool = False) -> dict:
    """Compare fully validated inventories; never infer parity from event semantics."""
    old = {m["name"]: m for m in before["members"]}
    new = {m["name"]: m for m in after["members"]}
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    if added:
        raise ArchiveError(f"comparison has added members: {added!r}")
    if removed and not allow_removals:
        raise ArchiveError("comparison removals require allow_removals=True (--allow-removals): " + repr(removed))
    retained = []
    for name in sorted(new):
        original, candidate = old[name], new[name]
        if any(original[key] != candidate[key]
               for key in ("uncompressed_bytes", "sha256", "crc32", "is_directory")):
            raise ArchiveError(f"comparison retained payload differs: {name!r}")
        retained.append({"name": name, "uncompressed_bytes": original["uncompressed_bytes"],
                         "before_sha256": original["sha256"], "after_sha256": candidate["sha256"]})
        if original["category"] == "resources":
            retained[-1].update(before_resource_roles=original["resource_roles"],
                                after_resource_roles=candidate["resource_roles"])
    old_events, new_events = _event_allocation(before), _event_allocation(after)
    events = {}
    for category in old_events:
        events[category] = {}
        for kind in sorted(old_events[category].keys() | new_events[category].keys()):
            zero = {"event_count": 0, "uncompressed_bytes": 0}
            events[category][kind] = _delta(old_events[category].get(kind, zero),
                                            new_events[category].get(kind, zero))
    metrics = ("archive_bytes", "stored_bytes", "uncompressed_bytes", "zip_overhead_bytes", "member_count")
    losses = [old[name] for name in removed]
    budget = after["budget"]
    before_measured = before[{"archive": "archive_bytes", "stored": "stored_bytes",
                              "uncompressed": "uncompressed_bytes"}[budget["basis"]]]
    old_refs, new_refs = before["resource_reference_diagnostics"], after["resource_reference_diagnostics"]
    return {"schema_version": 1,
            "totals": _delta({k: before[k] for k in metrics}, {k: after[k] for k in metrics}),
            "allocation": {c: _delta(before["allocation"][c], after["allocation"][c]) for c in CATEGORIES},
            "resource_allocation": {g: _delta(before["resource_allocation"][g], after["resource_allocation"][g])
                                    for g in RESOURCE_GROUPS},
            "resource_reference_diagnostics": {
                "before": old_refs, "after": new_refs,
                "delta": {"missing_occurrences": new_refs["missing_occurrences"] - old_refs["missing_occurrences"],
                          "missing_occurrences_by_role": {
                              role: new_refs["missing_occurrences_by_role"][role] - count
                              for role, count in old_refs["missing_occurrences_by_role"].items()}},
            },
            "event_allocation": events,
            "loss_ledger": {"removals_allowed": allow_removals, "removed_member_count": len(losses),
                            "removed_uncompressed_bytes": sum(m["uncompressed_bytes"] for m in losses),
                            "members": losses},
            "retained_member_count": len(retained), "retained_payloads": retained,
            "all_retained_hashes_equal": True,
            "budget": {**budget, "before_measured_bytes": before_measured,
                       "before_exceeded": budget["limit_bytes"] is not None and before_measured > budget["limit_bytes"]}}


def compare_archives(before_path: str | os.PathLike, after_path: str | os.PathLike, *,
                     limits: Limits = Limits(), allow_removals: bool = False,
                     budget_bytes: int | None = None, budget_basis: str = "archive") -> dict:
    """Audit two archives with exact retained payload parity and an explicit loss ledger.

    Removals must be opted into; added members or changed retained bytes always
    fail. Both inputs must independently pass validation. The budget measures the
    after archive. This function never modifies either archive.
    """
    if type(allow_removals) is not bool:
        raise ArchiveError("allow_removals must be a boolean")
    _options(0, budget_bytes, budget_basis)
    before = inspect_archive(before_path, limits=limits, top=0)
    after = inspect_archive(after_path, limits=limits, top=0,
                            budget_bytes=budget_bytes, budget_basis=budget_basis)
    return _comparison(before, after, allow_removals=allow_removals)


def repack_archive(path: str | os.PathLike, output: str | os.PathLike, *, force: bool = False,
                   limits: Limits = Limits(), level: int = 9) -> dict:
    """Repack into a new ZIP; verify every member before atomic publication.

    Filenames and member bytes survive. ZIP timestamps, ordering, permissions,
    comments, extra fields and compression metadata are intentionally normalized.
    """
    if type(level) is not int or not 0 <= level <= 9:
        raise ArchiveError("compression level must be an integer from 0 to 9")
    target = Path(output)
    ensure_output(path, target, force)
    start = time.perf_counter()
    temp: Path | None = None
    try:
        with open(path, "rb") as handle:
            before = _fingerprint(handle)
            if before[2] > limits.max_archive_bytes:
                raise ArchiveError("archive-size limit exceeded")
            declared_entries = _archive_end_records(handle, before[2])
            with zipfile.ZipFile(handle) as source:
                original = _scan(source, handle, before[2], limits, 0, declared_entries=declared_entries)
                fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
                temp = Path(name)
                with os.fdopen(fd, "w+b") as dest_handle:
                    with zipfile.ZipFile(dest_handle, "w", compression=zipfile.ZIP_DEFLATED,
                                         compresslevel=level, allowZip64=True) as dest:
                        copied_total = 0
                        for member in sorted(source.infolist(), key=lambda info: info.filename):
                            info = zipfile.ZipInfo(member.filename, date_time=(1980, 1, 1, 0, 0, 0))
                            info.compress_type = zipfile.ZIP_DEFLATED
                            info.create_system = 3
                            info.external_attr = ((stat.S_IFDIR | 0o755) if member.is_dir() else (stat.S_IFREG | 0o644)) << 16
                            if member.is_dir():
                                info.external_attr |= 0x10
                            # Set explicitly: ZipInfo's default otherwise overrides ZipFile's level.
                            info._compresslevel = level
                            info.file_size = member.file_size
                            with dest.open(info, "w") as writer:
                                for chunk in _verified_chunks(source, handle, member, limits,
                                                              limits.max_total_bytes - copied_total):
                                    writer.write(chunk)
                                    copied_total += len(chunk)
                            if dest_handle.tell() > limits.max_archive_bytes:
                                raise ArchiveError("repacked archive-size limit exceeded")
                    dest_handle.flush()
                    os.fsync(dest_handle.fileno())
                verified = inspect_archive(temp, limits=limits, top=0)
                _compare_members(original, verified)
                comparison = _comparison(original, verified)
                if _fingerprint(handle) != before:
                    raise ArchiveError("input changed during repack")
                ensure_output(path, target, force)
                _publish(temp, target, force)
        old, new = original["archive_bytes"], verified["archive_bytes"]
        savings = old - new
        return {"schema_version": 1, "original_bytes": old, "new_bytes": new,
                "ratio": new / old, "saved_bytes": savings, "savings_percent": 100 * savings / old,
                "outcome": "smaller" if savings > 0 else "unchanged" if savings == 0 else "larger",
                "elapsed_seconds": time.perf_counter() - start,
                "verified_member_count": original["member_count"], "all_member_hashes_equal": True,
                "comparison": comparison,
                "compression": "ZIP_DEFLATED", "level": level,
                "normalization": "sorted filenames; 1980-01-01T00:00:00; Unix 0644/0755; no comments/extra fields"}
    except ZIP_ERRORS as exc:
        raise ArchiveError(f"invalid ZIP archive: {exc}") from exc
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
