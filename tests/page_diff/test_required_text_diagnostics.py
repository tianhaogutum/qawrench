"""Exact failure locations without changing required-text reselection or reports."""
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


def diagnostic(side, selector, stage, path, index=0):
    prefix = (f'{side}: required text contract failed: required_selectors[{index}] '
              f'selector={selector!r} stage={stage} ')
    return prefix + (f'empty text original_path={path!r}' if path is not None else
                     "expected at least 1 matched elements, got 0 original_path=null original_scope_path='/region'")


# (name, bad snapshot, passing snapshot, extra rules, selector, stage, original path)
CASES = [
    ('parsed-empty', '<main><p>Keep</p><p> </p></main>', '<main><p>Keep</p><p>Other</p></main>',
     {}, 'p', 'parsed', '/region/p[1]'),
    ('parsed-zero', '<main><h1>Keep</h1></main>', '<main><p>Keep</p></main>',
     {}, 'p', 'parsed', None),
    ('ignored-empty', '<main><p class="noise">Gone</p><p><b class="noise">Gone</b></p><h1>Keep</h1></main>',
     '<main><p class="noise">Gone</p><p>Keep<b class="noise">Gone</b></p></main>',
     {'ignore_selectors': ['.noise']}, 'p', 'after_ignores', '/region/p[1]'),
    ('ignored-zero', '<main><p class="noise">Gone</p><h1>Keep</h1></main>',
     '<main><p class="noise">Gone</p><p>Keep</p></main>',
     {'ignore_selectors': ['.noise']}, 'p', 'after_ignores', None),
    ('volatile-empty', '<main><h1>Keep</h1><p title="clock"> </p></main>',
     '<main><h1>Keep</h1><p title="clock">Other</p></main>',
     {'volatile_attributes': [{'selector': 'p', 'attributes': ['title']}]},
     'p:not([title]), h1', 'after_volatile', '/region/p[0]'),
    ('volatile-zero', '<main><p title="clock">Keep</p></main>',
     '<main><p title="clock">Keep</p><p title="stable">Other</p></main>',
     {'volatile_attributes': [{'selector': 'p:first-child', 'attributes': ['title']}]},
     'p[title]', 'after_volatile', None),
]


