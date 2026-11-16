"""Validated checkpoint -> published candidate export schema, without extracted schemas."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import qawrench.crawlee_export as cpe
from qawrench.crawlee_export import compatibility, resume
from qawrench.crawlee_export.cli import main
from qawrench.crawlee_export.resume import Resume


def field(name, kind='string', nullable=False):
    return dict(name=name, type=kind, nullable=nullable)


def schema(fields, policy=None):
    obj = dict(version=1 if policy is None else 2, fields=fields)
    if policy is not None:
        obj['evolution'] = policy
    return obj


class ResumeExportCompatibilityTests(unittest.TestCase):
    flag = '--check-resume-export-schema'

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-resume-export-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fields = [field('id', 'int64'), field('note', nullable=True)]
        self.target = schema([field('extra', 'bool', True), field('id', 'int64', True),
                              self.fields[1]], 'nullable-union')
        self.old, self.new = self.root / 'old.json', self.root / 'new.json'
        self.old.write_text(json.dumps(schema(self.fields)))
        self.new.write_text(json.dumps(self.target))
        self.candidate_source = self.make_source('candidate-export', self.target)
        self.candidate = self.root / 'candidate-export.parquet'
        cpe.export_dataset(self.candidate_source, schema=self.new, out=self.candidate,
                           input_format='jsonl', batch_size=2)
        self.source = self.make_source('baseline', schema(self.fields))
        self.work, self.out = self.root / 'checkpoint', self.root / 'baseline.parquet'
        self.manifest = self.work / 'resume.json'
        self.sidecar = Path(str(self.candidate) + '.manifest.json')

    def make_source(self, name, obj, mode='jsonl', empty=False):
        values = {'string': '雪', 'bool': True, 'int64': 9007199254740993,
                  'float64': 1.25, 'json-string': {'nested': [1, None]}}
        row = {f['name']: values[f['type']] for f in obj['fields']}
        source = self.root / (name + ('.jsonl' if mode == 'jsonl' else '-dataset'))
        if mode == 'jsonl':
            raw = json.dumps(row, ensure_ascii=False).encode()
            source.write_bytes(b'' if empty else raw + b'\r\n' + raw + b'\n' + raw)
        else:
            source.mkdir()
            if not empty:
                for n in range(3):
                    (source / f'{n}.json').write_text(json.dumps(row))
        return source

    def checkpoint(self, mode='jsonl', stage='interrupted'):
        def export():
            cpe.export_dataset(self.source, schema=self.old, out=self.out,
                               input_format=mode, resume_dir=self.work, batch_size=2)
        if stage == 'completed':
            export()
        else:
            method = '_save_manifest' if stage == 'zero' else '_write_part'
            original = getattr(Resume, method)
            def save_then_stop(state, *args):
                original(state, *args)
                raise KeyboardInterrupt
            with patch.object(Resume, method, save_then_stop), self.assertRaises(KeyboardInterrupt):
                export()
        return json.loads(self.manifest.read_text())

    def cli(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([self.flag, str(self.work), str(self.candidate)])
        return code, stdout.getvalue(), stderr.getvalue()

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mtime_ns,
                ('link', str(p.readlink())) if p.is_symlink() else
                ('dir',) if p.is_dir() else ('file', p.read_bytes()))
                for p in [self.root, *self.root.rglob('*')]}

    def assert_report(self, expected):
        before = self.snapshot()
        code, stdout, stderr = self.cli()
        self.assertEqual((code, json.loads(stdout), stderr),
                         (0 if expected['compatible'] else 2, expected, ''))
        self.assertEqual(self.snapshot(), before)

    def reject(self, pattern='.'):
        before = self.snapshot()
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stdout), (2, ''))
        error = json.loads(stderr)
        self.assertRegex(error['error'], pattern)
        self.assertEqual(error['rows_committed'], 0)
        self.assertEqual(self.snapshot(), before)

    def test_cli_interrupted_checkpoint_to_verified_export(self):
        self.checkpoint()
        cpe.verify_export(self.candidate)
        self.assertEqual(cpe.inspect_resume(self.work)['reusable_rows'], 2)
        self.assertFalse(self.out.exists())
        self.assert_report(dict(compatible=True, added_nullable_fields=['extra'],
                                relaxed_nullable_fields=['id'], issues=[]))


    def test_reverse_direction_is_not_compatible(self):
        self.old.write_text(json.dumps(self.target))
        self.source = self.make_source('union-baseline', self.target)
        self.candidate = self.root / 'strict-candidate.parquet'
        self.new.write_text(json.dumps(schema(self.fields)))
        cpe.export_dataset(self.make_source('strict-export', schema(self.fields)),
                           schema=self.new, out=self.candidate, input_format='jsonl')
        self.checkpoint()
        self.assert_report(dict(compatible=False, added_nullable_fields=[],
            relaxed_nullable_fields=[], issues=[
                dict(code='field_removed', field='extra'),
                dict(code='nullability_tightened', field='id', old=True, new=False),
                dict(code='evolution_tightened', old='nullable-union', new='strict')]))
        self.assertTrue(cpe.check_export_resume_schema_compatibility(
            self.candidate, self.work)['compatible'])

    def test_exact_directional_issue_order(self):
        old = schema([field('id', 'int64'), field('note', nullable=True),
                      field('removed'), field('relax', 'bool')], 'nullable-union')
        new = schema([field('extra', nullable=True), field('required'),
                      field('note', 'bool'), field('id', 'float64'),
                      field('relax', 'bool', True)], 'strict')
        self.old.write_text(json.dumps(old))
        self.new.write_text(json.dumps(new))
        self.candidate = self.root / 'strict.parquet'
        cpe.export_dataset(self.make_source('strict', new), schema=self.new, out=self.candidate,
                           input_format='jsonl')
        self.source = self.make_source('union', old)
        self.checkpoint()
        self.assert_report(dict(compatible=False, added_nullable_fields=['extra'],
            relaxed_nullable_fields=['relax'], issues=[
                dict(code='type_changed', field='id', old='int64', new='float64'),
                dict(code='type_changed', field='note', old='string', new='bool'),
                dict(code='nullability_tightened', field='note', old=True, new=False),
                dict(code='field_removed', field='removed'),
                dict(code='nullable_addition_requires_union', field='extra'),
                dict(code='required_field_added', field='required'),
                dict(code='evolution_tightened', old='nullable-union', new='strict')]))

    def test_policy_and_declaration_matrix(self):
        for n, policy in enumerate((None, 'strict', 'nullable-union')):
            old = schema(self.fields, policy)
            self.old.write_text(json.dumps(old))
            targets = [schema(list(reversed(self.fields)), p)
                       for p in (None, 'strict', 'nullable-union')]
            targets += [self.target, schema([field('id', 'float64'), field('required')]),
                        schema(self.fields + [field('extra', nullable=True)])]
            for m, target in enumerate(targets):
                with self.subTest(policy=policy, target=target):
                    self.new.write_text(json.dumps(target))
                    self.candidate = self.root / f'candidate-{n}-{m}.parquet'
                    cpe.export_dataset(self.make_source(f'candidate-{n}-{m}', target),
                                       schema=self.new, out=self.candidate, input_format='jsonl')
                    self.source = self.make_source(f'baseline-{n}-{m}', old)
                    self.work = self.root / f'checkpoint-{n}-{m}'
                    self.manifest = self.work / 'resume.json'
                    self.out = self.root / f'baseline-{n}-{m}.parquet'
                    self.checkpoint()
                    expected = cpe.check_schema_compatibility(self.old, self.new)
                    self.assert_report(expected)
                    self.assertEqual(cpe.check_resume_export_schema_compatibility(
                        self.work, self.candidate), expected)

    def test_dataset_jsonl_interrupted_zero_empty_and_completed(self):
        expected = cpe.check_schema_compatibility(self.old, self.new)
        for mode in ('dataset', 'jsonl'):
            for stage in ('interrupted', 'zero', 'empty', 'completed'):
                with self.subTest(mode=mode, stage=stage):
                    name = f'{mode}-{stage}'
                    self.source = self.make_source(name, schema(self.fields), mode, empty=stage == 'empty')
                    self.work = self.root / (name + '-checkpoint')
                    self.manifest = self.work / 'resume.json'
                    self.out = self.root / (name + '.parquet')
                    self.checkpoint(mode, 'completed' if stage == 'empty' else stage)
                    # Inspection cannot rely on the checkpoint destination pair.
                    for path in (self.out, Path(str(self.out) + '.manifest.json')):
                        if path.exists():
                            path.unlink()
                    old_raw, new_raw = self.old.read_bytes(), self.new.read_bytes()
                    self.old.unlink()
                    self.new.unlink()
                    self.assert_report(expected)
                    self.old.write_bytes(old_raw)
                    self.new.write_bytes(new_raw)

    def test_no_original_schemas_destination_access_or_writes(self):
        self.checkpoint()
        self.old.unlink()
        self.new.unlink()
        self.candidate_source.unlink()
        (self.work / 'unreferenced.parquet').write_bytes(b'orphan is preserved')
        self.out.write_bytes(b'foreign output')
        Path(str(self.out) + '.manifest.json').write_bytes(b'foreign sidecar')
        before = self.snapshot()
        real_open, real_os_open = Path.open, os.open
        def read_only(path, mode='r', *args, **kwargs):
            self.assertFalse(any(c in mode for c in 'wax+'), f'write open: {path}')
            self.assertNotIn(path, (self.out, Path(str(self.out) + '.manifest.json')))
            return real_open(path, mode, *args, **kwargs)
        def os_read_only(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            return real_os_open(path, flags, *args, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, 'open', read_only))
            stack.enter_context(patch('os.open', os_read_only))
            for target in ('builtins.open', 'pathlib.Path.mkdir', 'pathlib.Path.unlink',
                           'os.replace', 'os.link', 'os.unlink', 'os.fsync',
                           'tempfile.mkstemp', 'tempfile.mkdtemp',
                           'qawrench.crawlee_export.core._paths',
                           'qawrench.crawlee_export.core._temp',
                           'qawrench.crawlee_export.resume.Resume.open',
                           'qawrench.crawlee_export.resume.Resume.batches',
                           'qawrench.crawlee_export.resume.Resume._save_manifest',
                           'qawrench.crawlee_export.cli.export_dataset'):
                stack.enter_context(patch(target, side_effect=AssertionError('must not write')))
            code, stdout, stderr = self.cli()
            self.assertEqual((code, json.loads(stdout), stderr),
                (0, dict(compatible=True, added_nullable_fields=['extra'],
                         relaxed_nullable_fields=['id'], issues=[]), ''))
        self.assertEqual(self.snapshot(), before)

    def test_artifacts_missing_corrupt_nonregular_or_symlinked(self):
        self.reject('directory|No such file')
        state = self.checkpoint()
        part = self.work / state['parts'][0]['name']
        for path in (self.candidate, self.sidecar, self.manifest, self.source, part):
            with self.subTest(path=path.name):
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
        self.work = self.root / 'symlink-checkpoint'
        self.work.symlink_to(self.manifest.parent, target_is_directory=True)
        self.reject('directory.*symlink')

    def test_unsupported_sidecars_and_full_export_readback(self):
        self.checkpoint()
        raw = self.sidecar.read_bytes()
        original = json.loads(raw)
        for key, value in (('format_version', 99), ('schema', {}), ('row_count', 9),
                           ('row_groups', 9), ('rows_sha256', '0' * 64),
                           ('parquet_bytes', 1), ('parquet_sha256', '0' * 64),
                           ('provenance_columns', [])):
            obj = copy.deepcopy(original)
            obj[key] = value
            self.sidecar.write_text(json.dumps(obj))
            self.reject()
        for bad in (b'\xff', b'{"format_version":1,"format_version":1}',
                    b' ' * (1024 * 1024 + 1)):
            self.sidecar.write_bytes(bad)
            self.reject()
        self.sidecar.write_bytes(raw)
        broken = b'X' + self.candidate.read_bytes()[1:-1] + b'X'
        self.candidate.write_bytes(broken)
        original['parquet_sha256'] = hashlib.sha256(broken).hexdigest()
        self.sidecar.write_text(json.dumps(original))
        self.reject('Parquet|parquet')

    def test_checkpoint_bindings_offsets_digest_and_readback(self):
        state = self.checkpoint()
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
            self.reject()
        part = self.work / state['parts'][0]['name']
        part.write_bytes(b'not parquet')
        state['parts'][0].update(parquet_bytes=part.stat().st_size,
            parquet_sha256=hashlib.sha256(part.read_bytes()).hexdigest())
        self.manifest.write_text(json.dumps(state))
        self.reject('Parquet|parquet')

    def test_exclusive_lock_refused_shared_lock_allowed_and_source_rechecked(self):
        self.checkpoint()
        import fcntl
        fd = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.reject('locked')
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assert_report(dict(compatible=True, added_nullable_fields=['extra'],
                                    relaxed_nullable_fields=['id'], issues=[]))
        finally:
            os.close(fd)
        with patch.object(Resume, 'check_source', side_effect=cpe.ExportError('source changed')):
            self.reject('source changed')

    def test_exact_validated_schemas_no_reread_and_no_early_comparison(self):
        self.checkpoint()
        real_open = Path.open
        reads = {self.sidecar: 0, self.manifest: 0}
        real_inspect, real_compare = resume._inspect_resume, compatibility._compare
        seen = []
        def read_once(path, *args, **kwargs):
            if path in reads:
                reads[path] += 1
                self.assertEqual(reads[path], 1, 'must not reread a validated manifest')
            return real_open(path, *args, **kwargs)
        def inspected(path):
            result = real_inspect(path)
            seen.append(result[1])
            return result
        def compared(old, new):
            self.assertIs(old, seen[0], 'must consume the exact parsed schema')
            return real_compare(old, new)
        with patch.object(Path, 'open', read_once), \
                patch.object(resume, '_inspect_resume', inspected), \
                patch.object(compatibility, '_compare', compared):
            result = cpe.check_resume_export_schema_compatibility(self.work, self.candidate)
        self.assertTrue(result['compatible'])
        self.assertEqual(list(reads.values()), [1, 1])
        for target in ('qawrench.crawlee_export.compatibility.verify_export',
                       'qawrench.crawlee_export.resume._inspect_resume'):
            with patch(target, side_effect=cpe.ExportError('validation refused')), \
                    patch.object(compatibility, '_compare', side_effect=AssertionError('early report')):
                self.reject('validation refused')

    def test_compatible_report_does_not_change_resume_binding(self):
        self.checkpoint()
        self.assertTrue(cpe.check_resume_export_schema_compatibility(
            self.work, self.candidate)['compatible'])
        before = self.snapshot()
        with self.assertRaisesRegex(cpe.ExportError, 'binding mismatch'):
            cpe.export_dataset(self.source, schema=self.new, out=self.out,
                               input_format='jsonl', resume_dir=self.work, batch_size=2)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
