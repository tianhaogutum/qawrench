"""Saved baseline and group-isolation integration tests with synthetic 12.8.2 LHRs."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from examples.lighthouse.fixture_factory import AUDIT, lhr, policy, write_runs
from qawrench.lighthouse import compare, load_json
from qawrench.lighthouse.core import canonical
from qawrench.lighthouse.report import markdown

ROOT = Path(__file__).resolve().parents[2]


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.b, self.c = self.root / 'b', self.root / 'c'
        write_runs(self.b, [2000]*7)
        write_runs(self.c, [2500]*7)
        self.p = policy()
        self.config = self.root / 'gate.json'
        self.config.write_text(json.dumps(self.p))
        self.saved = self.root / 'baseline.json'

    def manifest(self):
        from qawrench.lighthouse import create_baseline
        return create_baseline(self.b, self.p)

    def save(self, manifest=None):
        self.saved.write_text(json.dumps(self.manifest() if manifest is None else manifest))
        return self.saved

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'qawrench.lighthouse', *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=15)

    def desktop(self, directory, cpu=4, value=2000):
        for index in range(7):
            doc = lhr(value, 'https://synthetic.example.test/catalog')
            doc['configSettings']['formFactor'] = 'desktop'
            doc['configSettings']['screenEmulation'].update(mobile=False, width=1350, height=940)
            doc['configSettings']['throttling']['cpuSlowdownMultiplier'] = cpu
            (directory / f'desktop-{index}.json').write_text(json.dumps(doc))

    def test_manifest_roundtrip_is_deterministic_self_contained_and_pins_policy(self):
        self.desktop(self.b)
        self.desktop(self.c, value=2500)
        direct = compare(self.b, self.c, self.p, confidence_level=.95)
        manifest = self.manifest()
        self.assertEqual(manifest, self.manifest())
        self.assertEqual(manifest['kind'], 'lighthouse-noise-gate-baseline')
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(manifest['policy'], self.p)
        self.assertEqual(len(manifest['runs']), 14)
        self.save(manifest)
        shutil.rmtree(self.b)
        report = compare(self.saved, self.c, self.p, confidence_level=.95)
        self.assertEqual(report['groups'], direct['groups'])
        self.assertEqual(report['runs'], direct['runs'])
        self.assertEqual(report['decision'], 'regression')
        self.assertEqual(report['baseline_manifest']['run_count'], 14)
        self.assertEqual(report, compare(self.saved, self.c, self.p, confidence_level=.95))
        self.p['metrics'][AUDIT]['absolute_tolerance'] = 1000
        with self.assertRaisesRegex(ValueError, 'policy'):
            compare(self.saved, self.c, self.p)
        self.assertEqual(manifest['policy']['metrics'][AUDIT]['absolute_tolerance'], 100)

    def test_manifest_corruption_and_invalid_content_are_rejected(self):
        original = self.manifest()
        mutations = [lambda m: m.update(schema_version=True), lambda m: m.update(schema_version=2),
                     lambda m: m.update(kind='other'), lambda m: m.update(unexpected=True),
                     lambda m: m.update(runs=[]), lambda m: m['runs'].append(deepcopy(m['runs'][0])),
                     lambda m: m['runs'][0].update(file='../escape.json'),
                     lambda m: m['runs'][0].update(file='escape\\file.json'),
                     lambda m: m['runs'][0].update(sha256='0'*64),
                     lambda m: m['runs'][0].update(lhr=[]),
                     lambda m: m['runs'][0]['lhr']['configSettings'].update(formFactor='watch')]
        for mutation in mutations:
            changed = deepcopy(original)
            mutation(changed)
            with self.subTest(manifest=changed), self.assertRaises(ValueError):
                compare(self.save(changed), self.c, self.p)
        # A recomputed checksum cannot bypass LHR validation or sample completeness.
        for edit in (lambda d: d.update(runtimeError={'code': 'NO_FCP'}),
                     lambda d: d['audits'][AUDIT].update(numericValue=None),
                     lambda d: d['audits'][AUDIT].update(numericUnit='second')):
            changed = deepcopy(original)
            entry = changed['runs'][0]
            edit(entry['lhr'])
            entry['sha256'] = hashlib.sha256(canonical(entry['lhr']).encode()).hexdigest()
            with self.assertRaises(ValueError):
                compare(self.save(changed), self.c, self.p)

    def test_manifest_is_revalidated_for_each_group_sample_count(self):
        self.desktop(self.b)
        changed = self.manifest()
        changed['runs'] = [r for r in changed['runs'] if not r['file'].startswith('desktop-') or r['file'] == 'desktop-0.json']
        with self.assertRaisesRegex(ValueError, 'samples'):
            compare(self.save(changed), self.c, self.p)

    def test_noisy_complete_baseline_can_be_saved_and_key_order_is_ignored(self):
        write_runs(self.b, [1800, 1900, 2000, 2100, 2200, 2300, 2400])
        write_runs(self.c, [1800, 1900, 2000, 2100, 2200, 2300, 2400])
        direct = compare(self.b, self.c, self.p)
        self.assertEqual(direct['decision'], 'inconclusive')
        manifest = self.manifest()
        for entry in manifest['runs']:
            entry['lhr'] = dict(reversed(list(entry['lhr'].items())))
        reordered = dict(reversed(list(self.p.items())))
        self.assertEqual(compare(self.save(manifest), self.c, reordered)['groups'], direct['groups'])

    def test_strict_json_and_manifest_as_current_are_rejected(self):
        self.save()
        raw = self.saved.read_text()
        self.saved.write_text(raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1))
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            compare(self.saved, self.c, self.p)
        self.save()
        self.assertEqual(compare(self.b, self.saved, self.p)['decision'], 'invalid')

    def test_incomplete_or_invalid_baselines_cannot_be_saved(self):
        from qawrench.lighthouse import create_baseline
        for bad in (None, True, '2000'):
            doc = lhr(bad)
            (self.b / 'run-001.json').write_text(json.dumps(doc))
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                create_baseline(self.b, self.p)
        (self.b / 'run-001.json').write_text('{')
        with self.assertRaises(ValueError):
            create_baseline(self.b, self.p)
        shutil.rmtree(self.b)
        write_runs(self.b, [2000]*4)
        with self.assertRaisesRegex(ValueError, 'samples'):
            create_baseline(self.b, self.p)
        self.desktop(self.b)
        with self.assertRaisesRegex(ValueError, 'samples'):
            create_baseline(self.b, self.p)

    def test_valid_group_confidence_survives_other_profile_mismatch(self):
        self.desktop(self.b)
        self.desktop(self.c, cpu=8)
        # Use live directories first to prove this gap without the manifest feature.
        report = compare(self.b, self.c, self.p, confidence_level=.95)
        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
        mobile = next(g for g in report['groups'] if g['identity']['configSettings']['formFactor'] == 'mobile')
        self.assertEqual(mobile['decision'], 'regression')
        self.assertEqual(mobile['metrics'][0]['confidence']['status'], 'available')
        self.assertEqual(mobile['metrics'][0]['confidence']['delta_interval'], [500, 500])
        unmatched = [g for g in report['groups'] if g is not mobile]
        self.assertEqual(len(unmatched), 2)
        for group in unmatched:
            self.assertEqual(group['decision'], 'invalid')
            self.assertIn('unmatched', ' '.join(group['errors']))
            self.assertEqual(group['metrics'][0]['decision'], 'invalid')
            self.assertEqual(group['metrics'][0]['confidence']['status'], 'invalid')
        rendered = markdown(report)
        self.assertIn('Group decisions', rendered)
        self.assertIn('https://synthetic.example.test/catalog', rendered)
        self.assertIn('desktop', rendered)
        self.assertEqual(compare(self.save(), self.c, self.p, confidence_level=.95)['groups'], report['groups'])

    def test_known_invalid_group_does_not_suppress_valid_group(self):
        self.desktop(self.b)
        self.desktop(self.c)
        doc = load_json(self.c / 'desktop-0.json')
        doc['runtimeError'] = {'code': 'NO_FCP'}
        (self.c / 'desktop-0.json').write_text(json.dumps(doc))
        report = compare(self.b, self.c, self.p, confidence_level=.95)
        self.assertEqual(report['decision'], 'invalid')
        by_device = {g['identity']['configSettings']['formFactor']: g for g in report['groups']}
        self.assertEqual(by_device['mobile']['metrics'][0]['confidence']['status'], 'available')
        self.assertEqual(by_device['desktop']['metrics'][0]['confidence']['status'], 'invalid')

    def test_cli_save_compare_dry_run_and_output_protection(self):
        result = self.cli('save-baseline', self.b, '--config', self.config, '--out', self.saved)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = load_json(self.saved)
        self.assertEqual(manifest, self.manifest())
        saved_bytes = self.saved.read_bytes()
        result = self.cli('save-baseline', self.b, '--config', self.config, '--out', self.saved, '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), manifest)
        self.assertEqual(self.saved.read_bytes(), saved_bytes)
        out = self.root / 'report.json'
        result = self.cli(self.saved, self.c, '--config', self.config, '--out', out, '--confidence-level', '.95')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(load_json(out)['groups'][0]['metrics'][0]['confidence']['delta_interval'], [500, 500])
        for target in (self.saved, self.c / 'report.json', self.config):
            self.assertEqual(self.cli(self.saved, self.c, '--config', self.config, '--out', target).returncode, 2)
        for target in (self.b / 'run-001.json', self.b / 'new.json', self.config):
            self.assertEqual(self.cli('save-baseline', self.b, '--config', self.config, '--out', target).returncode, 2)
        (self.b / 'bad.json').write_text('{')
        self.assertEqual(self.cli('save-baseline', self.b, '--config', self.config, '--out', self.saved).returncode, 2)
        self.assertEqual(self.saved.read_bytes(), saved_bytes)

    def test_cli_bad_manifest_or_policy_preserves_report(self):
        self.save()
        out = self.root / 'report.json'
        out.write_text('keep this')
        manifest = load_json(self.saved)
        manifest['runs'][0]['lhr']['audits'][AUDIT]['numericValue'] = 1
        self.save(manifest)
        result = self.cli(self.saved, self.c, '--config', self.config, '--out', out)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('checksum', json.loads(result.stderr)['errors'][0])
        self.assertEqual(out.read_text(), 'keep this')
        self.save()
        changed = deepcopy(self.p)
        changed['noise_multiplier'] = 0
        self.config.write_text(json.dumps(changed))
        result = self.cli(self.saved, self.c, '--config', self.config, '--out', out)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('policy', result.stderr)
        self.assertEqual(out.read_text(), 'keep this')


if __name__ == '__main__':
    unittest.main()
