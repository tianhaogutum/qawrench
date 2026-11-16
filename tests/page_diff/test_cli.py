"""CLI subprocess tests, sequential and offline; exercise real files and packaging entry point."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import ContractError, load_rules
from qawrench.page_diff.cli import markdown

ROOT = Path(__file__).resolve().parents[2]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.before = self.directory / 'before.html'
        self.after = self.directory / 'after.html'
        self.rules = self.directory / 'rules.json'
        self.before.write_text('<main><p>$10</p></main>')
        self.after.write_text('<main><p>$12</p></main>')
        self.rules.write_text('{"version": 1, "required_region": "main"}')

    def cli(self, *extra):
        return subprocess.run([sys.executable, '-m', 'qawrench.page_diff', str(self.before), str(self.after), '--rules', str(self.rules), *map(str, extra)], cwd=ROOT, capture_output=True, text=True, timeout=20)

    def test_changed_json_and_exit_policy(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'changed')
        self.assertEqual(report['changes'][0]['old'], '$10')
        self.assertEqual(self.cli('--fail-on-change').returncode, 1)
        self.after.write_bytes(self.before.read_bytes())
        self.assertEqual(self.cli('--fail-on-change').returncode, 0)

    def test_atomic_output_and_markdown(self):
        out = self.directory / 'report.md'
        out.write_text('stale')
        result = self.cli('--format', 'markdown', '--out', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('Status: **changed**', out.read_text())
        self.assertIn('$10', out.read_text())
        self.assertIn('$12', out.read_text())
        self.assertIn('Full normalization audit', out.read_text())
        self.assertFalse(list(self.directory.glob('.stable-diff-*')))

    def test_dry_run_does_not_write_or_execute_script(self):
        sentinel = self.directory / 'executed'
        self.after.write_text(f'<main>ok<script>require("fs").writeFileSync("{sentinel}", "bad")</script></main>')
        out = self.directory / 'out.json'
        result = self.cli('--out', out, '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'changed')
        self.assertFalse(out.exists())
        self.assertFalse(sentinel.exists())
        out.write_text('unchanged on disk')
        self.assertEqual(self.cli('--out', out, '--dry-run').returncode, 0)
        self.assertEqual(out.read_text(), 'unchanged on disk')

    def test_invalid_utf8_and_missing_file(self):
        self.after.write_bytes(b'<main>\xff</main>')
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn('UTF-8', result.stderr)
        self.assertFalse(result.stdout)
        self.after.unlink()
        self.assertEqual(self.cli().returncode, 2)

    def test_blank_failure_leaves_existing_output_intact(self):
        self.after.write_text('<main></main>')
        out = self.directory / 'out.json'
        out.write_text('previous report')
        result = self.cli('--out', out)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(result.stdout)
        self.assertEqual(out.read_text(), 'previous report')

    def test_invalid_json_duplicate_json_keys_and_non_utf8_rules(self):
        for data in (b'{', b'{"version":1,"required_region":"main","required_region":"body"}', b'\xff'):
            self.rules.write_bytes(data)
            result = self.cli()
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            with self.assertRaises(ContractError):
                load_rules(self.rules)

    def test_bad_selector_and_duplicate_keys_exit_nonzero(self):
        self.rules.write_text('{"version": 1, "required_region": "["}')
        self.assertEqual(self.cli().returncode, 2)
        self.rules.write_text('{"version": 1, "required_region": "main", "unordered_collections":[{"selector":"section","items":"article","key_attribute":"id"}]}')
        self.before.write_text('<main><section><article id="a">x</article><article id="a">y</article></section></main>')
        self.assertEqual(self.cli().returncode, 2)

    def test_refuses_input_overwrite_and_links(self):
        original = self.before.read_bytes()
        for target in (self.before, self.after, self.rules):
            self.assertEqual(self.cli('--out', target).returncode, 2)
        symlink = self.directory / 'sym'
        symlink.symlink_to(self.before)
        self.assertEqual(self.cli('--out', symlink).returncode, 2)
        hardlink = self.directory / 'hard'
        os.link(self.before, hardlink)
        self.assertEqual(self.cli('--out', hardlink).returncode, 2)
        self.assertEqual(self.before.read_bytes(), original)

    def test_output_failure(self):
        result = self.cli('--out', self.directory / 'missing' / 'out.json')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.directory / 'missing').exists())

    def test_coverage_failure_diagnoses_rule_and_preserves_output(self):
        self.before.write_text('<main><h1>Catalog</h1><p class="clock">10:00</p></main>')
        self.after.write_text('<main><h1>Catalog</h1><p class="time">10:01</p></main>')
        self.rules.write_text(json.dumps({'version': 1, 'required_region': 'main', 'ignore_selectors': ['.clock']}))
        out = self.directory / 'out.json'
        out.write_text('previous report')
        for extra in ((), ('--fail-on-change',), ('--dry-run',)):
            result = self.cli('--out', out, *extra)
            self.assertEqual(result.returncode, 2, result.stderr)
            for detail in ('after', 'ignore_selectors[0]', '.clock', 'got 0'):
                self.assertIn(detail, result.stderr)
            self.assertFalse(result.stdout)
            self.assertEqual(out.read_text(), 'previous report')

    def test_expired_rule_fails_without_writing_report(self):
        self.rules.write_text(json.dumps({'version': 1, 'required_region': 'main', 'volatile_attributes': [
            {'selector': 'p', 'attributes': ['id'], 'owner': 'catalog-team', 'expires': '2000-01-01'}]}))
        out = self.directory / 'out.json'
        result = self.cli('--out', out)
        self.assertEqual(result.returncode, 2, result.stderr)
        for detail in ('expired', 'volatile_attributes[0]', 'catalog-team', '2000-01-01'):
            self.assertIn(detail, result.stderr)
        self.assertFalse(out.exists())
        self.assertFalse(result.stdout)

    def test_json_and_markdown_expose_zero_match_coverage_and_owner(self):
        self.rules.write_text(json.dumps({'version': 1, 'required_region': 'main', 'ignore_selectors': [
            {'selector': '.optional', 'min_matches': 0, 'owner': '<catalog-team>`', 'expires': '2099-01-01'}]}))
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)['rule_coverage']['before'][0]
        self.assertEqual((row['matched_elements'], row['min_matches']), (0, 0))
        self.assertEqual(row['owner'], '<catalog-team>`')
        result = self.cli('--format', 'markdown')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Normalization rule coverage', result.stdout)
        self.assertIn('matched_elements', result.stdout)
        self.assertIn('2099-01-01', result.stdout)
        self.assertNotIn('<catalog-team>', result.stdout)

    def test_markdown_escapes_html_and_fences(self):
        report = {'status': 'changed', 'changes': [{'kind': 'changed', 'path': '/x`', 'old': '<script>x</script>', 'new': '```\n'}], 'suppressed': [], 'normalizations': {'before': [], 'after': []}}
        output = markdown(report)
        self.assertNotIn('<script>', output)
        self.assertIn('\\u003c', output)
        self.assertIn('\\u0060', output)

    def test_sibling_identity_json_markdown_and_ambiguity_exit_policy(self):
        self.rules.write_text(json.dumps({'version': 1, 'required_region': 'main', 'sibling_identity': [
            {'selector': 'article', 'key_attribute': 'data-id'}]}))
        self.before.write_text('<main><article data-id="A"><p>$10</p></article><article data-id="B"><p>$20</p></article></main>')
        self.after.write_text('<main><article data-id="B"><p>$20</p></article><article data-id="A"><p>$12</p></article></main>')
        result = self.cli('--fail-on-change')
        self.assertEqual(result.returncode, 1, result.stderr)
        changes = json.loads(result.stdout)['changes']
        self.assertEqual(sorted(c['kind'] for c in changes), ['changed', 'moved', 'moved'])
        self.assertEqual(changes[0]['after_path'], '/region/article[1]/p[0]/#text[0]')
        result = self.cli('--format', 'markdown')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('after location:', result.stdout)
        self.assertIn('/region/article[1]/p[0]/#text[0]', result.stdout)
        self.after.write_text('<main><article data-id="A">$12</article><article data-id="A">$30</article></main>')
        result = self.cli('--fail-on-change')
        self.assertEqual(result.returncode, 1, result.stderr)
        finding = next(c for c in json.loads(result.stdout)['changes'] if c['kind'] == 'ambiguous')
        self.assertEqual(finding['reason'], 'duplicate_key')
        self.assertEqual(len(finding['new']), 2)
        self.assertIn('duplicate_key', self.cli('--format', 'markdown').stdout)

    def test_invalid_identity_contract_preserves_existing_output(self):
        self.rules.write_text(json.dumps({'version': 1, 'required_region': 'main', 'sibling_identity': [
            {'selector': 'p', 'key_attribute': 'id'}],
            'volatile_attributes': [{'selector': 'p', 'attributes': ['id']}]}))
        out = self.directory / 'report.json'
        out.write_text('previous report')
        result = self.cli('--out', out)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('cannot normalize sibling identity key', result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(out.read_text(), 'previous report')
