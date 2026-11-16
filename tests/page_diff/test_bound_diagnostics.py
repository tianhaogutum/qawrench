"""Bound failures locate the actual stage selection without changing reports."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import ContractError, compare_html

ROOT = Path(__file__).resolve().parents[2]
BASE = {'version': 1, 'required_region': 'main'}
CLI = [sys.executable, '-m', 'qawrench.page_diff']
CLI_CWD = ROOT


def diagnostic(side, field, selector, count, bound, paths, minimum=False, index=0,
               owner='catalog-team'):
    stage = 'parsed' if field == 'ignore_selectors' else 'after_ignores'
    location = ('original_paths=' + json.dumps(paths) if paths else
                "original_path=null original_scope_path='/region'")
    return (f'{side}: normalization coverage failed: {field}[{index}] selector={selector!r} '
            f'stage={stage} expected at {"least" if minimum else "most"} {bound} '
            f'matched elements, got {count} (owner={owner!r}) {location}')


def rule(field, selector, **bounds):
    return {'selector': selector, 'owner': 'catalog-team', **bounds,
            **({'attributes': ['title']} if field == 'volatile_attributes' else {})}


# name, field, selector, bad/good HTML, extra rules, bound, minimum?, original paths
CASES = []
for field in ('ignore_selectors', 'volatile_attributes'):
    for minimum in (False, True):
        CASES.append((field + ('-min' if minimum else '-max'), field, 'p',
                      '<main><h1>Keep</h1><p>SECRET-TEXT</p>' + ('' if minimum else '<p title="SECRET-VALUE">B</p>') + '</main>',
                      '<main><h1>Keep</h1><p>A</p>' + ('<p>B</p>' if minimum else '') + '</main>',
                      {}, 2 if minimum else 1, minimum,
                      ['/region/p[0]'] if minimum else ['/region/p[0]', '/region/p[1]']))
CASES += [
    ('ignore-zero', 'ignore_selectors', 'p', '<main><h1>Keep</h1></main>',
     '<main><h1>Keep</h1><p>A</p></main>', {}, 1, True, []),
    ('volatile-zero-after-ignore', 'volatile_attributes', 'p[title]',
     '<main><p class="cut" title="SECRET-VALUE">Gone</p><h1>Keep</h1></main>',
     '<main><p class="cut" title="clock">Gone</p><p title="clock">Keep</p></main>',
     {'ignore_selectors': ['.cut']}, 1, True, []),
    ('volatile-drift', 'volatile_attributes', 'p:first-child, p:last-child',
     '<main><p class="cut">Gone</p><p title="clock">Keep</p><p>Other</p></main>',
     '<main><p class="cut">Gone</p><p>Keep</p></main>',
     {'ignore_selectors': ['.cut']}, 1, False, ['/region/p[1]', '/region/p[2]']),
]


class BoundDiagnosticsTests(unittest.TestCase):
    def failure(self, bad, good, rules, expected):
        for before, after, side in ((bad, good, 'before'), (good, bad, 'after')):
            with self.subTest(side=side), self.assertRaises(ContractError) as error:
                compare_html(before, after, rules)
            self.assertEqual(str(error.exception), expected(side))
            for value in ('SECRET-TEXT', 'SECRET-VALUE'):
                self.assertNotIn(value, str(error.exception))

    def test_ignore_and_volatile_maximum_regressions(self):
        for name, field, selector, bad, good, extra, bound, minimum, paths in CASES:
            if minimum or extra: continue
            with self.subTest(case=name):
                self.failure(bad, good, {**BASE, field: [rule(field, selector, max_matches=bound)]},
                             lambda side: diagnostic(side, field, selector, len(paths), bound, paths))

    def test_minimum_zero_and_post_ignore_drift_on_either_side(self):
        for name, field, selector, bad, good, extra, bound, minimum, paths in CASES:
            with self.subTest(case=name):
                entry = rule(field, selector, **{'min_matches' if minimum else 'max_matches': bound})
                self.failure(bad, good, {**BASE, **extra, field: [entry]},
                             lambda side: diagnostic(side, field, selector, len(paths), bound, paths, minimum))

    def test_root_bounds_precede_protected_ignore_gate(self):
        bad = '<main class="cut"><p class="cut">Keep</p></main>'
        good = '<main><p>Keep</p></main>'
        for field in ('ignore_selectors', 'volatile_attributes'):
            for minimum, bound in ((False, 0), (False, 1), (True, 3)):
                # For a minimum failure on before, the other side need not pass;
                # root ignores are forbidden even when a bound passes.
                rules = {**BASE, field: [rule(field, 'main.cut, p.cut', min_matches=bound if minimum else 0,
                                             **({} if minimum else {'max_matches': bound}))]}
                with self.subTest(field=field, minimum=minimum, bound=bound):
                    if minimum:
                        with self.assertRaises(ContractError) as error: compare_html(bad, bad, rules)
                        self.assertEqual(str(error.exception), diagnostic('before', field, 'main.cut, p.cut', 2, bound,
                                                                         ['/region', '/region/p[0]'], True))
                    else:
                        self.failure(bad, good, rules, lambda side: diagnostic(side, field, 'main.cut, p.cut', 2, bound,
                                                                             ['/region', '/region/p[0]']))

    def test_repeated_overlapping_rules_share_stage_and_keep_rule_order(self):
        html = '<main><h1>Keep</h1><aside title="clock"><p title="clock">Other</p></aside></main>'
        paths = ['/region/aside[0]', '/region/aside[0]/p[0]']
        for field in ('ignore_selectors', 'volatile_attributes'):
            for selector in ('aside, p', '[title]'):
                entries = [rule(field, '[title]', max_matches=2), rule(field, selector, max_matches=1),
                           rule(field, selector, min_matches=3)]
                with self.subTest(field=field, selector=selector), self.assertRaises(ContractError) as error:
                    compare_html(html, html, {**BASE, field: entries})
                self.assertEqual(str(error.exception), diagnostic('before', field, selector, 2, 1, paths, index=1))

    def test_paths_are_dom_preorder_not_lexicographic_or_selector_order(self):
        html = '<main><h1>Keep</h1>' + ''.join('<p title="clock">A</p>' for _ in range(12)) + '</main>'
        selector = 'p:nth-of-type(11), p'
        paths = [f'/region/p[{i}]' for i in range(12)]
        for field in ('ignore_selectors', 'volatile_attributes'):
            with self.subTest(field=field), self.assertRaises(ContractError) as error:
                compare_html(html, html, {**BASE, field: [rule(field, selector, max_matches=1)]})
            self.assertEqual(str(error.exception), diagnostic('before', field, selector, 12, 1, paths))

    def test_legacy_zero_and_missing_owner(self):
        for field in ('ignore_selectors', 'volatile_attributes'):
            entry = 'missing' if field == 'ignore_selectors' else {'selector': 'missing', 'attributes': ['title']}
            with self.subTest(field=field), self.assertRaises(ContractError) as error:
                compare_html('<main>Keep</main>', '<main>Keep</main>', {**BASE, field: [entry]})
            self.assertEqual(str(error.exception), diagnostic('before', field, 'missing', 0, 1, [], True,
                                                             owner='(unspecified)'))

    def test_stage_and_earlier_gate_precedence(self):
        html = '<main><p>A</p><p>B</p></main>'
        common = {**BASE, 'ignore_selectors': [rule('ignore_selectors', 'p', max_matches=1)],
                  'volatile_attributes': [rule('volatile_attributes', 'p', min_matches=3)]}
        earlier = [({'required_selectors': ['missing']}, 'required text contract failed:'),
                   ({'required_attributes': [{'selector': 'p', 'attributes': ['href']}]}, 'required attribute contract failed:'),
                   ({'unordered_collections': [{'selector': 'missing', 'items': 'p', 'key_attribute': 'id'}]}, 'collection')]
        for extra, message in earlier:
            with self.subTest(extra=extra), self.assertRaises(ContractError) as error:
                compare_html(html, html, {**common, **extra})
            self.assertIn(message, str(error.exception))
        with self.assertRaises(ContractError) as error: compare_html(html, html, common)
        self.assertEqual(str(error.exception), diagnostic('before', 'ignore_selectors', 'p', 2, 1,
                                                         ['/region/p[0]', '/region/p[1]']))
        # Required text after ignores still runs before volatile bound checking.
        common['ignore_selectors'] = ['p']
        with self.assertRaises(ContractError) as error:
            compare_html('<main>Keep<p>A</p></main>', html, {**common, 'required_selectors': ['p']})
        self.assertIn('required_selectors[0]', str(error.exception))
        self.assertIn('stage=after_ignores', str(error.exception))

    def test_cli_exact_diagnostics_empty_stdout_and_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before, after, config, out = [directory / n for n in ('before.html', 'after.html', 'rules.json', 'out')]
            for name, field, selector, bad, good, extra, bound, minimum, paths in CASES:
                entry = rule(field, selector, **{'min_matches' if minimum else 'max_matches': bound})
                config.write_text(json.dumps({**BASE, **extra, field: [entry]}))
                for first, second, side in ((bad, good, 'before'), (good, bad, 'after')):
                    before.write_text(first); after.write_text(second)
                    for flags in ([], ['--format', 'markdown', '--fail-on-change'], ['--dry-run'],
                                  ['--dry-run', '--format', 'markdown', '--fail-on-change']):
                        for exists in (False, True):
                            with self.subTest(case=name, side=side, flags=flags, exists=exists):
                                out.unlink(missing_ok=True)
                                if exists: out.write_bytes(b'previous\x00\xff')
                                result = subprocess.run(CLI + [str(before), str(after), '--rules', str(config),
                                                              '--out', str(out), *flags], cwd=CLI_CWD,
                                                        capture_output=True, text=True, timeout=20)
                                self.assertEqual(result.returncode, 2, result.stderr)
                                self.assertEqual(result.stdout, '')
                                self.assertEqual(result.stderr, 'qawrench page-diff: ' + diagnostic(
                                    side, field, selector, len(paths), bound, paths, minimum) + '\n')
                                self.assertEqual(out.read_bytes() if exists else out.exists(), b'previous\x00\xff' if exists else False)
                                self.assertFalse(list(directory.glob('.stable-diff-*')))
