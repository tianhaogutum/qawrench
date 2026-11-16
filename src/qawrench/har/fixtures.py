"""File adapter for bounded, self-contained Playwright HAR ZIPs.

Core matching stays on embedded HAR JSON. Only this adapter resolves archive
members, in memory, never by extracting files or opening manifest references.
"""
from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import json
from pathlib import Path
import stat
import zipfile
import zlib

from .core import (DoctorError, _digest, _fail, _json_tree, _object, _string, _unique_object,
                   atomic_write_file, atomic_write_json, load_json, select_requests,
                   _selected_request_indexes, validate_har)

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


def _same_typed_json(supplied, expected):
    """Exact report schema and equality; bool/float must never stand in for int."""
    if type(supplied) is not type(expected):
        return False
    if isinstance(expected, dict):
        return (supplied.keys() == expected.keys()
                and all(_same_typed_json(supplied[key], value) for key, value in expected.items()))
    if isinstance(expected, list):
        return (len(supplied) == len(expected)
                and all(_same_typed_json(a, b) for a, b in zip(supplied, expected)))
    return supplied == expected


def _member_name(name, where):
    _string(name, where, nonempty=True)
    if (any(ord(c) < 32 or ord(c) == 127 for c in name) or '\\' in name or ':' in name
            or any(part in ('', '.', '..') for part in name.split('/'))):
        _fail(where, f'unsafe archive member path {name!r}; use exact relative file names')
    return name


@dataclass
class HarFixture:
    har: dict
    # Private archive data is retained separately from the embedded analysis view.
    _manifest: dict | None = None
    _manifest_name: str | None = None
    _members: dict | None = None

    @property
    def is_zip(self):
        return self._members is not None

    def write(self, rewritten, output, *, protected=()):
        """Publish URL/query/header projection for ZIPs; retain original payloads."""
        if not self.is_zip:
            if str(output).lower().endswith('.zip'):
                raise DoctorError('JSON input requires a JSON HAR output, not a .zip path')
            atomic_write_json(rewritten, output, protected=protected)
            return
        if not str(output).endswith('.har.zip'):
            raise DoctorError('archive apply output must end in .har.zip; choose a new path')
        validate_har(rewritten)
        if len(rewritten['log']['entries']) != len(self.har['log']['entries']):
            raise DoctorError('archive rewrite must preserve every HAR entry')
        manifest = copy.deepcopy(self._manifest)
        for target, source in zip(manifest['log']['entries'], rewritten['log']['entries']):
            target['request']['url'] = source['request']['url']
            target['request']['headers'] = copy.deepcopy(source['request']['headers'])
            if 'queryString' in source['request']:
                target['request']['queryString'] = copy.deepcopy(source['request']['queryString'])
        _write_archive(manifest, self._manifest_name, self._members, output, protected=protected)

    def write_selection(self, inventory, output, *, protected=()):
        """Select using resolved exact keys, publish original entries/references."""
        if not self.is_zip:
            result = select_requests(self.har, inventory)
            self.write(result, output, protected=protected)
            return {"selectedEntries": len(result['log']['entries'])}
        selected = _selected_request_indexes(self.har, inventory)
        manifest = copy.deepcopy(self._manifest)
        manifest['log']['entries'] = [entry for i, entry in enumerate(manifest['log']['entries'])
                                    if i in selected]
        retained = self._selected_payload_members(selected) | {self._manifest_name}
        # Loading has already checked every member/reference, including discarded
        # ones. Exact-name membership preserves source order and shared bytes once.
        members = {name: body for name, body in self._members.items() if name in retained}
        _write_archive(manifest, self._manifest_name, members, output, protected=protected)
        return {"selectedEntries": len(selected), "retainedPayloadMembers": len(members) - 1}

    def _selected_payload_members(self, selected):
        """Distinct exact reference names shared by planning and ZIP selection."""
        retained = set()
        for index in selected:
            entry = self._manifest['log']['entries'][index]
            payloads = [entry['response']['content']]
            if 'postData' in entry['request']:
                payloads.append(entry['request']['postData'])
            retained.update(payload['_file'] for payload in payloads if '_file' in payload)
        return retained

    def build_selection_plan(self, inventory):
        """Read-only membership report, with IDs bound to the resolved HAR digest.

        Full validation and exact unique matching precede any result. Report no
        request values or attachment names, even though preflight uses them.
        """
        selected = sorted(_selected_request_indexes(self.har, inventory))
        summary = {"totalEntries": len(self.har['log']['entries']), "selectedEntries": len(selected)}
        if self.is_zip:
            retained = len(self._selected_payload_members(selected))
            total = len(self._members) - 1  # The manifest is never a payload.
            summary.update(totalPayloadMembers=total, retainedPayloadMembers=retained,
                           discardedPayloadMembers=total - retained)
        return {"schemaVersion": 1, "tool": "har-replay-doctor", "matchModel": "exact-url-method-body-v1",
                "harSHA256": _digest(self.har), "inventorySHA256": _digest(inventory),
                "selectedEntryIndexes": selected,
                "selectedEntryIds": [f"/log/entries/{i}" for i in selected], "summary": summary}

    def check_selection_plan(self, inventory, supplied):
        """Validate all inputs and compare the entire report, never extract by it."""
        expected = self.build_selection_plan(inventory)
        if not _same_typed_json(supplied, expected):
            raise DoctorError('selection plan does not equal the recomputed report')


