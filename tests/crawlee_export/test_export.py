"""All data here is SYNTHETIC. Tests use real PyArrow, no network or subprocess crawlers."""
import builtins
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
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


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cpe-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'dataset'
        shutil.copytree(ROOT / 'examples/crawlee_export/dataset', self.data)
        self.schema = self.root / 'schema.json'
        shutil.copyfile(ROOT / 'examples/crawlee_export/schema.json', self.schema)
        self.out = self.root / 'out.parquet'
        self.sidecar = Path(str(self.out) + '.manifest.json')

    def export(self, **kwargs):
        return export_dataset(self.data, schema=self.schema, out=self.out, batch_size=2, **kwargs)

    def modify(self, filename, transform):
        path = self.data / filename
        obj = json.loads(path.read_text())
        transform(obj)
        path.write_text(json.dumps(obj, ensure_ascii=True))

    def assert_clean_failure(self, pattern, **kwargs):
        with self.assertRaisesRegex(ExportError, pattern) as caught:
            self.export(**kwargs)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.sidecar.exists())
        self.assertEqual(list(self.root.glob('.cpe-*')), [])
        return caught.exception

    def test_real_parquet_e2e_independent_fixture_verifier(self):
        manifest = self.export()
        self.assertEqual(self.out.read_bytes()[:4], b'PAR1')
        self.assertEqual(manifest['row_count'], 3)
        self.assertEqual(manifest['row_groups'], 2)
        result = subprocess.run([sys.executable, str(ROOT / 'examples/crawlee_export/verify_roundtrip.py'),
                                 str(self.out)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verified 3 synthetic rows', result.stdout)
        self.assertEqual(verify_export(self.out, batch_size=1), manifest)
        self.assertEqual(manifest['ignored_metadata'], ['__metadata__.json'])

    def test_numeric_not_lexical_order_sparse_and_zero(self):
        (self.data / '000000001.json').rename(self.data / '10.json')
        (self.data / '000000002.json').rename(self.data / '2.json')
        (self.data / '000000010.json').rename(self.data / '0.json')
        self.export()
        rows = pq.ParquetFile(self.out).read(use_threads=False).to_pylist()
        self.assertEqual([r['_cpe_item_id'] for r in rows], ['0', '2', '10'])
        self.assertEqual([r['_cpe_source'] for r in rows], ['0.json', '2.json', '10.json'])

    def test_int64_boundaries_and_signed_zero(self):
        self.export()
        rows = pq.ParquetFile(self.out).read(use_threads=False).to_pylist()
        self.assertEqual([r['count'] for r in rows], [2**63-1, -2**63, 2**53+1])
        self.assertEqual(math.copysign(1, rows[2]['price']), -1)
        self.assertIsNone(rows[0]['note'])
        self.assertEqual(rows[1]['note'], '')

    def test_jsonl_e2e_physical_line_provenance_and_crlf(self):
        source = self.root / 'rows.jsonl'
        source.write_bytes((ROOT / 'examples/crawlee_export/items.jsonl').read_bytes().replace(b'\n', b'\r\n').rstrip(b'\r\n'))
        manifest = export_dataset(source, schema=self.schema, out=self.out,
                                  input_format='jsonl', batch_size=1)
        rows = pq.ParquetFile(self.out).read(use_threads=False).to_pylist()
        self.assertEqual([r['_cpe_line'] for r in rows], [1, 2, 3])
        self.assertTrue(all(r['_cpe_item_id'] is None for r in rows))
        self.assertTrue(all(r['_cpe_source'] == source.name for r in rows))
        self.assertEqual(manifest['row_groups'], 3)
        verify_export(self.out)

    def test_empty_dataset_is_typed_and_verified(self):
        for path in self.data.glob('[0-9]*.json'):
            path.unlink()
        result = self.export()
        self.assertEqual(result['row_count'], 0)
        self.assertEqual(result['rows_sha256'], hashlib.sha256(b'').hexdigest())
        self.assertEqual(verify_export(self.out)['row_count'], 0)
        self.assertEqual(pq.ParquetFile(self.out).schema_arrow.field('count').type, pa.int64())

    def test_late_schema_drift_after_written_batch_rolls_back(self):
        # Four rows: the first batch is flushed when row 3 is accepted; row 4 fails.
        shutil.copyfile(self.data / '000000001.json', self.data / '11.json')
        self.modify('11.json', lambda o: o.update(undeclared='do not drop'))
        error = self.assert_clean_failure('undeclared fields')
        self.assertEqual(error.rows_validated, 3)

    def test_late_type_failure_rolls_back(self):
        self.modify('000000010.json', lambda o: o.update(count='123'))
        error = self.assert_clean_failure('expected exact int64')
        self.assertEqual(error.rows_validated, 2)

    def test_required_nullable_field_cannot_be_missing(self):
        self.modify('000000001.json', lambda o: o.pop('note'))
        self.assert_clean_failure('missing fields')

    def test_null_nonnullable(self):
        self.modify('000000001.json', lambda o: o.update(title=None))
        self.assert_clean_failure('null in non-nullable')

    def test_scalar_type_coercions_forbidden(self):
        original = (self.data / '000000001.json').read_bytes()
        for field, value, message in [('count', True, 'int64'), ('count', 1.0, 'int64'),
                                      ('price', 12, 'float64'), ('price', '12.5', 'float64'),
                                      ('active', 1, 'bool'), ('title', ['x'], 'string')]:
            with self.subTest(field=field, value=value):
                (self.data / '000000001.json').write_bytes(original)
                self.modify('000000001.json', lambda o: o.update({field: value}))
                self.assert_clean_failure(message)

    def test_int64_overflow(self):
        for value in [2**63, -2**63-1]:
            with self.subTest(value=value):
                self.modify('000000001.json', lambda o: o.update(count=value))
                self.assert_clean_failure('overflow')

    def test_nan_infinity_nested_and_float_overflow(self):
        for raw in ['NaN', 'Infinity', '-Infinity', '1e400', '{"nested":[NaN]}']:
            with self.subTest(raw=raw):
                (self.data / '000000010.json').write_text('{"tags":' + raw + '}')
                self.assert_clean_failure('non-finite')

    def test_malformed_duplicate_keys_invalid_utf8_and_nonobject(self):
        for raw, message in [(b'{', 'invalid'), (b'{"x":1,"x":2}', 'duplicate JSON key'),
                             (b'{"x":{"a":1,"a":2}}', 'duplicate JSON key'),
                             (b'\xff', 'UTF-8'), (b'[]', 'JSON object'),
                             (b'null', 'JSON object')]:
            with self.subTest(raw=raw):
                (self.data / '000000010.json').write_bytes(raw)
                self.assert_clean_failure(message)

    def test_unpaired_surrogate_not_written(self):
        self.modify('000000010.json', lambda o: o.update(title='\ud800'))
        self.assert_clean_failure('invalid canonical row')

    def test_unknown_filenames_not_ignored(self):
        for filename in ['README.md', '.DS_Store', '__other__.json', '1.JSON', '-1.json', 'a.json']:
            with self.subTest(filename=filename):
                path = self.data / filename
                path.write_text('{}')
                self.assert_clean_failure('unexpected dataset filename')
                path.unlink()

    def test_subdirectory_and_symlink_fail(self):
        path = self.data / 'extra'
        path.mkdir()
        self.assert_clean_failure('regular file')
        path.rmdir()
        (self.data / '3.json').symlink_to(self.data / '000000001.json')
        self.assert_clean_failure('symlinks')

    def test_duplicate_numeric_id(self):
        shutil.copyfile(self.data / '000000001.json', self.data / '1.json')
        self.assert_clean_failure('duplicate numeric item id 1')

    def test_only_documented_metadata_ignored_without_interpreting_it(self):
        (self.data / '__metadata__.json').write_bytes(b'opaque metadata not trusted')
        self.assertEqual(self.export()['row_count'], 3)

    def test_output_and_sidecar_inside_dataset_forbidden(self):
        for out in [self.data / 'output.parquet', self.data / 'new-dir' / 'out.parquet']:
            with self.subTest(out=out):
                if not out.parent.exists():
                    out.parent.mkdir()
                with self.assertRaisesRegex(ExportError, 'inside dataset'):
                    export_dataset(self.data, schema=self.schema, out=out)
                self.assertFalse(out.exists())
        self.assertEqual(list(self.data.glob('.cpe-*')), [])

    def test_collisions_and_existing_output_are_never_overwritten(self):
        for target in [self.schema, self.data / '000000001.json']:
            original = target.read_bytes()
            with self.assertRaisesRegex(ExportError, 'already exists'):
                export_dataset(self.data, schema=self.schema, out=target)
            self.assertEqual(target.read_bytes(), original)
        self.out.write_bytes(b'existing')
        with self.assertRaisesRegex(ExportError, 'already exists'):
            self.export()
        self.assertEqual(self.out.read_bytes(), b'existing')

    def test_existing_sidecar_and_broken_symlink(self):
        self.sidecar.write_bytes(b'existing sidecar')
        with self.assertRaisesRegex(ExportError, 'already exists'):
            self.export()
        self.assertEqual(self.sidecar.read_bytes(), b'existing sidecar')
        self.sidecar.unlink()
        self.out.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ExportError, 'already exists'):
            self.export()
        self.assertTrue(self.out.is_symlink())

    def test_jsonl_cannot_overwrite_input(self):
        source = self.root / 'input.jsonl'
        source.write_text('{"x":1}\n')
        with self.assertRaisesRegex(ExportError, 'already exists'):
            export_dataset(source, schema=self.schema, out=source, input_format='jsonl')
        self.assertEqual(source.read_text(), '{"x":1}\n')

    def test_output_through_parent_symlink_inside_dataset(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.data, target_is_directory=True)
        with self.assertRaisesRegex(ExportError, 'inside dataset'):
            export_dataset(self.data, schema=self.schema, out=alias / 'out.parquet')

    def test_bounded_item_and_canonical_row_bytes(self):
        self.assert_clean_failure('byte limit', max_item_bytes=10)
        self.assert_clean_failure('canonical row exceeds', batch_bytes=10)

    def test_byte_budget_flushes_batches(self):
        # Each first/last row fits 350 bytes, but their combined payload does not.
        result = export_dataset(self.data, schema=self.schema, out=self.out,
                                batch_size=1000, batch_bytes=350)
        self.assertGreater(result['row_groups'], 1)
        verify_export(self.out)

    def test_jsonl_blank_line_and_oversize_fail(self):
        source = self.root / 'input.jsonl'
        for raw, limit, message in [(b'\n', 1024, 'invalid'),
                                    (b'{"x":"' + b'a'*50 + b'"}\n', 10, 'byte limit')]:
            source.write_bytes(raw)
            with self.assertRaisesRegex(ExportError, message):
                export_dataset(source, schema=self.schema, out=self.out,
                               input_format='jsonl', max_item_bytes=limit)
            self.assertFalse(self.out.exists())

    def test_json_string_canonical_order_and_explicit_nullable(self):
        schema = json.loads(self.schema.read_text())
        schema['fields'][-1]['nullable'] = True
        self.schema.write_text(json.dumps(schema))
        self.modify('000000002.json', lambda o: o.update(tags=None))
        self.export()
        rows = pq.ParquetFile(self.out).read(use_threads=False).to_pylist()
        self.assertEqual(rows[0]['tags'], '["x",{"a":"é\\n","z":2}]')
        self.assertIsNone(rows[1]['tags'])

    def test_schema_rejects_unsupported_and_ambiguous_contracts(self):
        field = {'name':'x','type':'string'}
        cases = [{}, {'version':2,'fields':[field]}, {'version':True,'fields':[field]},
                 {'version':1,'fields':[]}, {'version':1,'fields':[field,field]},
                 {'version':1,'fields':[{'name':'_cpe_source','type':'string'}]},
                 {'version':1,'fields':[{'name':'x','type':'array'}]},
                 {'version':1,'fields':[dict(field,nullable='true')]},
                 {'version':1,'fields':[dict(field,required=False)]},
                 {'version':1,'fields':[{'name':'x','type':[]}]},
                 {'version':1,'fields':[{'name':[], 'type':'string'}]}]
        for case in cases:
            with self.subTest(case=case):
                self.schema.write_text(json.dumps(case))
                self.assert_clean_failure('schema|field|nullable|version|type')

    def test_invalid_limits_and_format(self):
        for kwargs in [{'batch_size':0}, {'batch_bytes':-1}, {'max_item_bytes':True},
                       {'input_format':'auto'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ExportError):
                export_dataset(self.data, schema=self.schema, out=self.out, **kwargs)
        self.assertFalse(self.out.exists())

    def test_missing_dependency_guidance_no_output(self):
        original = builtins.__import__
        def no_arrow(name, *args, **kwargs):
            if name.startswith('pyarrow'):
                raise ImportError('synthetic missing dependency')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=no_arrow):
            self.assert_clean_failure(r'needs pyarrow: pipx install .qawrench\[parquet\]')

    def test_write_failure_and_readback_failure_cleanup(self):
        with patch('pyarrow.parquet.ParquetWriter.write_table', side_effect=OSError('synthetic disk full')):
            self.assert_clean_failure('disk full')
        with patch('qawrench.crawlee_export.core._readback', side_effect=ExportError('synthetic mismatch')):
            self.assert_clean_failure('mismatch')

    def test_publish_second_link_failure_removes_sidecar(self):
        import os
        link = os.link
        def fail_parquet(src, dest):
            if Path(dest) == self.out:
                raise OSError('synthetic publication failure')
            return link(src, dest)
        with patch('qawrench.crawlee_export.core.os.link', side_effect=fail_parquet):
            self.assert_clean_failure('publication failure')

    def test_publish_race_preserves_other_writers_destination(self):
        import os
        link = os.link
        def other_writer(src, dest):
            if Path(dest) == self.out:
                self.out.write_bytes(b'other writer')
            return link(src, dest)
        with patch('qawrench.crawlee_export.core.os.link', side_effect=other_writer):
            with self.assertRaises(ExportError):
                self.export()
        self.assertEqual(self.out.read_bytes(), b'other writer')
        self.assertFalse(self.sidecar.exists())
        self.assertEqual(list(self.root.glob('.cpe-*')), [])

    def test_value_verifier_detects_tampered_sidecar_and_parquet(self):
        self.export()
        original = self.sidecar.read_bytes()
        for key, value, message in [('row_count', 5, 'row count'),
                                    ('rows_sha256', '0'*64, 'value digest'),
                                    ('row_groups', 7, 'row_groups'),
                                    ('format_version', 2, 'format_version'),
                                    ('row_count', '3', 'nonnegative'),
                                    ('parquet_sha256', 'invalid', 'SHA-256')]:
            with self.subTest(key=key):
                manifest = json.loads(original)
                manifest[key] = value
                self.sidecar.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ExportError, message):
                    verify_export(self.out)
        self.sidecar.write_bytes(original)
        raw = self.out.read_bytes()
        self.out.write_bytes(raw[:-5] + b'XXXXX')
        with self.assertRaisesRegex(ExportError, 'digest mismatch'):
            verify_export(self.out)

    def test_schema_verification_does_not_trust_manifest(self):
        self.export()
        manifest = json.loads(self.sidecar.read_text())
        manifest['schema']['fields'][0]['nullable'] = True
        self.sidecar.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ExportError, 'schema mismatch'):
            verify_export(self.out)

    def test_cli_success_and_failure_accounting_and_verify(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [str(self.data), '--schema', str(self.schema), '--out', str(self.out), '--batch-size', '2']
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(argv), 0)
        self.assertEqual(json.loads(stdout.getvalue())['row_count'], 3)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(['--verify', str(self.out)]), 0)
        self.out.unlink()
        self.sidecar.unlink()
        self.modify('000000010.json', lambda o: o.update(count='bad'))
        with redirect_stderr(stderr):
            self.assertEqual(main(argv), 2)
        error = json.loads(stderr.getvalue())
        self.assertEqual(error['rows_validated'], 2)
        self.assertEqual(error['rows_committed'], 0)
        self.assertIn('000000010.json', error['error'])


if __name__ == '__main__':
    unittest.main()