class RequiredTextDiagnosticsTests(unittest.TestCase):
    def failure(self, bad, good, rules, selector, stage, path, index=0):
        for before, after, side in ((bad, good, 'before'), (good, bad, 'after')):
            with self.subTest(side=side), self.assertRaises(ContractError) as error:
                compare_html(before, after, rules)
            self.assertEqual(str(error.exception), diagnostic(side, selector, stage, path, index))

    def test_empty_second_match_and_post_volatile_zero_regressions(self):
        for name, bad, good, extra, selector, stage, path in (CASES[0], CASES[-1]):
            with self.subTest(case=name):
                self.failure(bad, good, {**BASE, **extra, 'required_selectors': ['main', selector, selector]},
                             selector, stage, path, 1)

    def test_both_sides_empty_and_zero_at_every_stage(self):
        for name, bad, good, extra, selector, stage, path in CASES:
            with self.subTest(case=name):
                self.failure(bad, good, {**BASE, **extra, 'required_selectors': [selector]}, selector, stage, path)

    def test_first_empty_is_dom_preorder_not_rule_or_path_sort_order(self):
        # Both p[2] and p[10] lose all text; p[0] is removed entirely.
        def page(empty):
            return '<main><h1>Keep</h1>' + ''.join(
                '<p class="noise">Gone</p>' if i == 0 else
                '<p><b class="noise">Gone</b>' + ('' if empty and i in (2, 10) else 'Keep') + '</p>'
                for i in range(12)) + '</main>'
        selector = 'p:nth-of-type(11), p'
        rules = {**BASE, 'required_selectors': ['main', selector, 'p', selector], 'ignore_selectors': ['.noise']}
        self.failure(page(True), page(False), rules, selector, 'after_ignores', '/region/p[2]', 1)

    def test_new_post_ignore_membership_uses_original_position(self):
        bad = '<main><p class="noise">Gone</p><p> </p><h1>Keep</h1></main>'
        good = bad.replace('<p> </p>', '<p>Other</p>')
        rules = {**BASE, 'required_selectors': ['p:first-child'], 'ignore_selectors': ['.noise']}
        self.failure(bad, good, rules, 'p:first-child', 'after_ignores', '/region/p[1]')

    def test_root_membership_loss_is_zero_with_region_scope(self):
        rules = {**BASE, 'required_selectors': ['main[title]'],
                 'volatile_attributes': [{'selector': 'main.cut', 'attributes': ['title'], 'min_matches': 0}]}
        self.failure('<main class="cut" title="clock">Keep</main>', '<main title="stable">Keep</main>',
                     rules, 'main[title]', 'after_volatile', None)

    def test_region_empty_retains_precedence_and_has_stage_and_root_path(self):
        cases = [('<main> </main>', '<main>Keep</main>', {}, 'parsed'),
                 ('<main><p class="noise">Gone</p></main>', '<main>Keep<p class="noise">Gone</p></main>',
                  {'ignore_selectors': ['.noise']}, 'after_ignores')]
        for bad, good, extra, stage in cases:
            for before, after, side in ((bad, good, 'before'), (good, bad, 'after')):
                with self.subTest(stage=stage, side=side), self.assertRaises(ContractError) as error:
                    compare_html(before, after, {**BASE, **extra, 'required_selectors': ['main']})
                self.assertEqual(str(error.exception),
                                 f"{side}: required region has no text content stage={stage} original_path='/region'")

    def test_parsed_failure_precedes_other_gates_and_does_not_leak_values(self):
        html = '<main><h1>SECRET-TEXT</h1><section><p id="SECRET-KEY"> </p></section></main>'
        rules = {**BASE, 'required_selectors': ['h1', 'p', 'missing'],
                 'required_attributes': [{'selector': 'p', 'attributes': ['href']}],
                 'unordered_collections': [{'selector': 'section', 'items': 'p', 'key_attribute': 'absent'}]}
        with self.assertRaises(ContractError) as error:
            compare_html(html, html, rules)
        self.assertEqual(str(error.exception), diagnostic('before', 'p', 'parsed', '/region/section[0]/p[0]', 1))
        for value in ('SECRET-TEXT', 'SECRET-KEY'):
            self.assertNotIn(value, str(error.exception))

    def test_cli_every_stage_both_sides_exact_diagnostic_and_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before, after, config, out = [directory / n for n in ('before.html', 'after.html', 'rules.json', 'out')]
            for name, bad, good, extra, selector, stage, path in CASES:
                config.write_text(json.dumps({**BASE, **extra, 'required_selectors': ['main', selector, selector]}))
                for first, second, side in ((bad, good, 'before'), (good, bad, 'after')):
                    before.write_text(first); after.write_text(second)
                    for flags in ([], ['--format', 'markdown', '--fail-on-change'],
                                  ['--dry-run'], ['--dry-run', '--fail-on-change']):
                        for exists in (False, True):
                            with self.subTest(case=name, side=side, flags=flags, exists=exists):
                                out.unlink(missing_ok=True)
                                if exists: out.write_bytes(b'previous\x00\xff')
                                result = subprocess.run(CLI + [str(before), str(after), '--rules', str(config),
                                                              '--out', str(out), *flags], cwd=CLI_CWD,
                                                        capture_output=True, text=True, timeout=20)
                                self.assertEqual(result.returncode, 2, result.stderr)
                                self.assertEqual(result.stdout, '')
                                self.assertEqual(result.stderr, 'qawrench page-diff: ' + diagnostic(side, selector, stage, path, 1) + '\n')
                                self.assertEqual(out.read_bytes() if exists else out.exists(), b'previous\x00\xff' if exists else False)
                                self.assertFalse(list(directory.glob('.stable-diff-*')))
