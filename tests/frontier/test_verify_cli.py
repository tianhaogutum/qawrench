"""Saved-report CLI contract; no re-audit or remote source resolution."""

import contextlib
import copy
import hashlib
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import Checkpoint, Policy, RobotsPolicy, audit
from qawrench.frontier.core import json_text

FIXTURE = Path(__file__).resolve().parents[2] / 'examples/frontier/robots-byte-ranges-snapshots.json'
LIMIT = 16 * 1024 * 1024


class VerifyCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report_path = self.root / 'report.json'
        self.manifest_path = self.root / 'manifest.json'
        self.manifest = json.loads(FIXTURE.read_text())
        self.manifest['origins']['https://second.test'] = {
            'status': 'available', 'source': 'https://127.0.0.1:1/never-open',
            'text': '\ufeffAllow /é # first\rUser-agent: AuditBot\nAllow: /\r\nDisallow /重 # eof'}
        history = Checkpoint(Policy()).append(
            '{"url":"https://example.test/private/public"}\n'
            '{"url":"https://second.test/public"}\n', 'jsonl')
        requests, issues, total = history.load_requests()
        self.report = audit(requests, history.policy, issues=issues, total=total,
            robots=RobotsPolicy.from_dict(self.manifest), user_agent='AuditBot',
            enforce_robots=True, strict_robots=True)[1]
        self.witnesses = self.report['robots']['malformed_rules']
        self.save()

    def save(self):
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False), encoding='utf-8')
        self.manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False), encoding='utf-8')

    def command(self, *args):
        return subprocess.run([sys.executable, '-B', '-m', 'qawrench', 'frontier', 'verify-witnesses', *map(str, args)],
                              capture_output=True, text=True)

    def run_cli(self, *options):
        return self.command(self.report_path, '--robots', self.manifest_path, *options)

    def expected(self, results):
        verified = sum(r['verified'] for r in results)
        return {'schema_version': 1, 'scope': 'submitted-witnesses-only',
                'counts': {'submitted': len(results), 'verified': verified, 'failed': len(results) - verified},
                'results': [{'index': i, **r} for i, r in enumerate(results)]}

    def assert_output(self, p, expected, code=0):
        self.assertEqual(p.returncode, code, p.stderr)
        self.assertEqual(p.stderr, '')
        self.assertEqual(p.stdout, json_text(expected) + '\n')

    def assert_input_error(self, p):
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(p.stdout, '')
        self.assertTrue(p.stderr.startswith('error: '), p.stderr)
        self.assertNotIn('Traceback', p.stderr)

    def inventory(self):
        return {str(p.relative_to(self.root)): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                for p in [self.root, *self.root.rglob('*')]}

    def test_multiple_origins_exact_bom_multibyte_mixed_separators_repeated_text_and_eof(self):
        self.assertEqual(len(self.witnesses), 9)
        self.assertEqual(self.witnesses[1]['text'], self.witnesses[2]['text'])
        before = self.inventory()
        expected = self.expected([{'verified': True, 'failure': None}] * len(self.witnesses))
        self.assert_output(self.run_cli(), expected)
        self.assert_output(self.run_cli(), expected)  # byte-identical output on repeat
        self.assertEqual(self.inventory(), before)

    def test_verify_reason_genuine_categories_and_flag_positions(self):
        self.assertEqual({w['reason'] for w in self.witnesses}, {
            'invalid path pattern', 'invalid user-agent token',
            'rule outside a valid group', 'missing colon after core directive'})
        before = self.inventory()
        expected = {**self.expected([{'verified': True, 'failure': None}] * len(self.witnesses)),
                    'verification_scope': 'provenance-and-strict-reason'}
        self.assert_output(self.run_cli('--verify-reason'), expected)
        self.assert_output(self.command('--verify-reason', self.report_path, '--robots',
                                        self.manifest_path, '--verify-reason'), expected)
        self.assertEqual(self.inventory(), before)

    def test_verify_reason_keeps_mixed_semantic_and_provenance_failures_in_order(self):
        from qawrench.frontier import verify_malformed_rule_witness
        genuine = copy.deepcopy(self.witnesses)
        snapshot = self.manifest['origins']['https://EXAMPLE.test:443/']['text'].encode('utf-8')
        records = list(re.finditer(rb'([^\r\n]*)(?:\r\n|\r|\n|$)', snapshot))

        def consistent(line):
            record = records[line - 1]
            return {**genuine[0], 'line': line, 'text': record.group(1).decode('utf-8'),
                    'snapshot_sha256': hashlib.sha256(snapshot).hexdigest(),
                    'byte_start': record.start(1), 'byte_end': record.end(1)}

        wrong = {**genuine[0], 'reason': 'invalid path pattern'}
        variants = [genuine[0], wrong, consistent(4), genuine[-1], consistent(2),
                    {**wrong, 'source': 'changed'}, {**wrong, 'snapshot_sha256': '0' * 64},
                    {**wrong, 'line': 999}, {**wrong, 'text': 'changed'},
                    {**wrong, 'byte_start': True}, {**wrong, 'reason': 'unknown reason'},
                    None, consistent(3), genuine[0], wrong]
        self.report['robots']['malformed_rules'] = variants
        self.save()
        before = self.inventory()
        for semantic in (False, True):
            with self.subTest(verify_reason=semantic):
                results = [verify_malformed_rule_witness(w, self.manifest, verify_reason=semantic)
                           for w in variants]
                expected_codes = [None, 'reason_mismatch', 'not_malformed_rule', None,
                    'not_malformed_rule', 'source_mismatch', 'digest_mismatch', 'line_mismatch',
                    'text_mismatch', 'invalid_span', 'invalid_witness', 'invalid_witness',
                    'not_malformed_rule', None, 'reason_mismatch']
                if not semantic:
                    expected_codes = [None if c in ('reason_mismatch', 'not_malformed_rule') else c
                                      for c in expected_codes]
                self.assertEqual([r['failure']['code'] if r['failure'] else None for r in results],
                                 expected_codes)
                expected = self.expected(results)
                if semantic:
                    expected['verification_scope'] = 'provenance-and-strict-reason'
                self.assert_output(self.run_cli(*(['--verify-reason'] if semantic else [])), expected, 1)
        self.assertEqual(self.inventory(), before)

    def test_verify_reason_grammar_rejects_values_negation_and_abbreviations(self):
        for option in ('--verify-reason=true', '--verify-reason=false', '--verify-rea',
                       '--no-verify-reason'):
            with self.subTest(option=option):
                p = self.run_cli(option)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(p.stdout, '')
                self.assertIn('usage:', p.stderr)
        p = self.run_cli('--verify-reason', 'true')
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, '')
        self.assertIn('usage:', p.stderr)
        p = self.command('--help')
        self.assertEqual((p.returncode, p.stderr), (0, ''))
        self.assertIn('--verify-reason', p.stdout)

    def test_verify_reason_is_explicit_and_labels_requested_scope(self):
        from qawrench.frontier import verify_malformed_rule_witness
        witness = {**self.witnesses[0], 'reason': 'invalid path pattern'}
        self.assertNotEqual(witness['reason'], self.witnesses[0]['reason'])
        self.report['robots']['malformed_rules'] = [witness]
        self.save()
        before = self.inventory()
        # Omitted flag retains the entire historical output, even a wrong reason.
        self.assert_output(self.run_cli(), self.expected([{'verified': True, 'failure': None}]))
        result = verify_malformed_rule_witness(witness, self.manifest, verify_reason=True)
        self.assertEqual(result['failure']['code'], 'reason_mismatch')
        expected = {**self.expected([result]), 'verification_scope': 'provenance-and-strict-reason'}
        self.assert_output(self.command(self.report_path, '--robots', self.manifest_path,
                                        '--verify-reason'), expected, 1)
        self.assertEqual(self.inventory(), before)

    def test_all_original_entries_preserve_order_and_helper_failures(self):
        from qawrench.frontier import verify_malformed_rule_witness
        original = copy.deepcopy(self.witnesses)
        variants = [original[0], {**original[1], 'source': 'changed'}, original[-1],
            {**original[2], 'origin': 'https://absent.test'},
            {**original[0], 'origin': 'https://unavailable.test'},
            {**original[1], 'snapshot_sha256': '0' * 64},
            {**original[1], 'byte_start': original[2]['byte_start'], 'byte_end': original[2]['byte_end']},
            {**original[0], 'byte_start': 1}, {**original[0], 'text': 'changed'},
            {**original[0], 'byte_end': True}, None, {'line': 1}, original[0]]
        self.manifest['origins']['https://unavailable.test'] = {
            'status': 'unavailable', 'source': 'no snapshot', 'reason': 'not retained'}
        self.report['robots']['malformed_rules'] = variants
        self.save()
        before = self.inventory()
        results = [verify_malformed_rule_witness(w, self.manifest) for w in variants]
        self.assert_output(self.run_cli(), self.expected(results), 1)
        self.assertEqual([r['failure']['code'] for r in results if r['failure']],
            ['source_mismatch', 'missing_origin', 'unavailable_snapshot', 'digest_mismatch',
             'line_mismatch', 'record_mismatch', 'text_mismatch', 'invalid_span',
             'invalid_witness', 'invalid_witness'])
        self.assertEqual(self.inventory(), before)

    def test_one_failure_between_valid_entries_is_not_repaired_or_short_circuited(self):
        good, repeated = self.witnesses[1:3]
        self.report['robots']['malformed_rules'] = [good, {**good, 'line': repeated['line']}, repeated]
        self.save()
        p = self.run_cli()
        self.assertEqual(p.returncode, 1, p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result['counts'], {'submitted': 3, 'verified': 2, 'failed': 1})
        self.assertEqual([r['index'] for r in result['results']], [0, 1, 2])
        self.assertEqual([r['verified'] for r in result['results']], [True, False, True])
        self.assertEqual(result['results'][1]['failure']['code'], 'line_mismatch')

    def test_changed_snapshot_bytes_fail_every_affected_witness(self):
        self.manifest['origins']['https://EXAMPLE.test:443/']['text'] += '# distant comment'
        self.save()
        p = self.run_cli()
        self.assertEqual(p.returncode, 1, p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result['counts'], {'submitted': 9, 'verified': 2, 'failed': 7})
        self.assertEqual([r['failure']['code'] for r in result['results'][:7]], ['digest_mismatch'] * 7)
        self.assertTrue(all(r['verified'] for r in result['results'][7:]))

    def test_empty_array_is_explicitly_zero_submitted_and_minimal_envelope_is_supported(self):
        self.report = {'schema_version': 1, 'robots': {'mode': 'offline-enforced',
                       'strict': True, 'malformed_rules': []}, 'uninterpreted': {'anything': [1]}}
        self.save()
        self.assert_output(self.run_cli(), self.expected([]))
        self.assert_output(self.run_cli('--verify-reason'), {
            **self.expected([]), 'verification_scope': 'provenance-and-strict-reason'})
        self.manifest['origins'] = {}
        self.save()
        self.assert_output(self.run_cli(), self.expected([]))
        self.assert_output(self.run_cli('--verify-reason'), {
            **self.expected([]), 'verification_scope': 'provenance-and-strict-reason'})

    def test_missing_nonarray_and_invalid_envelopes_are_input_errors(self):
        base = {'schema_version': 1, 'robots': {'mode': 'offline-enforced', 'strict': True,
                                               'malformed_rules': []}}
        cases = [None, [], {}, {'schema_version': True, 'robots': base['robots']},
                 {'schema_version': 2, 'robots': base['robots']}, {'schema_version': 1},
                 {'schema_version': 1, 'robots': []}]
        for key, values in [('strict', [None, 1, False]),
                            ('mode', [None, 'offline-informational']),
                            ('malformed_rules', [None, {}, '', 1, True])]:
            for value in values:
                cases.append({**base, 'robots': {**base['robots'], key: value}})
            robot = dict(base['robots']); del robot[key]
            cases.append({**base, 'robots': robot})
        for report in cases:
            with self.subTest(report=report):
                self.report = report; self.save()
                self.assert_input_error(self.run_cli())
                self.assert_input_error(self.run_cli('--verify-reason'))

    def test_complete_manifest_is_validated_even_for_empty_witnesses_and_unused_later_origins(self):
        self.report['robots']['malformed_rules'] = []
        original = copy.deepcopy(self.manifest)
        for manifest in (None, [], {}, {'version': True, 'origins': {}},
                {**original, 'origins': {**original['origins'], 'https://later.test':
                    {'status': 'available', 'source': 'bad', 'text': '\ud800'}}}):
            with self.subTest(manifest=manifest):
                self.manifest = manifest
                # ASCII JSON can carry invalid decoded Unicode for manifest validation.
                self.report_path.write_text(json.dumps(self.report))
                self.manifest_path.write_text(json.dumps(self.manifest))
                self.assert_input_error(self.run_cli())
                self.assert_input_error(self.run_cli('--verify-reason'))

    def test_strict_json_utf8_and_complete_later_content(self):
        for path in (self.report_path, self.manifest_path):
            for content in (b'\xff', b'{', b'{} {}', b'{"x":1,"x":2}',
                            b'{"x":NaN}', b'{"x":1e999}'):
                with self.subTest(path=path.name, content=content):
                    self.save(); path.write_bytes(content)
                    self.assert_input_error(self.run_cli())
                    self.assert_input_error(self.run_cli('--verify-reason'))
        self.save()
        # A valid witness before malformed later JSON never produces partial success.
        self.report_path.write_text(json.dumps(self.report)[:-1] + ',"late": [1,]}')
        self.assert_input_error(self.run_cli())
        self.assert_input_error(self.run_cli('--verify-reason'))

    def test_usage_requires_two_paths_and_rejects_audit_flags_or_abbreviations(self):
        for args in ([], [self.report_path], ['--robots', self.manifest_path],
                     [self.report_path, '--rob', self.manifest_path],
                     [self.report_path, '--robots', self.manifest_path, '--dry-run'],
                     [self.report_path, '--robots', self.manifest_path, 'extra']):
            with self.subTest(args=args):
                p = self.command(*args)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(p.stdout, '')
                self.assertIn('usage:', p.stderr)
        p = self.command('--help')
        self.assertEqual((p.returncode, p.stderr), (0, ''))
        self.assertIn('--robots', p.stdout)
        self.assertIn('usage: qawrench frontier verify-witnesses', p.stdout)

    def test_bounded_reads_regular_files_symlink_inputs_and_missing_paths(self):
        original_report = self.report_path.read_bytes()
        original_manifest = self.manifest_path.read_bytes()
        for path, content in ((self.report_path, original_report), (self.manifest_path, original_manifest)):
            with self.subTest(path=path.name):
                path.write_bytes(content + b' ' * (LIMIT - len(content)))
                self.assertEqual(self.run_cli().returncode, 0)
                self.assertEqual(self.run_cli('--verify-reason').returncode, 0)
                with path.open('ab') as stream:
                    stream.write(b' ')
                self.assert_input_error(self.run_cli())
                self.assert_input_error(self.run_cli('--verify-reason'))
                path.write_bytes(content)
        self.report_path.unlink()
        self.assert_input_error(self.run_cli())
        self.assert_input_error(self.run_cli('--verify-reason'))
        self.report_path.mkdir()
        self.assert_input_error(self.run_cli())
        self.assert_input_error(self.run_cli('--verify-reason'))
        self.report_path.rmdir()
        os.mkfifo(self.report_path)
        # A FIFO with no writer must be rejected without blocking.
        p = subprocess.run([sys.executable, '-B', '-m', 'qawrench', 'frontier', 'verify-witnesses',
            str(self.report_path), '--robots', str(self.manifest_path)], capture_output=True,
            text=True, timeout=5)
        self.assert_input_error(p)
        p = subprocess.run([sys.executable, '-B', '-m', 'qawrench', 'frontier', 'verify-witnesses',
            str(self.report_path), '--robots', str(self.manifest_path), '--verify-reason'],
            capture_output=True, text=True, timeout=5)
        self.assert_input_error(p)
        self.report_path.unlink()
        retained = self.root / 'retained.json'
        retained.write_bytes(original_report)
        self.report_path.symlink_to(retained)
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.run_cli('--verify-reason').returncode, 0)

    def test_verify_reason_calls_helper_for_every_entry_without_audit_or_writes(self):
        from qawrench.frontier import verify_malformed_rule_witness
        from qawrench.frontier.verify_cli import main
        self.report['robots']['malformed_rules'] = [self.witnesses[0], None, self.witnesses[-1]]
        self.save()
        before = self.inventory()
        real_open = os.open

        def input_only(path, flags, *args, **kwargs):
            self.assertIn(Path(path), (self.report_path, self.manifest_path))
            self.assertEqual(flags, os.O_RDONLY | os.O_NONBLOCK)
            return real_open(path, flags, *args, **kwargs)

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch('os.open', side_effect=input_only), \
                patch('builtins.open', side_effect=AssertionError('extra file I/O forbidden')), \
                patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('qawrench.frontier.robots.RobotsPolicy.audit', side_effect=AssertionError('audit forbidden')), \
                patch('qawrench.frontier.checkpoint.Checkpoint.append', side_effect=AssertionError('checkpoint forbidden')), \
                patch('qawrench.frontier.verify_cli.verify_malformed_rule_witness',
                      wraps=verify_malformed_rule_witness) as helper:
            self.assertEqual(main([str(self.report_path), '--robots', str(self.manifest_path),
                                   '--verify-reason']), 1)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual([call.args[0] for call in helper.call_args_list], self.report['robots']['malformed_rules'])
        self.assertTrue(all(call.args[1] == self.manifest and call.kwargs == {'verify_reason': True}
                            for call in helper.call_args_list))
        self.assertEqual(len(helper.call_args_list), 3)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output['verification_scope'], 'provenance-and-strict-reason')
        self.assertEqual(output['counts'], {'submitted': 3, 'verified': 2, 'failed': 1})
        self.assertEqual(self.inventory(), before)

    def test_reuses_helper_for_each_original_entry_without_audit_or_source_io(self):
        from qawrench.frontier import verify_malformed_rule_witness
        from qawrench.frontier.verify_cli import main
        self.report['robots']['malformed_rules'] = [self.witnesses[0], None, self.witnesses[-1]]
        self.save()
        before = self.inventory()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('qawrench.frontier.robots._parse', side_effect=AssertionError('parse forbidden')), \
                patch('qawrench.frontier.robots.RobotsPolicy.audit', side_effect=AssertionError('audit forbidden')), \
                patch('qawrench.frontier.checkpoint.Checkpoint.append', side_effect=AssertionError('checkpoint forbidden')), \
                patch('qawrench.frontier.verify_cli.verify_malformed_rule_witness',
                      wraps=verify_malformed_rule_witness) as helper:
            self.assertEqual(main([str(self.report_path), '--robots', str(self.manifest_path)]), 1)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual([call.args[0] for call in helper.call_args_list], self.report['robots']['malformed_rules'])
        self.assertTrue(all(call.args[1] == self.manifest for call in helper.call_args_list))
        self.assertEqual(json.loads(stdout.getvalue())['counts'], {'submitted': 3, 'verified': 2, 'failed': 1})
        self.assertEqual(self.inventory(), before)


if __name__ == '__main__':
    unittest.main()
