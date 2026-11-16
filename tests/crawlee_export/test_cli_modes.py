"""Read-only CLI mode contracts are checked before any artifact I/O."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.crawlee_export import ExportError, export_dataset, verify_export
from qawrench.crawlee_export.cli import main


class ComparisonModeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cpe-cli-modes-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pairs = {flag: [] for flag in (
            '--check-schema', '--check-export-schema',
            '--check-exports-schema', '--check-resume-schema',
            '--check-export-resume-schema', '--check-resume-export-schema')}
        for n in range(2):
            source = self.root / f'source-{n}.jsonl'
            source.write_text(json.dumps({'id': n}) + '\n')
            spec = self.root / f'schema-{n}.json'
            spec.write_text(json.dumps(dict(version=1, fields=[dict(name='id', type='int64')])))
            out = self.root / f'export-{n}.parquet'
            checkpoint = self.root / f'checkpoint-{n}'
            export_dataset(source, schema=spec, out=out, input_format='jsonl',
                           resume_dir=checkpoint, batch_size=1)
            for flag, pair in zip(self.pairs, ((spec, spec), (out, spec),
                                              (out, out), (checkpoint, spec), (out, checkpoint),
                                              (checkpoint, out))):
                self.pairs[flag].append(list(map(str, pair)))

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.stat().st_mtime_ns,
                ('dir',) if p.is_dir() else ('file', p.read_bytes()))
                for p in [self.root, *self.root.rglob('*')]}

    def assert_argument_error(self, args, diagnostic):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                main(args)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('usage:', stderr.getvalue())
        self.assertIn(diagnostic, stderr.getvalue())

    def no_artifact_io(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target in (
                'qawrench.crawlee_export.cli.check_schema_compatibility',
                'qawrench.crawlee_export.cli.check_export_schema_compatibility',
                'qawrench.crawlee_export.cli.check_exports_schema_compatibility',
                'qawrench.crawlee_export.cli.check_resume_schema_compatibility',
                'qawrench.crawlee_export.cli.check_export_resume_schema_compatibility',
                'qawrench.crawlee_export.cli.check_resume_export_schema_compatibility',
                'qawrench.crawlee_export.cli.inspect_resume',
                'qawrench.crawlee_export.cli.verify_export',
                'qawrench.crawlee_export.cli.export_dataset',
                'qawrench.crawlee_export.compatibility._load_schema',
                'qawrench.crawlee_export.compatibility.verify_export',
                'qawrench.crawlee_export.resume._inspect_resume',
                'qawrench.crawlee_export.core._read',
                'qawrench.crawlee_export.core.Input.inspect',
                'qawrench.crawlee_export.resume.Resume.open',
                'builtins.open', 'pathlib.Path.open', 'pathlib.Path.mkdir', 'pathlib.Path.unlink',
                'os.open', 'os.replace', 'os.link', 'tempfile.mkstemp', 'fcntl.flock'):
            stack.enter_context(patch(target, side_effect=AssertionError('artifact I/O before rejection')))
        return stack

    def test_duplicate_valid_modes_are_argument_errors(self):
        # This regression uses real valid artifacts: formerly each invocation
        # succeeded with the last pair instead of raising an argument error.
        before = self.snapshot()
        for flag, (pair, _) in self.pairs.items():
            with self.subTest(flag=flag):
                self.assert_argument_error([flag, *pair, flag, *pair],
                                           f'argument {flag}: may only be specified once')
        self.assertEqual(self.snapshot(), before)

    def test_duplicates_rejected_before_io_for_pairs_order_and_option_positions(self):
        before = self.snapshot()
        with self.no_artifact_io():
            for flag, (first, second) in self.pairs.items():
                for a, b in ((first, first), (first, second), (second, first)):
                    for extra in ([], ['--batch-size', '2'], ['--out', str(self.root / 'unused')],
                                  ['--resume-dir', str(self.root / 'unused-job')], ['source']):
                        for position in ('prefix', 'middle', 'suffix'):
                            args = {'prefix': [*extra, flag, *a, flag, *b],
                                    'middle': [flag, *a, *extra, flag, *b],
                                    'suffix': [flag, *a, flag, *b, *extra]}[position]
                            with self.subTest(flag=flag, a=a, b=b, extra=extra, position=position):
                                self.assert_argument_error(args, f'argument {flag}: may only be specified once')
                for args in ([flag, *first, flag, *second, flag, *first],
                             [flag, *first, flag[:-1], *second],
                             [flag, 'missing-a', 'missing-b', flag, 'missing-c', 'missing-d']):
                    with self.subTest(args=args):
                        self.assert_argument_error(args, f'argument {flag}: may only be specified once')
        self.assertEqual(self.snapshot(), before)

    def single_path_modes(self):
        return {'--verify': [str(self.root / f'export-{n}.parquet') for n in range(2)],
                '--inspect-resume': [str(self.root / f'checkpoint-{n}') for n in range(2)]}

    def test_empty_comparison_paths_rejected_before_dispatch(self):
        # Keep the nonempty counterpart valid. Previously these reached schema
        # loading, export verification or checkpoint inspection (and locking).
        before = self.snapshot()
        with self.no_artifact_io():
            for flag, (pair, _) in self.pairs.items():
                for operands in (['', pair[1]], [pair[0], ''], ['', '']):
                    with self.subTest(flag=flag, operands=operands):
                        self.assert_argument_error([flag, *operands],
                                                   f'argument {flag}: paths must not be empty')
        self.assertEqual(self.snapshot(), before)

    def test_empty_comparison_paths_precede_option_exclusions(self):
        export = [str(self.root / 'source-0.jsonl'), '--input-format', 'jsonl',
                  '--schema', str(self.root / 'schema-0.json'),
                  '--out', str(self.root / 'unused.parquet')]
        options = [export, ['--schema='], ['--out', ''], [''],
                   ['--resume-dir', str(self.root / 'unused-job')],
                   ['--batch-size', '2'], ['--batch-size=0'], ['--batch-size', '-1'],
                   ['--input-format=dataset'], ['--batch-bytes=16777216'],
                   ['--max-item-bytes=8388608']]
        before = self.snapshot()
        with self.no_artifact_io():
            for flag, (pair, _) in self.pairs.items():
                for operands in (['', pair[1]], [pair[0], ''], ['', '']):
                    for spelling in (flag, flag[:-1]):
                        for extra in options:
                            mode = [spelling, *operands]
                            for args in (mode + extra, extra + mode):
                                with self.subTest(args=args):
                                    self.assert_argument_error(args, f'argument {flag}: paths must not be empty')
        self.assertEqual(self.snapshot(), before)

    def test_empty_comparison_paths_preserve_parser_precedence(self):
        modes = {flag: pairs[0] for flag, pairs in self.pairs.items()}
        modes.update({flag: [paths[0]] for flag, paths in self.single_path_modes().items()})
        before = self.snapshot()
        with self.no_artifact_io():
            for flag, (pair, _) in self.pairs.items():
                for operands in (['', pair[1]], [pair[0], ''], ['', '']):
                    mode = [flag, *operands]
                    for a, b in ((mode, [flag, *pair]), ([flag, *pair], mode), (mode, mode)):
                        with self.subTest(a=a, b=b):
                            self.assert_argument_error(a + b, f'argument {flag}: may only be specified once')
                    for other, paths in modes.items():
                        if other != flag:
                            for a, b in ((mode, [other, *paths]), ([other, *paths], mode)):
                                with self.subTest(a=a, b=b):
                                    self.assert_argument_error(a + b, f'not allowed with argument {a[0]}')
                    for malformed, diagnostic in (
                            (['--batch-size', 'bad'], 'argument --batch-size: invalid int value'),
                            (['--batch-size'], 'argument --batch-size: expected one argument'),
                            (['--input-format=bad'], 'argument --input-format: invalid choice'),
                            (['--unknown'], 'unrecognized arguments: --unknown')):
                        for args in (mode + malformed, malformed + mode):
                            with self.subTest(args=args):
                                self.assert_argument_error(args, diagnostic)
                    for args in (mode + ['--help'], ['--help'] + mode):
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with self.subTest(args=args), redirect_stdout(stdout), redirect_stderr(stderr):
                            with self.assertRaises(SystemExit) as caught:
                                main(args)
                        self.assertEqual(caught.exception.code, 0)
                        self.assertIn('usage:', stdout.getvalue())
                        self.assertEqual(stderr.getvalue(), '')
                for args in ([flag], [flag, ''], [flag + '=', pair[1]],
                             [flag, '', '', flag], [flag, '', '', flag, '']):
                    with self.subTest(args=args):
                        self.assert_argument_error(args, f'argument {flag}: expected 2 arguments')
                self.assert_argument_error([flag, '', '', flag, *pair, '--help'],
                                           f'argument {flag}: may only be specified once')
        self.assertEqual(self.snapshot(), before)

    def test_empty_single_path_modes_cannot_fall_through_to_export(self):
        # Valid export inputs used to reach export_dataset. Guard every dispatch
        # and I/O route so the pre-fix regression cannot publish anything.
        dataset = self.root / 'dataset'
        dataset.mkdir()
        (dataset / '000000001.json').write_text('{"id":1}')
        export = [str(dataset), '--schema', str(self.root / 'schema-0.json'),
                  '--out', str(self.root / 'unused.parquet')]
        before = self.snapshot()
        with self.no_artifact_io():
            for flag in self.single_path_modes():
                for mode in ([flag, ''], [flag + '=']):
                    for prefix in (True, False):
                        args = mode + export if prefix else export + mode
                        with self.subTest(args=args):
                            self.assert_argument_error(args, f'argument {flag}: must not be empty')
        self.assertEqual(self.snapshot(), before)

    def test_empty_single_path_modes_rejected_before_io(self):
        export = [str(self.root / 'source-0.jsonl'), '--input-format', 'jsonl',
                  '--schema', str(self.root / 'schema-0.json'),
                  '--out', str(self.root / 'unused.parquet')]
        options = [[], ['--batch-size', '2'], ['--batch-size=0'], ['--batch-size', '-1'],
                   ['--batch-bytes=16777216', '--max-item-bytes=8388608'],
                   ['--resume-dir', str(self.root / 'unused-job')]]
        before = self.snapshot()
        with self.no_artifact_io():
            for flag in self.single_path_modes():
                for mode in ([flag, ''], [flag + '='], [flag[:-1] + '=']):
                    for inputs in ([], export, ['--schema='], ['--out', ''], ['']):
                        for extra in options:
                            for prefix in (True, False):
                                args = mode + inputs + extra if prefix else inputs + extra + mode
                                with self.subTest(args=args):
                                    self.assert_argument_error(args, f'argument {flag}: must not be empty')
        self.assertEqual(self.snapshot(), before)

    def test_empty_mode_operands_preserve_parser_precedence(self):
        modes = {flag: pairs[0] for flag, pairs in self.pairs.items()}
        modes.update({flag: [paths[0]] for flag, paths in self.single_path_modes().items()})
        before = self.snapshot()
        with self.no_artifact_io():
            for flag, paths in self.single_path_modes().items():
                for mode in ([flag, ''], [flag + '=']):
                    for a, b in ((mode, [flag, paths[0]]), ([flag, paths[0]], mode),
                                 (mode, mode)):
                        with self.subTest(a=a, b=b):
                            self.assert_argument_error(a + b, f'argument {flag}: may only be specified once')
                    for other, operands in modes.items():
                        if other != flag:
                            for a, b in ((mode, [other, *operands]), ([other, *operands], mode)):
                                with self.subTest(a=a, b=b):
                                    self.assert_argument_error(a + b, f'not allowed with argument {a[0].split("=")[0]}')
                    for malformed, diagnostic in (
                            (['--batch-size', 'bad'], 'argument --batch-size: invalid int value'),
                            (['--batch-size'], 'argument --batch-size: expected one argument'),
                            (['--input-format=bad'], 'argument --input-format: invalid choice'),
                            (['--unknown'], 'unrecognized arguments: --unknown')):
                        for args in (mode + malformed, malformed + mode):
                            with self.subTest(args=args):
                                self.assert_argument_error(args, diagnostic)
                    for args in (mode + ['--help'], ['--help'] + mode):
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with self.subTest(args=args), redirect_stdout(stdout), redirect_stderr(stderr):
                            with self.assertRaises(SystemExit) as caught:
                                main(args)
                        self.assertEqual(caught.exception.code, 0)
                        self.assertIn('usage:', stdout.getvalue())
                        self.assertEqual(stderr.getvalue(), '')
                self.assert_argument_error([flag], f'argument {flag}: expected one argument')
                self.assert_argument_error([flag, '', flag], f'argument {flag}: expected one argument')
                self.assert_argument_error([flag, '', flag, paths[0], '--help'],
                                           f'argument {flag}: may only be specified once')
        self.assertEqual(self.snapshot(), before)

    def test_verify_rejects_empty_export_paths_on_valid_parquet(self):
        # An explicit empty path is still an export argument. Previously these
        # all verified a real artifact successfully, ignoring the supplied path.
        before = self.snapshot()
        for option in ([''], ['--schema', ''], ['--out', ''], ['--resume-dir', ''],
                       ['--schema='], ['--out='], ['--resume-dir=']):
            for prefix in (True, False):
                mode = ['--verify', str(self.root / 'export-0.parquet')]
                args = option + mode if prefix else mode + option
                with self.subTest(args=args):
                    self.assert_argument_error(
                        args, '--verify cannot be combined with source, --schema, --out or --resume-dir')
        self.assertEqual(self.snapshot(), before)

    def test_verify_empty_export_paths_rejected_before_io(self):
        corrupt = self.root / 'corrupt.parquet'
        corrupt.write_bytes(b'not parquet')
        before = self.snapshot()
        options = [[''], ['--schema', ''], ['--out', ''], ['--resume-dir', ''],
                   ['--schema='], ['--out='], ['--resume-dir='],
                   ['', '--schema=', '--out=', '--resume-dir='],
                   ['--schema', str(self.root / 'schema-0.json'), '--schema='],
                   ['--out', str(self.root / 'unused'), '--out='],
                   ['--resume-dir', str(self.root / 'unused-job'), '--resume-dir=']]
        with self.no_artifact_io():
            for path in (self.root / 'export-0.parquet', self.root / 'missing',
                         corrupt, self.root / 'checkpoint-0'):
                for option in options:
                    for extra in ([], ['--batch-size', '2'],
                                  ['--input-format', 'dataset', '--batch-bytes', '16777216',
                                   '--max-item-bytes', '8388608']):
                        for prefix in (True, False):
                            mode = ['--verify', str(path)]
                            args = option + mode + extra if prefix else mode + option + extra
                            with self.subTest(args=args):
                                self.assert_argument_error(
                                    args, '--verify cannot be combined with source, --schema, --out or --resume-dir')
        self.assertEqual(self.snapshot(), before)

    def test_verify_rejects_explicit_export_settings_on_valid_parquet(self):
        # These settings formerly succeeded because verify silently ignored them,
        # including explicit values equal to the ordinary export defaults.
        before = self.snapshot()
        for flag, values in (('--input-format', ('dataset', 'jsonl')),
                             ('--batch-bytes', ('16777216', '100')),
                             ('--max-item-bytes', ('8388608', '100'))):
            for value in values:
                for prefix in (True, False):
                    mode = ['--verify', str(self.root / 'export-0.parquet')]
                    option = [flag, value]
                    args = option + mode if prefix else mode + option
                    with self.subTest(args=args):
                        self.assert_argument_error(
                            args, '--verify accepts only PARQUET and optional --batch-size')
        self.assertEqual(self.snapshot(), before)

    def test_verify_export_settings_rejected_before_io(self):
        corrupt = self.root / 'corrupt.parquet'
        corrupt.write_bytes(b'not parquet')
        before = self.snapshot()
        options = [
            ['--input-format', 'dataset'], ['--input-format', 'jsonl'],
            ['--batch-bytes', '16777216'], ['--batch-bytes', '100'],
            ['--batch-bytes', '0'], ['--batch-bytes', '-1'],
            ['--max-item-bytes', '8388608'], ['--max-item-bytes', '100'],
            ['--max-item-bytes', '0'], ['--max-item-bytes', '-1'],
            ['--input-format', 'jsonl', '--batch-bytes', '16777216',
             '--max-item-bytes', '8388608', '--batch-size', '0'],
            ['--batch-bytes', '100', '--batch-bytes', '16777216'],
        ]
        with self.no_artifact_io():
            for path in (self.root / 'export-0.parquet', self.root / 'missing',
                         corrupt, self.root / 'checkpoint-0'):
                for option in options:
                    forms = [option]
                    if len(option) == 2:
                        forms.append([f'{option[0]}={option[1]}'])
                    for form in forms:
                        for prefix in (True, False):
                            mode = ['--verify', str(path)]
                            args = form + mode if prefix else mode + form
                            with self.subTest(args=args):
                                self.assert_argument_error(
                                    args, '--verify accepts only PARQUET and optional --batch-size')
            for extra in ([str(self.root / 'source-0.jsonl')],
                          ['--schema', str(self.root / 'schema-0.json')],
                          ['--out', str(self.root / 'unused')],
                          ['--resume-dir', str(self.root / 'unused-job')]):
                for prefix in (True, False):
                    mode = ['--verify', str(self.root / 'export-0.parquet')]
                    for ignored in ([], ['--input-format', 'dataset']):
                        options = extra + ignored
                        args = options + mode if prefix else mode + options
                        with self.subTest(args=args):
                            self.assert_argument_error(
                                args, '--verify cannot be combined with source, --schema, --out or --resume-dir')
        self.assertEqual(self.snapshot(), before)

    def test_verify_batch_size_and_artifact_semantics_preserved(self):
        artifact = self.root / 'export-0.parquet'
        expected = json.loads(Path(str(artifact) + '.manifest.json').read_text())
        corrupt = self.root / 'corrupt.parquet'
        corrupt.write_bytes(b'not parquet')
        before = self.snapshot()
        for size in (None, 1, 2, 1000):
            for prefix in (True, False):
                mode = ['--verify', str(artifact)]
                option = [] if size is None else ['--batch-size', str(size)]
                args = option + mode if prefix else mode + option
                stdout, stderr = io.StringIO(), io.StringIO()
                with self.subTest(args=args), patch(
                        'qawrench.crawlee_export.cli.verify_export', wraps=verify_export) as verify:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        self.assertEqual(main(args), 0)
                    verify.assert_called_once_with(str(artifact), batch_size=1000 if size is None else size)
                    self.assertEqual(json.loads(stdout.getvalue()), expected)
                    self.assertEqual(stderr.getvalue(), '')
        for path, size in ((self.root / 'missing', 1), (corrupt, 2),
                           (artifact, 0), (artifact, -1)):
            with self.assertRaises(ExportError) as caught:
                verify_export(path, batch_size=size)
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(path=path, size=size), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(['--batch-size', str(size), '--verify', str(path)]), 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(json.loads(stderr.getvalue()), dict(
                error=str(caught.exception), rows_validated=caught.exception.rows_validated,
                rows_committed=0))
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_valid_single_path_modes_are_argument_errors(self):
        # Valid artifacts used to succeed, silently selecting the last operand.
        before = self.snapshot()
        for flag, (first, second) in self.single_path_modes().items():
            for a, b in ((first, first), (first, second), (second, first)):
                with self.subTest(flag=flag, a=a, b=b):
                    self.assert_argument_error([flag, a, flag, b],
                                               f'argument {flag}: may only be specified once')
        self.assertEqual(self.snapshot(), before)

    def test_single_path_duplicates_rejected_before_io(self):
        before = self.snapshot()
        with self.no_artifact_io():
            for flag, (first, second) in self.single_path_modes().items():
                for a, b in ((first, first), (first, second), (second, first)):
                    for extra in ([], ['--batch-size', '2'], ['--batch-bytes', '100'],
                                  ['--max-item-bytes', '100'], ['--input-format', 'jsonl'],
                                  ['--schema', str(self.root / 'schema-0.json')],
                                  ['--out', str(self.root / 'unused')],
                                  ['--resume-dir', str(self.root / 'unused-job')],
                                  [str(self.root / 'source-0.jsonl')]):
                        for position in ('prefix', 'middle', 'suffix'):
                            args = {'prefix': [*extra, flag, a, flag, b],
                                    'middle': [flag, a, *extra, flag, b],
                                    'suffix': [flag, a, flag, b, *extra]}[position]
                            with self.subTest(args=args):
                                self.assert_argument_error(args, f'argument {flag}: may only be specified once')
                for args in ([flag, first, flag, second, flag, first],
                             [flag, first, flag[:-1], second],
                             [f'{flag}={first}', f'{flag}={second}'],
                             [flag, 'missing-a', flag, 'missing-b'],
                             [flag, '', flag, second]):
                    with self.subTest(args=args):
                        self.assert_argument_error(args, f'argument {flag}: may only be specified once')
        self.assertEqual(self.snapshot(), before)

    def test_export_resume_mode_rejects_export_options_and_wrong_arity_before_io(self):
        flag = '--check-export-resume-schema'
        pair = self.pairs[flag][0]
        options = [[''], ['source'], ['--schema='], ['--schema', pair[0]],
                   ['--out='], ['--out', 'unused'], ['--resume-dir='],
                   ['--resume-dir', 'unused'], ['--input-format=dataset'],
                   ['--input-format=jsonl'], ['--batch-size=1000'], ['--batch-size=0'],
                   ['--batch-bytes=16777216'], ['--max-item-bytes=8388608']]
        before = self.snapshot()
        with self.no_artifact_io():
            for operands in (pair, ['missing', 'missing'], [' ', ' ']):
                for extra in options:
                    for args in ([flag, *operands, *extra], [*extra, flag, *operands]):
                        with self.subTest(args=args):
                            self.assert_argument_error(args,
                                f'{flag} accepts only PREVIOUS and DIRECTORY paths')
            for operands in ([], pair[:1], pair + [pair[1]]):
                with self.subTest(operands=operands):
                    self.assert_argument_error([flag, *operands],
                        'expected 2 arguments' if len(operands) < 2 else
                        f'{flag} accepts only PREVIOUS and DIRECTORY paths')
        self.assertEqual(self.snapshot(), before)

    def test_resume_export_mode_rejects_export_options_and_wrong_arity_before_io(self):
        flag = '--check-resume-export-schema'
        pair = self.pairs[flag][0]
        options = [[''], ['source'], ['--schema='], ['--schema', pair[0]],
                   ['--out='], ['--out', 'unused'], ['--resume-dir='],
                   ['--resume-dir', 'unused'], ['--input-format=dataset'],
                   ['--input-format=jsonl'], ['--batch-size=1000'], ['--batch-size=0'],
                   ['--batch-bytes=16777216'], ['--max-item-bytes=8388608']]
        before = self.snapshot()
        with self.no_artifact_io():
            for operands in (pair, ['missing', 'missing'], [' ', ' ']):
                for extra in options:
                    for args in ([flag, *operands, *extra], [*extra, flag, *operands]):
                        with self.subTest(args=args):
                            self.assert_argument_error(args,
                                f'{flag} accepts only DIRECTORY and CANDIDATE paths')
            for operands in ([], pair[:1], pair + [pair[1]]):
                with self.subTest(operands=operands):
                    self.assert_argument_error([flag, *operands],
                        'expected 2 arguments' if len(operands) < 2 else
                        f'{flag} accepts only DIRECTORY and CANDIDATE paths')
        self.assertEqual(self.snapshot(), before)

    def test_distinct_modes_still_conflict_before_io(self):
        modes = {flag: pairs[0] for flag, pairs in self.pairs.items()}
        modes.update({'--verify': [str(self.root / 'export-0.parquet')],
                      '--inspect-resume': [str(self.root / 'checkpoint-0')]})
        before = self.snapshot()
        with self.no_artifact_io():
            for first, a in modes.items():
                for second, b in modes.items():
                    if first != second:
                        with self.subTest(first=first, second=second):
                            self.assert_argument_error([first, *a, second, *b],
                                                       f'not allowed with argument {first}')
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
