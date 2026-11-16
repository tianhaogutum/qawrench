"""Exact byte-position contracts for source-bound JSONL checkpoints (synthetic)."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pyarrow.parquet as pq

from qawrench.crawlee_export import ExportError, export_dataset, verify_export
from qawrench.crawlee_export.core import canonical
from qawrench.crawlee_export.resume import Resume


class OffsetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-offset-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'items.jsonl'
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps({'version': 1, 'fields': [
            {'name': 'n', 'type': 'int64'}, {'name': 'text', 'type': 'string'}]}))
        self.rows = [{'n': 9007199254740993, 'text': '東京\n😀'},
                     {'n': -9223372036854775808, 'text': ''},
                     {'n': 9223372036854775807, 'text': 'café'},
                     {'n': 4, 'text': 'last'}]
        self.lines = [json.dumps(row, ensure_ascii=False).encode() + ending
                      for row, ending in zip(self.rows, (b'\r\n', b'\n', b'\r\n', b''))]
        self.source.write_bytes(b''.join(self.lines))
        self.out = self.root / 'out.parquet'
        self.work = self.root / 'checkpoint'

    def export(self, **kwargs):
        return export_dataset(self.source, schema=self.schema, out=self.out,
                              resume_dir=self.work, input_format='jsonl', **kwargs)

    def interrupt(self, **kwargs):
        original = Resume._write_part
        def save_then_interrupt(state, batch):
            original(state, batch)
            raise KeyboardInterrupt
        with patch.object(Resume, '_write_part', save_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.export(**kwargs)
        self.assertFalse(self.out.exists())
        return json.loads((self.work / 'resume.json').read_text())

    def test_offsets_exclude_read_ahead_and_seek_without_completed_line_reads(self):
        expected_rows = [dict(row, _cpe_source='items.jsonl', _cpe_item_id=None, _cpe_line=i)
                         for i, row in enumerate(self.rows, 1)]
        clean = self.root / 'clean.parquet'
        expected = export_dataset(self.source, schema=self.schema, out=clean,
                                  input_format='jsonl', batch_size=2)
        state = self.interrupt(batch_size=2)
        offset = len(b''.join(self.lines[:2]))
        self.assertEqual(state['parts'][0].get('next_jsonl_offset'), offset)
        self.assertEqual(state['binding']['source']['files'][0]['sha256'],
                         hashlib.sha256(self.source.read_bytes()).hexdigest())
        saved = (self.work / state['parts'][0]['name']).read_bytes()
        # Full hash reads are allowed; the row reader must seek before readline.
        original_open = Path.open
        reads, seeks = [], []
        class Tracked:
            def __init__(tracker, stream):
                tracker.stream = stream
            def __enter__(tracker):
                return tracker
            def __exit__(tracker, *args):
                tracker.stream.close()
            def __getattr__(tracker, name):
                return getattr(tracker.stream, name)
            def seek(tracker, position, *args):
                seeks.append(position)
                return tracker.stream.seek(position, *args)
            def readline(tracker, *args):
                reads.append(tracker.stream.tell())
                self.assertGreaterEqual(reads[-1], offset, 'completed lines must not be reread')
                return tracker.stream.readline(*args)
        def tracked_open(path, mode='r', *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return Tracked(stream) if path == self.source and mode == 'rb' else stream
        with patch.object(Path, 'open', tracked_open):
            actual = self.export(batch_size=2)
        self.assertIn(offset, seeks)
        self.assertTrue(reads)
        self.assertEqual(actual, expected)
        self.assertEqual(pq.read_table(self.out).to_pylist(), expected_rows)
        self.assertEqual((self.work / state['parts'][0]['name']).read_bytes(), saved)
        state = json.loads((self.work / 'resume.json').read_text())
        self.assertEqual([p['next_jsonl_offset'] for p in state['parts']],
                         [offset, self.source.stat().st_size])
        self.assertEqual(verify_export(self.out), expected)
        self.assertEqual(self.export(batch_size=2), expected)

    def test_byte_limited_repeated_interruptions_and_final_unterminated_row(self):
        expected_rows = [dict(row, _cpe_source='items.jsonl', _cpe_item_id=None, _cpe_line=i)
                         for i, row in enumerate(self.rows, 1)]
        limit = max(len(canonical(row) + b'\n') for row in expected_rows)
        for i in range(1, 5):
            state = self.interrupt(batch_size=100, batch_bytes=limit)
            self.assertEqual([p.get('next_jsonl_offset') for p in state['parts']],
                             [len(b''.join(self.lines[:j])) for j in range(1, i + 1)])
            self.assertEqual(state['source_row_count'], None)
        result = self.export(batch_size=100, batch_bytes=limit)
        self.assertEqual(result['row_groups'], 4)
        self.assertEqual(pq.read_table(self.out).to_pylist(), expected_rows)
        self.assertEqual(verify_export(self.out), result)

    def test_invalid_offsets_and_legacy_version_fail_before_checkpoint_reuse(self):
        self.interrupt(batch_size=2)
        path = self.work / 'resume.json'
        original = path.read_bytes()
        # A valid line boundary after the WRONG row must also be refused.
        for value in (None, True, '42', 1.5, -1, 0, self.source.stat().st_size + 1,
                      len(self.lines[0]), len(b''.join(self.lines[:3])),
                      len(b''.join(self.lines[:2])) - 1):
            state = json.loads(original)
            state['parts'][0]['next_jsonl_offset'] = value
            path.write_text(json.dumps(state))
            with self.subTest(value=value), patch.object(Resume, '_write_part') as write:
                with self.assertRaisesRegex(ExportError, 'checkpoint.*offset'):
                    self.export(batch_size=2)
                write.assert_not_called()
                self.assertFalse(self.out.exists())
                self.assertFalse(Path(str(self.out) + '.manifest.json').exists())
        for legacy in (False, True):
            state = json.loads(original)
            state['parts'][0].pop('next_jsonl_offset', None)
            if legacy:
                state['resume_version'] = 1
            path.write_text(json.dumps(state))
            with self.subTest(legacy=legacy), self.assertRaisesRegex(ExportError, 'checkpoint|resume'):
                self.export(batch_size=2)
        path.write_bytes(original)
        self.assertEqual(self.export(batch_size=2)['row_count'], 4)

    def test_completed_manifest_requires_eof_and_monotone_offsets(self):
        self.interrupt(batch_size=1)
        self.interrupt(batch_size=1)
        path = self.work / 'resume.json'
        original = path.read_bytes()
        for change in ('nonmonotone', 'false-eof'):
            state = json.loads(original)
            if change == 'nonmonotone':
                state['parts'][1]['next_jsonl_offset'] = state['parts'][0]['next_jsonl_offset']
            else:
                state['source_row_count'] = 2
            path.write_text(json.dumps(state))
            with self.subTest(change=change), self.assertRaisesRegex(ExportError, 'checkpoint.*offset'):
                self.export(batch_size=1)
        path.write_bytes(original)
        self.assertEqual(self.export(batch_size=1)['row_count'], 4)

    def test_large_line_crosses_hash_chunks_and_mid_utf8_offset_is_rejected(self):
        self.rows[0]['text'] = 'x' * (1024 * 1024) + '東京'
        self.lines[0] = json.dumps(self.rows[0], ensure_ascii=False).encode() + b'\r\n'
        self.source.write_bytes(b''.join(self.lines))
        state = self.interrupt(batch_size=1)
        offset = len(self.lines[0])
        self.assertEqual(state['parts'][0]['next_jsonl_offset'], offset)
        path = self.work / 'resume.json'
        original = path.read_bytes()
        state['parts'][0]['next_jsonl_offset'] = self.lines[0].index('東京'.encode()) + 1
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ExportError, 'checkpoint.*offset'):
            self.export(batch_size=1)
        path.write_bytes(original)
        result = self.export(batch_size=1)
        self.assertEqual(pq.read_table(self.out).to_pylist()[0]['text'], self.rows[0]['text'])
        self.assertEqual(verify_export(self.out), result)

    def test_completed_prefix_is_still_hash_bound_and_late_bad_line_is_not_skipped(self):
        self.interrupt(batch_size=2)
        original = self.source.read_bytes()
        self.source.write_bytes(original.replace(b'9007199254740993', b'9007199254740992'))
        with self.assertRaisesRegex(ExportError, 'binding mismatch'):
            self.export(batch_size=2)
        self.source.write_bytes(original)
        self.assertEqual(self.export(batch_size=2)['row_count'], 4)
        self.out = self.root / 'bad.parquet'
        self.work = self.root / 'bad-checkpoint'
        self.source.write_bytes(b''.join(self.lines[:3]) + b'{"n":true,"text":"bad"}')
        for attempt in range(2):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(ExportError, 'items.jsonl:4') as caught:
                self.export(batch_size=2)
            self.assertEqual(caught.exception.rows_validated, 3)
            self.assertFalse(self.out.exists())
            self.assertFalse(Path(str(self.out) + '.manifest.json').exists())

    def test_empty_jsonl_and_dataset_offsets_have_explicit_contracts(self):
        self.source.write_bytes(b'')
        result = self.export(batch_size=2)
        self.assertEqual(result['row_count'], 0)
        self.assertEqual(self.export(batch_size=2), result)
        state = json.loads((self.work / 'resume.json').read_text())
        self.assertEqual(state['resume_version'], 2)
        self.assertEqual(state['parts'], [])
        dataset = self.root / 'dataset'
        dataset.mkdir()
        (dataset / '1.json').write_text(json.dumps(self.rows[0]))
        out, work = self.root / 'dataset.parquet', self.root / 'dataset-checkpoint'
        export_dataset(dataset, schema=self.schema, out=out, resume_dir=work)
        path = work / 'resume.json'
        state = json.loads(path.read_text())
        self.assertIsNone(state['parts'][0]['next_jsonl_offset'])
        state['parts'][0]['next_jsonl_offset'] = 1
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ExportError, 'checkpoint.*offset'):
            export_dataset(dataset, schema=self.schema, out=out, resume_dir=work)


if __name__ == '__main__':
    unittest.main()
