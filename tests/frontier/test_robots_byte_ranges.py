"""Strict witness offsets select original UTF-8 records, bound to whole snapshots."""

import hashlib
import json
from pathlib import Path
import unittest

from qawrench.frontier import Checkpoint, Policy, RobotsPolicy, audit


FIXTURE = Path(__file__).resolve().parents[2] / 'examples/frontier/robots-byte-ranges-snapshots.json'


class RobotsByteRangeTests(unittest.TestCase):
    def audit(self, text, *, extra=None, strict=True, enforced=True, urls=None):
        manifest = {'version': 1, 'origins': {'https://EXAMPLE.test:443/': {
            'status': 'available', 'source': 'synthetic:byte-ranges', 'text': text}, **(extra or {})}}
        urls = urls or ['https://example.test/private/public']
        history = Checkpoint(Policy()).append(''.join(json.dumps({'url': u}) + '\n' for u in urls), 'jsonl')
        requests, issues, total = history.load_requests()
        return audit(requests, history.policy, issues=issues, total=total,
                     robots=RobotsPolicy.from_dict(manifest), user_agent='AuditBot',
                     enforce_robots=enforced, strict_robots=strict)

    def assert_binding(self, witness, snapshot, start, end):
        self.assertEqual(witness['byte_start'], start)
        self.assertEqual(witness['byte_end'], end)
        self.assertIs(type(witness['byte_start']), int)
        self.assertIs(type(witness['byte_end']), int)
        self.assertEqual(witness['snapshot_sha256'], hashlib.sha256(snapshot.encode('utf-8')).hexdigest())
        self.assertEqual(snapshot.encode('utf-8')[start:end].decode('utf-8'), witness['text'])

    def test_mixed_records_bind_exact_byte_ranges_and_repeated_text(self):
        text = json.loads(FIXTURE.read_text())['origins']['https://EXAMPLE.test:443/']['text']
        _, report = self.audit(text, extra={'https://unused.test': {
            'status': 'available', 'source': 'unused', 'text': 'Disallow unused'}})
        self.assertEqual(report['status'], 'rejected')
        robots = report['robots']
        self.assertEqual(robots['counts'], {'allow': 1, 'disallow': 0, 'unknown': 0})
        expected = [(1, 0, 33, 'missing colon after core directive'),
                    (5, 95, 124, 'missing colon after core directive'),
                    (6, 125, 154, 'missing colon after core directive'),
                    (7, 155, 180, 'invalid user-agent token'),
                    (8, 182, 201, 'rule outside a valid group'),
                    (10, 223, 245, 'invalid path pattern'),
                    (13, 291, 310, 'missing colon after core directive')]
        self.assertEqual(len(robots['malformed_rules']), len(expected))
        for witness, (line, start, end, reason) in zip(robots['malformed_rules'], expected):
            self.assert_binding(witness, text, start, end)
            self.assertEqual(witness['line'], line)
            self.assertEqual(witness['reason'], reason)
            self.assertEqual(witness['origin'], 'https://example.test')
            self.assertEqual(witness['source'], 'synthetic:byte-ranges')
            self.assertEqual(witness['snapshot_sha256'], robots['origins'][0]['sha256'])
        self.assertEqual(robots['malformed_rules'][1]['text'], robots['malformed_rules'][2]['text'])
        self.assertEqual(len(robots['origins']), 1)
        # Tolerant text still omits the signature and never gains byte fields.
        first = robots['origins'][0]['diagnostics'][0]
        self.assertEqual(first, {'line': 1, 'text': 'Disallow /private # café 🐈',
                                 'reason': 'ignored non-rule record'})

    def test_each_separator_and_eof_keep_exact_whitespace_bom_and_multibyte_bytes(self):
        for separator in ('\r', '\n', '\r\n'):
            for trailing in ('', separator):
                for first in ('\ufeff \tAllow bad # é ', '\ufeffUser-agent: AuditBot'):
                    with self.subTest(separator=separator, trailing=trailing, first=first):
                        last = '\tDisallow /重 # 🐈\ufeff '
                        prefix = first + separator + '# é\u2028漢\x85' + separator
                        text = prefix + last + trailing
                        _, report = self.audit(text)
                        witnesses = report['robots']['malformed_rules']
                        self.assertEqual([w['line'] for w in witnesses], [1, 3] if 'Allow' in first else [3])
                        if len(witnesses) == 2:
                            self.assert_binding(witnesses[0], text, 0, len(first.encode()))
                        self.assert_binding(witnesses[-1], text, len(prefix.encode()),
                                            len(prefix.encode()) + len(last.encode()))

    def test_digest_binds_entire_snapshot_not_only_malformed_record(self):
        prefix = '\ufeffAllow bad # é\r\nUser-agent: AuditBot\nAllow: /\r'
        witnesses = []
        for suffix in ('# comment A', '# comment B'):
            text = prefix + suffix
            _, report = self.audit(text)
            witness = report['robots']['malformed_rules'][0]
            self.assert_binding(witness, text, 0, len('\ufeffAllow bad # é'.encode()))
            witnesses.append(witness)
        self.assertEqual(witnesses[0]['text'], witnesses[1]['text'])
        self.assertNotEqual(witnesses[0]['snapshot_sha256'], witnesses[1]['snapshot_sha256'])

    def test_each_encountered_origin_uses_its_own_snapshot_even_with_same_source(self):
        one = 'User-agent: AuditBot\nDisallow /x'
        two = '# é\r\nUser-agent: AuditBot\rDisallow /x'
        _, report = self.audit(one, extra={
            'https://other.test': {'status': 'available', 'source': 'synthetic:byte-ranges', 'text': two},
            'https://down.test': {'status': 'unavailable', 'source': 'missing', 'reason': 'not captured'}},
            urls=['https://example.test/', 'https://other.test/', 'https://down.test/', 'https://absent.test/'])
        witnesses = report['robots']['malformed_rules']
        self.assertEqual([w['origin'] for w in witnesses], ['https://example.test', 'https://other.test'])
        self.assert_binding(witnesses[0], one, 21, 32)
        self.assert_binding(witnesses[1], two, 27, 38)
        self.assertEqual(report['robots']['counts'], {'allow': 2, 'disallow': 0, 'unknown': 2})

    def test_extensions_interior_boms_and_default_modes_retain_tolerant_behavior(self):
        text = ('\ufeff\ufeffDisallow /x\r\nUser-agent: AuditBot\nSitemap: map\r'
                'Disallow-extra /x\n\ufeffAllow bad\nAllow: /private/public\n'
                'Disallow: /interior\ufeffpath\nFuture: é\u2028Disallow /x')
        candidate, strict = self.audit(text)
        self.assertEqual(strict['status'], 'ok')
        self.assertEqual(strict['robots']['malformed_rules'], [])
        for enforced in (False, True):
            other, report = self.audit(text, strict=False, enforced=enforced)
            self.assertEqual(candidate, other)
            self.assertEqual(set(report['robots']), {'mode', 'user_agent', 'origins', 'counts'})
            self.assertEqual(report['robots']['origins'], strict['robots']['origins'])
        self.assertEqual(strict['robots']['origins'][0]['requests'][0]['winning_rule']['line'], 6)


if __name__ == '__main__':
    unittest.main()
