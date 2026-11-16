"""Verified-export baseline preflight: reports, validation and no-write contracts."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.crawlee_export import core, export_dataset, check_schema_compatibility
from qawrench.crawlee_export.cli import main


def field(name, kind='string', nullable=False):
    return dict(name=name, type=kind, nullable=nullable)


def schema(fields, policy=None):
    obj = dict(version=1 if policy is None else 2, fields=fields)
    if policy is not None:
        obj['evolution'] = policy
    return obj


class ExportCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-export-compatibility-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'input.jsonl'
        self.source.write_text('{"id":9007199254740993,"payload":{"x":true},"note":null}\n'
                               '{"id":-9223372036854775808,"payload":[1,2],"note":"雪"}\n')
        self.fields = [field('id', 'int64'), field('payload', 'json-string'),
                       field('note', nullable=True)]
        self.old = self.root / 'old.json'
        self.new = self.root / 'new.json'
        self.old.write_text(json.dumps(schema(self.fields)))
        self.target = schema(self.fields + [field('extra', nullable=True)], 'nullable-union')
        self.new.write_text(json.dumps(self.target))
        self.out = self.root / 'previous.parquet'
        self.sidecar = Path(str(self.out) + '.manifest.json')
        self.manifest = export_dataset(self.source, schema=self.old, out=self.out,
                                       input_format='jsonl', batch_size=1)

    def cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['--check-export-schema', str(self.out), str(self.new), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def snapshot(self):
        result = {}
        for p in [self.root, *self.root.rglob('*')]:
            key = str(p.relative_to(self.root))
            result[key] = (p.lstat().st_mtime_ns,
                           ('link', str(p.readlink())) if p.is_symlink() else
                           ('dir',) if p.is_dir() else ('file', p.read_bytes()))
        return result

    def assert_invalid(self):
        before = self.snapshot()
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stdout), (2, ''))
        error = json.loads(stderr)
        self.assertTrue(error['error'])
        self.assertEqual(error['rows_committed'], 0)
        self.assertEqual(self.snapshot(), before)
        return error['error']

    def test_cli_verified_strict_export_nullable_addition(self):
        before = self.snapshot()
        code, stdout, stderr = self.cli()
        expected = dict(compatible=True, added_nullable_fields=['extra'],
                        relaxed_nullable_fields=[], issues=[])
        self.assertEqual((code, json.loads(stdout), stderr), (0, expected, ''))
        self.assertEqual(json.loads(stdout), check_schema_compatibility(self.old, self.new))
        from qawrench.crawlee_export import check_export_schema_compatibility
        self.assertEqual(check_export_schema_compatibility(self.out, self.new), expected)
        self.assertEqual(self.snapshot(), before)

    def test_reports_match_declared_files_in_both_directions_and_policies(self):
        for old_policy in (None, 'strict', 'nullable-union'):
            old_obj = schema(self.fields, old_policy)
            self.old.write_text(json.dumps(old_obj))
            self.out.unlink()
            self.sidecar.unlink()
            export_dataset(self.source, schema=self.old, out=self.out, input_format='jsonl')
            targets = [schema(list(reversed(self.fields)), policy)
                       for policy in (None, 'strict', 'nullable-union')]
            targets += [self.target,
                        schema([field('extra', nullable=True), field('id', 'int64', True),
                                *self.fields[1:], field('β', 'bool', True)], 'nullable-union'),
                        schema([field('id', 'float64'), field('payload'), field('note'),
                                field('required')], 'strict'),
                        schema(self.fields + [field('extra', nullable=True)]),
                        schema([field('renamed')], 'nullable-union')]
            for target in targets:
                with self.subTest(old=old_policy, target=target):
                    self.new.write_text(json.dumps(target))
                    expected = check_schema_compatibility(self.old, self.new)
                    before = self.snapshot()
                    code, stdout, stderr = self.cli()
                    self.assertEqual((code, json.loads(stdout), stderr),
                                     (0 if expected['compatible'] else 2, expected, ''))
                    self.assertEqual(self.snapshot(), before)

    def test_baseline_requires_no_original_source_or_schema_and_never_writes(self):
        self.source.unlink()
        self.old.unlink()
        before = self.snapshot()
        real_open = Path.open

        def read_only(path, mode='r', *args, **kwargs):
            self.assertFalse(any(c in mode for c in 'wax+'), f'write open: {path}')
            return real_open(path, mode, *args, **kwargs)

        with patch.object(Path, 'open', read_only), \
                patch('qawrench.crawlee_export.cli.export_dataset', side_effect=AssertionError('export')), \
                patch.object(core, '_paths', side_effect=AssertionError('destination')), \
                patch('tempfile.mkstemp', side_effect=AssertionError('temporary')):
            self.assertEqual(self.cli()[0], 0)
        self.assertEqual(self.snapshot(), before)

    def test_missing_directory_and_symlink_previous_artifacts(self):
        for path in (self.out, self.sidecar):
            raw = path.read_bytes()
            for kind in ('missing', 'directory', 'symlink'):
                path.unlink()
                link_target = self.root / 'saved'
                if kind == 'directory':
                    path.mkdir()
                elif kind == 'symlink':
                    link_target.write_bytes(raw)
                    path.symlink_to(link_target)
                with self.subTest(path=path.name, kind=kind):
                    self.assert_invalid()
                if kind == 'directory':
                    path.rmdir()
                elif kind == 'symlink':
                    path.unlink()
                    link_target.unlink()
                path.write_bytes(raw)

    def test_corrupt_unsupported_or_mismatched_sidecar(self):
        original = self.sidecar.read_bytes()
        mutations = [('format_version', 2), ('format_version', True), ('row_count', 9),
                     ('row_groups', 9), ('parquet_bytes', 1), ('rows_sha256', '0' * 64),
                     ('parquet_sha256', '0' * 64), ('provenance_columns', []),
                     ('schema', schema([field('id')]))]
        raws = [b'\xff', b'{}', b'{"format_version":1,"format_version":1}',
                b' ' * (core.MAX_SCHEMA_BYTES + 1)]
        for key, value in mutations:
            obj = copy.deepcopy(self.manifest)
            obj[key] = value
            raws.append(json.dumps(obj).encode())
        for raw in raws:
            with self.subTest(raw=raw[:180]):
                self.sidecar.write_bytes(raw)
                self.assert_invalid()
        self.sidecar.write_bytes(original)

    def test_valid_sidecar_does_not_hide_corrupt_parquet_even_with_updated_file_digest(self):
        original = self.out.read_bytes()
        broken = b'X' + original[1:-1] + b'X'
        self.out.write_bytes(broken)
        self.assertIn('digest', self.assert_invalid())
        # A consistent file digest alone still must not bypass physical read-back.
        manifest = copy.deepcopy(self.manifest)
        manifest['parquet_sha256'] = hashlib.sha256(broken).hexdigest()
        self.sidecar.write_text(json.dumps(manifest))
        self.assert_invalid()

    def test_other_valid_exports_sidecar_is_not_a_baseline(self):
        other = self.root / 'other.parquet'
        self.source.write_text('{"id":1,"payload":{},"note":null}\n')
        manifest = export_dataset(self.source, schema=self.old, out=other, input_format='jsonl')
        self.sidecar.write_text(json.dumps(manifest))
        self.assertIn('mismatch', self.assert_invalid())

    def test_union_field_provenance_is_verified(self):
        self.old.write_text(json.dumps(schema(self.fields, 'nullable-union')))
        self.out.unlink()
        self.sidecar.unlink()
        manifest = export_dataset(self.source, schema=self.old, out=self.out, input_format='jsonl')
        manifest['field_provenance']['id']['present'] = 0
        self.sidecar.write_text(json.dumps(manifest))
        self.assertIn('provenance', self.assert_invalid())

    def test_invalid_missing_directory_and_symlink_new_schema(self):
        for raw in (b'{}', b'\xff', b'{"version":1,"version":1,"fields":[]}',
                    b'{"version":1,"fields":[{"name":"x","type":"date"}]}',
                    b'{"version":1,"fields":[{"name":"x","type":"string","nullable":1}]}',
                    b' ' * (core.MAX_SCHEMA_BYTES + 1)):
            self.new.write_bytes(raw)
            self.assert_invalid()
        self.new.unlink()
        self.assert_invalid()
        self.new.mkdir()
        self.assert_invalid()
        self.new.rmdir()
        self.new.symlink_to(self.old)
        self.assert_invalid()

    def test_cli_rejects_conflicting_or_incomplete_options(self):
        conflicts = [['source'], ['--schema', str(self.old)], ['--out', 'unused.parquet'],
                     ['--resume-dir', 'checkpoint'], ['--verify', str(self.out)],
                     ['--check-schema', str(self.old), str(self.new)],
                     ['--inspect-resume', 'checkpoint'], ['--input-format', 'dataset'],
                     ['--batch-size', '1000'], ['--batch-bytes', '16777216'],
                     ['--max-item-bytes', '8388608']]
        before = self.snapshot()
        for extra in conflicts:
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as caught:
                self.cli(*extra)
            self.assertEqual(caught.exception.code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['--check-export-schema', str(self.out)])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
