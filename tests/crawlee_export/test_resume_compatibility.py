"""Verified checkpoint baseline contracts; no resume binding changes or writes."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
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


def field(name, kind='string', nullable=False):
    return dict(name=name, type=kind, nullable=nullable)


def schema(fields, policy=None):
    obj = dict(version=1 if policy is None else 2, fields=fields)
    if policy is not None:
        obj['evolution'] = policy
    return obj


class ResumeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-resume-compatibility-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'items.jsonl'
        self.source.write_bytes(b'{"id":9007199254740993,"note":null}\r\n'
                                b'{"id":2,"note":"snow"}\n{"id":3,"note":"last"}')
        self.old, self.new = self.root / 'old.json', self.root / 'new.json'
        self.fields = [field('id', 'int64'), field('note', nullable=True)]
        self.old.write_text(json.dumps(schema(self.fields)))
        self.target = schema([field('extra', 'bool', True), field('id', 'int64', True),
                              self.fields[1]], 'nullable-union')
        self.new.write_text(json.dumps(self.target))
        self.work, self.out = self.root / 'checkpoint', self.root / 'previous.parquet'
        self.manifest = self.work / 'resume.json'

    def export(self, mode='jsonl'):
        return cpe.export_dataset(self.source, schema=self.old, out=self.out,
                                  input_format=mode, resume_dir=self.work, batch_size=2)

    def interrupt(self, mode='jsonl', zero_parts=False):
        method = '_save_manifest' if zero_parts else '_write_part'
        original = getattr(Resume, method)
        def save_then_stop(state, *args):
            original(state, *args)
            raise KeyboardInterrupt
        with patch.object(Resume, method, save_then_stop), self.assertRaises(KeyboardInterrupt):
            self.export(mode)
        return json.loads(self.manifest.read_text())

    def cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['--check-resume-schema', str(self.work), str(self.new), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mtime_ns,
                ('link', str(p.readlink())) if p.is_symlink() else
                ('dir',) if p.is_dir() else ('file', p.read_bytes()))
                for p in [self.root, *self.root.rglob('*')]}

    def reject(self, pattern):
        before = self.snapshot()
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stdout), (2, ''))
        error = json.loads(stderr)
        self.assertRegex(error['error'], pattern)
        self.assertEqual(error['rows_committed'], 0)
        self.assertEqual(self.snapshot(), before)

    def assert_report(self, expected):
        before = self.snapshot()
        code, stdout, stderr = self.cli()
        self.assertEqual((code, json.loads(stdout), stderr),
                         (0 if expected['compatible'] else 2, expected, ''))
        self.assertEqual(self.snapshot(), before)

    def test_interrupted_checkpoint_preflight_matches_declared_schema(self):
        self.interrupt()
        expected = dict(compatible=True, added_nullable_fields=['extra'],
                        relaxed_nullable_fields=['id'], issues=[])
        self.assertEqual(cpe.check_schema_compatibility(self.old, self.new), expected)
        self.assert_report(expected)
        self.assertEqual(cpe.check_resume_schema_compatibility(self.work, self.new), expected)
        self.assertFalse(self.out.exists())

    def test_policies_reports_and_verified_export_parity(self):
        for n, policy in enumerate((None, 'strict', 'nullable-union')):
            self.old.write_text(json.dumps(schema(self.fields, policy)))
            self.work = self.root / f'checkpoint-{n}'
            self.out = self.root / f'export-{n}.parquet'
            self.export()
            for target in (self.target, schema(list(reversed(self.fields)), policy),
                           schema([field('id', 'float64'), field('required')], 'strict'),
                           schema(self.fields + [field('extra', nullable=True)])):
                with self.subTest(policy=policy, target=target):
                    self.new.write_text(json.dumps(target))
                    expected = cpe.check_schema_compatibility(self.old, self.new)
                    self.assert_report(expected)
                    self.assertEqual(cpe.check_export_schema_compatibility(self.out, self.new), expected)

    def test_read_only_without_original_schema_or_published_destination(self):
        self.interrupt()
        self.old.unlink()
        (self.work / 'unreferenced.parquet').write_bytes(b'keep orphan')
        self.out.write_bytes(b'foreign output is not opened')
        Path(str(self.out) + '.manifest.json').write_bytes(b'foreign sidecar')
        before = self.snapshot()
        with ExitStack() as stack:
            for target in ('pathlib.Path.mkdir', 'pathlib.Path.unlink', 'os.replace', 'os.link',
                           'os.fsync', 'tempfile.mkstemp',
                           'qawrench.crawlee_export.resume.Resume.open',
                           'qawrench.crawlee_export.resume.Resume.batches',
                           'qawrench.crawlee_export.resume.Resume._save_manifest',
                           'qawrench.crawlee_export.cli.export_dataset'):
                stack.enter_context(patch(target, side_effect=AssertionError('preflight must not write')))
            self.assert_report(dict(compatible=True, added_nullable_fields=['extra'],
                                    relaxed_nullable_fields=['id'], issues=[]))
        self.assertEqual(self.snapshot(), before)

    def test_zero_part_empty_and_dataset_checkpoints(self):
        expected = cpe.check_schema_compatibility(self.old, self.new)
        self.interrupt(zero_parts=True)
        self.assert_report(expected)
        self.source = self.root / 'empty.jsonl'
        self.source.write_bytes(b'')
        self.work, self.out = self.root / 'empty-job', self.root / 'empty.parquet'
        self.export()
        self.assert_report(expected)
        self.source = self.root / 'dataset'
        self.source.mkdir()
        for n in range(3):
            (self.source / f'{n}.json').write_text(json.dumps(dict(id=n, note=None)))
        self.work, self.out = self.root / 'dataset-job', self.root / 'dataset.parquet'
        self.interrupt('dataset')
        self.assert_report(expected)
        self.export('dataset')
        self.assert_report(expected)

    def test_checkpoint_manifest_source_and_part_missing_symlink_or_corrupt(self):
        self.reject('directory|No such file')
        state = self.interrupt()
        part = self.work / state['parts'][0]['name']
        for path in (self.manifest, self.source, part):
            original = path.read_bytes()
            path.write_bytes(b'broken')
            self.reject('JSON|binding mismatch|size/digest')
            path.unlink()
            self.reject('No such file')
            path.mkdir()
            self.reject('regular file')
            path.rmdir()
            alias = self.root / 'alias'
            alias.write_bytes(original)
            path.symlink_to(alias)
            self.reject('regular file')
            path.unlink()
            alias.unlink()
            path.write_bytes(original)
        self.work = self.root / 'alias-job'
        self.work.symlink_to(self.manifest.parent, target_is_directory=True)
        self.reject('directory.*symlink')

    def test_invalid_checkpoint_bindings_versions_offsets_and_readback(self):
        state = self.interrupt()
        changes = [lambda m: m.update(resume_version=1),
                   lambda m: m['binding'].update(tool_version='99'),
                   lambda m: m['binding'].update(pyarrow_version='99'),
                   lambda m: m['binding'].update(batch_size=True),
                   lambda m: m['binding']['schema'].update(version=99),
                   lambda m: m['parts'][0].update(next_jsonl_offset=1),
                   lambda m: m.update(source_row_count=2),
                   lambda m: m['parts'][0].update(rows_sha256='0' * 64)]
        for change in changes:
            obj = copy.deepcopy(state)
            change(obj)
            self.manifest.write_text(json.dumps(obj))
            self.reject('version|binding|offset|EOF|digest')
        # File size/hash consistency alone cannot authorize a baseline.
        part = self.work / state['parts'][0]['name']
        part.write_bytes(b'not parquet')
        state['parts'][0].update(parquet_bytes=part.stat().st_size,
            parquet_sha256=hashlib.sha256(part.read_bytes()).hexdigest())
        self.manifest.write_text(json.dumps(state))
        self.reject('Parquet|parquet')

    def test_target_schema_validation(self):
        self.interrupt()
        for raw in (b'{}', b'\xff', b'{"version":1,"version":1}',
                    b'{"version":1,"fields":[{"name":"x","type":"date"}]}',
                    b' ' * (1024 * 1024 + 1)):
            self.new.write_bytes(raw)
            self.reject('new schema')
        self.new.unlink()
        self.reject('new schema')
        self.new.mkdir()
        self.reject('new schema')
        self.new.rmdir()
        self.new.symlink_to(self.old)
        self.reject('new schema')

    def test_exclusive_lock_and_final_source_recheck_are_required(self):
        self.interrupt()
        import fcntl
        fd = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.reject('locked')
        finally:
            os.close(fd)
        with patch.object(Resume, 'check_source', side_effect=cpe.ExportError('source changed')):
            self.reject('source changed')

    def test_uses_exact_verified_schema_without_manifest_reread(self):
        self.interrupt()
        real_read = Path.read_bytes
        reads = []
        def read_once(path):
            if path == self.manifest:
                reads.append(path)
                self.assertEqual(len(reads), 1, 'must consume validated schema, not reread sidecar')
            return real_read(path)
        with patch.object(Path, 'read_bytes', read_once):
            result = cpe.check_resume_schema_compatibility(self.work, self.new)
        self.assertTrue(result['compatible'])
        self.assertEqual(len(reads), 1)

    def test_comparison_does_not_relax_existing_resume_binding(self):
        self.interrupt()
        self.assertTrue(cpe.check_resume_schema_compatibility(self.work, self.new)['compatible'])
        before = self.snapshot()
        with self.assertRaisesRegex(cpe.ExportError, 'binding mismatch'):
            cpe.export_dataset(self.source, schema=self.new, out=self.out,
                               input_format='jsonl', resume_dir=self.work, batch_size=2)
        self.assertEqual(self.snapshot(), before)

    def test_argument_conflicts_are_rejected_before_validation(self):
        for extra in (['source'], ['--schema', str(self.old)], ['--out', 'unused.parquet'],
                      ['--resume-dir', 'unused'], ['--verify', str(self.out)],
                      ['--inspect-resume', str(self.work)],
                      ['--check-schema', str(self.old), str(self.new)],
                      ['--check-export-schema', str(self.out), str(self.new)],
                      ['--input-format', 'jsonl'], ['--batch-size', '1000'],
                      ['--batch-bytes', '16777216'], ['--max-item-bytes', '8388608']):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as caught:
                self.cli(*extra)
            self.assertEqual(caught.exception.code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['--check-resume-schema', str(self.work)])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.work.exists())


if __name__ == '__main__':
    unittest.main()
