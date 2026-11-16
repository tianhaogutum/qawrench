"""Explicit supplied-snapshot coverage, independent of request/audit coverage."""

import copy
import hashlib
import re
import unittest
from unittest.mock import patch

import qawrench.frontier as library


class WitnessCollectionTests(unittest.TestCase):
    def setUp(self):
        self.origin = 'https://example.test'
        self.text = ('\ufeffAllow nope # é\r\n'
                     'Allow: /重 # 🐈\r'
                     'User-agent: AuditBot\n'
                     'Allow: /重 # 🐈\r\n'
                     'User-agent: BadBot/1\r'
                     'Allow: /重 # 🐈\n'
                     'User-agent: OtherBot\r\n'
                     'Disallow: bad%xx # 漢\r'
                     '# é\u2028\u2029\x85\n'
                     'Allow nope # é')
        self.manifest = {'version': 1, 'origins': {
            'https://EXAMPLE.test:443/': self.entry(self.text),
            'https://other.test': self.entry('User-agent: Bot\nDisallow: bad%xx'),
            'https://clean.test': self.entry('User-agent: *\r\nAllow: /'),
            'https://empty.test': self.entry(''),
            'https://down.test': {'status': 'unavailable', 'source': 'synthetic:down',
                                  'reason': 'not retained'}}}
        self.witnesses = [self.witness(line, reason) for line, reason in (
            (1, 'missing colon after core directive'), (2, 'rule outside a valid group'),
            (5, 'invalid user-agent token'), (6, 'rule outside a valid group'),
            (8, 'invalid path pattern'), (10, 'missing colon after core directive'))]

    @staticmethod
    def entry(text):
        return {'status': 'available', 'source': 'synthetic:collection', 'text': text}

    def witness(self, line, reason, *, origin=None, text=None):
        snapshot = (self.text if text is None else text).encode('utf-8')
        record = list(re.finditer(rb'([^\r\n]*)(?:\r\n|\r|\n|$)', snapshot))[line - 1]
        return {'origin': self.origin if origin is None else origin,
                'source': 'synthetic:collection', 'line': line,
                'text': record.group(1).decode('utf-8'), 'reason': reason,
                'snapshot_sha256': hashlib.sha256(snapshot).hexdigest(),
                'byte_start': record.start(1), 'byte_end': record.end(1)}

    def verify(self, witnesses, origins=None):
        helper = getattr(library, 'verify_malformed_rule_collection', None)
        self.assertTrue(callable(helper), 'missing exported collection coverage helper')
        return helper(witnesses, self.manifest,
                      origins=[self.origin] if origins is None else origins)

    def assert_envelope_failure(self, result, code, field):
        self.assertEqual(set(result), {'complete', 'failure', 'entries', 'missing'})
        self.assertIs(result['complete'], False)
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['missing'], [])
        self.assertEqual(set(result['failure']), {'code', 'field', 'message'})
        self.assertEqual((result['failure']['code'], result['failure']['field']), (code, field))
        self.assertTrue(result['failure']['message'])

    def test_omitted_repeated_record_passes_individual_checks_but_is_missing(self):
        # This prefix is genuine, including semantic checks. Its success says
        # nothing about omitted records, even when omitted text repeats.
        submitted = self.witnesses[:3] + self.witnesses[4:]
        for witness in submitted:
            self.assertEqual(library.verify_malformed_rule_witness(
                witness, self.manifest, verify_reason=True), {'verified': True, 'failure': None})
        result = self.verify(submitted)
        self.assertIs(result['complete'], False)
        self.assertIsNone(result['failure'])
        self.assertEqual(result['missing'], [self.witnesses[3]])
        self.assertEqual(result['entries'], [
            {'index': i, 'status': 'verified', 'failure': None, 'duplicate_of': None}
            for i in range(len(submitted))])

    def test_complete_permuted_collection_and_empty_submissions(self):
        self.assertIn('verify_malformed_rule_collection', library.__all__)
        result = self.verify(list(reversed(self.witnesses)))
        self.assertEqual(set(result), {'complete', 'failure', 'entries', 'missing'})
        self.assertIs(result['complete'], True)
        self.assertIsNone(result['failure'])
        self.assertEqual(result['missing'], [])
        self.assertTrue(all(e['status'] == 'verified' for e in result['entries']))
        result = self.verify([])
        self.assertIs(result['complete'], False)
        self.assertEqual(result['missing'], self.witnesses)
        self.assertEqual(result['entries'], [])
        # An unavailable unused snapshot is allowed, and coverage is never
        # enlarged to include the malformed but unscoped snapshots.
        for scope in (['https://clean.test'], ['https://empty.test'],
                      ['https://empty.test', 'https://clean.test']):
            self.assertEqual(self.verify([], scope),
                             {'complete': True, 'failure': None, 'entries': [], 'missing': []})

    def test_expected_order_is_explicit_scope_then_record_order(self):
        other = self.witness(2, 'invalid path pattern', origin='https://other.test',
                             text=self.manifest['origins']['https://other.test']['text'])
        for scope, expected in ((['https://other.test', self.origin], [other] + self.witnesses),
                                ([self.origin, 'https://other.test'], self.witnesses + [other])):
            self.assertEqual(self.verify([], scope)['missing'], expected)
            result = self.verify([other] + list(reversed(self.witnesses)), scope)
            self.assertIs(result['complete'], True)
            self.assertEqual(result['missing'], [])
        # Exact same text/digest/source at another origin cannot cover it.
        self.manifest['origins']['https://copy.test'] = self.entry(self.text)
        copies = [{**w, 'origin': 'https://copy.test'} for w in self.witnesses]
        result = self.verify(copies + self.witnesses[:1], [self.origin, 'https://copy.test'])
        self.assertEqual(result['missing'], self.witnesses[1:])

    def test_mixed_invalid_unexpected_and_duplicates_keep_indices_and_missing(self):
        other = self.witness(2, 'invalid path pattern', origin='https://other.test',
                             text=self.manifest['origins']['https://other.test']['text'])
        wrong = {**self.witnesses[0], 'reason': 'invalid path pattern'}
        submitted = [wrong, self.witnesses[0], self.witnesses[0], other, other,
                     None, self.witnesses[0], self.witnesses[2]]
        result = self.verify(submitted)
        self.assertIs(result['complete'], False)
        self.assertIsNone(result['failure'])
        self.assertEqual(result['missing'], [self.witnesses[i] for i in (1, 3, 4, 5)])
        self.assertEqual([e['index'] for e in result['entries']], list(range(8)))
        self.assertEqual([e['status'] for e in result['entries']],
                         ['invalid', 'verified', 'duplicate', 'unexpected', 'unexpected',
                          'invalid', 'duplicate', 'verified'])
        self.assertEqual([e['duplicate_of'] for e in result['entries']],
                         [None, None, 1, None, None, None, 1, None])
        for index, code in ((0, 'reason_mismatch'), (2, 'duplicate_witness'),
                            (3, 'unexpected_origin'), (4, 'unexpected_origin'),
                            (5, 'invalid_witness'), (6, 'duplicate_witness')):
            self.assertEqual(result['entries'][index]['failure']['code'], code)
        for extra, status in ((self.witnesses[0], 'duplicate'), (other, 'unexpected')):
            result = self.verify(self.witnesses + [extra])
            self.assertEqual(result['missing'], [])
            self.assertIs(result['complete'], False)
            self.assertEqual(result['entries'][-1]['status'], status)

    def test_invalid_entries_reuse_all_single_witness_checks_before_scope_or_duplicates(self):
        first, repeated = self.witnesses[1], self.witnesses[3]
        invalid = [None, {}, {**first, 'extra': 1}, {**first, 'text': '\ud800'},
                   {**first, 'origin': 'https://EXAMPLE.test:443/'},
                   {**first, 'origin': 'https://absent.test'},
                   {**first, 'origin': 'https://down.test'},
                   {**first, 'source': 'changed'}, {**first, 'snapshot_sha256': '0' * 64},
                   {**first, 'byte_start': True}, {**first, 'byte_start': first['byte_start'] + 1},
                   {**first, 'text': 'changed'}, {**first, 'line': 99},
                   {**first, 'byte_start': repeated['byte_start'], 'byte_end': repeated['byte_end']},
                   {**first, 'reason': 'invalid path pattern'},
                   self.witness(4, 'rule outside a valid group')]
        result = self.verify([first] + invalid)
        for index, witness in enumerate(invalid, 1):
            with self.subTest(index=index):
                expected = library.verify_malformed_rule_witness(witness, self.manifest, verify_reason=True)
                self.assertIs(expected['verified'], False)
                self.assertEqual(result['entries'][index], {'index': index, 'status': 'invalid',
                                 'failure': expected['failure'], 'duplicate_of': None})
        self.assertEqual(result['missing'], [w for w in self.witnesses if w != first])

    def test_scope_shape_canonical_uniqueness_and_argument_contract(self):
        helper = library.verify_malformed_rule_collection
        for value in (None, True, 1, '', self.origin, {}, (), set(), [], iter([self.origin])):
            with self.subTest(value=value):
                self.assert_envelope_failure(helper(None, None, origins=value), 'invalid_scope', 'origins')
        for origin in (None, 1, True, [], {}, '', '\ud800', 'not-url', 'ftp://example.test',
                       self.origin + '/', self.origin + '/path', self.origin + '?',
                       'https://EXAMPLE.test', 'https://example.test:443', 'https://[0:0::1]'):
            with self.subTest(origin=origin):
                self.assert_envelope_failure(helper(None, None, origins=[origin]),
                                             'invalid_scope', 'origins[0]')
        self.assert_envelope_failure(helper(None, None, origins=[self.origin, self.origin]),
                                     'invalid_scope', 'origins[1]')
        # Entire scope syntax precedes manifest resolution of earlier origins.
        self.assert_envelope_failure(self.verify([], ['https://absent.test', 'bad']),
                                     'invalid_scope', 'origins[1]')
        with self.assertRaises(TypeError):
            helper([], self.manifest)
        with self.assertRaises(TypeError):
            helper([], self.manifest, [self.origin])
        with self.assertRaises(TypeError):
            helper([], self.manifest, origins=[self.origin], verify_reason=False)

    def test_entire_manifest_precedes_scope_resolution_and_collection_shape(self):
        helper = library.verify_malformed_rule_collection
        invalid = [None, [], {}, {'version': True, 'origins': {}},
                   {'version': 2, 'origins': {}}, {'version': 1, 'origins': []}]
        for entry in ({'status': 'available', 'source': 'label', 'text': '\ud800'},
                      {'status': 'unavailable', 'source': 'label', 'reason': ''},
                      {**self.entry(''), 'extra': 1}):
            invalid.append({'version': 1, 'origins': {self.origin: self.entry(''),
                                                    'https://unused.test': entry}})
        invalid.append({'version': 1, 'origins': {self.origin: self.entry(''),
                                                self.origin + '/': self.entry('')}})
        with patch('qawrench.frontier.witness._parse', side_effect=AssertionError('parse forbidden')):
            for manifest in invalid:
                self.assert_envelope_failure(helper(None, manifest, origins=['https://absent.test']),
                                             'invalid_manifest', 'manifest')
            for scope, code, field in ((['https://absent.test'], 'missing_origin', 'origins[0]'),
                    ([self.origin, 'https://down.test'], 'unavailable_snapshot', 'origins[1]'),
                    (['https://down.test', 'https://absent.test'], 'unavailable_snapshot', 'origins[0]')):
                self.assert_envelope_failure(self.verify(None, scope), code, field)
            for value in (None, {}, '', True, 1, (), iter(self.witnesses)):
                self.assert_envelope_failure(self.verify(value), 'invalid_collection', 'witnesses')

    def test_bom_multibyte_separators_eof_and_exact_missing_bindings(self):
        for separator in ('\r', '\n', '\r\n'):
            for trailing in ('', separator):
                with self.subTest(separator=separator, trailing=trailing):
                    self.text = separator.join(['\ufeffAllow nope # é', 'User-agent: Bot',
                                                '# 漢\u2028\u2029\x85', 'Disallow: /%xx # 🐈',
                                                'Disallow: /%xx # 🐈']) + trailing
                    self.manifest['origins']['https://EXAMPLE.test:443/']['text'] = self.text
                    expected = [self.witness(1, 'missing colon after core directive'),
                                self.witness(4, 'invalid path pattern'),
                                self.witness(5, 'invalid path pattern')]
                    result = self.verify(expected[:2] + expected[1:2])
                    self.assertEqual(result['missing'], expected[2:])
                    self.assertEqual(result['entries'][-1]['status'], 'duplicate')
                    self.assertIs(self.verify(expected)['complete'], True)
                    self.assertEqual(expected[0]['byte_start'], 0)
                    self.assertTrue(expected[0]['text'].startswith('\ufeff'))
                    self.assertEqual(expected[-1]['byte_end'],
                                     len(self.text.encode()) - len(trailing.encode()))

    def test_pure_immutable_detached_results_without_audit_io_or_checkpoints(self):
        scope = [self.origin]
        before = copy.deepcopy((self.manifest, self.witnesses, scope))
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('builtins.open', side_effect=AssertionError('I/O forbidden')), \
                patch('pathlib.Path.open', side_effect=AssertionError('I/O forbidden')), \
                patch('qawrench.frontier.robots.RobotsPolicy.audit',
                      side_effect=AssertionError('audit forbidden')), \
                patch('qawrench.frontier.checkpoint.Checkpoint.append',
                      side_effect=AssertionError('checkpoint forbidden')):
            result = self.verify([], scope)
            self.assertIs(self.verify(self.witnesses, scope)['complete'], True)
            self.assertEqual(result, self.verify([], scope))
        self.assertEqual((self.manifest, self.witnesses, scope), before)
        result['missing'][0]['text'] = 'changed returned value'
        self.assertEqual((self.manifest, self.witnesses, scope), before)
        self.assertEqual(self.verify([])['missing'], self.witnesses)
