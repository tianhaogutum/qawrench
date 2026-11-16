"""Wildcard policies use original array positions in synthetic offline captures."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json

from qawrench.structured_data import ParityError, compare_files, make_baseline
from qawrench.structured_data.cli import main, markdown_report
from .test_parity import BASE, ROOT, FixtureCase, document


class WildcardNormalizationTests(FixtureCase):
    def test_repeated_values_and_ordered_list_regression(self):
        policy = {'/labels/*/@value': 'collapse-whitespace', '/steps/@list/*/@value': 'trim'}
        report = compare_files(ROOT / 'examples/structured_data/wildcard-old.html', ROOT / 'examples/structured_data/wildcard-new.html',
                               base_url=BASE, normalize=policy)
        self.assertEqual(report['policy']['normalize'], policy)
        self.assertEqual([(c['kind'], c['path']) for c in report['changes']], [
            ('changed', '/steps/@list/0/@value'), ('changed', '/steps/@list/1/@value'),
            ('removed', '/steps/@list/2')])
        self.assertEqual([(c['old'], c.get('new')) for c in report['changes']],
                         [('brew', 'serve'), ('serve', 'brew'), ({'@value': 'serve'}, None)])
        matches = report['normalization_matches']
        self.assertEqual(matches['/labels/*/@value'], {
            side: [{'entity': 'https://fixture.test/catalog/x', 'path': f'/labels/{i}/@value'}
                   for i in range(2)] for side in ('old', 'new')})
        self.assertEqual(len(matches['/steps/@list/*/@value']['old']), 3)
        self.assertEqual(len(matches['/steps/@list/*/@value']['new']), 2)
        self.assertIn('normalization_matches', markdown_report(report))
        with self.assertRaises(ParityError) as raised:
            compare_files(ROOT / 'examples/structured_data/wildcard-old.html', ROOT / 'examples/structured_data/wildcard-new.html',
                          base_url=BASE, normalize=policy, set_valued=('/steps/@list',))
        self.assertIn('ordered JSON-LD @list', str(raised.exception))

    def test_nested_wildcards_escapes_literal_star_and_numeric_order(self):
        left = {'@id': 'x', 'a/b~c': [[{'@value': f' {i} '} for i in range(12)]],
                '*': {'name': ' literal '}, 'label*': ' partial '}
        right = {'@id': 'x', 'a/b~c': [[{'@value': str(i)} for i in range(12)]],
                 '*': {'name': 'literal'}, 'label*': 'partial'}
        policy = {'/a~1b~0c/*/*/@value': 'trim', '/*/name': 'trim', '/label*': 'trim'}
        report = self.compare(document(left), document(right), normalize=policy)
        self.assertEqual(report['status'], 'equal')
        self.assertEqual([m['path'] for m in report['normalization_matches']['/a~1b~0c/*/*/@value']['old']],
                         [f'/a~1b~0c/0/{i}/@value' for i in range(12)])
        self.assertEqual(report['normalization_matches']['/*/name']['old'][0]['path'], '/*/name')

    def test_null_absence_empty_arrays_and_added_removed_entities(self):
        left = [{'@id': 'z', 'labels': [{'@value': ' gone '}]},
                {'@id': 'x', 'labels': [{'@value': None}, {}, {'@value': ' \n '}]},
                {'@id': 'a', 'labels': []}]
        right = [{'@id': 'y', 'labels': [{'@value': ' new '}]},
                 {'@id': 'x', 'labels': [{'@value': ''}, {'@value': None}, {}]}]
        report = self.compare(document(left), document(right), normalize={'/labels/*/@value': 'trim'})
        self.assertEqual([(c['kind'], c['path']) for c in report['changes']], [
            ('removed_entity', ''), ('changed', '/labels/0/@value'), ('added', '/labels/1/@value'),
            ('removed', '/labels/2/@value'), ('added_entity', ''), ('removed_entity', '')])
        self.assertIsNone(report['changes'][1]['old'])
        self.assertEqual(report['changes'][1]['new'], '')
        self.assertEqual(report['changes'][3]['old'], '')
        self.assertNotIn('new', report['changes'][3])
        self.assertEqual(report['changes'][4]['new']['labels'], [{'@value': 'new'}])
        self.assertEqual(report['changes'][5]['old']['labels'], [{'@value': 'gone'}])
        self.assertEqual([m['entity'] for m in report['normalization_matches']['/labels/*/@value']['old']],
                         ['https://fixture.test/catalog/x'] * 2 + ['https://fixture.test/catalog/z'])

    def test_normalization_precedes_set_deduplication_and_baseline_acceptance(self):
        left = {'@id': 'x', 'labels': [{'@value': ' B '}, {'@value': ' A '}, {'@value': ' A '}]}
        right = {'@id': 'x', 'labels': [{'@value': 'A'}, {'@value': 'B'}]}
        policy = {'/labels/*/@value': 'trim'}
        report = self.compare(document(left), document(right), normalize=policy, set_valued=('/labels',))
        self.assertEqual(report['status'], 'equal')
        self.assertEqual([m['path'] for m in report['normalization_matches']['/labels/*/@value']['old']],
                         [f'/labels/{i}/@value' for i in range(3)])
        baseline = self.directory / 'baseline.json'
        baseline.write_text(json.dumps(make_baseline(report)), encoding='utf-8')
        replay = compare_files(self.old, self.new, base_url=BASE, normalize=policy,
                               set_valued=('/labels',), baseline=baseline)
        self.assertEqual(replay['status'], 'equal')
        self.assertEqual(replay['normalization_matches'], report['normalization_matches'])

    def test_unmatched_selectors_include_empty_arrays_and_missing_suffixes(self):
        for labels in [[], [{}], [{'other': 'x'}]]:
            with self.subTest(labels=labels):
                error = self.failure('invalid_config', document({'@id': 'x', 'labels': labels}),
                                     normalize={'/labels/*/@value': 'trim'})
                self.assertIn('matched no JSON-LD values', str(error))
                self.assertIn('/labels/*/@value', str(error))

    def test_wrong_wildcard_container_and_incompatible_members_have_concrete_errors(self):
        cases = [(None, '/labels'), ({}, '/labels'), ({'0': {'@value': 'x'}}, '/labels'),
                 ([1], '/labels/0'), ([{'@value': 1}], '/labels/0/@value'),
                 ([{'@value': False}], '/labels/0/@value'),
                 ([{'@value': []}], '/labels/0/@value'), ([{'@value': {}}], '/labels/0/@value')]
        for labels, path in cases:
            with self.subTest(labels=labels):
                error = self.failure('invalid_config', document({'@id': 'x', 'labels': labels}),
                                     normalize={'/labels/*/@value': 'trim'})
                self.assertIn('/labels/*/@value', str(error))
                self.assertIn(repr(path), str(error))
                source = error.provenance[0]
                self.assertEqual(source['file'], str(self.old))
                self.assertEqual(source['entity_pointer'], path)
                self.assertEqual(source['normalization_pointer'], '/labels/*/@value')
                self.assertEqual(source['script_index'], 0)

    def test_overlaps_fail_even_with_same_mode_and_are_order_independent(self):
        text = document({'@id': 'x', 'labels': [{'@value': ' x '}]})
        for policy in [ {'/labels/*/@value': 'trim', '/labels/0/@value': 'casefold'},
                        {'/labels/*/@value': 'trim', '/labels/0/@value': 'trim'} ]:
            errors = []
            for items in [list(policy.items()), list(reversed(list(policy.items())))]:
                error = self.failure('invalid_config', text, normalize=dict(items))
                errors.append(str(error))
                self.assertIn('overlap', str(error))
                self.assertIn('/labels/0/@value', str(error))
            self.assertEqual(*errors)
        self.failure('invalid_config', text, normalize={'/labels/*/@value': 'trim'},
                     set_valued=('/labels/0/@value',))
        text = document({'@id': 'x', 'groups': [[{'@value': ' x '}]]})
        error = self.failure('invalid_config', text, normalize={
            '/groups/*/0/@value': 'trim', '/groups/0/*/@value': 'trim'})
        self.assertIn('overlap', str(error))

    def test_first_error_is_stable_by_entity_capture_policy_and_numeric_index(self):
        left = [{'@id': 'z', 'a': [False]}, {'@id': 'a', 'z': [False],
                 'a': ['ok'] * 2 + [42] + ['ok'] * 7 + [False]}]
        right = [{'@id': 'a', 'a': [False]}]
        with self.assertRaises(ParityError) as raised:
            self.compare(document(left), document(right), normalize={'/z/*': 'trim', '/a/*': 'trim'})
        self.assertIn("'/a/2'", str(raised.exception))
        self.assertEqual(raised.exception.provenance[0]['file'], str(self.old))
        self.assertEqual(raised.exception.provenance[0]['pointer'], '/1')

    def test_cli_wildcard_exit_codes_matches_markdown_and_error_output(self):
        self.old.write_text(document({'@id': 'x', 'labels': [{'@value': ' Straße '}]}), encoding='utf-8')
        self.new.write_text(document({'@id': 'x', 'labels': [{'@value': ' STRASSE '}]}), encoding='utf-8')
        def run(*args):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main([str(self.old), str(self.new), '--base-url', BASE, *args])
            return code, out.getvalue(), err.getvalue()
        options = ('--normalize', '/labels/*/@value', 'casefold', '--fail-on-diff')
        code, output, err = run(*options)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)['status'], 'equal')
        self.assertEqual(len(json.loads(output)['normalization_matches']['/labels/*/@value']['new']), 1)
        code, output, err = run(*options, '--format', 'markdown')
        self.assertEqual(code, 0, err)
        self.assertIn('normalization_matches', output)
        self.new.write_text(document({'@id': 'x', 'labels': [{'@value': 'Other'}]}), encoding='utf-8')
        self.assertEqual(run(*options)[0], 1)
        self.new.write_text(document({'@id': 'x', 'labels': [{'@value': 1}]}), encoding='utf-8')
        destination = self.directory / 'error.json'
        code, output, _ = run(*options, '--out', str(destination))
        self.assertEqual((code, output), (2, ''))
        report = json.loads(destination.read_text(encoding='utf-8'))
        self.assertEqual((report['status'], report['changes']), ('error', []))
        self.assertEqual(report['errors'][0]['code'], 'invalid_config')
        self.assertEqual(report['errors'][0]['provenance'][0]['file'], str(self.new))
