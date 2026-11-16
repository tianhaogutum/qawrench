"""Durable, opt-in batch checkpoints for quiescent local inputs (POSIX only)."""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import re

from .core import (ExportError, Input, MAX_SCHEMA_BYTES, Schema, _arrow, _batches,
                   _parse, _readback, _regular, _sha256_file, _temp, canonical)


class Resume:
    def __init__(self, directory, inputs, spec, schema_path, out,
                 batch_size, batch_bytes, max_item_bytes, pa, pq):
        directory = Path(directory)
        self.directory = directory.parent.resolve(strict=True) / directory.name
        self.inputs, self.spec, self.out = inputs, spec, out
        self.batch_size, self.batch_bytes = batch_size, batch_bytes
        self.max_item_bytes, self.pa, self.pq = max_item_bytes, pa, pq
        self.fd = None
        # A checkpoint must never create entries in the source, or own any input/output.
        protected = (inputs.path.resolve(), out,
                     out.with_name(out.name + '.manifest.json'))
        if schema_path is not None:
            protected += (schema_path.resolve(),)
        if (any(p.is_relative_to(self.directory) for p in protected)
                or (inputs.mode == 'dataset'
                    and self.directory.is_relative_to(inputs.path.resolve()))):
            raise ExportError('resume directory must be separate from source/schema/output')
        self.manifest_path = self.directory / 'resume.json'

    def _sync_directory(self):
        os.fsync(self.fd)

    def _jsonl_sha256(self, path):
        """Validate saved row/byte boundaries during the existing full hash read.

        Read bounded chunks ending at each checkpoint, counting LF in bytes rather
        than decoding or rereading completed lines. EOF may end an unterminated row.
        """
        length = path.stat().st_size
        digest = hashlib.sha256()
        position = lines = total = 0
        last = b''
        with path.open('rb') as stream:
            for part in self.manifest['parts']:
                offset = part['next_jsonl_offset']
                total += part['row_count']
                if offset > length:
                    raise ExportError('invalid checkpoint JSONL offset: past EOF')
                while position < offset:
                    chunk = stream.read(min(1024 * 1024, offset - position))
                    if not chunk:
                        raise ExportError('invalid checkpoint JSONL offset: source truncated')
                    digest.update(chunk)
                    position += len(chunk)
                    lines += chunk.count(b'\n')
                    last = chunk[-1:]
                rows = lines + (offset == length and last != b'\n')
                if (rows != total or (last != b'\n' and offset != length)):
                    raise ExportError('invalid checkpoint JSONL offset: row boundary/count mismatch')
            if (self.manifest['source_row_count'] is not None and position != length):
                raise ExportError('invalid checkpoint JSONL offset: completed source must end at EOF')
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def _snapshot(self):
        inputs = Input.inspect(self.inputs.path, self.inputs.mode)
        paths = ([p for _, p in inputs.items] + [inputs.path / n for n in inputs.ignored]
                 if inputs.mode == 'dataset' else [inputs.path])
        return {'path': str(inputs.path.resolve()), 'input_format': inputs.mode,
                'files': [{'name': p.name, 'bytes': p.stat().st_size,
                           'sha256': (self._jsonl_sha256(p) if inputs.mode == 'jsonl'
                                      else _sha256_file(p))} for p in paths]}

    def open(self):
        try:
            self.directory.mkdir(mode=0o700)
            # Make the new checkpoint directory durable in its parent too.
            parent_fd = os.open(self.directory.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except FileExistsError:
            pass
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ExportError('resume directory must be a directory, not a symlink')
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExportError('resume directory is locked by another export') from exc
        if self.manifest_path.exists() or self.manifest_path.is_symlink():
            _regular(self.manifest_path)
            self.manifest = _parse(self.manifest_path.read_bytes(), 'resume manifest')
            self._validate_manifest()
        else:
            self.manifest = {'resume_version': 2, 'binding': None,
                             'parts': [], 'source_row_count': None}
        self.binding = {
            'source': self._snapshot(), 'schema': self.spec.as_dict(),
            'out': str(self.out), 'tool_version': '0.1.0',
            'pyarrow_version': self.pa.__version__, 'batch_size': self.batch_size,
            'batch_bytes': self.batch_bytes, 'max_item_bytes': self.max_item_bytes,
        }
        if self.manifest_path.exists():
            if self.manifest['binding'] != self.binding:
                raise ExportError('resume binding mismatch: source/schema/destination/settings changed')
        else:
            self.manifest['binding'] = self.binding
        # Only generated, unreferenced files may be removed. These can remain after
        # a kill between part rename and manifest replacement. Never adopt them.
        referenced = {p['name'] for p in self.manifest['parts']}
        garbage = []
        for path in self.directory.iterdir():
            _regular(path)
            if path.name == 'resume.json' or path.name in referenced:
                continue
            if (re.fullmatch(r'part-[0-9]{8,}\.parquet', path.name)
                    or re.fullmatch(r'\.cpe-.+\.tmp', path.name)):
                garbage.append(path)
            else:
                raise ExportError(f'unexpected file in resume directory: {path.name}')
        if not self.manifest_path.exists():
            # Refuse to reinterpret arbitrary preexisting parts as a new checkpoint.
            if any(path.name.startswith('part-') for path in garbage):
                raise ExportError('resume manifest missing; use a new checkpoint directory')
            self._save_manifest()
        for path in garbage:
            path.unlink()
        self._sync_directory()

    def _validate_manifest(self):
        m = self.manifest
        _manifest_header(m)
        if not isinstance(m['parts'], list):
            raise ExportError('invalid checkpoint parts')
        total = previous_offset = 0
        for index, part in enumerate(m['parts']):
            if (not isinstance(part, dict) or set(part) !=
                    {'name', 'row_count', 'rows_sha256', 'parquet_bytes', 'parquet_sha256',
                     'next_jsonl_offset'}
                    or part['name'] != f'part-{index:08d}.parquet'
                    or type(part['row_count']) is not int
                    or not 0 < part['row_count'] <= self.batch_size
                    or type(part['parquet_bytes']) is not int or part['parquet_bytes'] <= 0
                    or any(not isinstance(part[k], str) or not re.fullmatch(r'[0-9a-f]{64}', part[k])
                           for k in ('rows_sha256', 'parquet_sha256'))):
                raise ExportError('invalid checkpoint part metadata')
            offset = part['next_jsonl_offset']
            if self.inputs.mode == 'jsonl':
                if type(offset) is not int or offset <= previous_offset:
                    raise ExportError('invalid checkpoint JSONL offset: expected increasing byte positions')
                previous_offset = offset
            elif offset is not None:
                raise ExportError('invalid checkpoint JSONL offset: dataset parts require null')
            total += part['row_count']
        if (m['source_row_count'] is not None
                and (type(m['source_row_count']) is not int or m['source_row_count'] != total)):
            raise ExportError('invalid checkpoint completed row count')

    def _save_manifest(self):
        staged = _temp(self.directory)
        try:
            with staged.open('wb') as stream:
                stream.write(canonical(self.manifest) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, self.manifest_path)
            self._sync_directory()
        finally:
            staged.unlink(missing_ok=True)

    def _write_part(self, batch):
        staged = _temp(self.directory)
        name = f"part-{len(self.manifest['parts']):08d}.parquet"
        try:
            digest = hashlib.sha256()
            for row in batch:
                digest.update(canonical(row) + b'\n')
            with self.pq.ParquetWriter(staged, self.spec.arrow(self.pa), compression='NONE',
                                       version='2.6', use_dictionary=False,
                                       write_statistics=True) as writer:
                writer.write_table(self.pa.Table.from_pylist(batch, schema=self.spec.arrow(self.pa)),
                                   row_group_size=len(batch))
            _readback(staged, self.spec, len(batch), digest.hexdigest(),
                      self.batch_size, self.pa, self.pq)
            part = {'name': name, 'row_count': len(batch), 'rows_sha256': digest.hexdigest(),
                    'parquet_bytes': staged.stat().st_size, 'parquet_sha256': _sha256_file(staged),
                    'next_jsonl_offset': batch.next_jsonl_offset}
            with staged.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(staged, self.directory / name)
            self._sync_directory()
            self.manifest['parts'].append(part)
            self._save_manifest()
        finally:
            staged.unlink(missing_ok=True)

    def _verify_part(self, part):
        path = self.directory / part['name']
        _regular(path)
        if (path.stat().st_size != part['parquet_bytes']
                or _sha256_file(path) != part['parquet_sha256']):
            raise ExportError(f"checkpoint part size/digest mismatch: {part['name']}")
        groups = _readback(path, self.spec, part['row_count'], part['rows_sha256'],
                           self.batch_size, self.pa, self.pq)
        if groups != 1:
            raise ExportError('checkpoint must contain exactly one row group')
        return path

    def batches(self, progress):
        total = 0
        for part in self.manifest['parts']:
            path = self._verify_part(part)
            with self.pq.ParquetFile(path) as parquet:
                batch = parquet.read(use_threads=False).to_pylist()
            progress(len(batch))
            total += len(batch)
            yield batch
        if self.manifest['source_row_count'] is None:
            offset = (self.manifest['parts'][-1]['next_jsonl_offset']
                      if self.inputs.mode == 'jsonl' and self.manifest['parts'] else 0)
            for batch in _batches(self.inputs, self.spec, self.batch_size, self.batch_bytes,
                                  self.max_item_bytes, progress, start=total, jsonl_offset=offset):
                self._write_part(batch)
                total += len(batch)
                yield batch
            self.manifest['source_row_count'] = total
            self._save_manifest()

    def check_source(self):
        if self._snapshot() != self.binding['source']:
            raise ExportError('resume binding mismatch: source changed during export')

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _manifest_header(manifest):
    if (not isinstance(manifest, dict) or set(manifest) !=
            {'resume_version', 'binding', 'parts', 'source_row_count'}
            or type(manifest['resume_version']) is not int or manifest['resume_version'] != 2):
        raise ExportError('invalid resume manifest version/fields; version 2 required, '
                          'use a new checkpoint directory for older jobs')


def inspect_resume(directory):
    """Verify saved progress without cleanup, retry or destination access.

    The stored binding supplies the schema/settings. Input must be stopped; a
    shared nonblocking lock refuses active exporters but cannot lock source writers.
    Unreferenced files are left alone. Completion describes ingestion, not publication.
    """
    report, _ = _inspect_resume(directory)
    return report


def _inspect_resume(directory):
    """Return progress and its parsed Schema from the same locked validation."""
    fd = None
    try:
        directory = Path(directory)
        directory = directory.parent.resolve(strict=True) / directory.name
        if directory.is_symlink() or not directory.is_dir():
            raise ExportError('resume directory must be an existing directory, not a symlink')
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExportError('resume directory is locked by another export') from exc
        path = directory / 'resume.json'
        _regular(path)
        manifest = _parse(path.read_bytes(), 'resume manifest')
        _manifest_header(manifest)
        binding = manifest['binding']
        if (not isinstance(binding, dict) or set(binding) !=
                {'source', 'schema', 'out', 'tool_version', 'pyarrow_version',
                 'batch_size', 'batch_bytes', 'max_item_bytes'}):
            raise ExportError('invalid checkpoint binding fields')
        for key in ('batch_size', 'batch_bytes', 'max_item_bytes'):
            if type(binding[key]) is not int or binding[key] <= 0:
                raise ExportError(f'invalid checkpoint binding: {key} must be a positive integer')
        source = binding['source']
        if (not isinstance(source, dict) or set(source) != {'path', 'input_format', 'files'}
                or not isinstance(source['path'], str) or not Path(source['path']).is_absolute()
                or not isinstance(source['input_format'], str)
                or source['input_format'] not in ('dataset', 'jsonl')
                or not isinstance(source['files'], list)
                or not isinstance(binding['out'], str) or not Path(binding['out']).is_absolute()):
            raise ExportError('invalid checkpoint source/destination binding')
        for file in source['files']:
            if (not isinstance(file, dict) or set(file) != {'name', 'bytes', 'sha256'}
                    or not isinstance(file['name'], str)
                    or type(file['bytes']) is not int or file['bytes'] < 0
                    or not isinstance(file['sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', file['sha256'])):
                raise ExportError('invalid checkpoint source file binding')
        spec = Schema.parse(binding['schema'])
        if (binding['schema'] != spec.as_dict()
                or len(canonical(binding['schema'])) > MAX_SCHEMA_BYTES):
            raise ExportError('invalid checkpoint normalized schema binding')
        pa, pq = _arrow()
        if binding['tool_version'] != '0.1.0' or binding['pyarrow_version'] != pa.__version__:
            raise ExportError('unsupported checkpoint tool/PyArrow version binding')
        inputs = Input.inspect(Path(source['path']), source['input_format'])
        state = Resume(directory, inputs, spec, None, Path(binding['out']),
                       binding['batch_size'], binding['batch_bytes'], binding['max_item_bytes'], pa, pq)
        state.manifest, state.binding = manifest, binding
        state._validate_manifest()
        if state._snapshot() != source:
            raise ExportError('resume binding mismatch: source changed')
        total = sum(part['row_count'] for part in manifest['parts'])
        complete = manifest['source_row_count'] is not None
        if inputs.mode == 'dataset':
            if total > len(inputs.items) or (complete and total != len(inputs.items)):
                raise ExportError('invalid checkpoint completed/source item count')
        for part in manifest['parts']:
            state._verify_part(part)
        state.check_source()
        offset = (manifest['parts'][-1]['next_jsonl_offset'] if manifest['parts'] else 0)
        return dict(resume_version=manifest['resume_version'],
                    source={'path': source['path'], 'input_format': inputs.mode},
                    part_count=len(manifest['parts']), reusable_rows=total,
                    next_jsonl_offset=offset if inputs.mode == 'jsonl' else None,
                    source_complete=complete), spec
    except ExportError:
        raise
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ExportError(f'checkpoint inspection failed: {exc}') from exc
    finally:
        if fd is not None:
            os.close(fd)
