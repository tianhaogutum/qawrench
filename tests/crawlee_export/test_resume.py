"""Synthetic integration fixtures for durable resume; actual PyArrow parts/readers."""
import json
import subprocess
import sys
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pyarrow.parquet as pq

from qawrench.crawlee_export import ExportError, export_dataset, verify_export

ROOT = Path(__file__).resolve().parents[2]


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-resume-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'dataset'
        shutil.copytree(ROOT / 'examples/crawlee_export/dataset', self.source)
        self.schema = self.root / 'schema.json'
        shutil.copyfile(ROOT / 'examples/crawlee_export/schema.json', self.schema)
        self.out = self.root / 'resumed.parquet'
        self.work = self.root / 'checkpoint'

    def export(self, **kwargs):
        return export_dataset(self.source, schema=self.schema, out=self.out,
                              resume_dir=self.work, batch_size=2, **kwargs)

    def interrupt(self, **kwargs):
        # The first table becomes a durable checkpoint, then interrupt the next write.
        original = pq.ParquetWriter.write_table
        calls = []
        def interrupted(writer, table, **options):
            calls.append(table.num_rows)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return original(writer, table, **options)
        with patch.object(pq.ParquetWriter, 'write_table', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.export(**kwargs)
        self.assertFalse(self.out.exists())
        manifest = json.loads((self.work / 'resume.json').read_text())
        self.assertEqual([part['row_count'] for part in manifest['parts']], [2])

    def test_interrupted_dataset_and_jsonl_equal_clean_rows_and_reuse_checkpoint(self):
        for mode in ('dataset', 'jsonl'):
            with self.subTest(mode=mode):
                if mode == 'jsonl':
                    self.source = self.root / 'rows.jsonl'
                    self.source.write_bytes((ROOT / 'examples/crawlee_export/items.jsonl').read_bytes()
                                            .replace(b'\n', b'\r\n').rstrip(b'\r\n'))
                self.out = self.root / (mode + '.parquet')
                self.work = self.root / (mode + '-checkpoint')
                clean = self.root / (mode + '-clean.parquet')
                expected = export_dataset(self.source, schema=self.schema, out=clean,
                                          input_format=mode, batch_size=2)
                self.interrupt(input_format=mode)
                from qawrench.crawlee_export.resume import Resume
                original = Resume._write_part
                writes = []
                def tracked(state, batch):
                    writes.append(len(batch))
                    return original(state, batch)
                with patch.object(Resume, '_write_part', tracked):
                    actual = self.export(input_format=mode)
                self.assertEqual(writes, [1], 'completed two-row part must be reused')
                self.assertEqual(actual, expected)
                self.assertEqual(pq.read_table(self.out).to_pylist(), pq.read_table(clean).to_pylist())
                self.assertEqual(verify_export(self.out), actual)
                # Retrying after publication is safe and read-back verified.
                self.assertEqual(self.export(input_format=mode), actual)

    def test_resume_rejects_changed_source_schema_options_and_corrupt_part(self):
        self.interrupt()
        original = (self.source / '000000001.json').read_bytes()
        (self.source / '000000001.json').write_bytes(original.replace(b'caf', b'CAF'))
        with self.assertRaisesRegex(ExportError, 'binding mismatch'):
            self.export()
        (self.source / '000000001.json').write_bytes(original)
        spec = self.schema.read_bytes()
        self.schema.write_bytes(spec.replace(b'"string"', b'"json-string"', 1))
        with self.assertRaisesRegex(ExportError, 'binding mismatch'):
            self.export()
        self.schema.write_bytes(spec)
        with self.assertRaisesRegex(ExportError, 'binding mismatch'):
            self.export(batch_bytes=1024)
        (self.work / 'part-00000000.parquet').write_bytes(b'broken')
        with self.assertRaisesRegex(ExportError, 'checkpoint.*digest'):
            self.export()
        self.assertFalse(self.out.exists())

    def test_repeated_interruptions_preserve_all_rows_across_many_checkpoints(self):
        from qawrench.crawlee_export.resume import Resume
        original = Resume._write_part
        self.schema.write_text(json.dumps({'version': 1, 'fields': [{'name': 'n', 'type': 'int64'}]}))
        for path in self.source.iterdir():
            path.unlink()
        for n in range(23):
            (self.source / f'{n * 3}.json').write_text(json.dumps({'n': n}))
        for mode in ('dataset', 'jsonl'):
            with self.subTest(mode=mode):
                if mode == 'jsonl':
                    self.source = self.root / 'many.jsonl'
                    self.source.write_bytes(b'\r\n'.join(
                        json.dumps({'n': n}).encode() for n in range(23)))
                self.out = self.root / (mode + '-many.parquet')
                self.work = self.root / (mode + '-many-work')
                clean = self.root / (mode + '-many-clean.parquet')
                expected = export_dataset(self.source, schema=self.schema, out=clean,
                                          batch_size=2, input_format=mode)
                def save_then_interrupt(state, batch):
                    original(state, batch)
                    raise KeyboardInterrupt
                for checkpoint_number in range(1, 4):
                    with patch.object(Resume, '_write_part', save_then_interrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            self.export(input_format=mode)
                    state = json.loads((self.work / 'resume.json').read_text())
                    self.assertEqual(len(state['parts']), checkpoint_number)
                self.assertEqual(self.export(input_format=mode), expected)
                rows = pq.read_table(self.out).to_pylist()
                self.assertEqual([row['n'] for row in rows], list(range(23)))
                self.assertEqual(rows, pq.read_table(clean).to_pylist())

    def test_byte_budget_and_empty_inputs_match_clean_export(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    for path in self.source.glob('[0-9]*.json'):
                        path.unlink()
                self.work = self.root / f'work-{empty}'
                self.out = self.root / f'out-{empty}.parquet'
                clean = self.root / f'clean-{empty}.parquet'
                expected = export_dataset(self.source, schema=self.schema, out=clean,
                                          batch_size=2, batch_bytes=350)
                self.assertEqual(self.export(batch_bytes=350), expected)
                self.assertEqual(self.export(batch_bytes=350), expected)

    def test_invalid_late_row_keeps_checkpoint_but_publishes_nothing(self):
        shutil.copyfile(self.source / '000000001.json', self.source / '11.json')
        path = self.source / '11.json'
        row = json.loads(path.read_text())
        row['count'] = 'invalid'
        path.write_text(json.dumps(row))
        with self.assertRaisesRegex(ExportError, 'int64') as error:
            self.export()
        self.assertEqual(error.exception.rows_validated, 3)
        state = json.loads((self.work / 'resume.json').read_text())
        self.assertEqual(state['parts'][0]['row_count'], 2)
        with self.assertRaisesRegex(ExportError, 'int64'):
            self.export()
        self.assertFalse(self.out.exists())
        self.assertFalse(Path(str(self.out) + '.manifest.json').exists())

    def test_source_file_set_is_bound_and_checkpoint_parts_are_strict(self):
        self.interrupt()
        item = self.source / '000000010.json'
        item.rename(self.source / '11.json')
        with self.assertRaisesRegex(ExportError, 'binding mismatch'):
            self.export()
        (self.source / '11.json').rename(item)
        path = self.work / 'resume.json'
        original = path.read_bytes()
        for transform in (lambda m: m.update(source_row_count=99),
                          lambda m: m['parts'][0].update(name='../outside.parquet'),
                          lambda m: m['parts'][0].update(row_count=True)):
            state = json.loads(original)
            transform(state)
            path.write_text(json.dumps(state))
            with self.assertRaisesRegex(ExportError, 'invalid checkpoint'):
                self.export()
        path.write_bytes(original)
        part = self.work / 'part-00000000.parquet'
        part.unlink()
        part.symlink_to(self.schema)
        with self.assertRaisesRegex(ExportError, 'regular file'):
            self.export()

    def test_directory_ownership_locking_and_foreign_destination(self):
        from qawrench.crawlee_export.core import Input, Schema
        from qawrench.crawlee_export.resume import Resume
        import pyarrow as pa
        state = Resume(self.work, Input.inspect(self.source, 'dataset'),
                       Schema.parse(json.loads(self.schema.read_text())), self.schema,
                       self.out, 2, 16*1024*1024, 8*1024*1024, pa, pq)
        state.open()
        try:
            with self.assertRaisesRegex(ExportError, 'locked'):
                self.export()
        finally:
            state.close()
        self.out.write_bytes(b'foreign output')
        with self.assertRaisesRegex(ExportError, 'existing destination differs'):
            self.export()
        self.assertEqual(self.out.read_bytes(), b'foreign output')
        self.assertFalse(Path(str(self.out) + '.manifest.json').exists())
        self.work = self.root / 'foreign'
        self.work.mkdir()
        (self.work / 'unrelated').write_text('keep')
        with self.assertRaisesRegex(ExportError, 'unexpected file'):
            self.export()
        self.assertEqual((self.work / 'unrelated').read_text(), 'keep')
        self.work = self.source / 'checkpoints'
        with self.assertRaisesRegex(ExportError, 'separate'):
            self.export()
        self.assertFalse(self.work.exists())

    def test_process_death_at_checkpoint_and_publication_boundaries(self):
        # os._exit bypasses all context-manager/finally cleanup, like a killed worker.
        script = r"""
import os, sys
from unittest.mock import patch
from qawrench.crawlee_export import export_dataset
from qawrench.crawlee_export.resume import Resume
source, schema, out, work, boundary = sys.argv[1:]
save = Resume._save_manifest
link = os.link
def crash_save(state):
    if boundary == 'before-checkpoint' and state.manifest['parts']:
        os._exit(73)
    save(state)
    if boundary == 'after-checkpoint' and state.manifest['parts']:
        os._exit(73)
def crash_link(src, dest):
    link(src, dest)
    if boundary == 'sidecar' or (boundary == 'parquet' and str(dest) == out):
        os._exit(73)
with patch.object(Resume, '_save_manifest', crash_save), patch('os.link', crash_link):
    export_dataset(source, schema=schema, out=out, resume_dir=work, batch_size=2)
"""
        clean = self.root / 'clean.parquet'
        expected = export_dataset(self.source, schema=self.schema, out=clean, batch_size=2)
        for boundary in ('before-checkpoint', 'after-checkpoint', 'sidecar', 'parquet'):
            with self.subTest(boundary=boundary):
                self.out = self.root / (boundary + '.parquet')
                self.work = self.root / (boundary + '-work')
                result = subprocess.run([sys.executable, *(['-I'] if sys.flags.isolated else []),
                                         '-c', script, str(self.source),
                                         str(self.schema), str(self.out), str(self.work), boundary],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 73, result.stderr)
                self.assertEqual(self.export(), expected)
                self.assertEqual(verify_export(self.out), expected)
                self.assertEqual(pq.read_table(self.out).to_pylist(), pq.read_table(clean).to_pylist())
                self.assertEqual(list(self.work.glob('.cpe-*.tmp')), [])

    def test_source_mutation_during_export_prevents_publication(self):
        from qawrench.crawlee_export.resume import Resume
        original = Resume._write_part
        def mutate(state, batch):
            original(state, batch)
            path = self.source / '000000001.json'
            path.write_bytes(path.read_bytes() + b' ')
        with patch.object(Resume, '_write_part', mutate):
            with self.assertRaisesRegex(ExportError, 'source changed during export'):
                self.export()
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
