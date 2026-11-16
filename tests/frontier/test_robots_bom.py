"""Strict witnesses preserve original BOM text; tolerant diagnostics stay compatible."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import Checkpoint, Policy, RobotsPolicy, audit, load_requests
from qawrench.frontier.cli import atomic_write, main
from qawrench.frontier.core import json_text


class RobotsBomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / 'input.jsonl'
        self.input.write_text('{"url":"https://example.test/private/public"}\n')
        self.requests, _, _ = load_requests(self.input)
        self.saved = self.root / 'saved.json'
        checkpoint = Checkpoint(Policy()).append(self.input.read_text(), 'jsonl', batch_id='seed:a')
        self.saved.write_text(json_text(checkpoint.to_dict()) + '\n')
        self.manifest = self.root / 'robots.json'
        self.frontier, self.report, self.next = [self.root / p for p in ('frontier', 'report', 'next')]

    def policy(self, text):
        self.snapshot_text = text
        manifest = {'version': 1, 'origins': {'https://EXAMPLE.test:443/': {
            'status': 'available', 'source': 'synthetic:bom-witness', 'text': text},
            'https://unused.test': {'status': 'available', 'source': 'unused',
                                    'text': '\ufeffDisallow /unused'}}}
        self.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        return RobotsPolicy.from_dict(manifest)

    def audit(self, text, *, strict=True, enforced=True):
        return audit(self.requests, Policy(), robots=self.policy(text), user_agent='AuditBot',
                     enforce_robots=enforced, strict_robots=strict)

    def witness(self, line, text, reason):
        records = re.split(r'(\r\n|\r|\n)', self.snapshot_text)
        start = len(''.join(records[:2 * (line - 1)]).encode('utf-8'))
        binding = {'snapshot_sha256': hashlib.sha256(self.snapshot_text.encode('utf-8')).hexdigest(),
                   'byte_start': start, 'byte_end': start + len(text.encode('utf-8'))}
        return {'origin': 'https://example.test', 'source': 'synthetic:bom-witness',
                'line': line, 'text': text, 'reason': reason, **binding}

    def cli(self, *, dry=False):
        out, err = io.StringIO(), io.StringIO()
        args = ['--checkpoint-only', '--checkpoint-in', str(self.saved),
                '--checkpoint-out', str(self.next), '--frontier', str(self.frontier),
                '--report', str(self.report), '--robots', str(self.manifest),
                '--user-agent', 'AuditBot', '--enforce-robots', '--strict-robots']
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args + (['--dry-run'] if dry else []))
        return code, out.getvalue(), err.getvalue()

    def test_initial_bom_is_retained_in_strict_original_record(self):
        original = '\ufeffDisallow /private # original first record'
        _, report = self.audit(original + '\r\nUser-agent: AuditBot\r\nAllow: /\r\n')
        self.assertEqual(report['status'], 'rejected')
        self.assertEqual(report['robots']['malformed_rules'], [self.witness(
            1, original, 'missing colon after core directive')])

    def test_first_core_witnesses_keep_bom_whitespace_comments_and_record_boundaries(self):
        cases = [('Disallow /private', 'missing colon after core directive'),
                 ('Allow /private', 'missing colon after core directive'),
                 ('User-agent AuditBot', 'missing colon after core directive'),
                 ('dIsAlLoW\t/private # comment', 'missing colon after core directive'),
                 ('User-agent # no token', 'missing colon after core directive'),
                 ('Allow', 'missing colon after core directive'),
                 ('User-agent: BadBot/1', 'invalid user-agent token'),
                 ('Disallow: /private', 'rule outside a valid group'),
                 ('Allow: /private', 'rule outside a valid group')]
        for separator in ('\r', '\n', '\r\n'):
            for record, reason in cases:
                with self.subTest(separator=separator, record=record):
                    first = '\ufeff \t' + record + ' \t# original\ufeffcomment '
                    later = ' \tAllow bad\ufeffpath # later\ufeffcomment '
                    text = separator.join([first, 'User-agent: AuditBot', 'Allow: /', later, ''])
                    _, strict = self.audit(text)
                    robots = strict['robots']
                    self.assertEqual(robots['malformed_rules'], [self.witness(1, first, reason),
                        self.witness(4, later, 'missing colon after core directive')])
                    diagnostics = robots['origins'][0]['diagnostics']
                    self.assertEqual(diagnostics[0]['text'], first[1:])  # tolerant compatibility
                    self.assertEqual(diagnostics[1]['text'], later)
                    self.assertEqual(robots['counts'], {'allow': 1, 'disallow': 0, 'unknown': 0})
                    self.assertEqual(robots['origins'][0]['sha256'], hashlib.sha256(text.encode()).hexdigest())
                    candidate, tolerant = self.audit(text, strict=False)
                    strict_candidate, repeated = self.audit(text)
                    self.assertEqual(candidate, strict_candidate)
                    remaining = dict(repeated['robots'])
                    remaining.pop('strict'); remaining.pop('malformed_rules')
                    self.assertEqual(remaining, tolerant['robots'])
                    self.assertEqual(len(robots['origins']), 1)

    def test_valid_initial_bom_group_and_interior_boms_keep_matching(self):
        text = ('\ufeffUser-agent: AuditBot\r\nDisallow: /private\n'
                'Allow: /private/public\rDisallow: /interior\ufeffpath\n')
        _, report = self.audit(text)
        origin = report['robots']['origins'][0]
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['robots']['malformed_rules'], [])
        self.assertEqual(origin['selected_agents'], [{'line': 1, 'value': 'AuditBot'}])
        self.assertEqual(origin['requests'][0]['winning_rule']['line'], 3)
        self.assertEqual(len(origin['requests'][0]['matched_rules']), 2)
        other = self.root / 'other.jsonl'
        other.write_text('{"url":"https://example.test/interior%EF%BB%BFpath"}\n')
        requests, _, _ = load_requests(other)
        _, matched = audit(requests, Policy(), robots=self.policy(text), user_agent='AuditBot',
                           enforce_robots=True, strict_robots=True)
        self.assertEqual(matched['robots']['counts']['disallow'], 1)
        self.assertEqual(matched['robots']['origins'][0]['requests'][0]['winning_rule']['pattern'],
                         '/interior\ufeffpath')

    def test_only_one_initial_bom_is_a_signature_and_extensions_are_not_core(self):
        for first in ('\ufeff\ufeffDisallow /private', '\ufeff\ufeffDisallow: /private',
                      ' \ufeffDisallow /private', '\ufeffDisallow-extra /private',
                      '\ufeffAllowance /private', '\ufeffSitemap: https://example.test/map',
                      '\ufeffFuture Disallow /private', '\ufeff# Disallow /private'):
            with self.subTest(first=first):
                later = '\ufeffDisallow /private'
                _, report = self.audit(first + '\nUser-agent: AuditBot\n' + later + '\nAllow: /\n')
                self.assertEqual(report['status'], 'ok')
                self.assertEqual(report['robots']['malformed_rules'], [])
                diagnostics = report['robots']['origins'][0]['diagnostics']
                expected = [] if '#' == first[1:2] else [
                    {'line': 1, 'text': first.removeprefix('\ufeff'), 'reason': 'ignored non-rule record'}]
                expected.append({'line': 3, 'text': later, 'reason': 'ignored non-rule record'})
                self.assertEqual(diagnostics, expected)

    def test_checkpoint_only_rejection_writes_report_and_preserves_other_outputs(self):
        first = '\ufeffDisallow /private # original first record'
        self.policy(first + '\r\nUser-agent: AuditBot\r\nAllow: /\r\n')
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.frontier.write_bytes(b'old frontier\n'); self.next.write_bytes(b'old state\n')
                    self.report.write_bytes(b'old report\n')
                before = self.saved.read_bytes()
                with patch('qawrench.frontier.cli.atomic_write', wraps=atomic_write) as writer:
                    code, _, err = self.cli()
                self.assertEqual(code, 2, err)
                report = json.loads(self.report.read_text())
                self.assertEqual(report['robots']['malformed_rules'][0]['text'], first)
                self.assertEqual(report['checkpoint']['incoming_batch']['status'], 'not_supplied')
                self.assertEqual(report['checkpoint']['batches'], 1)
                self.assertEqual(report['robots']['counts']['allow'], 1)
                self.assertFalse(report['frontier_emitted']); self.assertFalse(report['checkpoint']['emitted'])
                self.assertEqual([c.args[0] for c in writer.call_args_list], [self.report])
                self.assertEqual(self.saved.read_bytes(), before)
                for path, old in ((self.frontier, b'old frontier\n'), (self.next, b'old state\n')):
                    self.assertEqual(path.read_bytes(), old) if existing else self.assertFalse(path.exists())

    def test_checkpoint_only_accepted_and_rejected_dry_runs_write_nothing(self):
        for existing in (False, True):
            if existing:
                for path in (self.frontier, self.next, self.report):
                    path.write_bytes(b'original\n')
            for first, expected in (('\ufeffDisallow /private # original', 2),
                                    ('\ufeffSitemap: https://example.test/map', 0),
                                    ('\ufeffUser-agent: AuditBot', 0)):
                with self.subTest(existing=existing, first=first):
                    self.policy(first + '\r\nUser-agent: AuditBot\r\nAllow: /\r\n')
                    before = {p: p.read_bytes() for p in self.root.iterdir()}
                    with patch('qawrench.frontier.cli.atomic_write', side_effect=AssertionError('write forbidden')):
                        code, out, err = self.cli(dry=True)
                    self.assertEqual(code, expected, err)
                    report = json.loads(out)
                    self.assertEqual(report['status'], 'rejected' if expected else 'ok')
                    if expected:
                        self.assertEqual(report['robots']['malformed_rules'][0]['text'], first)
                    self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
