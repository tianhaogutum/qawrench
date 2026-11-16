"""Opt-in classification of an exactly bound strict robots record."""

import copy
import hashlib
import re
import unittest
from unittest.mock import patch

import qawrench.frontier as library


class WitnessReasonTests(unittest.TestCase):
    def setUp(self):
        # Same Allow record is outside a valid group, then valid, then an
        # invalid path in a different group. All record delimiters and EOF.
        self.text = ('\ufeffAllow nope # é\r\n'
                     'Allow: /重 # 🐈\r'
                     'User-agent: AuditBot\n'
                     'Allow: /重 # 🐈\r\n'
                     'User-agent: BadBot/1\r'
                     'Allow: /重 # 🐈\n'
                     'User-agent: OtherBot\r\n'
                     'Disallow: bad%xx # 漢\r'
                     'Sitemap: https://example.test/漢\n'
                     'Allow: \r\n'
                     '# é\u2028\u2029\x85\r'
                     'Allow nope # é')
        self.manifest = {'version': 1, 'origins': {'https://EXAMPLE.test:443/': {
            'status': 'available', 'source': 'synthetic:reason', 'text': self.text}}}

    def witness(self, line, reason):
        snapshot = self.text.encode('utf-8')
        record = list(re.finditer(rb'([^\r\n]*)(?:\r\n|\r|\n|$)', snapshot))[line - 1]
        return {'origin': 'https://example.test', 'source': 'synthetic:reason',
                'line': line, 'text': record.group(1).decode('utf-8'), 'reason': reason,
                'snapshot_sha256': hashlib.sha256(snapshot).hexdigest(),
                'byte_start': record.start(1), 'byte_end': record.end(1)}

    def verify(self, witness, **options):
        return library.verify_malformed_rule_witness(witness, self.manifest, **options)

    def assert_failure(self, result, code, field='reason'):
        self.assertEqual(set(result), {'verified', 'failure'})
        self.assertIs(result['verified'], False)
        self.assertEqual(set(result['failure']), {'code', 'field', 'message'})
        self.assertEqual(result['failure']['code'], code)
        self.assertEqual(result['failure']['field'], field)
        self.assertTrue(result['failure']['message'])

    def test_wrong_recognized_reason_passes_default_but_fails_opt_in(self):
        witness = self.witness(1, 'invalid path pattern')
        self.assertEqual(self.verify(witness), {'verified': True, 'failure': None})
        self.assert_failure(self.verify(witness, verify_reason=True), 'reason_mismatch')

    def test_genuine_categories_and_every_wrong_recognized_reason(self):
        expected = {1: 'missing colon after core directive',
                    2: 'rule outside a valid group', 5: 'invalid user-agent token',
                    6: 'rule outside a valid group', 8: 'invalid path pattern',
                    12: 'missing colon after core directive'}
        history = library.Checkpoint(library.Policy()).append(
            '{"url":"https://example.test/"}\n', 'jsonl')
        requests, issues, total = history.load_requests()
        report = library.audit(requests, history.policy, issues=issues, total=total,
            robots=library.RobotsPolicy.from_dict(self.manifest), user_agent='AuditBot',
            enforce_robots=True, strict_robots=True)[1]
        emitted = report['robots']['malformed_rules']
        self.assertEqual({w['line']: w['reason'] for w in emitted}, expected)
        for witness in emitted:
            with self.subTest(line=witness['line']):
                self.assertEqual(witness, self.witness(witness['line'], expected[witness['line']]))
                self.assertEqual(self.verify(witness, verify_reason=True),
                                 {'verified': True, 'failure': None})
                for wrong in set(expected.values()) - {witness['reason']}:
                    self.assert_failure(self.verify({**witness, 'reason': wrong}, verify_reason=True),
                                        'reason_mismatch')

    def test_valid_and_ignored_records_are_not_strict_malformed(self):
        for line in (3, 4, 7, 9, 10, 11):
            for reason in ('invalid path pattern', 'invalid user-agent token',
                           'rule outside a valid group', 'missing colon after core directive'):
                with self.subTest(line=line, reason=reason):
                    witness = self.witness(line, reason)
                    self.assertEqual(self.verify(witness), {'verified': True, 'failure': None})
                    self.assert_failure(self.verify(witness, verify_reason=True), 'not_malformed_rule')

    def test_repeated_records_use_exact_location_and_whole_group_context(self):
        orphan = self.witness(2, 'rule outside a valid group')
        valid = self.witness(4, 'rule outside a valid group')
        later_orphan = self.witness(6, 'rule outside a valid group')
        self.assertEqual(orphan['text'], valid['text'])
        self.assertEqual(orphan['text'], later_orphan['text'])
        self.assertEqual(self.verify(orphan, verify_reason=True), {'verified': True, 'failure': None})
        self.assert_failure(self.verify(valid, verify_reason=True), 'not_malformed_rule')
        self.assertEqual(self.verify(later_orphan, verify_reason=True), {'verified': True, 'failure': None})
        self.assert_failure(self.verify({**orphan, 'byte_start': valid['byte_start'],
            'byte_end': valid['byte_end']}, verify_reason=True), 'line_mismatch', 'line')
        # The same bad path is outside a group first and inside a valid group
        # later; parsing a single record would give the wrong later reason.
        self.text = 'Disallow: bad%xx\rUser-agent: OtherBot\nDisallow: bad%xx'
        self.manifest['origins']['https://EXAMPLE.test:443/']['text'] = self.text
        for line, reason in ((1, 'rule outside a valid group'), (3, 'invalid path pattern')):
            self.assertEqual(self.verify(self.witness(line, reason), verify_reason=True),
                             {'verified': True, 'failure': None})

    def test_bom_separators_multibyte_and_final_eof(self):
        for separator in ('\r', '\n', '\r\n'):
            for trailing in ('', separator):
                with self.subTest(separator=separator, trailing=trailing):
                    self.text = separator.join(['\ufeffAllow nope # é', 'User-agent: Bot',
                                                '# 漢\u2028\u2029\x85', 'Disallow: /%xx # 🐈']) + trailing
                    self.manifest['origins']['https://EXAMPLE.test:443/']['text'] = self.text
                    for line, reason in ((1, 'missing colon after core directive'),
                                         (4, 'invalid path pattern')):
                        self.assertEqual(self.verify(self.witness(line, reason), verify_reason=True),
                                         {'verified': True, 'failure': None})

    def test_boolean_keyword_validation_and_python_argument_contract(self):
        witness = self.witness(1, 'missing colon after core directive')
        for value in (None, 0, 1, 0.0, 1.0, '', 'true', [], {}, object()):
            with self.subTest(value=value):
                self.assert_failure(library.verify_malformed_rule_witness(
                    None, None, verify_reason=value), 'invalid_option', 'verify_reason')
        for option in (False, True):
            self.assertEqual(self.verify(witness, verify_reason=option),
                             {'verified': True, 'failure': None})
        with self.assertRaises(TypeError):
            library.verify_malformed_rule_witness(witness, self.manifest, True)
        with self.assertRaises(TypeError):
            self.verify(witness, unknown_option=True)

    def test_existing_failures_precede_classification_without_parsing(self):
        witness = self.witness(1, 'invalid path pattern')
        invalid = [None, {**witness, 'reason': 'unknown'},
                   {**witness, 'origin': 'https://missing.test'},
                   {**witness, 'source': 'changed'}, {**witness, 'snapshot_sha256': '0' * 64},
                   {**witness, 'byte_start': True}, {**witness, 'byte_start': 3},
                   {**witness, 'byte_end': witness['byte_end'] + 1},
                   {**witness, 'text': 'changed'}, {**witness, 'line': 2}]
        with patch('qawrench.frontier.witness._parse', side_effect=AssertionError('parse forbidden')):
            for bad in invalid:
                default = library.verify_malformed_rule_witness(bad, self.manifest)
                self.assertIs(default['verified'], False)
                self.assertEqual(library.verify_malformed_rule_witness(
                    bad, self.manifest, verify_reason=True), default)
            self.assert_failure(library.verify_malformed_rule_witness(
                witness, None, verify_reason=True), 'invalid_manifest', 'manifest')
            self.assertEqual(self.verify(witness, verify_reason=False),
                             {'verified': True, 'failure': None})

    def test_opt_in_is_pure_does_not_audit_and_preserves_inputs(self):
        witnesses = [self.witness(1, 'missing colon after core directive'),
                     self.witness(1, 'invalid path pattern'),
                     self.witness(4, 'rule outside a valid group')]
        before = copy.deepcopy((self.manifest, witnesses))
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('builtins.open', side_effect=AssertionError('I/O forbidden')), \
                patch('pathlib.Path.open', side_effect=AssertionError('I/O forbidden')), \
                patch('qawrench.frontier.robots.RobotsPolicy.audit',
                      side_effect=AssertionError('audit forbidden')), \
                patch('qawrench.frontier.checkpoint.Checkpoint.append',
                      side_effect=AssertionError('checkpoint changes forbidden')):
            self.assertEqual(self.verify(witnesses[0], verify_reason=True),
                             {'verified': True, 'failure': None})
            self.assert_failure(self.verify(witnesses[1], verify_reason=True), 'reason_mismatch')
            self.assert_failure(self.verify(witnesses[2], verify_reason=True), 'not_malformed_rule')
        self.assertEqual((self.manifest, witnesses), before)


if __name__ == '__main__':
    unittest.main()
