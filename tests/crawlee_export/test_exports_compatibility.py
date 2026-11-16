"""Directional comparisons of two fully verified exports, with read-only inputs."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import qawrench.crawlee_export as cpe
from qawrench.crawlee_export import compatibility
from qawrench.crawlee_export.cli import main


def field(name, kind='string', nullable=False):
    return dict(name=name, type=kind, nullable=nullable)


def schema(fields, policy=None):
    obj = dict(version=1 if policy is None else 2, fields=fields)
    if policy is not None:
        obj['evolution'] = policy
    return obj


class ExportsCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-exports-compatibility-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fields = [field('id', 'int64'), field('note', nullable=True)]
        self.target = schema([field('extra', 'bool', True), field('id', 'int64', True),
                              self.fields[1]], 'nullable-union')
        self.previous, self.old = self.make_export('previous', schema(self.fields))
        self.candidate, self.new = self.make_export('candidate', self.target)

    def make_export(self, name, obj, empty=False):
        spec = self.root / (name + '.json')
        spec.write_text(json.dumps(obj))
        source = self.root / (name + '.jsonl')
        # Values are valid for any matrix schema, including changed logical types.
        values = {'string': '雪', 'bool': True, 'int64': 9007199254740993,
                  'float64': 1.25, 'json-string': {'nested': [1, None]}}
        row = {f['name']: values[f['type']] for f in obj['fields']}
        source.write_text('' if empty else json.dumps(row) + '\n')
        out = self.root / (name + '.parquet')
        cpe.export_dataset(source, schema=spec, out=out, input_format='jsonl', batch_size=1)
        return out, spec

    def cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['--check-exports-schema', str(self.previous), str(self.candidate), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mtime_ns,
                ('link', str(p.readlink())) if p.is_symlink() else
                ('dir',) if p.is_dir() else ('file', p.read_bytes()))
                for p in [self.root, *self.root.rglob('*')]}

    def reject(self):
        before = self.snapshot()
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stdout), (2, ''))
        error = json.loads(stderr)
        self.assertTrue(error['error'])
        self.assertEqual(error['rows_committed'], 0)
        self.assertEqual(self.snapshot(), before)
        return error['error']

    def assert_report(self, expected):
        before = self.snapshot()
        code, stdout, stderr = self.cli()
        self.assertEqual((code, json.loads(stdout), stderr),
                         (0 if expected['compatible'] else 2, expected, ''))
        self.assertEqual(self.snapshot(), before)

    def test_cli_two_verified_exports_nullable_addition_and_relaxation(self):
        expected = dict(compatible=True, added_nullable_fields=['extra'],
                        relaxed_nullable_fields=['id'], issues=[])
        self.assert_report(expected)
        self.assertEqual(cpe.check_exports_schema_compatibility(self.previous, self.candidate), expected)
        self.assertEqual(cpe.check_schema_compatibility(self.old, self.new), expected)
        self.assertEqual(cpe.check_export_schema_compatibility(self.previous, self.new), expected)

    def test_directional_policy_matrix_and_empty_exports(self):
        objects = [schema(self.fields, p) for p in (None, 'strict', 'nullable-union')]
        objects += [self.target, schema(list(reversed(self.fields)), 'strict'),
                    schema([field('id', 'float64'), field('required')], 'strict'),
                    schema(self.fields + [field('extra', nullable=True)])]
        exports = [self.make_export(f'matrix-{n}', obj, empty=(n == 4))
                   for n, obj in enumerate(objects)]
        for previous, old in exports:
            for candidate, new in exports:
                with self.subTest(previous=previous.name, candidate=candidate.name):
                    self.previous, self.candidate = previous, candidate
                    expected = cpe.check_schema_compatibility(old, new)
                    self.assert_report(expected)
                    self.assertEqual(cpe.check_export_schema_compatibility(previous, new), expected)

    def test_no_original_inputs_or_schemas_and_no_writes(self):
        for path in [*self.root.glob('*.json'), *self.root.glob('*.jsonl')]:
            if not path.name.endswith('.manifest.json'):
                path.unlink()
        before = self.snapshot()
        real_open = Path.open
        def read_only(path, mode='r', *args, **kwargs):
            self.assertFalse(any(c in mode for c in 'wax+'), f'write open: {path}')
            return real_open(path, mode, *args, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, 'open', read_only))
            for target in ('tempfile.mkstemp', 'pathlib.Path.mkdir', 'pathlib.Path.unlink',
                           'os.replace', 'os.link', 'qawrench.crawlee_export.core._paths',
                           'qawrench.crawlee_export.cli.export_dataset'):
                stack.enter_context(patch(target, side_effect=AssertionError('must not write')))
            self.assert_report(dict(compatible=True, added_nullable_fields=['extra'],
                                    relaxed_nullable_fields=['id'], issues=[]))
        self.assertEqual(self.snapshot(), before)

    def test_each_artifact_missing_directory_symlink_or_corrupt(self):
        for out in (self.previous, self.candidate):
            for path in (out, Path(str(out) + '.manifest.json')):
                original = path.read_bytes()
                path.write_bytes(b'broken')
                self.reject()
                path.unlink()
                self.reject()
                path.mkdir()
                self.reject()
                path.rmdir()
                alias = self.root / 'alias'
                alias.write_bytes(original)
                path.symlink_to(alias)
                self.reject()
                path.unlink()
                alias.unlink()
                path.write_bytes(original)

    def test_both_sidecars_require_supported_consistent_metadata(self):
        for out in (self.previous, self.candidate):
            path = Path(str(out) + '.manifest.json')
            original = path.read_bytes()
            obj = json.loads(original)
            changes = [('format_version', 99), ('schema', {}), ('row_count', 9),
                       ('row_groups', 9), ('rows_sha256', '0' * 64),
                       ('parquet_bytes', 1), ('parquet_sha256', '0' * 64),
                       ('provenance_columns', [])]
            for key, value in changes:
                broken = copy.deepcopy(obj)
                broken[key] = value
                path.write_text(json.dumps(broken))
                self.reject()
            for raw in (b'\xff', b'{"format_version":1,"format_version":1}',
                        b' ' * (1024 * 1024 + 1)):
                path.write_bytes(raw)
                self.reject()
            path.write_bytes(original)
        path = Path(str(self.candidate) + '.manifest.json')
        obj = json.loads(path.read_bytes())
        obj['field_provenance']['id']['present'] = 0
        path.write_text(json.dumps(obj))
        self.assertIn('provenance', self.reject())

    def test_both_parquets_require_readback_even_with_updated_digest(self):
        for out in (self.previous, self.candidate):
            path = Path(str(out) + '.manifest.json')
            raw, sidecar = out.read_bytes(), path.read_bytes()
            broken = b'X' + raw[1:-1] + b'X'
            out.write_bytes(broken)
            obj = json.loads(sidecar)
            obj['parquet_sha256'] = hashlib.sha256(broken).hexdigest()
            path.write_text(json.dumps(obj))
            self.assertRegex(self.reject(), 'Parquet|parquet')
            out.write_bytes(raw)
            path.write_bytes(sidecar)

    def test_consumes_only_both_verified_results_and_defers_comparison(self):
        real_verify = compatibility.verify_export
        calls = []
        def verify_once(path):
            result = real_verify(path)
            calls.append(path)
            # A second independent sidecar read would now fail. The verified
            # returned schema must be the comparison input for each side.
            Path(str(path) + '.manifest.json').write_bytes(b'changed after verification')
            return result
        with patch.object(compatibility, 'verify_export', verify_once):
            result = cpe.check_exports_schema_compatibility(self.previous, self.candidate)
        self.assertTrue(result['compatible'])
        self.assertEqual(calls, [self.previous, self.candidate])
        # Either verification failure must prevent even an incompatibility report.
        for failure in (1, 2):
            calls.clear()
            def fail_verify(path):
                calls.append(path)
                if len(calls) == failure:
                    raise cpe.ExportError('verification refused')
                return {'schema': schema(self.fields)}
            with patch.object(compatibility, 'verify_export', fail_verify), \
                    patch.object(compatibility, '_compare', side_effect=AssertionError('early report')):
                with self.assertRaisesRegex(cpe.ExportError, 'verification refused'):
                    cpe.check_exports_schema_compatibility(self.previous, self.candidate)
            self.assertEqual(len(calls), failure)

    def test_argument_conflicts_and_arity_rejected_before_verification(self):
        before = self.snapshot()
        conflicts = [['source'], ['--schema', str(self.old)], ['--out', 'unused'],
                     ['--resume-dir', 'checkpoint'], ['--input-format', 'jsonl'],
                     ['--batch-size', '1000'], ['--batch-bytes', '16777216'],
                     ['--max-item-bytes', '8388608'], ['--verify', str(self.previous)],
                     ['--inspect-resume', 'checkpoint'],
                     ['--check-schema', str(self.old), str(self.new)],
                     ['--check-export-schema', str(self.previous), str(self.new)],
                     ['--check-resume-schema', 'checkpoint', str(self.new)]]
        with patch.object(compatibility, 'verify_export', side_effect=AssertionError('validation')):
            for extra in conflicts:
                with self.subTest(extra=extra), self.assertRaises(SystemExit) as caught:
                    self.cli(*extra)
                self.assertEqual(caught.exception.code, 2)
            for args in ([], [str(self.previous)],
                         [str(self.previous), str(self.candidate), str(self.candidate)]):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main(['--check-exports-schema', *args])
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
