"""Synthetic schema evolution integration tests using real Parquet batches."""
from contextlib import redirect_stderr
import io
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from qawrench.crawlee_export import ExportError, export_dataset, verify_export
from qawrench.crawlee_export.cli import main
from qawrench.crawlee_export.core import Schema, canonical

ROOT = Path(__file__).resolve().parents[2]
ROWS = [{'id': 1}, {'id': 2, 'note': None}, {'id': 3, 'note': 'new — 東京\n'},
        {'id': 4}, {'id': 5, 'note': ''}]
SCHEMA = {'version': 2, 'evolution': 'nullable-union', 'fields': [
    {'name': 'id', 'type': 'int64'},
    {'name': 'note', 'type': 'string', 'nullable': True},
    {'name': 'score', 'type': 'int64', 'nullable': True}]}
SUMMARY = {
    'id': {'present': 5, 'missing': 0, 'explicit_null': 0, 'first_present_row': 1},
    'note': {'present': 3, 'missing': 2, 'explicit_null': 1, 'first_present_row': 2},
    'score': {'present': 0, 'missing': 5, 'explicit_null': 0, 'first_present_row': None}}


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-evolution-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'items.jsonl'
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps(SCHEMA))
        self.out = self.root / 'out.parquet'
        self.write_rows(ROWS)

    def write_rows(self, rows):
        self.source.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))

    def export(self, **kwargs):
        return export_dataset(self.source, schema=self.schema, out=self.out,
                              input_format='jsonl', **kwargs)

    def assert_no_output(self):
        self.assertFalse(self.out.exists())
        self.assertFalse(Path(str(self.out) + '.manifest.json').exists())
        self.assertEqual(list(self.root.glob('.cpe-*')), [])

    def test_missing_added_and_disappearing_nullable_fields_across_batches(self):
        expected = [dict(id=r['id'], note=r.get('note'), score=None,
                         _cpe_missing_fields=[name for name in ('note', 'score') if name not in r],
                         _cpe_source='items.jsonl', _cpe_item_id=None, _cpe_line=i)
                    for i, r in enumerate(ROWS, 1)]
        for size in (1, 2, 10):
            with self.subTest(batch_size=size):
                self.out = self.root / f'out-{size}.parquet'
                manifest = self.export(batch_size=size)
                with pq.ParquetFile(self.out) as parquet:
                    self.assertEqual(parquet.read(use_threads=False).to_pylist(), expected)
                    self.assertEqual(parquet.schema_arrow.field('score').type, pa.int64())
                    self.assertTrue(parquet.schema_arrow.field('score').nullable)
                    missing = parquet.schema_arrow.field('_cpe_missing_fields')
                    self.assertFalse(missing.nullable)
                    self.assertEqual(missing.type.value_type, pa.string())
                    self.assertFalse(missing.type.value_field.nullable)
                    embedded = json.loads(parquet.schema_arrow.metadata[b'cpe.schema'])
                    self.assertEqual(embedded['evolution'], 'nullable-union')
                self.assertEqual(manifest['field_provenance'], SUMMARY)
                self.assertEqual(verify_export(self.out, batch_size=1), manifest)

    def test_strict_policy_and_version1_keep_missing_fields_error(self):
        for version in (1, 2):
            spec = dict(SCHEMA, version=version)
            if version == 1:
                spec.pop('evolution')
            else:
                spec['evolution'] = 'strict'
            self.schema.write_text(json.dumps(spec))
            with self.subTest(version=version), self.assertRaisesRegex(ExportError, 'missing fields'):
                self.export(batch_size=1)
            self.assert_no_output()

    def test_strict_v2_records_present_null_and_no_missing_markers(self):
        self.schema.write_text(json.dumps(dict(SCHEMA, evolution='strict')))
        self.write_rows([{'id': 1, 'note': None, 'score': 7}])
        result = self.export()
        self.assertEqual(result['field_provenance']['note'],
                         dict(present=1, missing=0, explicit_null=1, first_present_row=1))
        self.assertEqual(pq.read_table(self.out).to_pylist()[0]['_cpe_missing_fields'], [])
        self.assertEqual(verify_export(self.out), result)

    def test_late_required_missing_unknown_addition_and_type_changes_fail_closed(self):
        for bad, message in [({}, 'missing fields'), ({'id': 6, 'other': None}, 'undeclared fields'),
                             ({'id': 6, 'note': 123}, 'expected string'),
                             ({'id': None}, 'non-nullable'),
                             ({'id': 6, 'score': True}, 'exact int64')]:
            with self.subTest(bad=bad):
                self.write_rows(ROWS + [bad])
                with self.assertRaisesRegex(ExportError, message) as caught:
                    self.export(batch_size=1)
                self.assertEqual(caught.exception.rows_validated, 5)
                self.assert_no_output()

    def test_empty_input_has_typed_columns_and_zero_field_counts(self):
        self.write_rows([])
        result = self.export(batch_size=1)
        self.assertEqual(result['field_provenance'], {
            name: dict(present=0, missing=0, explicit_null=0, first_present_row=None)
            for name in SUMMARY})
        self.assertEqual(verify_export(self.out), result)

    def test_nullable_union_preserves_all_supported_types_without_coercion(self):
        fields = [{'name': kind, 'type': kind, 'nullable': True}
                  for kind in ('string', 'int64', 'float64', 'bool', 'json-string')]
        self.schema.write_text(json.dumps(dict(SCHEMA, fields=fields)))
        self.write_rows([{}, {f['name']: None for f in fields},
                         {'string': '', 'int64': 9007199254740993, 'float64': 1.5,
                          'bool': False, 'json-string': {'x': 'null'}}])
        result = self.export(batch_size=1)
        rows = pq.read_table(self.out).to_pylist()
        self.assertEqual(rows[0]['_cpe_missing_fields'], [f['name'] for f in fields])
        self.assertEqual(rows[1]['_cpe_missing_fields'], [])
        self.assertEqual(rows[2]['int64'], 9007199254740993)
        self.assertEqual(rows[2]['json-string'], '{"x":"null"}')
        for stats in result['field_provenance'].values():
            self.assertEqual(stats, dict(present=2, missing=1, explicit_null=1, first_present_row=2))
        self.assertEqual(verify_export(self.out), result)

    def test_byte_budget_includes_missing_field_provenance(self):
        expected = self.export(batch_size=10)
        rows = pq.read_table(self.out).to_pylist()
        limit = max(len(canonical(row) + b'\n') for row in rows)
        self.out = self.root / 'byte-limited.parquet'
        result = self.export(batch_size=10, batch_bytes=limit)
        self.assertEqual(result['row_groups'], 5)
        self.assertEqual(result['rows_sha256'], expected['rows_sha256'])
        self.assertEqual(result['field_provenance'], SUMMARY)
        self.out = self.root / 'too-small.parquet'
        with self.assertRaisesRegex(ExportError, 'canonical row exceeds'):
            self.export(batch_bytes=len(canonical(rows[0])))
        self.assert_no_output()

    def test_policy_validation_is_explicit_and_versioned(self):
        cases = [dict(SCHEMA, evolution='infer'), dict(SCHEMA, evolution=None),
                 dict(SCHEMA, evolution=[]), dict(SCHEMA, version=True),
                 dict(SCHEMA, version=1), dict(SCHEMA, unexpected=True),
                 {'version': 2, 'fields': SCHEMA['fields']}]
        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(ExportError):
                Schema.parse(spec)

    def test_resume_reuses_evolved_parts_and_binds_policy(self):
        from qawrench.crawlee_export.resume import Resume
        checkpoint = self.root / 'resume'
        clean = self.root / 'clean.parquet'
        expected = export_dataset(self.source, schema=self.schema, out=clean,
                                  input_format='jsonl', batch_size=1)
        original = Resume._write_part
        def commit_then_interrupt(state, batch):
            original(state, batch)
            raise KeyboardInterrupt
        with patch.object(Resume, '_write_part', commit_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.export(batch_size=1, resume_dir=checkpoint)
        part = checkpoint / 'part-00000000.parquet'
        saved = part.read_bytes()
        self.schema.write_text(json.dumps(dict(SCHEMA, evolution='strict')))
        with self.assertRaisesRegex(ExportError, 'binding mismatch'):
            self.export(batch_size=1, resume_dir=checkpoint)
        self.schema.write_text(json.dumps(SCHEMA))
        self.assertEqual(self.export(batch_size=1, resume_dir=checkpoint), expected)
        self.assertEqual(part.read_bytes(), saved)
        self.assertEqual(pq.read_table(self.out).to_pylist(), pq.read_table(clean).to_pylist())
        self.assertEqual(verify_export(self.out), expected)

    def test_verify_rejects_changed_or_missing_field_summary(self):
        self.export()
        path = Path(str(self.out) + '.manifest.json')
        original = json.loads(path.read_text())
        for change in ('count', 'missing', 'first', 'bool'):
            modified = json.loads(json.dumps(original))
            if change == 'missing':
                modified.pop('field_provenance')
            elif change == 'first':
                modified['field_provenance']['note']['first_present_row'] = 3
            elif change == 'bool':
                modified['field_provenance']['note']['explicit_null'] = True
            else:
                modified['field_provenance']['note']['missing'] = 0
            path.write_text(json.dumps(modified))
            with self.subTest(change=change), self.assertRaisesRegex(ExportError, 'field provenance'):
                verify_export(self.out)

    def test_readback_validates_presence_markers_even_with_matching_digests(self):
        manifest = self.export()
        table = pq.read_table(self.out)
        original = table.to_pylist()
        for markers in (['unknown'], ['note', 'note', 'score'], ['score', 'note'],
                        ['id', 'note', 'score']):
            with self.subTest(markers=markers):
                rows = json.loads(json.dumps(original))
                rows[0]['_cpe_missing_fields'] = markers
                pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), self.out,
                               compression='NONE', version='2.6', use_dictionary=False)
                changed = dict(manifest, parquet_bytes=self.out.stat().st_size,
                               parquet_sha256=hashlib.sha256(self.out.read_bytes()).hexdigest(),
                               rows_sha256=hashlib.sha256(b''.join(
                                   canonical(row) + b'\n' for row in rows)).hexdigest(), row_groups=1)
                Path(str(self.out) + '.manifest.json').write_text(json.dumps(changed))
                with self.assertRaisesRegex(ExportError, 'invalid field provenance'):
                    verify_export(self.out)

    def test_cli_failure_reports_late_line_and_zero_committed(self):
        self.write_rows(ROWS + [{'id': 6, 'added': None}])
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main([str(self.source), '--schema', str(self.schema), '--out', str(self.out),
                         '--input-format', 'jsonl', '--batch-size', '1'])
        self.assertEqual(code, 2)
        error = json.loads(stderr.getvalue())
        self.assertEqual(error['rows_validated'], 5)
        self.assertEqual(error['rows_committed'], 0)
        self.assertIn('items.jsonl:6', error['error'])
        self.assert_no_output()

    def test_bundled_dataset_and_jsonl_independent_verifier(self):
        for mode in ('dataset', 'jsonl'):
            with self.subTest(mode=mode):
                source = ROOT / 'examples/crawlee_export/evolution' / ('dataset' if mode == 'dataset' else 'items.jsonl')
                self.out = self.root / (mode + '.parquet')
                manifest = export_dataset(source, schema=ROOT / 'examples/crawlee_export/evolution/schema.json',
                                          out=self.out, input_format=mode, batch_size=1)
                self.assertEqual(manifest['field_provenance'], SUMMARY)
                result = subprocess.run([sys.executable, str(ROOT / 'examples/crawlee_export/verify_evolution.py'),
                                         str(self.out), mode], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Verified 5 synthetic evolution rows', result.stdout)


if __name__ == '__main__':
    unittest.main()