def _write_archive(manifest, manifest_name, members, output, *, protected=()):
    """Shared bounded serialization and atomic no-clobber archive publication."""
    if not str(output).endswith('.har.zip'):
        raise DoctorError('archive output must end in .har.zip; choose a new path')
    data = (json.dumps(manifest, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode('utf-8')
    total = sum(map(len, members.values())) - len(members[manifest_name]) + len(data)
    if len(data) > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
        raise DoctorError('rewritten archive exceeds uncompressed resource limit')

    def write(stream):
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, body in members.items():
                archive.writestr(name, data if name == manifest_name else body)
        if stream.tell() > MAX_ARCHIVE_BYTES:
            raise DoctorError('rewritten ZIP exceeds archive byte limit')
    atomic_write_file(output, write, protected=protected)


def _resolve_payload(payload, members, manifest_name, where, remaining):
    _object(payload, where)
    if '_sha1' in payload:
        _fail(where, 'trace _sha1 references are unsupported; supply a self-contained HAR ZIP')
    if '_file' not in payload:
        return 0
    name = _member_name(payload['_file'], where + '._file')
    if name == manifest_name:
        _fail(where + '._file', 'payload reference must not name the HAR manifest')
    if name not in members:
        _fail(where + '._file', f'missing archive member {name!r}; loose external files are never read')
    # Playwright 1.56.1 emits an empty text placeholder for attached postData.
    if 'text' in payload and payload['text'] != '':
        _fail(where, 'nonempty text alongside _file is ambiguous; keep only one body source')
    if 'encoding' in payload:
        _fail(where, 'encoding alongside _file is ambiguous; attachment bytes are authoritative')
    size = len(members[name])
    if size > remaining:
        _fail(where, 'cumulative referenced attachment byte limit exceeded (64 MiB); reduce repeated payload volume')
    payload.pop('_file')
    payload['text'] = base64.b64encode(members[name]).decode('ascii')
    payload['encoding'] = 'base64'
    return size


def load_fixture(path: str | Path) -> HarFixture:
    """Read JSON, or a strict .har.zip; reject all loose external references."""
    path = Path(path)
    if not str(path).endswith('.har.zip'):
        return HarFixture(load_json(path))
    try:
        # Check the actual opened descriptor as well as the ZIP's declared sizes.
        with path.open('rb') as stream:
            stream.seek(0, 2)
            if stream.tell() > MAX_ARCHIVE_BYTES:
                _fail(str(path), 'ZIP archive byte limit exceeded (64 MiB)')
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_MEMBERS:
                    _fail(str(path), 'ZIP member count limit exceeded (4096)')
                names, folded, total = set(), set(), 0
                for info in infos:
                    name = _member_name(info.orig_filename, str(path))
                    if name in names:
                        _fail(str(path), f'duplicate archive member {name!r}')
                    if name.casefold() in folded:
                        _fail(str(path), f'case-ambiguous archive member {name!r}')
                    names.add(name)
                    folded.add(name.casefold())
                    mode = stat.S_IFMT(info.external_attr >> 16)
                    if mode not in (0, stat.S_IFREG) or info.is_dir() or info.external_attr & 0x10:
                        _fail(str(path), f'archive members must be regular files: {name!r}')
                    if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        _fail(str(path), f'encrypted/unsupported ZIP compression for {name!r}')
                    total += info.file_size
                    if info.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
                        _fail(str(path), 'ZIP uncompressed resource limit exceeded (16 MiB/member, 64 MiB total)')
                manifests = [i.filename for i in infos if i.filename.lower().endswith('.har')]
                if len(manifests) != 1 or '/' in manifests[0] or not manifests[0].endswith('.har'):
                    _fail(str(path), 'ZIP requires exactly one root-level .har manifest')
                manifest_name = manifests[0]
                members = {}
                for info in infos:
                    with archive.open(info) as member:
                        data = member.read(MAX_MEMBER_BYTES + 1)
                    if len(data) > MAX_MEMBER_BYTES or len(data) != info.file_size:
                        _fail(str(path), f'ZIP member size/resource limit mismatch: {info.filename!r}')
                    members[info.filename] = data
        manifest = json.loads(members[manifest_name].decode('utf-8-sig'), object_pairs_hook=_unique_object,
                              parse_constant=lambda c: _fail(manifest_name, f'nonfinite JSON number {c}'))
        _json_tree(manifest)
        resolved = copy.deepcopy(manifest)
        entries = _object(_object(resolved, 'HAR').get('log'), 'log').get('entries')
        if not isinstance(entries, list):
            _fail('log.entries', 'expected an array')
        remaining = MAX_TOTAL_BYTES
        for i, entry in enumerate(entries):
            where = f'log.entries[{i}]'
            _object(entry, where)
            req = _object(entry.get('request'), where + '.request')
            res = _object(entry.get('response'), where + '.response')
            if 'postData' in req:
                remaining -= _resolve_payload(req['postData'], members, manifest_name, where + '.request.postData', remaining)
            remaining -= _resolve_payload(res.get('content'), members, manifest_name, where + '.response.content', remaining)
        validate_har(resolved)
        return HarFixture(resolved, manifest, manifest_name, members)
    except DoctorError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError, zipfile.BadZipFile,
            NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        raise DoctorError(f'cannot read HAR ZIP {path}: {exc}') from exc
