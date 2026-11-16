"""Opt-in nonblank OG floors use actual extracted values and preserve old floors."""

import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from . import test_baseline
from .test_parity import FixtureCase, document


class OpenGraphNonblankMinimumTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'

    def test_blank_title_cannot_satisfy_initial_nonblank_requirement(self):
        text = document(meta='<meta property="og:title" content="">'
                             '<meta property="og:title" content=" &#x9;&#xA;&#xA0; ">'
                             '<meta property="og:image" content="mug.png">')
        report = self.compare(text, text, syntaxes=('opengraph',))
        # Existing multiplicity counts blanks, so it accepts this pair.
        make_baseline(report, minimum_og_properties={'og:title': 2})
        code, out, err = self.run_cli('--syntax', 'opengraph', '--save-baseline', str(self.baseline),
                                     '--minimum-og-nonblank-property', 'og:title', '1')
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for source in error['provenance']:
            self.assertEqual(source['unmet'], [{'property': 'og:title', 'value_filter': 'nonblank',
                                               'required': 1, 'observed': 0}])
            coverage = source['coverage']['opengraph']
            self.assertEqual(coverage['selected'], 3)
            self.assertEqual(coverage['property_counts'], {'og:image': 1, 'og:title': 2})
            self.assertEqual(coverage['nonblank_property_counts'], {'og:image': 1, 'og:title': 0})
            self.assertIn('selector', coverage)

    def test_unicode_whitespace_mixed_repeats_and_custom_namespace(self):
        values = ['', ' &#x9;&#xA;&#xD;&#xA0;&#x2003; ', ' A ', ' A ', '&#x200B;']
        meta = ''.join(f'<meta property="og:title" content="{v}">' for v in values)
        text = document(meta=meta + '<meta property="custom:label" content=" B ">').replace(
            '<html>', '<html prefix="custom: https://fixture.test/ns#">')
        report = self.compare(text, text)
        coverage = report['captures']['old']['coverage']['opengraph']
        self.assertEqual(coverage['property_counts'], {'custom:label': 1, 'og:title': 5})
        self.assertEqual(coverage['nonblank_property_counts'], {'custom:label': 1, 'og:title': 3})
        requirements = {'og:title': 3, 'custom:label': 1}
        saved = make_baseline(report, minimum_og_nonblank_properties=requirements)
        requirements['og:title'] = 99
        self.assertEqual(saved['minimum_og_nonblank_properties'], {'custom:label': 1, 'og:title': 3})
        self.assertEqual(list(saved['minimum_og_nonblank_properties']), ['custom:label', 'og:title'])
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_og_nonblank_properties={'og:title': 4})
        self.assertEqual(raised.exception.provenance[0]['unmet'][0]['observed'], 3)
        # Counts do not trim or drop compared values (including blanks).
        right = text.replace('content=" A "', 'content="A"')
        changed = self.compare(text, right)
        self.assertEqual([(c['path'], c['old'], c['new']) for c in changed['changes']],
                         [('/og:title/2', ' A ', 'A'), ('/og:title/3', ' A ', 'A')])

    def test_save_replay_and_same_total_blanking(self):
        text = document({'@id': '../product'}, meta='<meta property="og:title" content="A">')
        self.compare(text, text.replace('content="A"', 'content="B"'))
        flags = ('--minimum-og-nonblank-property', 'og:title', '1',
                 '--minimum-og-property', 'og:title', '1', '--minimum-coverage', 'json-ld.entities', '1',
                 '--require-jsonld-entity', 'https://fixture.test/product')
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--fail-on-diff', *flags)
        self.assertEqual(code, 1)
        saved = json.loads(self.baseline.read_text())
        self.assertEqual(saved['minimum_og_nonblank_properties'], {'og:title': 1})
        self.assertNotIn('minimum_og_nonblank_properties', saved['policy'])
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['accepted_change_count'], 1)
        before = self.baseline.read_bytes()
        self.new.write_text(text.replace('content="A"', 'content=" "'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'minimum_coverage')
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_both_initial_captures_and_output_preserve_baseline(self):
        title = document(meta='<meta property="og:title" content="A">')
        blank = title.replace('content="A"', 'content=" "')
        self.compare(title, title)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        output = self.directory / 'error.json'
        for left, right, failed in [(blank, title, 'old'), (title, blank, 'new')]:
            with self.subTest(failed=failed):
                self.compare(left, right)
                code, _, _ = self.run_cli('--save-baseline', str(self.baseline), '--out', str(output),
                                          '--minimum-og-nonblank-property', 'og:title', '1')
                self.assertEqual(code, 2)
                report = json.loads(output.read_text())
                self.assertEqual((report['status'], report['changes']), ('error', []))
                self.assertEqual(report['errors'][0]['code'], 'minimum_coverage')
                self.assertEqual([p['capture'] for p in report['errors'][0]['provenance']], [failed])
                self.assertEqual(self.baseline.read_bytes(), before)

    def test_skipped_and_case_mismatched_properties(self):
        for meta, body, skipped in [
            ('<meta name="og:title" content="A">', '', 'name_attribute'),
            ('<meta property="og:title">', '', 'missing_content'),
            ('', '<meta property="og:title" content="A">', 'outside_head'),
            ('<meta property="unknown:title" content="A">', '', 'unknown_namespace'),
            ('<meta property="og:Title" content="A">', '', None),
        ]:
            with self.subTest(meta=meta, body=body):
                report = self.compare(document(meta=meta, body=body), document(meta=meta, body=body))
                name = 'unknown:title' if 'unknown' in meta else 'og:title'
                with self.assertRaises(ParityError) as raised:
                    make_baseline(report, minimum_og_nonblank_properties={name: 1})
                self.assertEqual(raised.exception.code, 'minimum_coverage')
                coverage = raised.exception.provenance[0]['coverage']['opengraph']
                if skipped:
                    self.assertEqual(coverage['skipped'][0]['reason'], skipped)
                    self.assertTrue(coverage['skipped'][0]['line'])

    def test_combined_requirements_have_sorted_distinct_diagnostics(self):
        report = self.compare(document(), document())
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_coverage={'json-ld.entities': 1},
                          minimum_og_properties={'og:title': 2}, required_jsonld_entities=['z', 'a'],
                          minimum_og_nonblank_properties={'og:title': 1, 'og:image': 1})
        self.assertEqual(raised.exception.provenance[0]['unmet'], [
            {'metric': 'json-ld.entities', 'required': 1, 'observed': 0},
            {'property': 'og:title', 'required': 2, 'observed': 0},
            {'property': 'og:image', 'value_filter': 'nonblank', 'required': 1, 'observed': 0},
            {'property': 'og:title', 'value_filter': 'nonblank', 'required': 1, 'observed': 0},
            {'entity': 'a', 'required': 1, 'observed': 0},
            {'entity': 'z', 'required': 1, 'observed': 0}])

    def test_invalid_library_requirements(self):
        report = self.compare(document(), document())
        invalid = [[], 'og:title', {1: 1}, {'': 1}, {' ': 1}, {' og:title': 1},
                   {'og:title ': 1}, {'og:title\n': 1}, {'og:ti tle': 1}, {'og:\u00a0title': 1},
                   {'og:title': True}, {'og:title': 1.0}, {'og:title': '1'}, {'og:title': -1}]
        for requirements in invalid:
            with self.subTest(requirements=requirements), self.assertRaises(ParityError) as raised:
                make_baseline(report, minimum_og_nonblank_properties=requirements)
            self.assertEqual(raised.exception.code, 'invalid_config')
        report = self.compare(document(), document(), syntaxes=('json-ld',))
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_og_nonblank_properties={'og:title': 0})
        self.assertEqual(raised.exception.code, 'invalid_config')

    def test_invalid_cli_requirements_preserve_baseline(self):
        self.compare(document(), document())
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        cases = [('--minimum-og-nonblank-property', 'og:title', count) for count in ('-1', '1.0', 'true', 'bad')]
        cases += [('--minimum-og-nonblank-property', name, '1') for name in ('', ' og:title', 'og:title ', 'og:title\n')]
        cases += [('--minimum-og-nonblank-property', 'og:title', '1', '--minimum-og-nonblank-property', 'og:title', '1'),
                  ('--syntax', 'json-ld', '--minimum-og-nonblank-property', 'og:title', '0')]
        for args in cases:
            with self.subTest(args=args):
                code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_config')
                self.assertEqual(self.baseline.read_bytes(), before)
        for args in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*args, '--minimum-og-nonblank-property', 'og:title', '1')
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_config')

    def test_historical_coverage_and_opt_in_only(self):
        text = document(meta='<meta property="og:title" content="A">')
        report = self.compare(text, text.replace('content="A"', 'content=""'))
        # Existing counts/delta acceptance still allow intentional blanking.
        for saved in [make_baseline(report), make_baseline(report, minimum_og_nonblank_properties={}),
                      make_baseline(report, minimum_og_nonblank_properties={'unknown:title': 0})]:
            self.baseline.write_text(json.dumps(saved))
            self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        historical = make_baseline(report, minimum_og_properties={'og:title': 1})
        for side in ('old', 'new'):
            del historical['captures'][side]['coverage']['opengraph']['nonblank_property_counts']
        for requirements in (None, {}):
            saved = copy.deepcopy(historical)
            if requirements is not None:
                saved['minimum_og_nonblank_properties'] = requirements
            self.baseline.write_text(json.dumps(saved))
            self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        # No guessing that one historical property means one nonblank value.
        for floor in (0, 1):
            saved = copy.deepcopy(historical)
            saved['minimum_og_nonblank_properties'] = {'og:title': floor}
            self.baseline.write_text(json.dumps(saved))
            code, out, _ = self.run_cli('--baseline', str(self.baseline))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')
            with self.assertRaises(ParityError) as raised:
                legacy_report = copy.deepcopy(report)
                legacy_report['captures'] = historical['captures']
                make_baseline(legacy_report, minimum_og_nonblank_properties={'og:title': floor})
            self.assertEqual(raised.exception.code, 'invalid_config')

    def test_malformed_saved_requirements_and_counts(self):
        text = document(meta='<meta property="og:title" content="">')
        legacy = make_baseline(self.compare(text, text))
        for requirements in [None, [], {'': 0}, {'og:title ': 0}, {'og:title': True},
                             {'og:title': 1.0}, {'og:title': -1}, {'og:title': 1}]:
            with self.subTest(requirements=requirements):
                saved = copy.deepcopy(legacy)
                saved['minimum_og_nonblank_properties'] = requirements
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')
        for counts in [None, [], {}, {'og:title': True}, {'og:title': -1},
                       {'og:title': 1.0}, {'og:title': 2}, {'og:title': 0, 'og:image': 0}]:
            with self.subTest(counts=counts):
                saved = copy.deepcopy(legacy)
                saved['captures']['old']['coverage']['opengraph']['nonblank_property_counts'] = counts
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')
        only = self.compare(document(), document(), syntaxes=('json-ld',))
        saved = make_baseline(only)
        saved['minimum_og_nonblank_properties'] = {'og:title': 0}
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--syntax', 'json-ld', '--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')

    def test_markdown_retains_nonblank_diagnostics(self):
        self.compare(document(meta='<meta property="og:title" content=" ">'), document())
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', 'markdown',
                                  '--minimum-og-nonblank-property', 'og:title', '1')
        self.assertEqual(code, 2)
        for detail in ('minimum_coverage', 'og:title', 'property', 'value_filter', 'nonblank', 'required', 'observed', 'selector'):
            self.assertIn(detail, out)
