"""Concise confidence evidence copies stored per-side intervals and ranks only."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from examples.lighthouse.fixture_factory import AUDIT, lhr, policy, write_runs
from qawrench.lighthouse import compare, create_baseline
from qawrench.lighthouse.report import markdown


HEADING = 'Confidence population-median evidence'
HEADER = ['| Group | Audit | Side | Population-median interval | Order-statistic ranks |',
          '| --- | --- | --- | --- | --- |']


def cell(value):
    return ('null' if value is None else str(value)).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


class ConciseConfidenceIntervalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.b, self.c = self.root/'b', self.root/'c'
        self.p = policy()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0

    def values(self, baseline, current):
        for directory in (self.b, self.c):
            for path in directory.glob('*.json'):
                path.unlink()
        write_runs(self.b, baseline)
        write_runs(self.c, current)

    def assert_evidence(self, report):
        before = deepcopy(report)
        text = markdown(report, include_evidence=False)
        self.assertNotIn('## '+HEADING+'\n', markdown(report))
        if 'confidence' not in report:
            self.assertNotIn('## '+HEADING+'\n', text)
            return ''
        self.assertIn('## '+HEADING+'\n', text)
        section = text.split('## '+HEADING+'\n', 1)[1].split('\n## ', 1)[0].split('\nComplete evidence is omitted', 1)[0]
        rows = []
        for group in report['groups']:
            for metric in group['metrics']:
                for side in ('baseline', 'current'):
                    data = metric['confidence'][side]
                    rows.append('| '+' | '.join(map(cell, (group['id'], metric['audit'], side,
                        None if data is None else data['interval'],
                        None if data is None else data['order_statistic_ranks'])))+' |')
        self.assertIn(f'Population-median evidence rows: **{len(rows)}**.', section)
        self.assertEqual([line for line in section.splitlines() if line.startswith('| ')], HEADER+rows if rows else [])
        self.assertIn('iid', section)
        self.assertIn('per group/metric', section)
        self.assertIn('use the JSON report', text)
        self.assertEqual(report, before)
        return section

    def test_seven_ten_ties_zero_and_mixed_units_in_both_confidence_modes(self):
        other = 'synthetic-throughput'
        self.p['metrics'][other] = dict(direction='higher', unit='unitless', absolute_tolerance=10, relative_tolerance=0)
        for n, baseline, current, ranks in ((7, [0]*7, [0]*6+[300], [1, 7]),
                                           (10, list(range(10)), list(range(10,20)), [2, 9])):
            self.values(baseline, current)
            for directory in (self.b, self.c):
                for path in directory.glob('*.json'):
                    doc = json.loads(path.read_text())
                    doc['audits'][other] = dict(id=other, scoreDisplayMode='numeric', numericUnit='unitless', numericValue=0)
                    path.write_text(json.dumps(doc))
            for gate in (False, True):
                with self.subTest(n=n, gate=gate):
                    report = compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=gate)
                    first, second = report['groups'][0]['metrics']
                    self.assertEqual((first['unit'],first['direction'],second['unit'],second['direction']),
                                     ('millisecond','lower','unitless','higher'))
                    self.assertEqual(first['confidence']['baseline']['order_statistic_ranks'], ranks)
                    self.assertEqual(first['confidence']['baseline']['interval'], [0, 0] if n==7 else [1, 8])
                    section = self.assert_evidence(report)
                    self.assertIn('rows: **4**.', section)
                    if n==7:
                        self.assertIn('| baseline | [0, 0] | [1, 7] |', section)
                        self.assertEqual(first['confidence']['status'], 'available')
                        self.assertEqual(first['decision'], 'inconclusive' if gate else 'pass')

    def test_insufficient_missing_invalid_and_interval_overflow(self):
        for case in ('insufficient', 'missing', 'invalid', 'overflow'):
            self.values([2000]*5 if case=='insufficient' else [2000]*8, [2000]*8)
            if case in ('missing', 'invalid'):
                path = self.c/'run-001.json'; doc = json.loads(path.read_text())
                doc['audits'][AUDIT]['numericValue'] = None if case=='missing' else True
                path.write_text(json.dumps(doc))
            if case=='overflow':
                # Finite MAD operands, but extreme within-side endpoints overflow subtraction.
                self.values([-1e308]+[0]*5+[1e308], [-1e308]+[0]*5+[1e308])
            for gate in (False, True):
                with self.subTest(case=case, gate=gate):
                    report = compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=gate)
                    data = report['groups'][0]['metrics'][0]['confidence']
                    self.assertEqual(data['status'], 'inconclusive' if case in ('insufficient','missing') else 'invalid')
                    self.assertIsNone(data['baseline']); self.assertIsNone(data['current'])
                    if case=='overflow':
                        self.assertIn('numeric overflow in confidence interval', data['reasons'][0])
                    section = self.assert_evidence(report)
                    self.assertIn('| baseline | null | null |', section)
                    self.assertIn('| current | null | null |', section)

    def test_stored_order_duplicates_escaping_nulls_and_no_recomputation(self):
        self.values([2000]*7, [2000]*7)
        report = compare(self.b, self.c, self.p, confidence_level=.95)
        group = report['groups'][0]; group['id'] = 'z|group\r\nline'
        metric = group['metrics'][0]; metric['audit'] = 'z|audit\nline'
        metric['confidence'].update(status='invalid', baseline=dict(interval=[-0.0, 123.45678901234567], order_statistic_ranks=[9, 2]),
                                    current=dict(interval='stored|interval\r\ntext', order_statistic_ranks='stored|ranks\ntext'))
        group['metrics'].extend([deepcopy(metric), dict(deepcopy(metric), audit='a-audit')])
        report['groups'].append(dict(deepcopy(group), id='a-group'))
        section = self.assert_evidence(report)
        self.assertIn('rows: **12**.', section)
        self.assertEqual(section.count('| z\\|group  line | z\\|audit line | baseline | [-0.0, 123.45678901234567] | [9, 2] |'), 2)
        self.assertIn('| current | stored\\|interval  text | stored\\|ranks text |', section)
        # Contradict status/decision in both directions: presence comes only from stored evidence.
        metric['confidence'].update(status='available', baseline=None, current=dict(interval=None, order_statistic_ranks=None))
        section = self.assert_evidence(report)
        self.assertIn('| baseline | null | null |', section)
        self.assertIn('| current | null | null |', section)

    def test_required_keys_unrequested_and_empty_groups(self):
        self.values([2000]*7, [2000]*7)
        report = compare(self.b, self.c, self.p, confidence_level=.95)
        for path in (('confidence',), ('confidence','baseline'), ('confidence','current'),
                     ('confidence','baseline','interval'), ('confidence','current','interval'),
                     ('confidence','baseline','order_statistic_ranks'), ('confidence','current','order_statistic_ranks')):
            broken = deepcopy(report); target = broken['groups'][0]['metrics'][0]
            for key in path[:-1]: target = target[key]
            del target[path[-1]]
            with self.subTest(path=path), self.assertRaises(KeyError):
                markdown(broken, include_evidence=False)
            if len(path) > 1:
                markdown(broken)  # Complete renderer does not read per-side fields.
        del report['confidence']
        del report['groups'][0]['metrics'][0]['confidence']['baseline']
        self.assert_evidence(report)  # Stray incomplete per-metric data is ignored.
        self.assert_evidence(compare(self.b, self.c, self.p))
        for options in ({}, dict(confidence_level=.95), dict(confidence_level=.95, confidence_gate=True)):
            empty = compare(self.b, self.c, self.p, **options); empty['groups'] = []
            section = self.assert_evidence(empty)
            if options:
                self.assertIn('rows: **0**.', section); self.assertNotIn('| Group |', section)

    def test_known_unassigned_invalid_caps_and_saved_parity(self):
        self.values([2000]*7, [2500]*7)
        for directory, cpus in ((self.b,(6,8)), (self.c,(10,12))):
            for cpu in cpus:
                doc = lhr(2000); doc['configSettings']['formFactor'] = 'desktop'
                doc['configSettings']['throttling']['cpuSlowdownMultiplier'] = cpu
                doc['userAgent'] = f'Synthetic/{cpu}'
                for i in range(7): (directory/f'profile-{cpu}-{i}.json').write_text(json.dumps(doc))
        saved = self.root/'baseline.json'; saved.write_text(json.dumps(create_baseline(self.b,self.p)))
        for unassigned in (False, True):
            if unassigned: (self.c/'broken.json').write_text('{')
            for gate in (False, True):
                with self.subTest(unassigned=unassigned, gate=gate):
                    options = dict(confidence_level=.95, confidence_gate=gate, mismatch_limit=1, mismatch_field_limit=1)
                    report = compare(self.b,self.c,self.p,**options)
                    self.assertEqual(report['exit_code'], 2)
                    statuses = [m['confidence']['status'] for g in report['groups'] for m in g['metrics']]
                    self.assertEqual(statuses.count('available'), 0 if unassigned else 1)
                    self.assert_evidence(report)
                    text = markdown(report, include_evidence=False)
                    self.assertEqual(text, markdown(compare(saved,self.c,self.p,**options), include_evidence=False))
                    self.assertIn('total **4**, emitted **1**, omitted **3**',text)
                    self.assertIn('| 2 | 1 | 1 | 1 | true |',text)
