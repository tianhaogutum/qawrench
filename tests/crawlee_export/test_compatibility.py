"""Declared-schema compatibility contracts; synthetic files, no source reads."""
import builtins
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.crawlee_export import ExportError
from qawrench.crawlee_export import core
from qawrench.crawlee_export.cli import main


def field(name, kind='string', nullable=False):
    return dict(name=name, type=kind, nullable=nullable)


def schema(fields, policy=None):
    result = dict(version=1 if policy is None else 2, fields=fields)
    if policy is not None:
        result['evolution'] = policy
    return result


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-compatibility-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.old = self.root / 'old.json'
        self.new = self.root / 'new.json'
        self.write(schema([field('id', 'int64')]),
                   schema([field('id', 'int64'), field('note', nullable=True)], 'nullable-union'))

    def write(self, old, new):
        self.old.write_text(json.dumps(old))
        self.new.write_text(json.dumps(new))

    def compare(self):
        # Look up at call time so the CLI regression runs against the prior implementation.
        from qawrench.crawlee_export import check_schema_compatibility
        return check_schema_compatibility(self.old, self.new)

    def cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(['--check-schema', str(self.old), str(self.new), *extra])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_cli_nullable_addition_preflight(self):
        code, stdout, stderr = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        self.assertEqual(json.loads(stdout), dict(compatible=True, added_nullable_fields=['note'],
                                                 relaxed_nullable_fields=[], issues=[]))

    def test_identity_reorder_defaults_and_version_transitions(self):
        fields = [dict(name='id', type='int64'), field('note', nullable=True)]
        for old_policy, new_policy in [(None, None), (None, 'strict'), ('strict', None),
                                        ('strict', 'nullable-union'),
                                        ('nullable-union', 'nullable-union')]:
            with self.subTest(old=old_policy, new=new_policy):
                self.write(schema(fields, old_policy), schema(list(reversed(fields)), new_policy))
                self.assertEqual(self.compare(), dict(compatible=True, added_nullable_fields=[],
                                                      relaxed_nullable_fields=[], issues=[]))

    def test_relaxation_and_nullable_additions_keep_new_schema_order(self):
        old = schema([field('id', 'int64')])
        new = schema([field('β', 'bool', True), field('id', 'int64', True),
                      field('a', 'json-string', True)], 'nullable-union')
        self.write(old, new)
        saved = copy.deepcopy(new)
        self.assertEqual(self.compare(), dict(compatible=True, added_nullable_fields=['β', 'a'],
                                              relaxed_nullable_fields=['id'], issues=[]))
        self.assertEqual(json.loads(self.new.read_text()), saved)

    def test_reports_every_incompatibility_in_stable_order(self):
        self.write(schema([field('gone'), field('amount', 'int64', True),
                           field('payload', 'json-string')], 'nullable-union'),
                   schema([field('amount', 'float64'), field('payload'), field('required')], 'strict'))
        expected = dict(compatible=False, added_nullable_fields=[], relaxed_nullable_fields=[], issues=[
            dict(code='field_removed', field='gone'),
            dict(code='type_changed', field='amount', old='int64', new='float64'),
            dict(code='nullability_tightened', field='amount', old=True, new=False),
            dict(code='type_changed', field='payload', old='json-string', new='string'),
            dict(code='required_field_added', field='required'),
            dict(code='evolution_tightened', old='nullable-union', new='strict')])
        self.assertEqual(self.compare(), expected)
        code, stdout, stderr = self.cli()
        self.assertEqual((code, json.loads(stdout), stderr), (2, expected, ''))

    def test_nullable_addition_requires_union_including_v1(self):
        fields = [field('id', 'int64')]
        for policy in (None, 'strict'):
            with self.subTest(policy=policy):
                self.write(schema(fields), schema(fields + [field('note', nullable=True)], policy))
                self.assertEqual(self.compare(), dict(compatible=False, added_nullable_fields=['note'],
                    relaxed_nullable_fields=[], issues=[dict(code='nullable_addition_requires_union',
                                                              field='note')]))

    def test_direction_rename_and_required_additions(self):
        self.assertTrue(self.compare()['compatible'])
        self.old, self.new = self.new, self.old
        self.assertEqual(self.compare()['issues'], [dict(code='field_removed', field='note'),
            dict(code='evolution_tightened', old='nullable-union', new='strict')])
        self.write(schema([field('old')]), schema([field('renamed')], 'nullable-union'))
        self.assertEqual(self.compare()['issues'], [dict(code='field_removed', field='old'),
                                                  dict(code='required_field_added', field='renamed')])

    def test_union_to_strict_refused_even_without_nullable_fields(self):
        fields = [field('id', 'int64')]
        for target in (None, 'strict'):
            self.write(schema(fields, 'nullable-union'), schema(fields, target))
            self.assertEqual(self.compare()['issues'], [dict(code='evolution_tightened',
                                                             old='nullable-union', new='strict')])

    def test_no_logical_type_promotion_for_any_supported_pair(self):
        for old in sorted(core.TYPES):
            for new in sorted(core.TYPES):
                with self.subTest(old=old, new=new):
                    self.write(schema([field('value', old)]), schema([field('value', new)]))
                    self.assertEqual(self.compare()['issues'], [] if old == new else [
                        dict(code='type_changed', field='value', old=old, new=new)])

    def test_invalid_schema_inputs_fail_with_json_error_and_no_report(self):
        valid = self.new.read_bytes()
        invalid = [b'{}', b'{"version":1,"version":1,"fields":[]}', b'\xff',
                   b'{"version":1,"fields":[{"name":"x","type":"string","nullable":1}]}',
                   b'{"version":1,"fields":[{"name":"_cpe_x","type":"string"}]}',
                   b'{"version":1,"fields":[{"name":"x","type":"string"},'
                   b'{"name":"x","type":"bool"}]}',
                   b'{"version":1,"fields":[{"name":"x","type":"date"}]}',
                   b'{"version":2,"evolution":"infer","fields":[]}',
                   b'{"version":1,"fields":[{"name":"\\ud800","type":"string"}]}',
                   b' ' * (core.MAX_SCHEMA_BYTES + 1)]
        for path in (self.old, self.new):
            original = path.read_bytes()
            for raw in invalid:
                with self.subTest(path=path.name, raw=raw[:80]):
                    path.write_bytes(raw)
                    with self.assertRaises(ExportError):
                        self.compare()
                    code, stdout, stderr = self.cli()
                    self.assertEqual((code, stdout), (2, ''))
                    error = json.loads(stderr)
                    self.assertTrue(error['error'])
                    self.assertEqual((error['rows_validated'], error['rows_committed']), (0, 0))
            path.write_bytes(original)
        self.assertEqual(self.new.read_bytes(), valid)

    def test_missing_directory_and_symlink_schemas_refused(self):
        self.old.unlink()
        for kind in ('missing', 'directory', 'symlink'):
            if kind == 'directory':
                self.old.mkdir()
            elif kind == 'symlink':
                self.old.rmdir()
                self.old.symlink_to(self.new)
            with self.subTest(kind=kind):
                with self.assertRaises(ExportError):
                    self.compare()
                code, stdout, stderr = self.cli()
                self.assertEqual((code, stdout), (2, ''))
                self.assertTrue(json.loads(stderr)['error'])

    def test_comparison_is_read_only_and_does_not_load_arrow_or_export(self):
        def snapshot():
            return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        before = snapshot()
        real_import = builtins.__import__

        def no_arrow(name, *args, **kwargs):
            if name.startswith('pyarrow'):
                self.fail('Schema comparison must not import PyArrow')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=no_arrow), \
                patch.object(core, '_arrow', side_effect=AssertionError('Arrow requested')), \
                patch('qawrench.crawlee_export.cli.export_dataset',
                      side_effect=AssertionError('Export requested')):
            self.assertTrue(self.compare()['compatible'])
            self.assertEqual(self.cli()[0], 0)
        self.assertEqual(snapshot(), before)

    def test_cli_rejects_conflicting_or_incomplete_modes(self):
        conflicts = [['source'], ['--schema', str(self.old)], ['--out', 'out.parquet'],
                     ['--resume-dir', 'checkpoint'], ['--verify', 'out.parquet'],
                     ['--input-format', 'dataset'], ['--batch-size', '1000'],
                     ['--batch-bytes', '16777216'], ['--max-item-bytes', '8388608']]
        for extra in conflicts:
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                self.cli(*extra)
            self.assertEqual(caught.exception.code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['--check-schema', str(self.old)])
        self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
