"""Read-only checkpoint inspection contracts; synthetic stopped local sources."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import qawrench.crawlee_export as cpe
from qawrench.crawlee_export.cli import main
from qawrench.crawlee_export.resume import Resume


class InspectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-inspect-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'items.jsonl'
        self.lines = [b'{"n":1,"text":"caf\xc3\xa9"}\r\n',
                      b'{"n":9007199254740993,"text":"\xe6\x9d\xb1\xe4\xba\xac"}\n',
                      b'{"n":3,"text":"last"}']
        self.source.write_bytes(b''.join(self.lines))
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps({'version': 2, 'evolution': 'nullable-union', 'fields': [
            {'name': 'n', 'type': 'int64'},
            {'name': 'text', 'type': 'string', 'nullable': True}]}))
        self.out = self.root / 'out.parquet'
        self.work = self.root / 'checkpoint'
        self.manifest = self.work / 'resume.json'

    def export(self, mode='jsonl'):
        return cpe.export_dataset(self.source, schema=self.schema, out=self.out,
                                  resume_dir=self.work, batch_size=2, input_format=mode)

    def interrupt(self, mode='jsonl', before_parts=False):
        method = '_save_manifest' if before_parts else '_write_part'
        original = getattr(Resume, method)
        def save_then_interrupt(state, *args):
            original(state, *args)
            raise KeyboardInterrupt
        with patch.object(Resume, method, save_then_interrupt), self.assertRaises(KeyboardInterrupt):
            self.export(mode)
        return json.loads(self.manifest.read_text())

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mtime_ns,
                os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
                for p in [self.root, *self.root.rglob('*')]}

    def cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['--inspect-resume', str(self.work), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def reject(self, diagnostic):
        before = self.snapshot()
        code, out, err = self.cli()
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertRegex(json.loads(err)['error'], diagnostic)
        self.assertEqual(self.snapshot(), before)

    def test_cli_interrupted_exact_progress_and_no_writes(self):
        self.interrupt()
        (self.work / 'part-00000001.parquet').write_bytes(b'unreferenced kill artifact')
        (self.work / '.cpe-left.tmp').write_bytes(b'keep temporary')
        self.out.write_bytes(b'foreign destination; never inspect or publish')
        sidecar = self.out.with_name(self.out.name + '.manifest.json')
        sidecar.write_bytes(b'foreign sidecar')
        self.schema.unlink()  # Uses normalized stored schema, not its old file path.
        before = self.snapshot()
        with ExitStack() as stack:
            for target in ('pathlib.Path.mkdir', 'pathlib.Path.unlink', 'os.replace',
                           'os.link', 'os.fsync', 'tempfile.mkstemp',
                           'qawrench.crawlee_export.resume.Resume.open',
                           'qawrench.crawlee_export.resume.Resume.batches',
                           'qawrench.crawlee_export.resume.Resume._save_manifest',
                           'qawrench.crawlee_export.cli.export_dataset'):
                stack.enter_context(patch(target, side_effect=AssertionError('inspection must not write')))
            code, out, err = self.cli()
        self.assertEqual(code, 0, err)
        self.assertEqual(err, '')
        self.assertEqual(json.loads(out), dict(resume_version=2,
            source={'path': str(self.source), 'input_format': 'jsonl'}, part_count=1,
            reusable_rows=2, next_jsonl_offset=len(b''.join(self.lines[:2])), source_complete=False))
        self.assertEqual(self.snapshot(), before)

    def test_completed_empty_and_zero_part_interrupted_progress(self):
        self.interrupt(before_parts=True)
        self.assertEqual(cpe.inspect_resume(self.work)['reusable_rows'], 0)
        self.assertEqual(cpe.inspect_resume(self.work)['next_jsonl_offset'], 0)
        self.assertFalse(cpe.inspect_resume(self.work)['source_complete'])
        self.export()
        before = self.snapshot()
        report = cpe.inspect_resume(self.work)
        self.assertEqual((report['reusable_rows'], report['part_count'], report['next_jsonl_offset'],
                          report['source_complete']), (3, 2, self.source.stat().st_size, True))
        self.assertEqual(self.snapshot(), before)
        self.work = self.root / 'empty-checkpoint'
        self.source = self.root / 'empty.jsonl'
        self.out = self.root / 'empty.parquet'
        self.source.write_bytes(b'')
        self.export()
        before = self.snapshot()
        report = cpe.inspect_resume(self.work)
        self.assertEqual((report['reusable_rows'], report['part_count'], report['next_jsonl_offset'],
                          report['source_complete']), (0, 0, 0, True))
        self.assertEqual(self.snapshot(), before)

    def test_dataset_progress_and_completion_must_match_item_count(self):
        self.source = self.root / 'dataset'
        self.source.mkdir()
        for n, line in enumerate(self.lines):
            (self.source / f'{n}.json').write_bytes(line)
        self.interrupt('dataset')
        report = cpe.inspect_resume(self.work)
        self.assertEqual((report['reusable_rows'], report['next_jsonl_offset'], report['source_complete']),
                         (2, None, False))
        original = self.manifest.read_bytes()
        state = json.loads(original)
        state['source_row_count'] = 2
        self.manifest.write_text(json.dumps(state))
        self.reject('completed.*count')
        state['source_row_count'] = None
        state['parts'][0]['next_jsonl_offset'] = 1
        self.manifest.write_text(json.dumps(state))
        self.reject('dataset.*null')
        self.manifest.write_bytes(original)
        self.export('dataset')
        self.assertTrue(cpe.inspect_resume(self.work)['source_complete'])
        self.assertIsNone(cpe.inspect_resume(self.work)['next_jsonl_offset'])

    def test_changed_source_and_dataset_metadata_are_hash_bound(self):
        self.interrupt()
        raw = self.source.read_bytes()
        self.source.write_bytes(raw.replace(b'last', b'LAST'))
        self.reject('binding mismatch')
        self.source.write_bytes(raw)
        self.source.unlink()
        self.source.symlink_to(self.schema)
        self.reject('regular file')
        self.source = self.root / 'dataset'
        self.source.mkdir()
        (self.source / '1.json').write_bytes(self.lines[0])
        metadata = self.source / '__metadata__.json'
        metadata.write_bytes(b'{}')
        self.work = self.root / 'dataset-checkpoint'
        self.manifest = self.work / 'resume.json'
        self.export('dataset')
        metadata.write_bytes(b'{ }')
        self.reject('binding mismatch')

    def test_corrupt_missing_symlink_and_valid_hash_but_invalid_readback_parts(self):
        state = self.interrupt()
        path = self.work / state['parts'][0]['name']
        original = path.read_bytes()
        path.write_bytes(b'broken')
        self.reject('part.*size/digest')
        path.unlink()
        self.reject('part-00000000.parquet')
        target = self.root / 'saved.parquet'
        target.write_bytes(original)
        path.symlink_to(target)
        self.reject('regular file')
        path.unlink()
        path.write_bytes(original)
        raw = self.manifest.read_bytes()
        for field, value in (('rows_sha256', '0' * 64), ('row_count', 1)):
            state = json.loads(raw)
            state['parts'][0][field] = value
            # Preserve valid JSONL boundary metadata to reach read-back count validation.
            if field == 'row_count':
                state['parts'][0]['next_jsonl_offset'] = len(self.lines[0])
            self.manifest.write_text(json.dumps(state))
            self.reject('read-back.*(count|digest)')
        # Even a matching recorded file hash must not bypass Parquet decoding.
        path.write_bytes(b'not parquet')
        state = json.loads(raw)
        state['parts'][0].update(parquet_bytes=path.stat().st_size,
            parquet_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.manifest.write_text(json.dumps(state))
        self.reject('Parquet|parquet')

    def test_all_parts_schema_values_and_group_count_are_verified(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        self.export()
        raw = self.manifest.read_bytes()
        state = json.loads(raw)
        last = self.work / state['parts'][-1]['name']
        saved = last.read_bytes()
        last.write_bytes(b'broken second part')
        self.reject('part.*size/digest')
        last.write_bytes(saved)
        path = self.work / state['parts'][0]['name']
        table = pq.read_table(path)
        for changed in ('schema', 'groups'):
            if changed == 'schema':
                pq.write_table(pa.table({'wrong': [1, 2]}), path)
            else:
                pq.write_table(table, path, row_group_size=1)
            state = json.loads(raw)
            state['parts'][0].update(parquet_bytes=path.stat().st_size,
                parquet_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            self.manifest.write_text(json.dumps(state))
            self.reject('schema mismatch' if changed == 'schema' else 'one row group')

    def test_bad_offsets_and_false_completion_have_no_success_report(self):
        self.interrupt()
        raw = self.manifest.read_bytes()
        for value in (None, True, '42', 1.5, -1, 0, 99999, len(self.lines[0]),
                      len(b''.join(self.lines[:2])) - 1, self.lines[0].index(b'\xc3') + 1):
            with self.subTest(offset=value):
                state = json.loads(raw)
                state['parts'][0]['next_jsonl_offset'] = value
                self.manifest.write_text(json.dumps(state))
                self.reject('checkpoint.*offset')
        state = json.loads(raw)
        state['source_row_count'] = 2
        self.manifest.write_text(json.dumps(state))
        self.reject('completed source.*EOF')
        self.manifest.write_bytes(raw)
        self.export()
        state = json.loads(self.manifest.read_bytes())
        state['parts'][1]['next_jsonl_offset'] = state['parts'][0]['next_jsonl_offset']
        self.manifest.write_text(json.dumps(state))
        self.reject('offset.*increasing')

    def test_invalid_manifest_versions_binding_settings_and_schema(self):
        self.interrupt()
        raw = self.manifest.read_bytes()
        for version in (1, 3, True, '2'):
            state = json.loads(raw)
            state['resume_version'] = version
            self.manifest.write_text(json.dumps(state))
            self.reject('version 2 required')
        changes = [lambda m: m.pop('binding'), lambda m: m.update(binding=None),
                   lambda m: m['binding'].update(unknown=1),
                   lambda m: m['binding'].update(source=[]),
                   lambda m: m['binding']['source'].update(path='relative.jsonl'),
                   lambda m: m['binding']['source'].update(files=[]),
                   lambda m: m['binding']['source'].update(input_format='csv'),
                   lambda m: m['binding']['source']['files'][0].update(bytes=True),
                   lambda m: m['binding']['source']['files'][0].update(sha256='bad'),
                   lambda m: m['binding'].update(out='relative.parquet'),
                   lambda m: m['binding'].update(tool_version='99'),
                   lambda m: m['binding'].update(pyarrow_version='99'),
                   lambda m: m['binding']['schema'].update(version=99),
                   lambda m: m.update(source_row_count=True),
                   lambda m: m['parts'][0].update(name='../outside.parquet')]
        for setting in ('batch_size', 'batch_bytes', 'max_item_bytes'):
            for value in (0, -1, True, '2', None):
                changes.append(lambda m, k=setting, v=value: m['binding'].update({k: v}))
        for change in changes:
            state = json.loads(raw)
            change(state)
            self.manifest.write_text(json.dumps(state))
            self.reject('invalid|unsupported|version|checkpoint|binding mismatch')
        self.manifest.write_bytes(b'{"resume_version":2,"resume_version":2}')
        self.reject('duplicate JSON key')

    def test_missing_manifest_and_symlink_directory_never_create_anything(self):
        self.reject('No such file|directory')
        self.work.mkdir()
        self.reject('resume.json')
        self.interrupt()
        saved = self.manifest.read_bytes()
        self.manifest.unlink()
        alias = self.root / 'alias.json'
        alias.write_bytes(saved)
        self.manifest.symlink_to(alias)
        self.reject('regular file')
        self.work = self.root / 'alias-dir'
        self.work.symlink_to(self.manifest.parent, target_is_directory=True)
        self.reject('directory.*symlink')

    def test_lock_and_source_recheck_and_no_publication_requirement(self):
        self.interrupt()
        import fcntl
        fd = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.reject('locked')
        finally:
            os.close(fd)
        # Fully checkpointed does not mean published: interrupt on source recheck.
        with patch.object(Resume, 'check_source', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.export()
        self.assertFalse(self.out.exists())
        self.assertTrue(cpe.inspect_resume(self.work)['source_complete'])
        snapshot = Resume._snapshot
        calls = []
        def mutate_after_snapshot(state):
            result = snapshot(state)
            calls.append(1)
            if len(calls) == 1:
                self.source.write_bytes(self.source.read_bytes().replace(b'last', b'LAST'))
            return result
        with patch.object(Resume, '_snapshot', mutate_after_snapshot):
            with self.assertRaisesRegex(cpe.ExportError, 'source changed'):
                cpe.inspect_resume(self.work)

    def test_cli_mode_conflicts_fail_before_inspection(self):
        for extra in (['input'], ['--schema', 'schema.json'], ['--out', 'out.parquet'],
                      ['--resume-dir', 'checkpoint'], ['--input-format', 'jsonl'],
                      ['--batch-size', '2'], ['--batch-bytes', '100'],
                      ['--max-item-bytes', '100'], ['--verify', 'out.parquet'],
                      ['--check-schema', 'old.json', 'new.json']):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), \
                    patch('qawrench.crawlee_export.cli.inspect_resume') as inspect:
                with self.assertRaises(SystemExit) as caught:
                    self.cli(*extra)
                self.assertEqual(caught.exception.code, 2)
                inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
