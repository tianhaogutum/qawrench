"""Offline provenance consistency against caller-retained robots snapshots."""

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import qawrench.frontier as library


FIXTURE = Path(__file__).resolve().parents[2] / 'examples/frontier/robots-byte-ranges-snapshots.json'


class WitnessVerificationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(FIXTURE.read_text())
        self.entry = self.manifest['origins']['https://EXAMPLE.test:443/']
        history = library.Checkpoint(library.Policy()).append(
            '{"url":"https://example.test/private/public"}\n', 'jsonl')
        requests, issues, total = history.load_requests()
        self.report = library.audit(requests, history.policy, issues=issues, total=total,
            robots=library.RobotsPolicy.from_dict(self.manifest), user_agent='AuditBot',
            enforce_robots=True, strict_robots=True)[1]
        self.witnesses = self.report['robots']['malformed_rules']

    def verify(self, witness=None, manifest=None):
        return library.verify_malformed_rule_witness(
            self.witnesses[0] if witness is None else witness,
            self.manifest if manifest is None else manifest)

    def assert_failure(self, result, code, field=None):
        self.assertIs(result['verified'], False)
        self.assertEqual(set(result), {'verified', 'failure'})
        self.assertEqual(set(result['failure']), {'code', 'field', 'message'})
        self.assertEqual(result['failure']['code'], code)
        self.assertIsInstance(result['failure']['message'], str)
        if field is not None:
            self.assertEqual(result['failure']['field'], field)

    def test_round_11_witnesses_verify_against_exact_retained_manifest(self):
        self.assertTrue(callable(getattr(library, 'verify_malformed_rule_witness', None)),
                        'missing exported offline verify_malformed_rule_witness helper')
        self.assertIn('verify_malformed_rule_witness', library.__all__)
        self.assertEqual([w['line'] for w in self.witnesses], [1, 5, 6, 7, 8, 10, 13])
        for witness in self.witnesses:
            with self.subTest(line=witness['line']):
                self.assertEqual(self.verify(witness), {'verified': True, 'failure': None})

    def test_each_separator_bom_multibyte_unicode_data_and_final_eof(self):
        for separator in ('\r', '\n', '\r\n'):
            for trailing in ('', separator):
                with self.subTest(separator=separator, trailing=trailing):
                    records = ['\ufeffAllow nope # é', '', '# 漢\u2028\u2029\x85',
                               '\tDisallow /重 # 🐈\ufeff ']
                    text = separator.join(records) + trailing
                    self.entry['text'] = text
                    start = len((separator.join(records[:-1]) + separator).encode())
                    witness = {**self.witnesses[0], 'line': 4, 'text': records[-1],
                        'snapshot_sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'byte_start': start, 'byte_end': start + len(records[-1].encode())}
                    self.assertEqual(self.verify(witness), {'verified': True, 'failure': None})

    def test_changed_snapshot_digest_including_distant_comment_and_line_endings(self):
        original = self.entry['text']
        for text in (original + '# distant comment', original.replace('préface', 'preface'),
                     original.replace('\r\n', '\n'), original.removeprefix('\ufeff')):
            with self.subTest(text=text):
                self.entry['text'] = text
                self.assert_failure(self.verify(), 'digest_mismatch', 'snapshot_sha256')

    def test_repeated_identical_records_are_never_relocated_or_repaired(self):
        one, two = self.witnesses[1:3]
        self.assertEqual(one['text'], two['text'])
        self.assert_failure(self.verify({**one, 'byte_start': two['byte_start'],
                                        'byte_end': two['byte_end']}), 'line_mismatch', 'line')
        self.assert_failure(self.verify({**one, 'line': two['line']}), 'line_mismatch', 'line')
        self.assert_failure(self.verify({**one, 'byte_start': one['byte_start'] + 1}),
                            'record_mismatch', 'byte_start/byte_end')
        # If all location fields agree with the other occurrence, that is a
        # consistent witness for that occurrence; there is no external anchor.
        self.assertEqual(self.verify({**one, 'line': two['line'],
            'byte_start': two['byte_start'], 'byte_end': two['byte_end']}),
            {'verified': True, 'failure': None})

    def test_invalid_offsets_and_partial_or_combined_records(self):
        witness = self.witnesses[0]
        size = len(self.entry['text'].encode())
        for field in ('byte_start', 'byte_end'):
            for value in (None, True, False, '0', 0.0, [], {}, -1, size + 1):
                with self.subTest(field=field, value=value):
                    self.assert_failure(self.verify({**witness, field: value}), 'invalid_span')
        for start, end in ((0, 0), (33, 1), (size, size)):
            self.assert_failure(self.verify({**witness, 'byte_start': start, 'byte_end': end}),
                                'invalid_span')
        # Inside BOM/multibyte, inside CRLF, separator inclusion, combined
        # records, and a proper UTF-8 suffix all fail whole-record boundaries.
        for start, end in ((1, 33), (3, 33), (0, 32), (0, 34), (0, 35), (34, 52), (0, 52)):
            with self.subTest(start=start, end=end):
                self.assert_failure(self.verify({**witness, 'byte_start': start, 'byte_end': end}),
                                    'record_mismatch', 'byte_start/byte_end')

    def test_text_line_source_and_origin_agreement(self):
        witness = self.witnesses[0]
        for text in (witness['text'].removeprefix('\ufeff'), witness['text'] + '\r\n', '', 'Allow other'):
            self.assert_failure(self.verify({**witness, 'text': text}), 'text_mismatch', 'text')
        for line in (2, 5, 999999):
            self.assert_failure(self.verify({**witness, 'line': line}), 'line_mismatch', 'line')
        self.assert_failure(self.verify({**witness, 'source': 'synthetic:changed'}),
                            'source_mismatch', 'source')
        self.assert_failure(self.verify({**witness, 'snapshot_sha256': '0' * 64}),
                            'digest_mismatch', 'snapshot_sha256')
        self.assert_failure(self.verify({**witness, 'origin': 'https://missing.test'}),
                            'missing_origin', 'origin')
        # Same source/text elsewhere never repairs a missing origin.
        self.manifest['origins'] = {'https://other.test': copy.deepcopy(self.entry)}
        self.assert_failure(self.verify(), 'missing_origin', 'origin')
        self.manifest['origins']['https://example.test'] = {
            'status': 'unavailable', 'source': witness['source'], 'reason': 'not retained'}
        self.assert_failure(self.verify(), 'unavailable_snapshot', 'origin')

    def test_invalid_witness_shapes_types_and_unicode_return_failures(self):
        for value in (None, [], '', 1, True):
            self.assert_failure(library.verify_malformed_rule_witness(value, self.manifest),
                                'invalid_witness', 'witness')
        witness = self.witnesses[0]
        for field in witness:
            missing = dict(witness); del missing[field]
            self.assert_failure(self.verify(missing), 'invalid_witness', 'witness')
        self.assert_failure(self.verify({**witness, 'extra': 1}), 'invalid_witness', 'witness')
        for field in ('origin', 'source', 'text', 'reason', 'snapshot_sha256'):
            for value in (None, 1, True, [], {}, '\ud800'):
                with self.subTest(field=field, value=value):
                    self.assert_failure(self.verify({**witness, field: value}), 'invalid_witness', field)
        for line in (0, -1, True, False, 1.0, '1', None, [], {}):
            self.assert_failure(self.verify({**witness, 'line': line}), 'invalid_witness', 'line')
        for origin in ('', 'https://EXAMPLE.test:443/', 'https://example.test/',
                       'https://example.test/path', 'https://example.test?x', 'not-a-url'):
            self.assert_failure(self.verify({**witness, 'origin': origin}), 'invalid_witness', 'origin')
        for field, value in (('source', '  '), ('reason', 'ignored non-rule record'),
                             ('snapshot_sha256', 'a' * 63), ('snapshot_sha256', 'A' * 64),
                             ('snapshot_sha256', 'g' * 64)):
            self.assert_failure(self.verify({**witness, field: value}), 'invalid_witness', field)

    def test_invalid_manifest_is_structured_and_validated_before_witness(self):
        invalid = [None, [], {}, {'version': True, 'origins': {}},
            {'version': 2, 'origins': {}}, {'version': 1, 'origins': []},
            {'version': 1, 'origins': {1: self.entry}},
            {'version': 1, 'origins': {'https://bad.test/path': self.entry}},
            {'version': 1, 'origins': {'https://bad.test': {**self.entry, 'text': '\ud800'}}},
            {'version': 1, 'origins': {'https://bad.test': {**self.entry, 'extra': 'x'}}},
            {'version': 1, 'origins': {'https://example.test': self.entry,
                                     'https://EXAMPLE.test:443/': self.entry}}]
        for manifest in invalid:
            with self.subTest(manifest=manifest):
                self.assert_failure(library.verify_malformed_rule_witness(None, manifest),
                                    'invalid_manifest', 'manifest')
        self.manifest['origins']['https://unused.test']['text'] = '\ud800'
        self.assert_failure(self.verify(), 'invalid_manifest', 'manifest')

    def test_pure_helper_preserves_inputs_and_never_audits_or_fetches(self):
        before = copy.deepcopy((self.manifest, self.report))
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('builtins.open', side_effect=AssertionError('I/O forbidden')), \
                patch('pathlib.Path.open', side_effect=AssertionError('I/O forbidden')), \
                patch('qawrench.frontier.robots.RobotsPolicy.audit',
                      side_effect=AssertionError('audit forbidden')), \
                patch('qawrench.frontier.robots._parse', side_effect=AssertionError('parse forbidden')), \
                patch('qawrench.frontier.checkpoint.Checkpoint.append',
                      side_effect=AssertionError('checkpoint changes forbidden')):
            self.assertEqual(self.verify(), {'verified': True, 'failure': None})
            self.assert_failure(self.verify({**self.witnesses[0], 'text': 'changed'}), 'text_mismatch')
        self.assertEqual((self.manifest, self.report), before)


if __name__ == '__main__':
    unittest.main()
